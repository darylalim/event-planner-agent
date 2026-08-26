"""Streamlit front end for the event planning agent.

Run with `uv run streamlit run streamlit_app.py`. The CLI (`uv run event-planner`)
remains the reference implementation; this is the same graph, the same
persistence, and the same approval gates behind a browser UI.

There is no authentication. "User id" is a free-text field, so anyone who can
reach the port can name any tenant and read that tenant's stored files. Do not
put it on a shared network without an auth layer in front.

`.streamlit/config.toml` binds the server to loopback, but Streamlit reads that
file from the **current working directory** rather than from this script's
directory — so it applies to `uv run streamlit run streamlit_app.py` from the
repo root and not to the same script launched from anywhere else. Since the
config cannot enforce itself, the page checks `server.address` at startup and
says so in the browser when the bind is not loopback; `--server.address 127.0.0.1`
still works and is the fix when it fires.

The turn loop is inverted relative to `cli._run_turn`. That loop blocks on
`input()` until the operator decides; a Streamlit script cannot block, because it
runs top to bottom and ends, then reruns on the next interaction. So the loop
state lives outside the script:

* The **transcript** is re-read from the checkpointer on every rerun rather than
  accumulated in `st.session_state`. The checkpointer is already the source of
  truth for conversation history, and a second copy in session state would drift
  from it the first time a turn errored halfway through.
* The **pending approval** is re-derived from `StateSnapshot.interrupts` for the
  same reason, which also means a half-answered booking survives a browser
  refresh instead of being stranded.
* Only the **next payload** is held in session state, in `pending_input`, and it
  is consumed before the turn runs so a spurious rerun cannot resubmit it.

Because the transcript is re-read and re-rendered on every rerun, the approval
gate is an `st.fragment`: choosing a decision or editing arguments would
otherwise replay an entire planning session to redraw one segmented control.
Submitting escapes the fragment on purpose — see `_approval_panel`.

Business logic lives in `event_planner.webui` so it stays testable without a
Streamlit runtime; this file is the page.
"""

from __future__ import annotations

import json
import os
from functools import partial
from pathlib import Path
from typing import Any

import streamlit as st
from langgraph.types import Command

from event_planner.agent import DEFAULT_MODEL, PROJECT_ROOT, build_agent
from event_planner.cli import STATE_DIR, _load_env
from event_planner.context import PlannerContext
from event_planner.webui import (
    DEFAULT_MAX_STEPS,
    SUPPORTED_DECISIONS,
    UnsafeDatabaseLocation,
    approve_decision,
    brief_args,
    close_persistence,
    credentials_problem,
    degraded_capability_note,
    download_name,
    edit_decision,
    markdown_safe,
    message_text,
    open_persistence,
    parse_edited_args,
    pending_reviews,
    reject_decision,
    review_token,
    stored_items,
    tool_calls_of,
    unsupported_decisions,
)

st.set_page_config(
    page_title="Event planner",
    page_icon=":material/event:",
    layout="centered",
)

# Same `.env` the CLI reads, loaded by the same pinned-path helper. Streamlit's
# own `st.secrets` would be the idiomatic choice for a new app, but this project
# already has one credential source and two would be one too many.
_load_env()

#: Marks "continue the turn already in the checkpoint" in `pending_input`, which
#: LangGraph expresses as streaming `None`. `None` is already this page's "nothing
#: to do", so the intent needs its own value — and it has to be a plain string
#: rather than an `object()` sentinel, because the page is re-exec'd on every
#: rerun and a fresh marker object would not match the one in session state.
RESUME_PENDING = "__resume_pending_turn__"


# --------------------------------------------------------------------------- #
# resources
# --------------------------------------------------------------------------- #


def _release_resources(value: tuple[Any, Any, Any]) -> None:
    """Close an evicted entry's SQLite connections.

    `max_entries` bounds how many graphs the cache keeps; it does not close what
    it drops. Without this, each eviction leaks a checkpointer and a store
    connection for the life of the process.
    """
    _graph, store, checkpointer = value
    close_persistence(checkpointer, store)


