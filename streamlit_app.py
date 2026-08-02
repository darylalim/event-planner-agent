"""Streamlit front end for the event planning agent.

Run with `uv run streamlit run streamlit_app.py`. The CLI (`uv run event-planner`)
remains the reference implementation; this is the same graph, the same
persistence, and the same approval gates behind a browser UI.

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

Business logic lives in `event_planner.webui` so it stays testable without a
Streamlit runtime; this file is the page.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import streamlit as st
from langgraph.types import Command

from event_planner.agent import DEFAULT_MODEL, PROJECT_ROOT, build_agent
from event_planner.cli import STATE_DIR, _load_env
from event_planner.context import PlannerContext
from event_planner.webui import (
    DEFAULT_MAX_STEPS,
    approve_decision,
    brief_args,
    download_name,
    edit_decision,
    message_text,
    open_persistence,
    parse_edited_args,
    pending_reviews,
    reject_decision,
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


# Bounded because `model` is a free-text field: every distinct value builds a
# graph and opens two SQLite connections, and an unbounded cache keyed on
# operator input accumulates both for the life of the process.
@st.cache_resource(show_spinner="Opening the planner…", max_entries=4)
def _resources(db: str, model: str) -> tuple[Any, Any]:
    """Build the graph once and share it across reruns and sessions.

    Cached on `(db, model)`: the checkpointer and store are per-database, and the
    graph binds the model at construction. Everything that varies per operator —
    thread id, user id — is passed per call as config and context instead, so it
    must not be part of this key.
    """
    checkpointer, store = open_persistence(Path(db))
    graph = build_agent(model=model, checkpointer=checkpointer, store=store)
    return graph, store


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _render_ai(message: Any) -> None:
    """Render one assistant turn: prose, then the tools it decided to call."""
    text = message_text(message)
    calls = tool_calls_of(message)
    if not text and not calls:
        return
    with st.chat_message("assistant"):
        if text:
            st.markdown(text)
        for call in calls:
            st.caption(f":material/build: **{call['name']}** — {brief_args(call.get('args', {}))}")


def _render_tool(message: Any) -> None:
    """Render a tool result, collapsed by default.

    Tool output is long — a venue comparison or a budget breakdown runs to
    hundreds of lines — and the assistant's summary of it is the part worth
    reading. `expanded=False` keeps the transcript legible with the detail one
    click away.
    """
    name = getattr(message, "name", None) or "tool"
    with st.expander(f"{name} result", icon=":material/output:"):
        st.code(str(message.content), language="text", wrap_lines=True)


def _render(message: Any) -> None:
    """Render any message by kind. Shared by the history replay and the live turn.

    One function for both on purpose: rendering the live stream differently from
    the checkpointed history makes the page visibly rearrange itself on the next
    rerun, which reads as a bug.
    """
    kind = getattr(message, "type", None)
    if kind == "human":
        with st.chat_message("user"):
            st.markdown(message_text(message))
    elif kind == "ai":
        _render_ai(message)
    elif kind == "tool":
        _render_tool(message)


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
                _render(message)


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
            "would merge every unidentified operator into one bucket."
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

if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
    st.error(
        "`ANTHROPIC_API_KEY` is not set. Copy `.env.example` to `.env` and fill it in, "
        "then restart the app.",
        icon=":material/key_off:",
    )
    st.stop()

if not os.environ.get("TAVILY_API_KEY", "").strip():
    st.caption(
        ":material/info: `TAVILY_API_KEY` is not set — `web_search` degrades to the "
        "structured directory only."
    )

# The CLI takes `--db`; a Streamlit script has no argv to read, so the same knob
# is an environment variable. Both default to the same file, so the two front
# ends share one set of threads and memories unless an operator separates them.
db_path = os.environ.get("EVENT_PLANNER_DB", "").strip() or str(STATE_DIR / "planner.sqlite")

try:
    graph, store = _resources(db_path, model)
except ValueError as exc:
    # `open_persistence` refuses a database inside the agent's filesystem root.
    st.error(str(exc), icon=":material/error:")
    st.stop()

config: dict[str, Any] = {
    "configurable": {"thread_id": thread},
    # LangGraph's default of 25 strands a session after ~5 tool calls with this
    # middleware stack. Same budget the CLI uses, imported rather than restated.
    "recursion_limit": DEFAULT_MAX_STEPS,
}
context = PlannerContext(user_id=user_id)


# --------------------------------------------------------------------------- #
# sidebar — stored artifacts (needs the store, so it comes after the build)
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.subheader("Stored", divider="gray")

    if user_id is None:
        st.caption(
            "No user id, so storage is scoped to this thread. Set one for memory "
            "and event files that carry across threads."
        )
    else:
        for kind, icon in (("memories", ":material/psychology:"), ("events", ":material/folder:")):
            items, namespace = stored_items(store, user_id, kind)
            if not items:
                st.caption(f"{icon} no {kind} yet")
                continue
            st.caption(f"{icon} {kind}")
            for item in items:
                content = (item.value or {}).get("content") or ""
                # Downloads rather than a server-side write: `cli._export` has to
                # validate agent-chosen keys against traversal because it builds
                # a path from them, and a second copy of that check is a second
                # thing to get wrong. Nothing here touches the filesystem.
                st.download_button(
                    item.key,
                    data=content,
                    file_name=download_name(item.key),
                    mime="text/markdown",
                    key=f"dl-{kind}-{item.key}",
                    icon=":material/download:",
                )
            st.caption(f"`{'/'.join(namespace)}`")


# --------------------------------------------------------------------------- #
# transcript — replayed from the checkpointer, not from session state
# --------------------------------------------------------------------------- #

snapshot = graph.get_state(config)
for message in (getattr(snapshot, "values", None) or {}).get("messages", []) or []:
    _render(message)


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
                st.markdown(entry["content"])

    # A plain string sentinel rather than a module constant: the page is re-exec'd
    # on every rerun, so an `object()` marker would be a different identity by the
    # time it came back out of session state.
    graph_input = None if payload == RESUME_PENDING else payload

    try:
        with st.spinner("Planning…"):
            _stream_turn(graph, graph_input, config, context)
    except Exception as exc:  # noqa: BLE001 - keep the page alive, like the REPL
        st.error(f"{type(exc).__name__}: {exc}", icon=":material/error:")

    # The turn may have ended in an interrupt, so re-read the state.
    snapshot = graph.get_state(config)


# --------------------------------------------------------------------------- #
# approval gate, or the input box — never both
# --------------------------------------------------------------------------- #

reviews = pending_reviews(getattr(snapshot, "interrupts", None))

if reviews:
    st.divider()
    decisions: list[dict[str, Any]] = []
    blocked = False

    for index, (action, allowed) in enumerate(reviews):
        with st.container(border=True):
            st.subheader(f":material/gavel: Approval required — `{action['name']}`")
            st.caption("This action is irreversible from the client's point of view.")

            st.json(action.get("args", {}))
            if action.get("description"):
                st.markdown(action["description"])

            if unsupported := unsupported_decisions(allowed):
                # Say so rather than silently narrowing the operator's options.
                st.warning(
                    f"The agent also allows {', '.join(f'`{d}`' for d in unsupported)} here, "
                    "which this UI does not offer. `respond` in particular is excluded by "
                    "design: a free-text reply to a booking request invites the model to "
                    "read commentary as confirmation. Use the CLI if you need it.",
                    icon=":material/info:",
                )

            offered = [d for d in ("approve", "edit", "reject") if d in allowed]
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
                key=f"choice-{index}",
                format_func=str.capitalize,
            )

            if choice == "approve":
                decisions.append(approve_decision())
            elif choice == "reject":
                reason = st.text_area(
                    "Reason (fed back to the agent)",
                    key=f"reason-{index}",
                    placeholder="Why this is not going ahead.",
                )
                decisions.append(reject_decision(reason))
            elif choice == "edit":
                edited = st.text_area(
                    "Arguments to execute instead",
                    value=json.dumps(action.get("args", {}), indent=2),
                    height=200,
                    key=f"args-{index}",
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

    # Guarded twice, deliberately. `disabled` is presentation — it stops a click
    # in the browser but is not a promise about what reaches this branch, and a
    # click that slipped through with nothing chosen would resume the graph with
    # an empty decision list against middleware that wants exactly one decision
    # per pending action.
    if submitted and ready:
        st.session_state.pending_input = Command(resume={"decisions": decisions})
        st.rerun()

    if not ready and not blocked:
        st.caption("Choose a decision for each pending action.")

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

if not reviews and not (getattr(snapshot, "values", None) or {}).get("messages"):
    st.info(
        "Describe the event — headcount, city, date, budget ceiling, and format. "
        "The planner shortlists venues, prices catering and AV, and stops for your "
        "approval before it holds anything.",
        icon=":material/lightbulb:",
    )

st.sidebar.caption(f"`{PROJECT_ROOT.name}` · thread `{thread}` · {DEFAULT_MAX_STEPS}-step budget")
