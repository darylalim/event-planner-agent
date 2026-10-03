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

from event_planner.agent import DEFAULT_EFFORT, DEFAULT_MODEL, WORKSPACE, build_agent
from event_planner.context import PlannerContext, namespace_for_user, safe_component
from event_planner.models import EFFORT_LEVELS, Effort

#: The checkout this was run from. It lives here rather than in `agent.py`
#: because everything below it — `.state/`, `.env`, `exports/` — is an operator
#: concept that exists only in a checkout, whereas `agent.py`'s paths must also
#: be right for an installed copy, and one constant cannot be both. Counting
#: levels up from `__file__` is correct here and meaningless in site-packages,
#: where it lands on `<venv>/lib/pythonX.Y`. `checkout_warning` reports that at
#: startup — it does not prevent it, and deliberately: the operator can still
#: point `--db` somewhere sensible and run. What it removes is the silence.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def checkout_warning() -> str | None:
    """Message describing a `PROJECT_ROOT` that is not a checkout, or None.

    The CLI is a development front end: it keeps `.state/` beside the repo and
    writes `/export` output into `exports/`. Installed rather than checked out,
    both land under the Python installation, and each is quiet on its own — a
    database created where nobody looks, exports written beside it. Said once,
    plainly, they are one recognisable problem.

    It names `--db` and nothing else. `EVENT_PLANNER_DB` is the browser front
    end's knob and this process never reads it, so naming it here would be the
    mistake `_check_db_outside_workspace(..., knob=...)` exists to avoid, in
    reverse. `.env` is left out for the same reason: `_load_env` falls back to
    an upward search, so it is degraded rather than broken.
    """
    if (PROJECT_ROOT / "pyproject.toml").is_file():
        return None
    return (
        f"Not running from a checkout: {PROJECT_ROOT} has no pyproject.toml.\n"
        f"  .state/ and exports/ will be created under that path.\n"
        f"  Pass --db to put the database somewhere you chose."
    )


#: Deliberately outside the agent's filesystem root, which now lives inside the
#: package at `src/event_planner/workspace/`. The agent has
#: `ls`/`read_file`/`glob`/`grep` over its filesystem root, so a database kept
#: under that root would let any session read every other user's memories and
#: every other thread's checkpoints straight out of the raw file — defeating
#: the per-user namespacing entirely. `_check_db_outside_workspace` enforces
#: this for operator-supplied paths too.
STATE_DIR = PROJECT_ROOT / ".state"

#: LangGraph counts every node as a super-step, and this harness runs five
#: middleware nodes at two different rates: three `before_agent` once per
#: invocation, two `after_model` on every model call.
#: Measured against the live model, one tool round trip costs ~4 steps, so a
#: planning session that shortlists venues, checks dates, prices catering and
#: delegates to subagents needs room for dozens of them.
#:
#: This is a ceiling, not a rescue. LangGraph's own default of 25 never reaches
#: this agent — `create_deep_agent` binds `recursion_limit: 9_999` onto the
#: compiled graph — so passing this lowers that bound rather than raising the
#: default, and dropping it uncaps a runaway session instead of stranding one.
DEFAULT_MAX_STEPS = 200

#: Root run name in LangSmith. Without it every trace is titled `LangGraph`,
#: which is also what Studio and any other graph in the same project emit.
RUN_NAME = "event-planner"