# Bounded because `model` is a free-text field: every distinct value builds a
# graph and opens two SQLite connections, so an unbounded cache keyed on operator
# input accumulates both. `on_release` is what actually reclaims them.
@st.cache_resource(
    show_spinner="Opening the planner…",
    max_entries=4,
    on_release=_release_resources,
)
def _resources(db: str, model: str) -> tuple[Any, Any, Any]:
    """Build the graph once and share it across reruns and sessions.

    Cached on `(db, model)`: the checkpointer and store are per-database, and the
    graph binds the model at construction. Everything that varies per operator —
    thread id, user id — is passed per call as config and context instead, so it
    must not be part of this key.

    The checkpointer is returned as well even though the page never touches it:
    `_release_resources` needs its connection, and reaching into `graph` for it
    would depend on deepagents internals.
    """
    checkpointer, store = open_persistence(Path(db))
    graph = build_agent(model=model, checkpointer=checkpointer, store=store)
    return graph, store, checkpointer


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _content_of(item: Any) -> str:
    """Materialise one stored item's text, for a download that was clicked."""
    return (item.value or {}).get("content") or ""


def _render_ai(message: Any) -> None:
    """Render one assistant turn: prose, then the tools it decided to call."""
    text = message_text(message)
    calls = tool_calls_of(message)
    if not text and not calls:
        return
    with st.chat_message("assistant"):
        if text:
            st.markdown(markdown_safe(text))
        for call in calls:
            args = markdown_safe(brief_args(call.get("args", {})))
            st.caption(f":material/build: **{call['name']}** — {args}")


#: Panel keys already claimed in this script run. Streamlit re-execs the page on
#: every rerun, so this resets with it — measured, not assumed. It has to be
#: per-run because a duplicate key *raises*, and an exception here takes the
#: whole page down: no transcript, no chat input, and no approval panel for a
#: booking still parked behind one.
_PANEL_KEYS: set[str] = set()


def _panel_key(message: Any) -> str | None:
    """A widget key unique to this tool result, or `None` if there isn't one.

    Namespaced by source, because `tool_call_id` and `id` are separate id
    spaces: flattened into one prefix, a value that appears in both collides.

    A key already claimed in this run yields `None` rather than colliding. Two
    `ToolMessage`s can carry one `tool_call_id` — a resumed node re-emitting, a
    replay overlapping a mid-stream render — and rendering that unkeyed costs a
    duplicated panel, while rendering it keyed costs the page.

    Keyed on the message rather than its position because `_render` is called
    from both the transcript replay and mid-stream, with no counter shared
    between them; and not on the label, which repeats seven times over on the
    recorded `full-brief-3` thread.
    """
    for prefix, attr in (("tc", "tool_call_id"), ("id", "id")):
        if raw := getattr(message, attr, None):
            key = f"tool-{prefix}-{raw}"
            if key in _PANEL_KEYS:
                return None
            _PANEL_KEYS.add(key)
            return key
    return None


def _render_tool(message: Any, *, gated: bool = True) -> None:
    """Render a tool result, collapsed — and, when gated, computed only if opened.

    Tool output is long — a venue comparison or a budget breakdown runs to
    hundreds of lines — and the assistant's summary of it is the part worth
    reading. Collapsing keeps the transcript legible with the detail one click
    away.

    `on_change="rerun"` is what makes "collapsed" mean *not sent*. Streamlit
    computes and ships an expander's whole body even while it is closed, and
    this page replays the entire checkpointed transcript on every rerun — so
    without the gate, every keystroke in the sidebar re-serialises every tool
    result in the thread. Measured across this repo's own recorded threads, tool
    output is 37-61% of all transcript text: 70.8 KB of it on `full-brief-3`,
    whose largest single result is 30.6 KB.

    `gated=False` renders a plain block instead, and the caller passes it for
    every message on a script run that will also stream a turn. A gated panel is
    a *widget*: toggling one posts a rerun request, Streamlit services that at
    the next `st.*` call — most of them are implicit yield points — by raising
    `RerunException`, and that subclasses `BaseException`, so the turn's `except
    Exception` does not catch it and `graph.stream` is abandoned mid-flight. The
    page already refuses turn-interrupting input while a turn runs
    (`submit_mode="disable"` on the chat box); a tool panel that quietly killed
    a 672s planning turn would be a hole in the same rule.

    What gating costs where it does apply is that opening a panel becomes a full
    app rerun rather than a client-side toggle: `graph.get_state` deserialises
    the transcript again and the sidebar re-lists the store once per kind, all
    against the same WAL database the CLI shares. Still the cheaper side, since
    that rerun carries no other panel's body — and safe beside a parked
    approval, because `turn_attempt` advances on a turn and the checkpoint id
    advances when the graph does, so `review_token` holds still and an
    in-progress decision is restored rather than cleared.
    """
    name = getattr(message, "name", None) or "tool"
    key = _panel_key(message) if gated else None
    gate = {"key": key, "on_change": "rerun"} if key else {}

    panel = st.expander(f"{name} result", icon=":material/output:", **gate)
    # `.open` is `None` on an ungated panel and `False` on a closed gated one,
    # so this renders eagerly in exactly the cases that carry no gate.
    if panel.open is not False:
        with panel:
            st.code(str(message.content), language="text", wrap_lines=True)


