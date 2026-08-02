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

    def __init__(self, interrupt=None, messages=(), next_nodes=(), then=None):
        self.sent = []
        self.interrupt = interrupt
        self.messages = list(messages)
        self.next_nodes = tuple(next_nodes)
        # Interrupt raised by the *next* run, so a test can model the real
        # sequence: approve `hold_venue`, and the agent immediately asks about
        # `send_invitations` in the same script run.
        self.then = then
        self.checkpoint = 0

    def get_state(self, config):
        interrupts = (SimpleNamespace(value=self.interrupt),) if self.interrupt else ()
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
        self.checkpoint += 1
        # Running clears the pending approval and the pending node, like the real
        # graph — and may immediately raise the next interrupt.
        self.interrupt = self.then
        self.then = None
        self.next_nodes = ("tools",) if self.interrupt else ()
        return iter([])

    @property
    def decisions(self):
        return [p.resume["decisions"] for p in self.sent if hasattr(p, "resume")]


@pytest.fixture
def page(tmp_path, monkeypatch):
    """Run the real page against a fake graph and return `(AppTest, FakeGraph)`."""

    def _run(interrupt=None, messages=(), api_key=True, next_nodes=(), then=None):
        fake = FakeGraph(interrupt, messages, next_nodes, then)
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
    """`disabled=` stops the click in a browser; it is not the guard."""
    at, fake = page(_interrupt())
    assert at.button[0].disabled
    at.button[0].click().run()
    assert fake.sent == []
    assert any("Approval required" in s.value for s in at.subheader)


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
