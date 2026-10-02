"""Tests for the Streamlit page itself, driven through Streamlit's AppTest.

`test_webui.py` covers the logic the page delegates to. This file runs the real
`streamlit_app.py` top to bottom and drives its widgets, because the interesting
failures live in the wiring rather than the helpers: a button that submits while
disabled, an approval that renders but never reaches the graph, a decision built
from the wrong widget.

Offline like the rest of the suite. The graph is replaced with a fake, so no
model is constructed and nothing leaves the process; `ANTHROPIC_API_KEY` is set
to a dummy value only to get past the page's credential gate.

The first version of the page failed `test_submitting_with_no_decision_sends_nothing`
— `disabled=` stopped the click in a browser but was not a guard in the branch
behind it, so a click that reached it resumed the graph with an empty decision
list.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"

HOLD = {"name": "hold_venue", "args": {"venue_id": "v-loft-mission", "headcount": 60}}


def _boilerplate(action):
    """What `HumanInTheLoopMiddleware` actually writes into `description`.

    Not prose for a human — it repeats the tool name and a Python dict repr of
    the arguments the panel already shows.
    """
    return f"Tool execution requires approval\n\nTool: {action['name']}\nArgs: {action['args']}"


def _interrupt(allowed=("approve", "edit", "reject"), actions=(HOLD,)):
    """Shaped like the payload the installed middleware really emits.

    `test_the_real_middleware_payload_parses` in `test_webui.py` pins that shape
    against the package by driving the real graph, so this fixture is checked
    rather than merely self-consistent. Two details came from there: the
    `review_configs` entries carry `action_name`, and `description` is generated.
    """
    return {
        "action_requests": [{**action, "description": _boilerplate(action)} for action in actions],
        "review_configs": [
            {"action_name": action["name"], "allowed_decisions": list(allowed)}
            for action in actions
        ],
    }


class FakeGraph:
    """Stands in for the compiled graph: records what a decision resumes with."""

    def __init__(
        self,
        interrupt=None,
        messages=(),
        next_nodes=(),
        then=None,
        stream_error=None,
        stream_updates=(),
    ):
        self.sent = []
        # Update chunks the turn yields, shaped like real `stream_mode="updates"`
        # output. Empty by default, which is what every case that only cares
        # about *what was sent* wants.
        self.stream_updates = list(stream_updates)
        # Raised instead of advancing, modelling the dangerous shape of failure:
        # the turn dies before any state change, so the checkpoint id — and hence
        # `review_token` — is unchanged when the panel renders again.
        self.stream_error = stream_error
        self.interrupt = interrupt
        self.messages = list(messages)
        self.next_nodes = tuple(next_nodes)
        # Interrupt raised by the *next* run, so a test can model the real
        # sequence: approve `hold_venue`, and the agent immediately asks about
        # `send_invitations` in the same script run.
        self.then = then
        self.checkpoint = 0
        # Counts reads so a test can resolve the interrupt *between* two of them,
        # which is where the concurrent-session race actually lives.
        self.reads = 0
        self.resolve_after_reads = None

    def get_state(self, config):
        self.reads += 1
        interrupt = self.interrupt
        if self.resolve_after_reads is not None and self.reads > self.resolve_after_reads:
            interrupt = None
        interrupts = (SimpleNamespace(value=interrupt),) if interrupt else ()
        return SimpleNamespace(
            values={"messages": self.messages},
            interrupts=interrupts,
            next=self.next_nodes,
            # A real snapshot carries the checkpoint id, which advances every
            # super-step. Widget identity depends on it.
            config={"configurable": {"thread_id": "t", "checkpoint_id": f"ck-{self.checkpoint}"}},
        )

    def stream(self, payload, config=None, context=None, stream_mode=None):
        self.sent.append(payload)
        if self.stream_error is not None:
            raise self.stream_error
        self.checkpoint += 1
        # Running clears the pending approval and the pending node, like the real
        # graph — and may immediately raise the next interrupt.
        self.interrupt = self.then
        self.then = None
        self.next_nodes = ("tools",) if self.interrupt else ()
        return iter(self.stream_updates)

    @property
    def decisions(self):
        return [p.resume["decisions"] for p in self.sent if hasattr(p, "resume")]


@pytest.fixture
def page(tmp_path, monkeypatch):
    """Run the real page against a fake graph and return `(AppTest, FakeGraph)`."""

    def _run(
        interrupt=None,
        messages=(),
        api_key=True,
        next_nodes=(),
        then=None,
        stream_error=None,
        stream_updates=(),
    ):
        fake = FakeGraph(interrupt, messages, next_nodes, then, stream_error, stream_updates)
        monkeypatch.setattr("event_planner.agent.build_agent", lambda **_kwargs: fake)
        monkeypatch.setenv("EVENT_PLANNER_DB", str(tmp_path / "planner.sqlite"))

        if api_key:
            monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
        else:
            # Stub `_load_env` too, or the project's own `.env` puts the real key
            # straight back and the credential gate never fires.
            monkeypatch.setattr("event_planner.cli._load_env", lambda: None)
            monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # The page caches the graph across reruns, which is the behaviour under
        # test everywhere except here, where each case needs its own fake.
        st.cache_resource.clear()
        at = AppTest.from_file(str(APP), default_timeout=60)
        at.run()
        return at, fake

    yield _run
    st.cache_resource.clear()


# --------------------------------------------------------------------------- #
# the page renders
# --------------------------------------------------------------------------- #


def test_the_page_runs_without_error(page):
    at, _ = page()
    assert not at.exception
    assert [t.value for t in at.title] == ["Event planner"]


def test_an_empty_thread_explains_what_to_type(page):
    at, _ = page()
    assert any("headcount" in info.value for info in at.info)
    assert len(at.chat_input) == 1


def test_a_missing_api_key_stops_the_page(page):
    """An actionable message beats an opaque auth error from inside the SDK."""
    at, _ = page(api_key=False)
    assert any("ANTHROPIC_API_KEY" in err.value for err in at.error)
    assert len(at.chat_input) == 0


def test_history_is_replayed_from_the_checkpointer(page):
    """The transcript has no second copy in session state to drift from."""
    at, _ = page(messages=[SimpleNamespace(type="human", content="85 guests in SF", tool_calls=[])])
    assert not at.exception
    assert any("85 guests in SF" in md.value for md in at.markdown)


def test_a_typed_brief_reaches_the_graph(page):
    at, fake = page()
    at.chat_input[0].set_value("85 guests in SF, $45k ceiling").run()
    assert fake.sent == [
        {"messages": [{"role": "user", "content": "85 guests in SF, $45k ceiling"}]}
    ]


def test_the_effort_picker_reaches_the_build(page, monkeypatch):
    """The picker defaults to the orchestrator's effort and a change rebuilds.

    Effort is bound at construction and the graph is cached, so the risk is a
    cache key that omits it: the page would keep serving the first level picked
    while the sidebar showed another. And "none" must arrive as `None`, exactly
    as the CLI's `--effort none` does — as a string, the API would receive it as
    a level and reject the request.
    """
    from event_planner.agent import DEFAULT_EFFORT, DEFAULT_MODEL
    from event_planner.cli import NO_EFFORT

    at, fake = page()
    assert at.selectbox(key="effort").value == DEFAULT_EFFORT

    builds: list[dict[str, object]] = []

    def _record(**kwargs):
        builds.append(kwargs)
        return fake

    monkeypatch.setattr("event_planner.agent.build_agent", _record)
    at.selectbox(key="effort").set_value("medium").run()
    at.selectbox(key="effort").set_value(NO_EFFORT).run()

    assert not at.exception
    assert [(b["model"], b["effort"]) for b in builds] == [
        (DEFAULT_MODEL, "medium"),
        (DEFAULT_MODEL, None),
    ]


def test_a_submitted_brief_streams_its_turn_in_the_same_run(page):
    """The submit and the turn are one run, not two.

    Read at the bottom of the script, a chat submission could only stash the
    message and rerun, so the operator paid a whole discarded render — a full
    `get_state` deserialisation, a walk of every message, three store searches —
    between pressing Enter and the turn starting. `on_submit` fires before the
    script body of the rerun the submission already causes, so `turn_pending` is
    true by the time the transcript renders.

    Counted through `get_state`, because that is the cost being removed: one run
    reads once before the turn and once after it.
    """
    at, fake = page()
    before = fake.reads
    at.chat_input[0].set_value("85 guests in SF").run()
    assert fake.sent == [{"messages": [{"role": "user", "content": "85 guests in SF"}]}]
    assert fake.reads - before == 2, "a discarded pre-turn run would add two more"


def _ai(text, *, identifier, calls=()):
    """An assistant message shaped like a real `AIMessage`, id included.

    The id is the whole point here: `add_messages` dedupes on it, and so does
    the page's replay-versus-stream reconciliation.
    """
    return SimpleNamespace(type="ai", content=text, tool_calls=list(calls), id=identifier)


def test_a_resumed_proposal_is_replaced_rather_than_drawn_twice(page):
    """One message, two sources, and for `edit` they disagree.

    `HumanInTheLoopMiddleware.after_model` re-emits the proposing message as its
    node update, and the model node that produced it was checkpointed a
    super-step earlier — so on the run that resumes a decision the replay and the
    stream both carry it. Appending both drew two assistant bubbles: identical
    for approve and reject, and for `edit` *different*, the replayed copy showing
    the model's arguments and the streamed copy the operator's, with nothing
    saying which one executed.

    It corrects itself on the next rerun. The run it is wrong on is the run that
    commits a booking, which is the one an operator reads.
    """
    at, _ = page(
        messages=[
            _ai(
                "Holding the loft.",
                identifier="m-1",
                calls=[
                    {"name": "hold_venue", "args": {"headcount": 60}},
                ],
            )
        ],
        stream_updates=[
            {
                "model": {
                    "messages": [
                        _ai(
                            "Holding the loft.",
                            identifier="m-1",
                            calls=[
                                {"name": "hold_venue", "args": {"headcount": 45}},
                            ],
                        ),
                    ]
                }
            }
        ],
    )
    at.chat_input[0].set_value("go ahead").run()

    assert not at.exception
    holds = [c.value for c in at.caption if "hold_venue" in c.value]
    assert len(holds) == 1, f"drawn {len(holds)} times: {holds}"
    # The re-emitted copy wins, so the arguments left on screen are the ones
    # that ran — not the ones the model first proposed.
    assert "headcount=45" in holds[0]


def test_a_stored_files_download_carries_its_real_content(page, tmp_path, monkeypatch):
    """Drive the deferred `data=` callable, which nothing offline had ever called.

    `DownloadButton.click()` in AppTest only sets the widget value; nothing in
    that stack calls `execute_deferred`. So a `_content_of` that raised, or that
    returned a shape the media endpoint rejects, passed the whole suite — and a
    legacy list-shaped store value does exactly that, with
    `MediaFileStorageError: Callable returned unsupported type`.

    Two internals are load-bearing and worth naming rather than rediscovering.
    `Runtime._instance` is set to `None` at the end of every `at.run()`, so the
    manager has to be captured *during* the run. And the file id the proto
    carries is the deferred placeholder, not the stored file — `execute_deferred`
    is what turns one into the other.
    """
    from streamlit.runtime.media_file_manager import MediaFileManager

    from event_planner.context import namespace_for_user
    from event_planner.webui import download_name, open_persistence

    _, store = open_persistence(tmp_path / "planner.sqlite")
    store.put(
        namespace_for_user("alice@example.com", "events"),
        "/events/offsite/brief.md",
        {"content": "# Brief\n\n85 guests"},
    )

    managers = []
    original_init = MediaFileManager.__init__

    def _capture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        managers.append(self)

    monkeypatch.setattr(MediaFileManager, "__init__", _capture)

    at, _ = page()
    at.text_input(key="user").set_value("alice@example.com").run()
    assert not at.exception

    buttons = at.download_button
    assert len(buttons) == 1, "the seeded file should be listed once"
    assert managers, "no MediaFileManager was constructed during the run"

    manager = managers[-1]
    url = manager.execute_deferred(buttons[0].proto.deferred_file_id)
    stored = manager._storage.get_file(url.rsplit("/", 1)[-1])

    assert stored.content == b"# Brief\n\n85 guests"
    assert stored.filename == download_name("/events/offsite/brief.md")


def test_the_committed_figures_follow_the_edit_not_the_proposal(page):
    """The headline number must be the one that executes.

    An earlier version of this row rendered `action["args"]` above the decision
    widgets. That put the model's figure in the largest, boldest element on a
    panel the operator had already overridden — the same "shows one thing,
    executes another" hazard this page escapes tool arguments to avoid, at
    greater visual weight, on the one panel that spends money.

    Nothing renders before a decision is picked: with no choice there is nothing
    committed to show, and the two-step gate is what that emptiness expresses.
    """
    action = {
        "name": "hold_venue",
        "args": {"venue_id": "v-loft", "headcount": 60, "total_cost_usd": 10281},
    }
    at, _ = page(
        interrupt={
            "action_requests": [action],
            "review_configs": [{"allowed_decisions": ["approve", "edit", "reject"]}],
        }
    )
    assert at.metric == [], "nothing is committed until a decision is picked"

    at.segmented_control[0].set_value("approve").run()
    assert [(m.label, m.value) for m in at.metric] == [
        ("headcount", "60"),
        ("total_cost_usd", "10281"),
    ]

    at.segmented_control[0].set_value("edit").run()
    box = next(t for t in at.text_area if "Arguments" in t.label)
    box.set_value('{"venue_id": "v-loft", "headcount": 45, "total_cost_usd": 8100}').run()

    assert not at.exception
    assert [(m.label, m.value) for m in at.metric] == [
        ("headcount", "45"),
        ("total_cost_usd", "8100"),
    ], "the panel is still showing the model's proposal, not what would execute"


def test_an_unusable_args_shape_does_not_take_the_panel_down(page):
    """The interrupt payload is deepagents' to shape, not this page's to trust.

    An exception inside the approval panel takes the whole page down with a
    booking still parked — the same outcome `_panel_key` exists to prevent — so
    an args value that is not a dict yields no figures rather than raising.
    """
    at, _ = page(
        interrupt={
            "action_requests": [{"name": "hold_venue", "args": ["not", "a", "dict"]}],
            "review_configs": [{"allowed_decisions": ["approve", "reject"]}],
        }
    )
    at.segmented_control[0].set_value("approve").run()
    assert not at.exception
    assert at.metric == []
    assert at.button, "the panel still renders its submit button"


# --------------------------------------------------------------------------- #
# tool results are not shipped while collapsed
# --------------------------------------------------------------------------- #


def _tool(name, content, tool_call_id="call-1"):
    """A tool result shaped like a real `ToolMessage`.

    `tool_call_id` is a required field on the real class, which is why the page
    keys its panel on it. Set here rather than left off so the fixture cannot
    quietly exercise the page's no-identity fallback instead of its main path.
    """
    return SimpleNamespace(type="tool", name=name, content=content, tool_call_id=tool_call_id)


def _panels(at):
    """The tool-result expanders.

    Read through `at.status`, not `at.expander`: `element_tree` sorts an
    `expandable` block by whether it carries an icon, and routes the ones that
    do to `Status`. These pass `icon=":material/output:"`, so `at.expander` is
    empty and an assertion written against it fails for a reason that has
    nothing to do with the page.
    """
    return [s for s in at.status if s.label.endswith(" result")]


def test_a_collapsed_tool_result_is_not_sent_to_the_browser(page):
    """Streamlit computes a closed expander's body unless `on_change` gates it.

    This page replays the whole checkpointed transcript on every rerun, so an
    ungated body is re-serialised on every keystroke in the sidebar. Measured on
    the repo's recorded threads, tool output is 37-61% of transcript text — 70.8
    KB on `full-brief-3`. The panel still renders; only its contents wait.
    """
    at, _ = page(messages=[_tool("search_venues", "MISSION LOFT " * 500)])

    assert not at.exception
    assert [s.label for s in _panels(at)] == ["search_venues result"]
    # Not merely hidden — absent from the rendered tree, so it never crosses
    # the wire. This is the whole point of the gate.
    assert not any("MISSION LOFT" in c.value for c in at.code)


def test_repeated_tool_names_get_distinct_panels(page):
    """Gating promotes the expander to a widget, and widget keys must be unique.

    An auto-generated key derives from the label, and labels repeat: the recorded
    `full-brief-3` thread holds seven `estimate_budget result` panels and
    `full-brief-2` holds six `check_availability result`. Keying on the label
    alone raises a duplicate-key error, which takes the entire page down rather
    than one panel — so this asserts on a thread shaped like the real one.
    """
    at, _ = page(messages=[_tool("estimate_budget", f"total {i}", f"call-{i}") for i in range(7)])

    assert not at.exception
    assert [s.label for s in _panels(at)] == ["estimate_budget result"] * 7


def _panel_keys(at):
    """The panel widget keys registered in session state.

    A gated expander is a stateful widget, so it registers its key — which is
    also how a test opens one, since there is no `.open` setter on the block.
    """
    return sorted(k for k in at.session_state.filtered_state if k.startswith("tool-"))


def test_an_opened_panel_renders_its_body(page):
    """The other half of the gate: closed hides, open must still show.

    Without this, a regression that left `panel.open` permanently false would
    hide every tool result in the app forever and the rest of the suite would
    stay green — every other assertion here is about content being *absent*.
    """
    at, _ = page(messages=[_tool("search_venues", "MISSION LOFT")])
    assert not any("MISSION LOFT" in c.value for c in at.code)

    at.session_state[_panel_keys(at)[0]] = True
    at.run()

    assert any("MISSION LOFT" in c.value for c in at.code)


def test_opening_one_panel_leaves_the_others_closed(page):
    """Per-panel identity, asserted directly rather than inferred from silence.

    `test_repeated_tool_names_get_distinct_panels` only shows that seven panels
    coexist. This shows they are seven *separate* widgets: with one shared key
    they would open together. Confirmed in a browser on the recorded
    `full-brief-3` thread, where opening one budget breakdown left the other six
    shut.
    """
    at, _ = page(messages=[_tool("estimate_budget", f"total {i}", f"call-{i}") for i in range(7)])
    keys = _panel_keys(at)
    assert len(keys) == 7

    at.session_state[keys[3]] = True
    at.run()

    assert [c.value for c in at.code] == ["total 3"]


def test_two_results_sharing_one_call_id_do_not_kill_the_page(page):
    """A duplicate key raises, and that is not a rendering glitch — it is fatal.

    An exception at this point takes the whole page: no transcript, no chat
    input, and no approval panel for a booking that may still be parked behind
    one. Two `ToolMessage`s can share a `tool_call_id` — a resumed node
    re-emitting, a replay overlapping a mid-stream render — so the second falls
    back to an unkeyed panel. Unkeyed means ungated, which is why its body is
    the one that renders here.
    """
    at, _ = page(
        messages=[
            _tool("estimate_budget", "first", "call-dup"),
            _tool("estimate_budget", "second", "call-dup"),
        ]
    )

    assert not at.exception
    assert len(_panels(at)) == 2
    assert len(at.chat_input) == 1
    assert [c.value for c in at.code] == ["second"]


def test_panels_are_inert_on_a_run_that_streams_a_turn(page):
    """A gated panel is a widget, and toggling one mid-turn would abort the turn.

    `on_change="rerun"` posts a rerun request; Streamlit services it at the next
    `st.*` call — most of them are implicit yield points — by raising
    `RerunException`, which subclasses `BaseException`, so the turn's `except
    Exception` does not catch it and `graph.stream` is abandoned. The transcript
    replays *before* the turn streams, so those panels would be live widgets for
    the whole turn. A planning turn ran 672s in the recorded live session; a
    stray click on a venue comparison must not cost it.

    Mirrors `submit_mode="disable"` on the chat box: no turn-interrupting
    control stays live while a turn is in flight.
    """
    at, _ = page(messages=[_tool("search_venues", "MISSION LOFT")])
    assert _panel_keys(at)  # idle: a widget, and openable

    at.chat_input[0].set_value("85 guests in SF").run()

    assert not at.exception
    assert _panel_keys(at) == []  # mid-turn: nothing to click


def test_a_tool_result_with_no_identity_still_renders(page):
    """Without a stable key the page must degrade, not raise.

    A real `ToolMessage` always carries `tool_call_id`, but `_render` takes
    `Any`. Two unkeyed panels are harmless; two panels sharing one key are not,
    so the fallback drops the gate rather than inventing an identity.
    """
    at, _ = page(
        messages=[
            SimpleNamespace(type="tool", name="ls", content="brief.md"),
            SimpleNamespace(type="tool", name="ls", content="venues.md"),
        ]
    )

    assert not at.exception
    assert [c.value for c in at.code] == ["brief.md", "venues.md"]


# --------------------------------------------------------------------------- #
# the approval gate
# --------------------------------------------------------------------------- #


def test_a_pending_approval_replaces_the_input_box(page):
    """The graph is interrupted; a new user message has nowhere to go."""
    at, _ = page(_interrupt())
    assert len(at.chat_input) == 0
    assert any("Approval required" in s.value for s in at.subheader)


def test_only_supported_decisions_are_offered(page):
    at, _ = page(_interrupt())
    assert [c.options for c in at.segmented_control] == [["Approve", "Edit", "Reject"]]


def test_respond_is_never_offered_even_when_allowed(page):
    """Config could widen the set; this UI must not follow it there."""
    at, _ = page(_interrupt(allowed=("approve", "edit", "reject", "respond")))
    assert [c.options for c in at.segmented_control] == [["Approve", "Edit", "Reject"]]
    assert any("respond" in w.value for w in at.warning)


def test_submitting_with_no_decision_sends_nothing(page):
    """From Streamlit 1.62 `disabled=` is enforced server side, not just in the browser.

    So this pins the framework's enforcement rather than `_approval_panel`'s own
    `ready` re-check: the click is dropped before the handler either way. The
    guard itself is covered by the next test, which does not rely on Streamlit
    to stop the click.
    """
    at, fake = page(_interrupt())
    assert at.button[0].disabled
    at.button[0].click().run()
    assert fake.sent == []
    assert any("Approval required" in s.value for s in at.subheader)


def test_a_submit_that_slips_past_the_disabled_button_sends_nothing(page, monkeypatch):
    """The handler's `ready` re-check, exercised without Streamlit's help.

    `ready` and `disabled=` derive from the same value, so once 1.62 began
    enforcing `disabled=` server side no sequence of real clicks could reach the
    branch with `ready` false — which means the test above passes whether or not
    the page keeps its own guard, and deleting `and ready` goes unnoticed.

    Forcing the button to report a click is therefore the only way to exercise
    the case the guard exists for: a submit arriving with nothing chosen, as it
    would on an older Streamlit, or if `disabled=` regressed the way it already
    behaved once. Resuming here would push an empty decision list at middleware
    that requires exactly one decision per pending action.
    """
    real_button = st.button

    def clicked_anyway(label, *args, **kwargs):
        real_button(label, *args, **kwargs)
        return str(label).startswith("Submit")

    monkeypatch.setattr(st, "button", clicked_anyway)
    _, fake = page(_interrupt())
    assert fake.sent == [], "a submit with no decision chosen reached the graph"


def test_approve_resumes_with_an_approve_decision(page):
    at, fake = page(_interrupt())
    at.segmented_control[0].set_value("approve").run()
    assert not at.button[0].disabled

    at.button[0].click().run()
    assert fake.decisions == [[{"type": "approve"}]]
    assert len(at.chat_input) == 1


def test_reject_carries_the_operator_reason_in_the_cli_wording(page):
    at, fake = page(_interrupt())
    at.segmented_control[0].set_value("reject").run()
    at.text_area[0].set_value("Client has not signed off on the deposit.").run()
    at.button[0].click().run()

    decision = fake.decisions[0][0]
    assert decision["type"] == "reject"
    assert "human operator" in decision["message"].lower()
    assert "must not be retried" in decision["message"].lower()
    assert "Client has not signed off on the deposit." in decision["message"]


def test_edit_prefills_the_models_arguments(page):
    at, _ = page(_interrupt())
    at.segmented_control[0].set_value("edit").run()
    assert '"headcount": 60' in at.text_area[0].value


def test_edit_executes_the_operators_arguments_not_the_models(page):
    """The recorded live run changed 60 guests to 45 through this path."""
    at, fake = page(_interrupt())
    at.segmented_control[0].set_value("edit").run()
    at.text_area[0].set_value('{"venue_id": "v-loft-mission", "headcount": 45}').run()
    at.button[0].click().run()

    assert fake.decisions == [
        [
            {
                "type": "edit",
                "edited_action": {
                    "name": "hold_venue",
                    "args": {"venue_id": "v-loft-mission", "headcount": 45},
                },
            }
        ]
    ]


def test_malformed_edited_arguments_block_the_submission(page):
    at, fake = page(_interrupt())
    at.segmented_control[0].set_value("edit").run()
    at.text_area[0].set_value("{headcount: 45}").run()

    assert any("Not valid JSON" in err.value for err in at.error)
    assert at.button[0].disabled

    at.button[0].click().run()
    assert fake.sent == []


# --------------------------------------------------------------------------- #
# one approval must not contaminate the next
# --------------------------------------------------------------------------- #

SEND = {"name": "send_invitations", "args": {"recipient_count": 250, "event_name": "Offsite"}}


def test_a_second_interrupt_is_not_pre_approved(page):
    """Positional widget keys made the next action inherit the last decision.

    Approving `hold_venue` resumes the graph, which immediately interrupts for
    `send_invitations` in the same run. With `key=f"choice-{index}"` Streamlit
    restored the stored value, so that panel came back with `approve` selected
    and Submit enabled — one reflexive click emailing 250 guests with no decision
    ever made for that action.
    """
    at, fake = page(_interrupt(), then=_interrupt(actions=(SEND,)))
    at.segmented_control[0].set_value("approve").run()
    at.button[0].click().run()

    assert fake.decisions == [[{"type": "approve"}]]
    assert any("send_invitations" in s.value for s in at.subheader)

    assert at.segmented_control[0].value is None
    assert at.button[0].disabled


def test_a_second_interrupt_gets_its_own_widget_identity(page):
    """Worse than the above: a stored widget value beats the `value=` argument.

    With positional keys the new action inherited `edit` *and* the previous
    action's arguments — the box parsed cleanly, so Submit was enabled and would
    have executed `send_invitations` with `hold_venue`'s arguments. Distinct
    widget identity is what makes that impossible, so that is what is asserted.

    Not asserted via the text area: after the page's internal `st.rerun()`,
    AppTest's element tree still lists the *previous* panel's text area even
    though Streamlit has dropped it from session state (reading its `.value`
    raises `KeyError`). It is a ghost in the harness, not a live widget.
    """
    at, _ = page(_interrupt(), then=_interrupt(actions=(SEND,)))
    first_key = at.segmented_control[0].key

    at.segmented_control[0].set_value("edit").run()
    at.text_area[0].set_value('{"venue_id": "v-loft-mission", "headcount": 45}').run()
    at.button[0].click().run()

    assert any("send_invitations" in s.value for s in at.subheader)
    assert at.segmented_control[0].key != first_key
    assert at.button[0].disabled


def test_an_approval_answered_elsewhere_is_not_resubmitted(page):
    """Another session can answer the interrupt between the render and the click.

    The panel is an `st.fragment`, so its `reviews` are as old as the last full
    run and a fragment rerun does not refresh them. Both front ends share one
    database by default and `_resources` caches one graph across browser
    sessions, so a CLI turn or a second tab on this thread can resolve the
    interrupt while the panel still shows it. Before the panel was a fragment the
    Submit click was itself a full rerun — it re-read the snapshot, found nothing
    pending, and never rendered the button; the compare-and-swap in
    `_approval_panel` is what replaces that accidental fail-closed with a
    deliberate one.

    The race is simulated where it really happens: the interrupt survives the
    page's read of the snapshot and is gone by the panel's read, one call later.
    Without the check, this submits `Command(resume=...)` into a graph that is no
    longer asking for anything.
    """
    at, fake = page(_interrupt())
    at.segmented_control[0].set_value("approve").run()

    # Reads so far: initial run, then the `set_value` rerun. The click's run
    # reads once from the main script (third) and once from the panel (fourth).
    fake.resolve_after_reads = fake.reads + 1
    at.button[0].click().run()

    assert fake.sent == []
    assert any("already been answered" in w.value for w in at.warning)
    # And the page has moved on rather than re-offering a decision on it.
    assert len(at.chat_input) == 1


def test_a_full_rerun_while_parked_keeps_an_in_progress_decision(page):
    """Opening a tool-result panel is a full app rerun; it must not cost a decision.

    `_render_tool` gates its body on `on_change="rerun"`, which escapes to
    `scope="app"`. So an operator who opens a budget breakdown to check it
    against a pending `hold_venue` — exactly what the panel's own caption tells
    them to do — reruns the whole page underneath the approval panel. That is
    only safe because `review_token` does not move while the graph is parked:
    `turn_attempt` advances on a turn and the checkpoint id advances when the
    graph does, and a rerun is neither.

    Driven with a bare rerun rather than by toggling a panel: the rerun is the
    mechanism under test, and that an expander triggers one is Streamlit's own
    documented behaviour, confirmed in a browser against a 22-panel thread.
    """
    at, fake = page(_interrupt())
    at.segmented_control[0].set_value("approve").run()
    assert not at.button[0].disabled

    at.run()  # what an expander toggle does to the rest of the page

    assert at.segmented_control[0].value == "approve"
    assert not at.button[0].disabled
    assert fake.sent == []


def test_the_panel_reads_no_graph_state_while_rendering(page):
    """The fragment's isolation is only sound while the panel renders from its args.

    A fragment rerun does not re-execute the main script, so any graph read the
    panel performed at *render* time would be served from a snapshot the page
    never refreshed — the panel would show one thing and act on another. The
    submit path deliberately does read the graph, but only after the operator has
    committed, which is why the count below is taken across a plain widget
    interaction rather than a submission.

    Asserted by counting reads rather than by inspecting the source, because the
    hazard is a call appearing anywhere in the panel, including inside a helper.
    `AppTest` cannot drive a real fragment-scoped rerun (it builds a fresh
    `LocalScriptRunner` per call and never sets `fragment_id_queue`), so this
    invariant is what stands in for that coverage.
    """
    at, fake = page(_interrupt())
    assert fake.reads == 1  # the main script's single read

    at.segmented_control[0].set_value("edit").run()
    assert fake.reads == 2  # one more from the main script, none from the panel


def test_a_failed_resume_does_not_leave_the_panel_pre_armed(page):
    """A turn that dies before advancing must not rebuild the panel pre-approved.

    `review_token` mixes in the checkpoint id, which only moves when the graph
    does. So a resume that raises first — a locked database while the CLI holds
    the write lock, a 429, the server reaped mid-turn — leaves the identical
    token, and Streamlit restores a keyed widget's value whenever that key
    renders again. The panel then came back under an error message with `approve`
    still selected and the primary button live: one reflexive click on a page
    that had just failed, executing a booking nobody re-confirmed. That is
    exactly what the two-step pick-then-confirm gate exists to prevent, so the
    widget identity carries the attempt count as well as the checkpoint.
    """
    at, fake = page(_interrupt(), stream_error=RuntimeError("database is locked"))
    at.segmented_control[0].set_value("approve").run()
    at.button[0].click().run()

    assert fake.decisions == [[{"type": "approve"}]]  # it was genuinely attempted
    assert any("database is locked" in err.value for err in at.error)

    # Still pending, and it has to be decided again rather than re-clicked.
    assert any("Approval required" in s.value for s in at.subheader)
    assert at.segmented_control[0].value is None
    assert at.button[0].disabled


def test_an_unreadable_interrupt_fails_closed(page):
    """A payload shape this page cannot parse must not look like "nothing pending".

    Showing the chat input here would run a follow-up against a thread holding a
    `tool_use` with no `tool_result`, with a booking un-gated in the meantime.
    """
    at, fake = page(interrupt="a shape this page does not understand")

    assert any("could not be read" in err.value for err in at.error)
    assert len(at.chat_input) == 0
    assert _resume_buttons(at) == []
    assert fake.sent == []


def test_the_disabled_submit_button_explains_itself(page):
    """The old `not ready and not blocked` spelling was unsatisfiable."""
    at, _ = page(_interrupt())
    assert at.button[0].disabled
    assert any("Choose a decision" in c.value for c in at.caption)


# --------------------------------------------------------------------------- #
# an unfinished turn
# --------------------------------------------------------------------------- #


def _resume_buttons(at):
    return [b for b in at.button if "Resume" in b.label]


def test_an_unfinished_turn_offers_to_resume(page):
    """Found live: the server was killed mid-`task`, leaving `next=('tools',)`.

    Nothing is being asked of the operator, so no approval renders — without this
    affordance the thread sits on an unfinished tool call for good.
    """
    at, _ = page(next_nodes=("tools",))
    assert any("unfinished turn" in w.value for w in at.warning)
    assert len(_resume_buttons(at)) == 1


def test_resuming_streams_none_to_the_graph(page):
    """LangGraph continues a pending node when the input is `None`."""
    at, fake = page(next_nodes=("tools",))
    _resume_buttons(at)[0].click().run()
    assert fake.sent == [None]


def test_a_finished_thread_offers_no_resume(page):
    at, _ = page()
    assert _resume_buttons(at) == []
    assert not any("unfinished turn" in w.value for w in at.warning)


def test_a_pending_approval_takes_precedence_over_resume(page):
    """A real interrupt also leaves `next` set; the decision is what matters."""
    at, _ = page(_interrupt(), next_nodes=("tools",))
    assert _resume_buttons(at) == []
    assert any("Approval required" in s.value for s in at.subheader)


def test_every_pending_action_needs_its_own_decision(page):
    """The middleware wants one decision per action and raises on a mismatch."""
    send = {"name": "send_invitations", "args": {"recipient_count": 60}}
    at, fake = page(_interrupt(actions=(HOLD, send)))

    assert len(at.segmented_control) == 2
    at.segmented_control[0].set_value("approve").run()

    # Only one of the two answered — still not submittable.
    assert at.button[0].disabled
    at.button[0].click().run()
    assert fake.sent == []

    at.segmented_control[1].set_value("approve").run()
    at.button[0].click().run()
    assert fake.decisions == [[{"type": "approve"}, {"type": "approve"}]]