def _render(message: Any, *, gated: bool = True) -> None:
    """Render any message by kind. Shared by the history replay and the live turn.

    One function for both on purpose: rendering the live stream differently from
    the checkpointed history makes the page visibly rearrange itself on the next
    rerun, which reads as a bug. `gated` is the one thing that does differ, and
    it changes no layout — only whether a tool panel is a widget. See
    `_render_tool`.
    """
    kind = getattr(message, "type", None)
    if kind == "human":
        with st.chat_message("user"):
            st.markdown(markdown_safe(message_text(message)))
    elif kind == "ai":
        _render_ai(message)
    elif kind == "tool":
        _render_tool(message, gated=gated)


def _stream_turn(graph: Any, payload: Any, config: dict[str, Any], ctx: PlannerContext) -> None:
    """Stream one turn, rendering updates as they arrive.

    Unlike `cli._run_turn` this does not loop on interrupts: it streams until the
    graph stops, and the caller re-derives any pending approval from the
    checkpoint afterwards. A turn that ends in an interrupt simply ends here.
    """
    for chunk in graph.stream(payload, config=config, context=ctx, stream_mode="updates"):
        for node, update in chunk.items():
            if node == "__interrupt__" or not isinstance(update, dict):
                continue
            for message in update.get("messages", []) or []:
                # Inert: a widget rendered here could be toggled while this loop
                # is still running, and the resulting `RerunException` would
                # abandon the stream. See `_render_tool`.
                _render(message, gated=False)


# --------------------------------------------------------------------------- #
# the approval gate
# --------------------------------------------------------------------------- #


def _review_tokens(
    snapshot_config: Any, reviews: list[tuple[dict[str, Any], list[str]]]
) -> list[str]:
    """Identity of a whole pending set, for comparing one against another."""
    return [
        review_token(snapshot_config, index, action) for index, (action, _) in enumerate(reviews)
    ]


