"""Interactive CLI for the event planning agent.

Persistence is SQLite rather than in-memory: memory that vanishes when the
process exits is not cross-session memory. The checkpointer keeps conversation
threads resumable; the store holds `/memories/`, which is where the agent
records durable facts about the client.

Run with `uv run event-planner`, or `uv run event-planner --help` for options.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore
from langgraph.types import Command

from event_planner.agent import DEFAULT_MODEL, PROJECT_ROOT, WORKSPACE, build_agent
from event_planner.context import PlannerContext

#: Deliberately a sibling of `workspace/`, never inside it. The agent has
#: `ls`/`read_file`/`glob`/`grep` over its filesystem root, so a database kept
#: under that root would let any session read every other user's memories and
#: every other thread's checkpoints straight out of the raw file — defeating
#: the per-user namespacing entirely. `_check_db_outside_workspace` enforces
#: this for operator-supplied paths too.
STATE_DIR = PROJECT_ROOT / ".state"

#: LangGraph counts every node as a super-step, and this harness runs five
#: middleware nodes per model turn (three `before_agent`, two `after_model`).
#: Measured against the live model, one tool round trip costs ~4 steps, so
#: LangGraph's default of 25 dies after about five tool calls — far short of a
#: planning session that shortlists venues, checks dates, prices catering, and
#: delegates to subagents. Budget for a long session instead.
DEFAULT_MAX_STEPS = 200

BANNER = """\
Event Planner  (Deep Agents)
  thread: {thread}   user: {user}   model: {model}
  Type your event brief. /exit to quit, /state to inspect saved memory.
"""


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _render_update(chunk: dict[str, Any]) -> None:
    """Print a `stream_mode="updates"` chunk in a readable form."""
    for node, update in chunk.items():
        if node == "__interrupt__" or not isinstance(update, dict):
            continue
        for message in update.get("messages", []) or []:
            kind = getattr(message, "type", None)
            if kind == "ai":
                text = message.content
                if isinstance(text, list):  # content blocks
                    text = "".join(
                        b.get("text", "") for b in text if isinstance(b, dict)
                    )
                if text and text.strip():
                    print(f"\n{text.strip()}\n")
                for call in getattr(message, "tool_calls", []) or []:
                    print(f"  → {call['name']}({_brief_args(call.get('args', {}))})")
            elif kind == "tool":
                name = getattr(message, "name", "tool")
                body = str(message.content).strip().splitlines()
                head = body[0] if body else ""
                more = f"  (+{len(body) - 1} more lines)" if len(body) > 1 else ""
                print(f"  ← {name}: {head[:140]}{more}")


def _brief_args(args: dict[str, Any], limit: int = 90) -> str:
    rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return rendered if len(rendered) <= limit else rendered[: limit - 3] + "..."


# --------------------------------------------------------------------------- #
# human-in-the-loop
# --------------------------------------------------------------------------- #


def _collect_decisions(interrupts: Any) -> list[dict[str, Any]]:
    """Prompt for one decision per pending action.

    The middleware requires exactly one decision per interrupted tool call, in
    order — a mismatch raises rather than being silently padded.
    """
    payload = interrupts[0].value if isinstance(interrupts, (list, tuple)) else interrupts.value
    actions = payload.get("action_requests", [])
    configs = payload.get("review_configs", [])

    decisions: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        allowed = (
            configs[index].get("allowed_decisions", ["approve", "reject"])
            if index < len(configs)
            else ["approve", "reject"]
        )

        print("\n" + "=" * 66)
        print(f"  APPROVAL REQUIRED — {action['name']}")
        print("=" * 66)
        for key, value in action.get("args", {}).items():
            print(f"  {key:22} {value}")
        if action.get("description"):
            print(f"\n  {action['description']}")
        print(f"\n  allowed: {', '.join(allowed)}")

        decisions.append(_prompt_one(action, allowed))
    return decisions


def _prompt_one(action: dict[str, Any], allowed: list[str]) -> dict[str, Any]:
    """Read a single decision from the terminal, re-prompting on bad input."""
    letters = {d[0]: d for d in allowed}
    hint = " / ".join(f"[{d[0]}]{d[1:]}" for d in allowed)

    while True:
        try:
            raw = input(f"  {hint} > ").strip().lower()
        except EOFError:
            print("\n  no input available — rejecting for safety")
            return {"type": "reject", "message": "No operator available to approve."}

        choice = letters.get(raw[:1]) if raw else None
        if choice is None:
            print(f"  Enter one of: {', '.join(allowed)}")
            continue

        if choice == "approve":
            return {"type": "approve"}

        if choice == "reject":
            reason = input("  reason (fed back to the agent): ").strip()
            return {"type": "reject", "message": reason or "Rejected by operator."}

        if choice == "respond":
            return {"type": "respond", "message": input("  response: ").strip()}

        if choice == "edit":
            print(f"  current args: {json.dumps(action.get('args', {}), indent=2)}")
            edited = input("  new args as JSON (blank to cancel): ").strip()
            if not edited:
                continue
            try:
                args = json.loads(edited)
            except json.JSONDecodeError as exc:
                print(f"  not valid JSON ({exc.msg}) — try again")
                continue
            if not isinstance(args, dict):
                print("  args must be a JSON object")
                continue
            return {
                "type": "edit",
                "edited_action": {"name": action["name"], "args": args},
            }
    # unreachable


# --------------------------------------------------------------------------- #
# turn loop
# --------------------------------------------------------------------------- #


def _run_turn(graph: Any, payload: Any, config: dict, context: PlannerContext) -> None:
    """Stream one turn, pausing for approval as many times as needed."""
    while True:
        pending: Any = None
        for chunk in graph.stream(
            payload, config=config, context=context, stream_mode="updates"
        ):
            if "__interrupt__" in chunk:
                pending = chunk["__interrupt__"]
                continue
            _render_update(chunk)

        if pending is None:
            return
        payload = Command(resume={"decisions": _collect_decisions(pending)})


def _load_env() -> None:
    """Load `.env` from the project root, deterministically.

    Bare `load_dotenv()` searches upward from the *calling file*, which happens
    to work for an editable install and silently finds nothing otherwise. The
    failure then surfaces as an opaque auth TypeError from deep inside the SDK,
    so pin the path instead.
    """
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        load_dotenv(env_file)
    else:
        load_dotenv()  # fall back to the default search


def _check_credentials() -> str | None:
    """Return an actionable message when required credentials are missing."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return (
            f"ANTHROPIC_API_KEY is not set.\n"
            f"  Copy .env.example to .env and fill it in:\n"
            f"    cp {PROJECT_ROOT / '.env.example'} {PROJECT_ROOT / '.env'}"
        )
    if not os.environ.get("TAVILY_API_KEY", "").strip():
        print(
            "  note: TAVILY_API_KEY not set — web_search will degrade to the "
            "structured directory only.\n",
            file=sys.stderr,
        )
    return None


