"""Shared logic for the Streamlit front end.

The page script (`streamlit_app.py` at the repo root) is deliberately thin: it
calls `st.*` and delegates everything that matters to this module, which touches
no Streamlit runtime state. That split exists so the part worth testing — how an
operator's click becomes a resume payload — stays reachable from the offline test
suite, like the rest of the harness.

Three things are imported from `cli` rather than restated here:

* `_decline_message`, which frames a refusal as a human decision. A bare reason
  reaches the model as the tool's return value and it reads that as the tool
  erroring, then retries — observed live. A second copy of that wording would
  drift from the CLI's, and the drift would surface only as a booking retried
  after a human already said no.
* `_check_db_outside_workspace`, the guard that keeps agent state out of the
  directory the agent itself can read.
* `DEFAULT_MAX_STEPS`. Both front ends drive the same middleware stack, so they
  need the same super-step budget; a second literal would go stale the next time
  middleware is added.

What this module deliberately does *not* mirror is `cli._export`. Writing
agent-chosen store keys to the server's filesystem is the wrong affordance for a
browser front end, and a second copy of the traversal check that makes it safe is
exactly the kind of duplicated security path this codebase avoids elsewhere. The
UI offers downloads instead, which build no server-side path at all — leaving
`download_name` responsible only for a safe *filename*.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.base import SearchItem
from langgraph.store.sqlite import SqliteStore

from event_planner.cli import (
    DEFAULT_MAX_STEPS,
    _check_db_outside_workspace,
    _decline_message,
)
from event_planner.context import namespace_for_user

__all__ = [
    "DEFAULT_MAX_STEPS",
    "SUPPORTED_DECISIONS",
    "approve_decision",
    "download_name",
    "edit_decision",
    "message_text",
    "open_persistence",
    "parse_edited_args",
    "pending_reviews",
    "reject_decision",
    "review_token",
    "stored_items",
    "tool_calls_of",
    "unsupported_decisions",
]

#: Decisions this front end can actually construct. `respond` is absent for the
#: same reason it is absent from `agent.ALLOWED_DECISIONS`: a free-text reply to
#: a booking request invites the model to read commentary as confirmation.
#: Building a text box for it here would quietly reintroduce what that config
#: removed, so an allowed-but-unsupported decision is reported rather than
#: rendered — mirroring the final branch of `cli._prompt_one`.
SUPPORTED_DECISIONS = ("approve", "edit", "reject")

#: Used when the interrupt payload carries no `review_configs` entry for an
#: action. Matches `cli._collect_decisions` so the two front ends fail the same
#: way: toward the narrower set, never toward a wider one.
FALLBACK_DECISIONS = ("approve", "reject")


# --------------------------------------------------------------------------- #
# message rendering
# --------------------------------------------------------------------------- #


def message_text(message: Any) -> str:
    """Flatten a message's content to plain text.

    Content is a list of blocks rather than a string once adaptive thinking is
    on, and thinking blocks carry no `text` key — so this skips any block that
    lacks one rather than assuming every block is a text block.
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        ).strip()
    return str(content).strip()


def tool_calls_of(message: Any) -> list[dict[str, Any]]:
    """Tool calls attached to a message, or an empty list."""
    return list(getattr(message, "tool_calls", None) or [])


def brief_args(args: dict[str, Any], limit: int = 90) -> str:
    """One-line argument summary for a tool-call caption."""
    rendered = ", ".join(f"{key}={value!r}" for key, value in args.items())
    return rendered if len(rendered) <= limit else rendered[: limit - 3] + "..."


# --------------------------------------------------------------------------- #
# human-in-the-loop
# --------------------------------------------------------------------------- #


def pending_reviews(interrupts: Any) -> list[tuple[dict[str, Any], list[str]]]:
    """Normalise a pending interrupt into `(action, allowed_decisions)` pairs.

    Accepts either the `__interrupt__` value from a stream chunk or the
    `interrupts` field of a `StateSnapshot`; both are sequences of `Interrupt`.
    The middleware requires exactly one decision per action, in order, so the
    caller must preserve this ordering when building the resume payload.
    """
    if not interrupts:
        return []

    first = interrupts[0] if isinstance(interrupts, (list, tuple)) else interrupts
    payload = getattr(first, "value", first)
    if not isinstance(payload, dict):
        return []

    actions = payload.get("action_requests") or []
    configs = payload.get("review_configs") or []

    reviews: list[tuple[dict[str, Any], list[str]]] = []
    for index, action in enumerate(actions):
        allowed = (
            configs[index].get("allowed_decisions", FALLBACK_DECISIONS)
            if index < len(configs)
            else FALLBACK_DECISIONS
        )
        reviews.append((action, list(allowed)))
    return reviews