@st.fragment
def _approval_panel(
    reviews: list[tuple[dict[str, Any], list[str]]],
    snapshot_config: Any,
    attempt: int,
    graph: Any,
    config: dict[str, Any],
) -> None:
    """Collect one decision per pending action and resume the graph with them.

    A fragment, so picking a decision or editing arguments reruns only this
    panel. Everything else on the page is a full-rerun cost that buys nothing
    here: the transcript is re-read from the checkpointer and re-rendered from
    scratch on every widget change, and by the time a booking is proposed that
    is the whole planning session — 15.7 KB of venue comparison in the recorded
    live run, before the operator has even chosen `edit`.

    What that isolation costs is the page's accidental fail-closed. `reviews` is
    captured on the last *full* run and a fragment rerun does not refresh it, so
    it goes stale whenever something else answers this interrupt first — another
    browser tab on the same thread, or a CLI turn, since both front ends share
    one database by default and `_resources` caches one graph across sessions.
    Before this was a fragment, the Submit click was itself a full rerun: it
    re-read the snapshot, found no pending review, and never rendered the button
    at all. That has to be made explicit rather than quietly lost, so the submit
    below re-reads state and compares before it resumes anything.

    Submitting is the deliberate exception to the isolation. `st.rerun()`
    defaults to `scope="app"`, so it escapes the fragment and the turn runs from
    the main script against freshly read state, exactly as it did before.
    """
    decisions: list[dict[str, Any]] = []
    blocked = False

    for index, (action, allowed) in enumerate(reviews):
        # Widget identity has to follow the *action*, not its position. Streamlit
        # restores a keyed widget's value whenever that key renders again, so
        # positional keys let a resolved approval's selection carry into the next
        # interrupt — the new action rendering pre-approved with submit enabled,
        # and for `edit`, prefilled with the previous action's arguments.
        #
        # `attempt` closes the same hole for a turn that *failed*: the checkpoint
        # id inside `review_token` only advances when the graph does, so a resume
        # that raised before any state change would otherwise rebuild these
        # widgets with the operator's last decision restored and submit live.
        token = f"{attempt}-{review_token(snapshot_config, index, action)}"

        with st.container(border=True):
            st.subheader(f":material/gavel: Approval required — `{action['name']}`")
            # The badge carries "irreversible"; the caption is the instruction,
            # not a restatement of it.
            st.badge("Irreversible", icon=":material/warning:", color="red")
            st.caption("Check these against the budget file before deciding.")

            st.caption("Proposed arguments")
            st.json(action.get("args", {}))

            # Collapsed, and as preformatted text rather than markdown. What the
            # middleware actually puts here is boilerplate that repeats the tool
            # name and a Python dict repr of the args — already shown above, and
            # markdown mangles the braces. Kept rather than dropped because a
            # future `interrupt_on` config could put something meaningful here.
            if description := action.get("description"):
                # Gated like the tool panels, for the same reason and with the
                # same rule: a closed expander still ships its body, and this one
                # re-ships on every fragment rerun — every decision picked, every
                # keystroke in the reason and edited-args boxes — for as long as
                # the approval is parked. Keyed on the action's token, since two
                # pending actions would otherwise collide on one key. `rerun`
                # here reruns the fragment, not the app; no turn is in flight to
                # interrupt, because the graph is parked waiting on this panel.
                note = st.expander(
                    "Middleware note",
                    icon=":material/notes:",
                    key=f"note-{token}",
                    on_change="rerun",
                )
                if note.open:
                    with note:
                        st.code(str(description), language="text", wrap_lines=True)

            if unsupported := unsupported_decisions(allowed):
                # Say so rather than silently narrowing the operator's options.
                st.warning(
                    f"The agent also allows {', '.join(f'`{d}`' for d in unsupported)} here, "
                    "which this UI does not offer. `respond` in particular is excluded by "
                    "design: a free-text reply to a booking request invites the model to "
                    "read commentary as confirmation. Use the CLI if you need it.",
                    icon=":material/info:",
                )

            # From the one list, not a second literal — `SUPPORTED_DECISIONS` is
            # what `unsupported_decisions` measures against, so a copy here could
            # silently omit a decision while that warning stayed satisfied.
            offered = [d for d in SUPPORTED_DECISIONS if d in allowed]
            if not offered:
                st.error("No decision this UI can construct is allowed.", icon=":material/block:")
                blocked = True
                continue

            # Two steps — pick, then confirm. A single-click Approve is far easier
            # to hit by accident in a browser than `a` + Enter is in a terminal,
            # and `hold_venue` starts a deposit clock.
            choice = st.segmented_control(
                "Decision",
                offered,
                key=f"choice-{token}",
                format_func=str.capitalize,
            )

            if choice == "approve":
                decisions.append(approve_decision())
            elif choice == "reject":
                reason = st.text_area(
                    "Reason (fed back to the agent)",
                    key=f"reason-{token}",
                    placeholder="Why this is not going ahead.",
                )
                decisions.append(reject_decision(reason))
            elif choice == "edit":
                edited = st.text_area(
                    "Arguments to execute instead",
                    value=json.dumps(action.get("args", {}), indent=2),
                    height=200,
                    key=f"args-{token}",
                )
                try:
                    decisions.append(edit_decision(action, parse_edited_args(edited)))
                except ValueError as exc:
                    st.error(str(exc), icon=":material/data_object:")
                    blocked = True
            else:
                blocked = True

    # The middleware wants exactly one decision per action, in order — a mismatch
    # raises rather than being padded, so the button stays disabled until every
    # pending action has a well-formed decision.
    ready = not blocked and len(decisions) == len(reviews)

    submitted = st.button(
        "Submit decision" if len(reviews) == 1 else f"Submit {len(reviews)} decisions",
        type="primary",
        disabled=not ready,
        icon=":material/send:",
    )
    # `not blocked` already implies every action produced a decision, so this is
    # simply the negation of `ready` — an earlier `not ready and not blocked`
    # spelling was unsatisfiable, and the operator got a greyed-out button with
    # no explanation at all. On its own line rather than beside the button: it is
    # two lines of prose, and in a horizontal row on a centered layout it squeezes
    # the button it is explaining.
    if not ready:
        st.caption(
            "Choose a decision for each pending action, and fix anything flagged "
            "above, before submitting."
        )

    # Guarded twice, deliberately. Streamlit 1.62 does enforce `disabled` server
    # side, dropping an incoming value for a disabled widget — but that is a
    # property of the pinned version, not a promise about what reaches this
    # branch. A click that slipped through with nothing chosen would resume the
    # graph with an empty decision list against middleware that wants exactly
    # one decision per pending action. The re-check costs nothing and does not
    # depend on which Streamlit is installed.
    if submitted and ready:
        # Compare-and-swap against live state, because `reviews` is as old as the
        # last full run. If another session answered this interrupt in between,
        # resuming would push decisions at a graph that is no longer asking — so
        # re-derive instead and let the main script render whatever is actually
        # pending. This is the one place the fragment reads the graph, and it
        # reads it *after* the operator has committed rather than while
        # rendering, so it cannot serve a stale panel.
        live = graph.get_state(config)
        current = pending_reviews(getattr(live, "interrupts", None))
        if _review_tokens(getattr(live, "config", None), current) == _review_tokens(
            snapshot_config, reviews
        ):
            st.session_state.pending_input = Command(resume={"decisions": decisions})
        else:
            st.session_state.stale_approval = True
        st.rerun()