def _check_db_outside_workspace(db_path: Path) -> None:
    """Refuse to put agent state where the agent can read it.

    Raises:
        ValueError: If the database would sit inside the agent's filesystem root.
    """
    resolved = db_path.resolve()
    root = WORKSPACE.resolve()
    if resolved == root or root in resolved.parents:
        msg = (
            f"--db {resolved} is inside the agent's filesystem root ({root}).\n"
            "The agent can read that directory, so it could read every user's "
            "memories and every thread's checkpoints out of the raw database. "
            "Choose a path outside the workspace."
        )
        raise ValueError(msg)


def _show_memory(store: SqliteStore, user_id: str) -> None:
    from event_planner.context import memory_namespace

    class _Fake:  # minimal stand-in: the factory only reads `.context`
        context = PlannerContext(user_id=user_id)

    namespace = memory_namespace(_Fake())
    items = list(store.search(namespace))
    if not items:
        print(f"\n  no memories stored yet under {namespace}\n")
        return
    print(f"\n  memories under {namespace}:")
    for item in items:
        print(f"    - {item.key}")
    print()


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(prog="event-planner", description=__doc__)
    parser.add_argument("--thread", default="default", help="Conversation thread id.")
    parser.add_argument(
        "--user",
        default="default",
        help="Scopes persistent memory. Different users get isolated memory.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model id.")
    parser.add_argument(
        "--db",
        default=str(STATE_DIR / "planner.sqlite"),
        help="SQLite file backing conversation state and memory.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help=(
            "Graph super-step budget per turn. Each tool round trip costs about "
            f"4 steps with this middleware stack (default: {DEFAULT_MAX_STEPS})."
        ),
    )
    args = parser.parse_args()

    _load_env()
    if (problem := _check_credentials()) is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    db_path = Path(args.db)
    try:
        _check_db_outside_workspace(db_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with (
        SqliteSaver.from_conn_string(args.db) as checkpointer,
        SqliteStore.from_conn_string(args.db) as store,
    ):
        store.setup()
        graph = build_agent(model=args.model, checkpointer=checkpointer, store=store)
        config = {
            "configurable": {"thread_id": args.thread},
            "recursion_limit": args.max_steps,
        }
        context = PlannerContext(user_id=args.user)

        print(BANNER.format(thread=args.thread, user=args.user, model=args.model))

        while True:
            try:
                line = input("you > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0

            if not line:
                continue
            if line in {"/exit", "/quit"}:
                return 0
            if line == "/state":
                _show_memory(store, args.user)
                continue

            try:
                _run_turn(
                    graph,
                    {"messages": [{"role": "user", "content": line}]},
                    config,
                    context,
                )
            except KeyboardInterrupt:
                print("\n  interrupted — the thread is checkpointed, just keep typing\n")
            except Exception as exc:  # noqa: BLE001 - keep the REPL alive
                print(f"\n  error: {type(exc).__name__}: {exc}\n", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
