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
from langgraph.store.base import SearchItem
from langgraph.store.sqlite import SqliteStore
from langgraph.types import Command

from event_planner.agent import DEFAULT_MODEL, PROJECT_ROOT, WORKSPACE, build_agent
from event_planner.context import PlannerContext, namespace_for_user, safe_component

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
  Type your event brief.
  /state   what is stored for this user     /export  write event files to disk
  /exit    quit
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
                    text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
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


class _OperatorAbsent(Exception):
    """stdin closed while a decision was being collected."""


def _ask(prompt: str) -> str:
    """Read one line, converting a closed stdin into a typed signal."""
    try:
        return input(prompt).strip()
    except EOFError as exc:
        raise _OperatorAbsent from exc


def _decline_message(reason: str) -> str:
    """Frame a rejection as a human decision, not a tool failure.

    A bare reason reaches the model as the tool's return value, and it reads
    that as the tool erroring — observed live: "hold_venue returned an error".
    The distinction is behavioural, not cosmetic: a failed tool invites a
    retry, whereas a refusal must not be retried. Say who decided and that the
    action did not happen.
    """
    reason = reason.strip() or "No reason given."
    return (
        "A human operator reviewed this action and declined it. "
        "The action was NOT performed and must not be retried unless the "
        f"operator's concern is resolved first. Operator's reason: {reason}"
    )


def _unique_prefix(option: str, allowed: list[str]) -> str:
    """Shortest prefix of `option` that no other allowed decision shares."""
    others = [o for o in allowed if o != option]
    for length in range(1, len(option) + 1):
        prefix = option[:length]
        if not any(o.startswith(prefix) for o in others):
            return prefix
    return option


def _resolve_choice(raw: str, allowed: list[str]) -> str | None:
    """Resolve typed input to exactly one decision, or None.

    Matching on first letter alone is unsafe: "reject" and "respond" share one,
    so `r` would silently pick whichever landed last in the map — turning an
    operator's refusal into a free-text reply the model may read as consent.
    An ambiguous entry resolves to None and re-prompts; it never guesses.
    """
    if not raw:
        return None
    if raw in allowed:
        return raw
    matches = [option for option in allowed if option.startswith(raw)]
    return matches[0] if len(matches) == 1 else None


def _prompt_one(action: dict[str, Any], allowed: list[str]) -> dict[str, Any]:
    """Read a single decision from the terminal, re-prompting on bad input."""
    hint = " / ".join(f"[{(p := _unique_prefix(d, allowed))}]{d[len(p) :]}" for d in allowed)

    # One guard for every read in this decision, not just the menu. A closed
    # stdin partway through — after choosing "reject" but before typing the
    # reason — used to raise EOFError out of the function, unwind past the
    # fail-closed path, and abandon the pending approval entirely.
    try:
        while True:
            raw = _ask(f"  {hint} > ").lower()

            choice = _resolve_choice(raw, allowed)
            if choice is None:
                if raw and any(o.startswith(raw) for o in allowed):
                    candidates = [o for o in allowed if o.startswith(raw)]
                    print(f"  {raw!r} is ambiguous — did you mean {' or '.join(candidates)}?")
                else:
                    print(f"  Enter one of: {', '.join(allowed)}")
                continue

            if choice == "approve":
                return {"type": "approve"}

            if choice == "reject":
                return {
                    "type": "reject",
                    "message": _decline_message(_ask("  reason (fed back to the agent): ")),
                }

            if choice == "respond":
                return {"type": "respond", "message": _ask("  response: ")}

            if choice == "edit":
                print(f"  current args: {json.dumps(action.get('args', {}), indent=2)}")
                edited = _ask("  new args as JSON (blank to cancel): ")
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

            # `allowed` comes from middleware config and may name a decision
            # this prompt has no handler for. Without this branch, `choice` is
            # non-None so neither error message prints and none of the returns
            # fire — the loop re-prints the menu forever with no diagnostic and
            # the only exit abandons the pending approval.
            print(
                f"  {choice!r} is allowed by the agent but this CLI cannot "
                f"construct it. Choose another option, or use the API directly."
            )
    except _OperatorAbsent:
        print("\n  no input available — rejecting for safety")
        return {
            "type": "reject",
            "message": _decline_message("No operator was available to review this action."),
        }