# --------------------------------------------------------------------------- #
# sidebar — rendered before any slow work, so it paints immediately
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.subheader("Session", divider="gray")

    thread = st.text_input(
        "Thread",
        value="default",
        key="thread",
        help="Names the conversation. Switching threads resumes a different plan.",
    )
    user_raw = st.text_input(
        "User id",
        value="",
        key="user",
        placeholder="none — scoped to this thread",
        help=(
            "Scopes memory and event files. Leave it blank and storage scopes to "
            "the thread instead — deliberately, since a shared placeholder id "
            "would merge every unidentified operator into one bucket. This field "
            "is not authenticated: it names a tenant, it does not prove one."
        ),
    )
    model = st.text_input("Model", value=DEFAULT_MODEL, key="model")

    # Blank must mean *absent*, never a placeholder string. This is the same
    # invariant as `PlannerContext.user_id` defaulting to None: a truthy fallback
    # here would send every unnamed operator down the identified branch and into
    # one shared namespace.
    user_id = user_raw.strip() or None

# `layout="centered"` keeps the transcript readable; the title paints before the
# graph is built so a cold start does not show an empty page.
st.title("Event planner")
st.caption("Deep Agents · bookings and invitations pause for your approval")

# Same tests the CLI runs, against the same environment — a third required
# credential added there must not leave this front end starting up and failing
# opaquely from inside the SDK. Only the rendering differs.
if (problem := credentials_problem()) is not None:
    st.error(problem.replace("\n", "\n\n"), icon=":material/key_off:")
    st.stop()

if (note := degraded_capability_note()) is not None:
    st.caption(f":material/info: {note}")

# `.streamlit/config.toml` pins the bind to loopback, but Streamlit resolves
# project config from the *current working directory*, not from the script's
# directory — verified: with CWD elsewhere, `server.address` comes back `None`,
# which is Streamlit's bind-to-every-interface default. So `streamlit run
# /path/to/streamlit_app.py` from a home directory, or a unit file with a
# different WorkingDirectory, serves this page publicly and silently. The config
# cannot enforce itself, so the page says so: "User id" names a tenant, it does
# not prove one, and there is no auth layer here to make that safe.
if st.get_option("server.address") not in ("127.0.0.1", "localhost", "::1"):
    st.warning(
        "This page is not bound to loopback, so anyone who can reach this port "
        "can read any tenant's memories and event files by typing their user id. "
        "`.streamlit/config.toml` only applies when Streamlit is launched from "
        "the repo root — restart from there, or pass "
        "`--server.address 127.0.0.1`.",
        icon=":material/lock_open:",
    )

# The CLI takes `--db`; a Streamlit script has no argv to read, so the same knob
# is an environment variable. Both default to the same file, so the two front
# ends share one set of threads and memories unless an operator separates them.
db_path = os.environ.get("EVENT_PLANNER_DB", "").strip() or str(STATE_DIR / "planner.sqlite")

try:
    graph, store, _checkpointer = _resources(db_path, model)
except UnsafeDatabaseLocation as exc:
    # Named specifically: building the agent raises `ValueError` for other
    # reasons too — an unparseable model id among them — and reporting a model
    # typo as a storage problem sends the operator to the wrong knob.
    st.error(str(exc), icon=":material/error:")
    st.stop()