def run_config(
    thread: str,
    *,
    user_id: str | None,
    front_end: str,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> dict[str, Any]:
    """The `config` every entry point invokes the graph with — one copy.

    LangGraph already copies `thread_id` into trace metadata, which is what
    LangSmith's Threads view groups on. `user_id` is not in `configurable` (it
    travels in `context=`), so a trace says nothing about whose session it was
    unless it is set here. It is omitted rather than sent as `None`, matching
    the unidentified branch in `context.py`. `front_end` is both a tag and
    metadata, so the CLI, browser and live-check runs can be told apart.

    LangGraph also writes this metadata into every checkpoint, tracing or not.
    That stays local, in the same database whose store already keys on the id.
    """
    metadata: dict[str, Any] = {"front_end": front_end}
    if user_id:
        metadata["user_id"] = user_id
    return {
        "configurable": {"thread_id": thread},
        "recursion_limit": max_steps,
        "run_name": RUN_NAME,
        "tags": [front_end],
        "metadata": metadata,
    }


BANNER = """\
Event Planner  (Deep Agents)
  thread: {thread}   user: {user}   model: {model} ({effort} effort)
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


def credentials_problem() -> str | None:
    """Message describing a missing *required* credential, or None.

    Split from its rendering so both front ends apply the same test against the
    same environment. A third required credential added here must not leave one
    of them starting up and failing opaquely from inside the SDK — the failure
    `_load_env` exists to prevent.
    """
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return None
    return (
        f"ANTHROPIC_API_KEY is not set.\n"
        f"  Copy .env.example to .env and fill it in:\n"
        f"    cp {PROJECT_ROOT / '.env.example'} {PROJECT_ROOT / '.env'}"
    )


def degraded_capability_note() -> str | None:
    """Message about an optional credential whose absence changes behaviour."""
    if os.environ.get("TAVILY_API_KEY", "").strip():
        return None
    return "TAVILY_API_KEY not set — web_search will degrade to the structured directory only."


def _check_credentials() -> str | None:
    """Return an actionable message when required credentials are missing."""
    if (problem := credentials_problem()) is not None:
        return problem
    if (note := degraded_capability_note()) is not None:
        print(f"  note: {note}\n", file=sys.stderr)
    return None


class UnsafeDatabaseLocation(ValueError):
    """The database would sit inside the directory the agent itself can read.

    A `ValueError` subclass so callers that catch `ValueError` keep working, but
    nameable on its own: the web UI has to tell this apart from the other
    `ValueError`s that building the agent can raise — an unparseable model id
    among them — so it does not report a model typo as a storage problem.
    """


def _check_db_outside_workspace(db_path: Path, knob: str = "--db") -> None:
    """Refuse to put agent state where the agent can read it.

    Args:
        db_path: Where the database would live.
        knob: How the *calling front end* names this setting, so the message
            points at something the operator actually set. The CLI has `--db`;
            the browser UI has the `EVENT_PLANNER_DB` environment variable, and
            being told to fix a flag that front end has no way to pass is a
            dead end.

    Raises:
        UnsafeDatabaseLocation: If the database would sit inside the agent's
            filesystem root.
    """
    resolved = db_path.resolve()
    root = WORKSPACE.resolve()
    if resolved == root or root in resolved.parents:
        msg = (
            f"{knob} {resolved} is inside the agent's filesystem root ({root}).\n"
            "The agent can read that directory, so it could read every user's "
            "memories and every thread's checkpoints out of the raw database. "
            "Choose a path outside the workspace."
        )
        raise UnsafeDatabaseLocation(msg)


#: Rows per `store.search` request while paging. Large enough that a typical
#: tenant is one round trip, small enough not to hold an unbounded page.
_STORE_PAGE = 100


def _stored(
    store: SqliteStore, user_id: str | None, kind: str
) -> tuple[tuple[SearchItem, ...], tuple[str, ...]]:
    """List one kind of stored item for a user, plus the namespace used.

    Typed concretely rather than as `Any`: callers reach `item.key`, and
    `_export` treats that key as untrusted input to a filesystem write. An
    `Any` element type would let a rename upstream pass unchecked.

    Paged rather than a bare `search`, because `langgraph.store.base` defaults
    `limit` to 10 and nothing at the call site says so: the browser's "Stored"
    panel listed at most ten of each kind, and `_export` below wrote ten files
    while printing the count as though it were the whole set. Silent in both
    directions — no error, no truncation marker. `artifacts` crosses ten first,
    since it holds deepagents' offload spill and nothing evicts it.
    """
    if user_id is None:
        return (), ()
    namespace = namespace_for_user(user_id, kind)

    items: list[SearchItem] = []
    while True:
        page = store.search(namespace, limit=_STORE_PAGE, offset=len(items))
        items.extend(page)
        # A short page is the last page. An exactly-full final page costs one
        # extra empty request, which is cheaper than guessing.
        if len(page) < _STORE_PAGE:
            return tuple(items), namespace


def _show_memory(store: SqliteStore, user_id: str | None) -> None:
    if user_id is None:
        print("\n  no --user given, so storage is scoped to this thread only.")
        print("  Pass --user <id> for memory that carries across threads.\n")
        return
    # "artifacts" is deepagents' offload spill, not something the planner
    # wrote. Listed anyway: it is the client's data, it accumulates with no
    # eviction, and before it was routed it at least sat on disk where an
    # operator could see and delete it.
    for kind in ("memories", "events", "artifacts"):
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


#: The `--effort` value that sends no effort at all. A word rather than an empty
#: string, because `--effort ''` reads like a typo and is easy to pass by accident.
NO_EFFORT = "none"


def _effort_arg(raw: str) -> Effort | None:
    """Parse `--effort`: one of the API's levels, or `none` to send nothing."""
    if raw == NO_EFFORT:
        return None
    for level in EFFORT_LEVELS:
        if raw == level:
            return level
    msg = f"invalid effort {raw!r}; choose from {', '.join((*EFFORT_LEVELS, NO_EFFORT))}"
    raise argparse.ArgumentTypeError(msg)


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
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="The orchestrator's model id. Subagents keep their own (subagents.py).",
    )
    parser.add_argument(
        "--effort",
        type=_effort_arg,
        default=DEFAULT_EFFORT,
        metavar="{" + ",".join((*EFFORT_LEVELS, NO_EFFORT)) + "}",
        help=(
            f"The orchestrator's effort (default: {DEFAULT_EFFORT}). '{NO_EFFORT}' "
            "sends none, for a model that rejects the parameter, such as "
            "claude-haiku-4-5."
        ),
    )
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
    # Beside the other startup checks rather than inside the credential gate:
    # "are credentials present?" should not also emit a filesystem note.
    if (warning := checkout_warning()) is not None:
        print(f"  note: {warning}\n", file=sys.stderr)
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
        try:
            graph = build_agent(
                model=args.model,
                effort=args.effort,
                checkpointer=checkpointer,
                store=store,
            )
        except FileNotFoundError as exc:
            # A wheel that dropped its skills is a packaging fault, and it should
            # read like the other startup refusals rather than as a traceback.
            print(f"error: {exc}", file=sys.stderr)
            return 2
        config = run_config(
            args.thread, user_id=args.user, front_end="cli", max_steps=args.max_steps
        )
        context = PlannerContext(user_id=args.user)

        print(
            BANNER.format(
                thread=args.thread,
                user=args.user or "(none — storage scoped to this thread)",
                model=args.model,
                effort=args.effort or "model-default",
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
