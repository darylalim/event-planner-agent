"""Tests for the agent harness.

The behaviours worth protecting here are the ones that fail *silently*:
approval gates that do not gate, memory that does not persist, and a prompt
that instructs the model to call a tool that was never bound.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from event_planner.agent import build_agent
from event_planner.context import PlannerContext, memory_namespace

THREAD = {"configurable": {"thread_id": "t-1"}}
CTX = PlannerContext(user_id="alice@example.com")


def _agent(model, store=None):
    return build_agent(
        model=model,
        checkpointer=InMemorySaver(),
        store=store or InMemoryStore(),
    )


def _hold_call() -> AIMessage:
    return AIMessage(
        content="Placing the hold.",
        tool_calls=[
            {
                "name": "hold_venue",
                "args": {
                    "venue_id": "v-loft-mission",
                    "event_date": "2026-09-26",
                    "headcount": 60,
                    "total_cost_usd": 12000.0,
                    "client_name": "Acme",
                },
                "id": "call-1",
            }
        ],
    )


# --------------------------------------------------------------------------- #
# configuration guards
# --------------------------------------------------------------------------- #


def test_missing_checkpointer_is_rejected(scripted):
    """interrupt_on without a checkpointer silently disables approval upstream.

    We would rather fail loudly at construction than book a venue unreviewed.
    """
    with pytest.raises(ValueError, match="no checkpointer"):
        build_agent(model=scripted())


def test_hosted_mode_allows_missing_checkpointer(scripted):
    """`langgraph dev` injects its own persistence, so the guard must be opt-out."""
    assert build_agent(model=scripted(), hosted=True) is not None


def test_planning_tool_is_bound(scripted):
    """The orchestrator prompt tells the model to plan with `write_todos`.

    create_deep_agent 0.7.1 does not bind it by default, so agent.py adds
    TodoListMiddleware explicitly. Without this test that regression is
    invisible until the model hallucinates a call to a missing tool.
    """
    model = scripted(AIMessage(content="ok"))
    _agent(model).invoke(
        {"messages": [{"role": "user", "content": "hi"}]}, config=THREAD, context=CTX
    )
    assert "write_todos" in model.bound_tools
    assert {"hold_venue", "send_invitations", "task"} <= set(model.bound_tools)


# --------------------------------------------------------------------------- #
# human-in-the-loop
# --------------------------------------------------------------------------- #


def test_booking_pauses_for_approval(scripted):
    """hold_venue must not execute before a human decides."""
    graph = _agent(scripted(_hold_call()))
    result = graph.invoke(
        {"messages": [{"role": "user", "content": "Book it."}]},
        config=THREAD,
        context=CTX,
    )

    assert "__interrupt__" in result, "expected an approval interrupt"
    payload = result["__interrupt__"][0].value
    action = payload["action_requests"][0]
    assert action["name"] == "hold_venue"
    assert action["args"]["headcount"] == 60
    assert set(payload["review_configs"][0]["allowed_decisions"]) == {
        "approve",
        "edit",
        "reject",
    }

    # Nothing ran: no tool message for the booking yet.
    assert not [m for m in result["messages"] if getattr(m, "name", None) == "hold_venue"]


def test_approval_executes_the_booking(scripted):
    graph = _agent(scripted(_hold_call(), AIMessage(content="Held.")))
    graph.invoke(
        {"messages": [{"role": "user", "content": "Book it."}]},
        config=THREAD,
        context=CTX,
    )
    resumed = graph.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}), config=THREAD, context=CTX
    )

    tool_msgs = [m for m in resumed["messages"] if getattr(m, "name", None) == "hold_venue"]
    assert tool_msgs, "approved tool did not run"
    assert "Provisional hold placed" in tool_msgs[-1].content


def test_rejection_blocks_the_booking_and_returns_feedback(scripted):
    graph = _agent(scripted(_hold_call(), AIMessage(content="Understood.")))
    graph.invoke(
        {"messages": [{"role": "user", "content": "Book it."}]},
        config=THREAD,
        context=CTX,
    )
    resumed = graph.invoke(
        Command(
            resume={
                "decisions": [
                    {"type": "reject", "message": "Budget not signed off yet."}
                ]
            }
        ),
        config=THREAD,
        context=CTX,
    )

    blob = "\n".join(str(m.content) for m in resumed["messages"])
    assert "Provisional hold placed" not in blob, "rejected booking still executed"
    assert "Budget not signed off yet." in blob, "rejection feedback not surfaced"


def test_edit_rewrites_the_arguments_before_execution(scripted):
    """An operator correcting the headcount must change what actually runs."""
    graph = _agent(scripted(_hold_call(), AIMessage(content="Held.")))
    graph.invoke(
        {"messages": [{"role": "user", "content": "Book it."}]},
        config=THREAD,
        context=CTX,
    )
    corrected = dict(_hold_call().tool_calls[0]["args"], headcount=45)
    resumed = graph.invoke(
        Command(
            resume={
                "decisions": [
                    {
                        "type": "edit",
                        "edited_action": {"name": "hold_venue", "args": corrected},
                    }
                ]
            }
        ),
        config=THREAD,
        context=CTX,
    )

    tool_msg = [m for m in resumed["messages"] if getattr(m, "name", None) == "hold_venue"][-1]
    assert "Headcount:   45" in tool_msg.content


def test_unlisted_tools_are_not_gated(scripted):
    """Read-only research must not stop for approval, or the agent is unusable."""
    call = AIMessage(
        content="Searching.",
        tool_calls=[
            {
                "name": "search_venues",
                "args": {"city": "San Francisco", "min_capacity": 50},
                "id": "call-2",
            }
        ],
    )
    graph = _agent(scripted(call, AIMessage(content="Here are options.")))
    result = graph.invoke(
        {"messages": [{"role": "user", "content": "Find venues."}]},
        config=THREAD,
        context=CTX,
    )
    assert "__interrupt__" not in result
    assert [m for m in result["messages"] if getattr(m, "name", None) == "search_venues"]


# --------------------------------------------------------------------------- #
# memory scoping
# --------------------------------------------------------------------------- #


class _Runtime:
    def __init__(self, ctx):
        self.context = ctx


def test_memory_namespace_is_per_user():
    a = memory_namespace(_Runtime(PlannerContext(user_id="alice@example.com")))
    b = memory_namespace(_Runtime(PlannerContext(user_id="bob@example.com")))
    assert a != b
    assert a[:2] == b[:2] == ("event_planner", "memories")


def test_memory_namespace_sanitizes_unsafe_ids():
    """StoreBackend rejects namespace components outside a safe charset.

    An id with a space or slash would otherwise raise mid-run.
    """
    ns = memory_namespace(_Runtime(PlannerContext(user_id="a b/c*d")))
    assert all(
        ch.isalnum() or ch in "-_.@+:~" for ch in ns[-1]
    ), f"unsafe component: {ns[-1]!r}"


def test_memory_namespace_falls_back_without_context():
    assert memory_namespace(_Runtime(None))[-1] == "default"
    assert memory_namespace(object())[-1] == "default"