except Exception as exc:  # noqa: BLE001 - surface it rather than a blank page
    st.error(
        f"Could not open the planner — {type(exc).__name__}: {exc}\n\n"
        "If you just changed **Model**, check the id; the sidebar is still live, "
        "so correcting it reruns the page.",
        icon=":material/error:",
    )
    st.stop()

config: dict[str, Any] = {
    "configurable": {"thread_id": thread},
    # A ceiling, not a rescue: create_deep_agent binds 9_999 onto the compiled
    # graph, so LangGraph's default of 25 never applies and this lowers the
    # bound. Same budget the CLI uses, imported rather than restated.
    "recursion_limit": DEFAULT_MAX_STEPS,
}
context = PlannerContext(user_id=user_id)

# Claimed now, filled after the turn runs. Listing the store here directly would
# show the *pre-turn* contents, so the files a run just produced would not appear
# until some unrelated interaction triggered another rerun.
stored_slot = st.sidebar.container()


# --------------------------------------------------------------------------- #
# transcript — replayed from the checkpointer, not from session state
# --------------------------------------------------------------------------- #

# Peeked rather than popped — the pop stays below, after the transcript, so a
# rerun arriving mid-turn cannot resubmit. All this decides is whether the
# panels below are widgets: on a run that will also stream a turn they must not
# be, since toggling one aborts that turn. See `_render_tool`.
turn_pending = st.session_state.get("pending_input") is not None

snapshot = graph.get_state(config)
for message in (getattr(snapshot, "values", None) or {}).get("messages", []) or []:
    _render(message, gated=not turn_pending)


# --------------------------------------------------------------------------- #
# run whatever this rerun was triggered to run
# --------------------------------------------------------------------------- #

# Popped before the turn runs, not after: consuming it first means a rerun that
# arrives mid-turn cannot resubmit the same booking decision twice.
payload = st.session_state.pop("pending_input", None)

if payload is not None:
    if isinstance(payload, dict):
        # The operator's own message is not in the checkpoint until the graph
        # runs, so render it now rather than letting it vanish for a whole turn.
        for entry in payload.get("messages", []):
            with st.chat_message("user"):
                st.markdown(markdown_safe(entry["content"]))

    # A plain string sentinel rather than a module constant: the page is re-exec'd
    # on every rerun, so an `object()` marker would be a different identity by the
    # time it came back out of session state.
    graph_input = None if payload == RESUME_PENDING else payload

    # Counts turn *attempts*, not successes, and feeds approval widget identity.
    # The checkpoint id already distinguishes one interrupt from the next, but it
    # only advances when the graph does — so a resume that fails before any state
    # change (a locked database, a 429, the server reaped mid-turn) leaves the
    # identical token, and Streamlit restores the decision that was just
    # submitted: the panel comes back under an error message with the primary
    # button already live, one reflexive click from executing a booking nobody
    # re-confirmed. Bumping here means every attempt yields fresh widgets, so a
    # failed turn costs a deliberate re-decision rather than a single click.
    st.session_state.turn_attempt = st.session_state.get("turn_attempt", 0) + 1

    try:
        # `show_time` because a planning turn ran 672s in the recorded live run.
        # A spinner with no elapsed time is indistinguishable from a hung page at
        # that length, and the operator's only recourse is to reload — which
        # abandons a turn that was working.
        with st.spinner("Planning…", show_time=True):
            _stream_turn(graph, graph_input, config, context)
    except Exception as exc:  # noqa: BLE001 - keep the page alive, like the REPL
        st.error(f"{type(exc).__name__}: {exc}", icon=":material/error:")

    # The turn may have ended in an interrupt, so re-read the state.
    snapshot = graph.get_state(config)


# --------------------------------------------------------------------------- #
# stored artifacts — filled after the turn, so a run's own output is listed
# --------------------------------------------------------------------------- #

