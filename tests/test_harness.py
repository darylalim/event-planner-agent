"""Tests for the agent harness.

The behaviours worth protecting here are the ones that fail *silently*:
approval gates that do not gate, memory that does not persist, and a prompt
that instructs the model to call a tool that was never bound.
"""

from __future__ import annotations

import pytest
from conftest import BACKTICKED, agent_bindings, bound_tool_names
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from event_planner.agent import WORKSPACE, build_agent
from event_planner.cli import DEFAULT_MAX_STEPS
from event_planner.context import PlannerContext

THREAD = {"configurable": {"thread_id": "t-1"}}
CTX = PlannerContext(user_id="alice@example.com")

#: Measured step model, from the live node sequence:
#:
#:   3 x before_agent            once per turn   (Skills, PatchToolCalls, Memory)
#:   model + 2 x after_model     per model call  (HumanInTheLoop, TodoList)
#:   tools                       per round trip
#:
#: N tool round trips cost 3 + 3(N+1) + N = 6 + 4N steps. after_model nodes run
#: on *every* model call, not once per turn, so subtracting them a single time
#: as fixed overhead — as an earlier version did — misattributes one model
#: turn's worth of cost.
_FIXED_OVERHEAD = 6  # 3 before_agent + the final model call's 3 nodes
_STEPS_PER_ROUND_TRIP = 4


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

    create_deep_agent 0.7.9 does not bind it by default, so agent.py adds
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
            resume={"decisions": [{"type": "reject", "message": "Budget not signed off yet."}]}
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


def test_pending_approval_survives_a_process_restart(scripted, tmp_path):
    """The CLI persists to SQLite, not memory — and operators walk away.

    A booking proposed on Monday should still be approvable on Tuesday, after
    the process has exited. The other HITL tests use InMemorySaver, so they
    cannot show that a pending interrupt serializes to disk and resumes from a
    completely fresh saver, graph, and model.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.store.sqlite import SqliteStore

    db = str(tmp_path / "resume.sqlite")
    config = {"configurable": {"thread_id": "overnight"}}

    # --- session one: the agent proposes a booking, then the process ends ---
    with SqliteSaver.from_conn_string(db) as cp, SqliteStore.from_conn_string(db) as store:
        store.setup()
        graph = build_agent(model=scripted(_hold_call()), checkpointer=cp, store=store)
        first = graph.invoke(
            {"messages": [{"role": "user", "content": "Book it."}]},
            config=config,
            context=CTX,
        )
        assert "__interrupt__" in first, "expected the approval gate to fire"

    # --- session two: brand-new saver, graph, and model. Approve. ---
    with SqliteSaver.from_conn_string(db) as cp, SqliteStore.from_conn_string(db) as store:
        graph = build_agent(
            model=scripted(AIMessage(content="Held.")), checkpointer=cp, store=store
        )
        resumed = graph.invoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
            context=CTX,
        )

    tool_msgs = [m for m in resumed["messages"] if getattr(m, "name", None) == "hold_venue"]
    assert tool_msgs, "approval did not survive the restart"
    assert "Provisional hold placed" in tool_msgs[-1].content


# --------------------------------------------------------------------------- #
# step budget
# --------------------------------------------------------------------------- #


def test_step_budget_survives_a_long_planning_session(scripted):
    """Every middleware node counts as a LangGraph super-step.

    This harness runs five middleware nodes per model turn, so a tool round
    trip costs far more than the two steps (model + tools) you would expect —
    measured live, not guessed. `DEFAULT_MAX_STEPS` is a ceiling rather than a
    rescue from LangGraph's default of 25: `create_deep_agent` binds
    `recursion_limit: 9_999` onto the graph, so 25 never applies here.

    If middleware is added or removed, this test reports the new per-round-trip
    cost rather than letting a silent truncation reach users.
    """
    call = AIMessage(
        content="Looking.",
        tool_calls=[
            {
                "name": "search_venues",
                "args": {"city": "San Francisco", "min_capacity": 50},
                "id": "step-1",
            }
        ],
    )
    graph = _agent(scripted(call, AIMessage(content="done")))
    steps = sum(
        1
        for _ in graph.stream(
            {"messages": [{"role": "user", "content": "Find venues."}]},
            config=THREAD,
            context=CTX,
            stream_mode="updates",
        )
    )

    # This run is exactly one tool round trip, so steps == 6 + 4.
    assert steps > 2, "step accounting looks wrong; middleware may not be running"
    per_round_trip = max(1, steps - _FIXED_OVERHEAD)
    assert per_round_trip == _STEPS_PER_ROUND_TRIP, (
        f"middleware changed: a tool round trip now costs {per_round_trip} steps, "
        f"not {_STEPS_PER_ROUND_TRIP}. Re-check DEFAULT_MAX_STEPS."
    )

    affordable = (DEFAULT_MAX_STEPS - _FIXED_OVERHEAD) // per_round_trip
    assert affordable >= 30, (
        f"budget of {DEFAULT_MAX_STEPS} affords only ~{affordable} tool round "
        f"trips at {per_round_trip} steps each — too few for a planning session"
    )
    bound = (getattr(graph, "config", None) or {}).get("recursion_limit")
    assert bound is not None, "deepagents no longer binds a recursion_limit to the graph"
    assert DEFAULT_MAX_STEPS < bound, (
        f"DEFAULT_MAX_STEPS={DEFAULT_MAX_STEPS} caps nothing: create_deep_agent binds "
        f"{bound} onto the compiled graph, so any larger value is inert"
    )


# --------------------------------------------------------------------------- #
# prompt/tool drift
# --------------------------------------------------------------------------- #
#
# prompts.py names tools as literal strings, so a rename ships an agent
# instructed to call something that does not exist and nothing catches it at
# import time. These checks used to live in .claude/hooks/check_prompt_drift.py
# behind a PostToolUse trigger on five paths, plus a CI canary that planted
# drift to prove the gate had run at all. As tests they need none of that: they
# run on every path, in CI, and for a contributor without Claude Code.
#
# The hook's `unknown` check is deliberately not ported. It flagged any
# backticked snake_case token that is not a tool anywhere, which made every
# piece of prose a candidate and needed a hand-maintained vocabulary list
# (HARNESS_TOOLS, EXTRA_NON_TOOL_TERMS) to stay quiet -- a second copy of
# deepagents' tool names that fails open the moment the package adds one.


def _named_in(text: str) -> set[str]:
    return set(BACKTICKED.findall(text))


def _named_anywhere() -> set[str]:
    """Every snake_case token the model is shown, across prompts and skills.

    Skills count as prompts here: workspace/skills/*/SKILL.md is loaded into
    context at runtime and names tools in backticks the same way.
    """
    shown = [prompt for _, prompt, _ in agent_bindings()]
    shown += [
        p.read_text(encoding="utf-8") for p in sorted((WORKSPACE / "skills").glob("*/SKILL.md"))
    ]
    return {token for text in shown for token in _named_in(text)}