def review_token(snapshot_config: Any, index: int, action: dict[str, Any]) -> str:
    """Identity for one pending action's widgets, for use in Streamlit `key=`.

    Positional keys (`choice-0`) are wrong here, and dangerously so. Streamlit
    restores a keyed widget's value whenever a widget with that key renders
    again, so when one approval resolves and the graph immediately interrupts
    for a *different* action, the new panel inherits the previous decision — it
    renders pre-approved with the submit button already enabled, and one
    reflexive click executes an action nobody reviewed. The edit box is worse:
    a stored value beats the `value=` argument, so the new action's arguments
    are silently replaced by the previous action's.

    The checkpoint id changes with every super-step, so it distinguishes one
    interrupt from the next while staying stable across the reruns that happen
    *within* one pending approval — which is what lets a selection survive long
    enough to be submitted. The action name and arguments are mixed in so the
    key still differs per action when no checkpoint id is available.
    """
    if not isinstance(snapshot_config, dict):
        snapshot_config = {}
    checkpoint = (snapshot_config.get("configurable") or {}).get("checkpoint_id") or ""
    raw = "|".join(
        [
            str(checkpoint),
            str(index),
            str(action.get("name", "")),
            json.dumps(action.get("args", {}), sort_keys=True, default=str),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def unsupported_decisions(allowed: list[str]) -> list[str]:
    """Allowed decisions this front end cannot construct.

    Surfaced to the operator rather than silently dropped. `cli._prompt_one`
    learned the same lesson the hard way: a decision with no handler left the
    menu re-printing forever with no diagnostic.
    """
    return [decision for decision in allowed if decision not in SUPPORTED_DECISIONS]


def approve_decision() -> dict[str, Any]:
    """Execute the action exactly as the model proposed it."""
    return {"type": "approve"}


def reject_decision(reason: str) -> dict[str, Any]:
    """Refuse the action, framed so the model does not read it as a tool error."""
    return {"type": "reject", "message": _decline_message(reason)}


def edit_decision(action: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Execute the action with the operator's arguments instead of the model's."""
    return {"type": "edit", "edited_action": {"name": action["name"], "args": args}}


def parse_edited_args(raw: str) -> dict[str, Any]:
    """Parse operator-edited arguments, rejecting anything that is not an object.

    Raises:
        ValueError: With an operator-readable message. The caller shows it and
            keeps the approval pending rather than falling through to a decision
            built from half-parsed input.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"Not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})."
        raise ValueError(msg) from exc
    if not isinstance(parsed, dict):
        msg = f"Arguments must be a JSON object, got {type(parsed).__name__}."
        raise ValueError(msg)
    return parsed


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def open_persistence(db_path: Path) -> tuple[SqliteSaver, SqliteStore]:
    """Open the checkpointer and store on connections that outlive one script run.

    `from_conn_string` is a context manager that closes the connection on exit,
    which does not fit Streamlit: these objects have to survive the script run
    that created them and be reused by every later rerun. Both classes accept a
    connection directly, so this constructs them the way their own docstrings
    show.

    Sharing one instance across Streamlit's script-runner threads is safe:
    `SqliteSaver` and `SqliteStore` each guard every statement with an internal
    `threading.Lock`, which is precisely why `from_conn_string` passes
    `check_same_thread=False` — replicated here for the same reason.

    The store's connection also needs `isolation_level=None`. `SqliteStore._cursor`
    issues its own `BEGIN`, so leaving Python's implicit transaction handling on
    would nest transactions against a driver that does not support it.

    Raises:
        ValueError: If the database would sit inside the agent's filesystem root,
            where the agent could read every user's memories out of the raw file.
    """
    _check_db_outside_workspace(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    checkpointer = SqliteSaver(
        sqlite3.connect(str(db_path), check_same_thread=False),
    )
    store = SqliteStore(
        sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None),
    )
    store.setup()
    return checkpointer, store


def stored_items(
    store: SqliteStore, user_id: str | None, kind: str
) -> tuple[tuple[SearchItem, ...], tuple[str, ...]]:
    """List one kind of stored item for a user, plus the namespace used.

    Returns nothing for an unidentified caller: with no `user_id` there is no
    cross-thread namespace to list, and inventing one would defeat the scoping.
    """
    if user_id is None:
        return (), ()
    namespace = namespace_for_user(user_id, kind)
    return tuple(store.search(namespace)), namespace


#: Anything outside this set is replaced in a download filename. Deliberately
#: narrow: the key comes from the agent, and the result reaches a
#: `Content-Disposition` header.
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def download_name(key: str) -> str:
    """Flatten an agent-chosen store key into a safe download filename.

    Nothing in the web UI writes these to the server's filesystem, so this is not
    the traversal guard `cli._export` needs — but the name still reaches a
    response header, so separators are flattened rather than trusted, and leading
    dots are stripped so a key of `..` cannot produce a dotfile or a bare `..`.
    """
    flattened = key.strip("/").replace("/", "_").replace("\\", "_")
    cleaned = _UNSAFE_IN_FILENAME.sub("_", flattened).lstrip(".")
    return cleaned or "event-file"