with stored_slot:
    st.subheader("Stored", divider="gray")

    if user_id is None:
        st.caption(
            "No user id, so storage is scoped to this thread. Set one for memory "
            "and event files that carry across threads."
        )
    else:
        for kind, icon in (
            ("memories", ":material/psychology:"),
            ("events", ":material/folder:"),
            # deepagents' offload spill rather than the planner's own files, but
            # it is the client's data and nothing evicts it. Hiding it would
            # make the store the only place it exists and the one place nobody
            # looks.
            ("artifacts", ":material/archive:"),
        ):
            items, namespace = stored_items(store, user_id, kind)
            if not items:
                st.caption(f"{icon} no {kind} yet")
                continue
            st.caption(f"{icon} {kind}")
            for item in items:
                # Downloads rather than a server-side write: `cli._export` has to
                # validate agent-chosen keys against traversal because it builds
                # a path from them, and a second copy of that check is a second
                # thing to get wrong. Nothing here touches the filesystem.
                #
                # `data` is a callable so the file's text is materialised only
                # when the button is clicked. Passing the string would load every
                # artifact on every rerun — and a venue comparison ran to 15.7 KB
                # in the recorded live run.
                st.download_button(
                    item.key,
                    data=partial(_content_of, item),
                    file_name=download_name(item.key),
                    mime="text/markdown",
                    key=f"dl-{kind}-{item.key}",
                    icon=":material/download:",
                )
            st.caption(f"`{'/'.join(namespace)}`")


# --------------------------------------------------------------------------- #
# approval gate, or the input box — never both
# --------------------------------------------------------------------------- #

raw_interrupts = getattr(snapshot, "interrupts", None)
reviews = pending_reviews(raw_interrupts)

# Set by the approval panel when the interrupt it was showing had already been
# answered elsewhere by the time Submit was clicked. Popped, so it is said once.
if st.session_state.pop("stale_approval", False):
    st.warning(
        "That approval had already been answered — by another tab, or by a CLI "
        "session on this thread. **Your decision was not submitted**, and nothing "
        "was executed on its behalf. The transcript above shows how it was "
        "resolved.",
        icon=":material/sync_problem:",
    )

if raw_interrupts and not reviews:
    # Fail closed. The graph is holding an unanswered tool call, so rendering the
    # normal chat input would let a follow-up run against a thread with a
    # `tool_use` and no `tool_result` — the model call fails, and in the meantime
    # a pending booking sits un-gated behind a UI that looks idle.
    # `cli._collect_decisions` raises on an unreadable payload rather than
    # continuing; the browser has to refuse just as loudly.
    st.error(
        "This thread is interrupted, but the pending action could not be read — "
        "the interrupt payload is not in a shape this page understands. **Nothing "
        "has been approved.** Resolve it with the CLI (`uv run event-planner`).",
        icon=":material/report:",
    )

elif reviews:
    # Rendered outside the fragment, so a fragment rerun leaves it in place
    # rather than redrawing it. It marks the boundary between a live transcript
    # and a decision the operator cannot take back, which is worth the weight.
    st.divider()
    _approval_panel(
        reviews,
        getattr(snapshot, "config", None),
        st.session_state.get("turn_attempt", 0),
        graph,
        config,
    )

else:
    # A turn can also end mid-flight — the process dies between super-steps, or a
    # step raises — leaving the graph with a pending node and *no* interrupt to
    # answer. LangGraph continues that by streaming `None`, but without an
    # affordance for it the thread sits on an unfinished tool call and every
    # later message queues behind it. Observed for real: the server was killed
    # mid-`task` and the thread came back with `next=('tools',)`.
    if getattr(snapshot, "next", None):
        st.warning(
            f"This thread has an unfinished turn (pending: `{'`, `'.join(snapshot.next)}`). "
            "It stopped without asking for anything, so it can be picked up where "
            "it left off.",
            icon=":material/pending:",
        )
        if st.button("Resume unfinished turn", type="primary", icon=":material/play_arrow:"):
            st.session_state.pending_input = RESUME_PENDING
            st.rerun()

    if prompt := st.chat_input(
        "Describe your event, or ask a follow-up",
        # A planning turn ran 672s in the recorded live session. Leaving the input
        # live during that invites a second message the graph has nowhere to put.
        submit_mode="disable",
    ):
        st.session_state.pending_input = {"messages": [{"role": "user", "content": prompt}]}
        st.rerun()

if not raw_interrupts and not (getattr(snapshot, "values", None) or {}).get("messages"):
    st.info(
        "Describe the event — headcount, city, date, budget ceiling, and format. "
        "The planner shortlists venues, prices catering and AV, and stops for your "
        "approval before it holds anything.",
        icon=":material/lightbulb:",
    )

st.sidebar.caption(f"`{PROJECT_ROOT.name}` · thread `{thread}` · {DEFAULT_MAX_STEPS}-step budget")