def test_every_bound_tool_is_named_somewhere():
    """A tool the model was given but never told about is a rename that only
    landed on one side of the loop.

    Checked globally rather than per agent: the orchestrator legitimately names
    only two of its seven tools and delegates the rest, so a per-agent version
    reports five false positives on a healthy tree.
    """
    orphaned = bound_tool_names() - _named_anywhere()
    assert not orphaned, (
        f"bound but named by no prompt or skill: {sorted(orphaned)}. Either the "
        f"model is never told these exist, or a rename left the prompts naming "
        f"the old spelling."
    )


def test_no_prompt_instructs_a_tool_its_agent_cannot_call():
    """The complement of the test above, and the one it cannot see.

    `test_every_bound_tool_is_named_somewhere` unions all four agents, so
    budget-analyst's prompt naming `hold_venue` passes it: the tool is bound
    somewhere and named somewhere. But binding is per agent, and that is what
    decides what the model can actually call -- the subagent burns a turn on a
    tool it was never given, and the failure surfaces as a confused transcript
    rather than an error.

    This check was dropped when the hook was pruned, on the grounds that
    legitimate prose ("the orchestrator calls `hold_venue`, which pauses for a
    human") would trip it. No prompt in this tree does that -- the exemption set
    below is empty and the check is silent. Add a pair here, with the sentence
    that justifies it, if prose ever needs to name another agent's tool; an
    empty allowlist that must be edited deliberately is the point.
    """
    #: (agent, tool) pairs where naming another agent's tool is deliberate prose.
    permitted: set[tuple[str, str]] = set()

    bound = bound_tool_names()
    misrouted = {
        (name, tool)
        for name, prompt, own in agent_bindings()
        for tool in sorted((_named_in(prompt) & bound) - own)
    } - permitted
    assert not misrouted, (
        f"prompts instruct tools their agent cannot call: {sorted(misrouted)}. "
        f"Either bind the tool to that agent, stop naming it in the prompt, or "
        f"add the pair to `permitted` above if it is prose about another agent."
    )


# Memory scoping and tenant isolation are covered in tests/test_security.py.