# --------------------------------------------------------------------------- #
# turn loop
# --------------------------------------------------------------------------- #


def _run_turn(graph: Any, payload: Any, config: dict[str, Any], context: PlannerContext) -> None:
    """Stream one turn, pausing for approval as many times as needed."""
    while True:
        pending: Any = None
        for chunk in graph.stream(payload, config=config, context=context, stream_mode="updates"):
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


def _stored(
    store: SqliteStore, user_id: str | None, kind: str
) -> tuple[tuple[SearchItem, ...], tuple[str, ...]]:
    """List one kind of stored item for a user, plus the namespace used.

    Typed concretely rather than as `Any`: callers reach `item.key`, and
    `_export` treats that key as untrusted input to a filesystem write. An
    `Any` element type would let a rename upstream pass unchecked.
    """
    if user_id is None:
        return (), ()
    namespace = namespace_for_user(user_id, kind)
    return tuple(store.search(namespace)), namespace


def _show_memory(store: SqliteStore, user_id: str | None) -> None:
    if user_id is None:
        print("\n  no --user given, so storage is scoped to this thread only.")
        print("  Pass --user <id> for memory that carries across threads.\n")
        return
    for kind in ("memories", "events"):
        items, namespace = _stored(store, user_id, kind)
        if not items:
            print(f"\n  no {kind} stored yet under {namespace}")
            continue
        print(f"\n  {kind} under {namespace}:")
        for item in items:
            size = len((item.value or {}).get("content", "") or "")
            print(f"    - {item.key}  ({size:,} bytes)")
    print()


def _export(store: SqliteStore, user_id: str | None, destination: Path) -> None:
    """Write this user's event files to disk.

    Event files live in the store rather than on the shared filesystem root so
    they cannot leak between planners, which means they are not browsable by
    default. This puts a copy where a human can read it, on request.
    """
    if user_id is None:
        print("\n  /export needs --user to know whose files to write.\n")
        return
    items, _ = _stored(store, user_id, "events")
    if not items:
        print("\n  nothing to export yet.\n")
        return

    # Store keys are the paths the *agent* chose, so they are untrusted input
    # to this filesystem write. A key of "/events/../../../x" escaped the
    # export directory entirely — and silently, since the written file then
    # sat outside the tree this function lists. user_id is likewise never used
    # as a raw path segment: it goes through the same sanitizer as namespaces.
    base = (destination / safe_component(user_id)).resolve()
    written = 0
    skipped = 0

    for item in items:
        content = (item.value or {}).get("content")
        if content is None:
            continue

        relative = Path(item.key.lstrip("/"))
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            print(f"    skipped {item.key!r} — unsafe path")
            skipped += 1
            continue

        target = (base / relative).resolve()
        if target != base and base not in target.parents:
            print(f"    skipped {item.key!r} — escapes the export directory")
            skipped += 1
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        print(f"    wrote {target}")
        written += 1

    note = f", skipped {skipped} unsafe path(s)" if skipped else ""
    print(f"\n  exported {written} file(s){note}.\n")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(prog="event-planner", description=__doc__)
    parser.add_argument("--thread", default="default", help="Conversation thread id.")
    parser.add_argument(
        "--user",
        default=None,
        help=(
            "Scopes persistent storage. Different users get isolated memory and "
            "event files. Omit it and storage is scoped to this thread instead "
            "— deliberately, since a shared placeholder id would merge every "
            "unidentified operator into one bucket."
        ),
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

        print(
            BANNER.format(
                thread=args.thread,
                user=args.user or "(none — storage scoped to this thread)",
                model=args.model,
            )
        )

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
            if line == "/export":
                _export(store, args.user, PROJECT_ROOT / "exports")
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
