"""Tests for the agent harness.

The behaviours worth protecting here are the ones that fail *silently*:
approval gates that do not gate, memory that does not persist, and a prompt
that instructs the model to call a tool that was never bound.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from conftest import BACKTICKED, agent_bindings, bound_tool_names
from deepagents._models import get_model_identifier
from deepagents.middleware.summarization import compute_summarization_defaults
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from event_planner.agent import WORKSPACE, build_agent
from event_planner.cli import DEFAULT_MAX_STEPS
from event_planner.context import PlannerContext
from event_planner.models import MAX_OUTPUT_TOKENS, ORCHESTRATOR_MODEL, ModelChoice
from event_planner.prompts import ORCHESTRATOR_PROMPT
from event_planner.subagents import SUBAGENT_MODELS, SUBAGENTS

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
# model and effort per role
# --------------------------------------------------------------------------- #


def _requested_effort(model: BaseChatModel) -> str | None:
    """The `output_config.effort` this model would put on the wire.

    Read off the request payload rather than off the model's own field, because
    the payload is the contract with the API: an effort stored on the object and
    dropped on the way out — a provider-package regression, or a model id the
    integration does not map — would pass a check of the field and still ship
    the API's per-model default. Building the payload is local; nothing is sent.
    """
    return (_payload(model).get("output_config") or {}).get("effort")


def _requested_max_tokens(model: BaseChatModel) -> int | None:
    return _payload(model).get("max_tokens")


def _payload(model: BaseChatModel) -> dict[str, Any]:
    return cast("Any", model)._get_request_payload([HumanMessage("hi")])


@pytest.fixture
def built(monkeypatch):
    """`build_agent`, with what it hands `create_deep_agent` captured.

    Captured at that seam because the compiled graph does not expose a
    subagent's model, and the seam is exactly what deepagents receives.
    """
    captured: dict[str, Any] = {}

    def _record(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("event_planner.agent.create_deep_agent", _record)

    def _build(**kwargs: Any) -> dict[str, Any]:
        build_agent(checkpointer=InMemorySaver(), store=InMemoryStore(), **kwargs)
        return captured

    return _build


def test_every_subagent_has_a_model_choice():
    """A new subagent needs a deliberate model, not an inherited Opus.

    `build_agent` indexes `SUBAGENT_MODELS` by name, so a missing entry would
    fail as a bare KeyError at build time; a stale one would sit unused.
    """
    assert set(SUBAGENT_MODELS) == {s["name"] for s in SUBAGENTS}


def test_the_orchestrator_requests_its_effort(built):
    """Claude Opus 5.5 defaults to `medium`, below Claude Opus 5's `high`.

    So an effort that stops reaching the request lowers how hard the agent that
    books venues thinks, and nothing else would notice.
    """
    model = built()["model"]
    assert isinstance(model, BaseChatModel)
    assert get_model_identifier(model) == ORCHESTRATOR_MODEL.model
    assert _requested_effort(model) == ORCHESTRATOR_MODEL.effort == "high"


def test_each_subagent_runs_on_its_own_model_and_effort(built):
    subagents = {spec["name"]: spec for spec in built()["subagents"]}
    assert set(subagents) == set(SUBAGENT_MODELS)
    for name, choice in SUBAGENT_MODELS.items():
        model = subagents[name]["model"]
        assert isinstance(model, BaseChatModel), name
        assert get_model_identifier(model) == choice.model, name
        assert _requested_effort(model) == choice.effort, name


def test_the_orchestrator_model_does_not_reach_the_subagents(built):
    """`--model` and the sidebar's Model field change the orchestrator only.

    Without an explicit `model` in each spec, deepagents hands the subagent the
    orchestrator's — so a regression here would quietly run every subagent on
    whatever an operator typed.
    """
    captured = built(model="claude-sonnet-5-5", effort="low")
    assert _requested_effort(captured["model"]) == "low"
    for spec in captured["subagents"]:
        choice = SUBAGENT_MODELS[spec["name"]]
        assert get_model_identifier(spec["model"]) == choice.model
        assert _requested_effort(spec["model"]) == choice.effort


def test_building_leaves_the_shared_specs_model_free(built):
    """The specs are module-level, so a build that wrote into them would leak
    one build's chat model into every later one — including a test's fake."""
    built()
    assert all("model" not in spec for spec in SUBAGENTS)


def test_a_preconfigured_model_is_used_as_given(built, scripted):
    """`effort` applies to an id; a chat model carries its own. That is what
    lets every other test here hand in a scripted fake."""
    fake = scripted()
    assert built(model=fake)["model"] is fake


def test_every_role_compacts_against_its_real_context_window(built):
    """deepagents sizes compaction from the model's profile, and falls back to
    fixed numbers when there is none: summarise at 170,000 tokens, and truncate
    old tool arguments after 20 messages — rather than at 85% of the window.

    `langchain-anthropic` 1.6.1 had no profile for `claude-opus-5-5` or
    `claude-sonnet-5-5`, so the roster ran on that fallback: a 1M-token window
    compacted at 170k. Unlike the 4,096 output cap, nothing failed — it only
    summarised away detail sooner, and compaction rewrites the history that
    Opus 5.5 checks its thinking blocks against. Asserted on the decision
    deepagents makes, not on the profile, so a renamed profile key still reds.
    """
    captured = built()
    models = [captured["model"], *(spec["model"] for spec in captured["subagents"])]
    for model in models:
        defaults = compute_summarization_defaults(model)
        assert defaults["trigger"] == ("fraction", 0.85), get_model_identifier(model)


def test_no_effort_sends_none_but_keeps_the_output_cap():
    """`effort=None` is the escape hatch for a model that rejects the parameter.

    It must send no effort — not an empty `output_config` — and it must still be
    built here: the first version handed deepagents the bare id, which resolves
    with the package's profile default and would reopen the 4,096 trap.
    """
    model = ModelChoice("claude-haiku-4-5", None).build()
    assert _requested_effort(model) is None
    assert _requested_max_tokens(model) == MAX_OUTPUT_TOKENS


def test_every_role_gets_the_explicit_output_cap(built):
    """Measured live: `langchain-anthropic` 1.6.1 has no profile for
    `claude-opus-5-5` or `claude-sonnet-5-5` and falls back to 4,096 output
    tokens. The budget analyst's `budget.md` ran past that, every `write_file`
    was cut off before its `content`, and it retried 34 times — $1.85 of a
    $2.50 brief. Read off the payload, like effort, because that is what the
    API enforces."""
    captured = built()
    models = [captured["model"], *(spec["model"] for spec in captured["subagents"])]
    assert [_requested_max_tokens(m) for m in models] == [MAX_OUTPUT_TOKENS] * 4
    # An operator's --model goes through the same door.
    assert _requested_max_tokens(built(model="claude-opus-5-5")["model"]) == MAX_OUTPUT_TOKENS


def test_a_provider_prefixed_id_still_resolves():
    model = ModelChoice("anthropic:claude-opus-5-5", "medium").build()
    assert isinstance(model, BaseChatModel)
    assert get_model_identifier(model) == "claude-opus-5-5"
    assert _requested_effort(model) == "medium"


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


def _anthropic_hold_call() -> AIMessage:
    """`_hold_call` shaped the way `langchain-anthropic` really returns it.

    The call lives twice: parsed into `tool_calls`, and as a `tool_use` block in
    `content` — the copy that is serialised back to the API. `_hold_call`'s
    plain-string content has no second copy, which is why no offline test saw
    an `edit` reach the tool and not the model.
    """
    call = _hold_call().tool_calls[0]
    return AIMessage(
        content=[
            {"type": "text", "text": "Placing the hold."},
            {"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["args"]},
        ],
        tool_calls=[call],
    )


def _resume(graph, decision):
    return graph.invoke(Command(resume={"decisions": [decision]}), config=THREAD, context=CTX)


def test_an_edit_is_announced_to_the_model(scripted):
    """Measured live on Claude Opus 5.5 before this existed: the operator's
    60 -> 45 came back as "45 guests instead of the 60 you asked for — don't
    pay the deposit", because the API still saw the model's own 60."""
    graph = _agent(scripted(_anthropic_hold_call(), AIMessage(content="Held.")))
    graph.invoke({"messages": [{"role": "user", "content": "Book it."}]}, THREAD, context=CTX)
    corrected = dict(_hold_call().tool_calls[0]["args"], headcount=45)
    messages = _resume(
        graph, {"type": "edit", "edited_action": {"name": "hold_venue", "args": corrected}}
    )["messages"]

    tool_msg = next(m for m in messages if getattr(m, "name", None) == "hold_venue")
    assert "Headcount:   45" in tool_msg.content
    assert "[Operator edit] The human reviewer changed this `hold_venue` call" in tool_msg.content
    assert 'You proposed {"headcount": 60}; what executed was {"headcount": 45}' in tool_msg.content

    # Announced, not repaired: the proposal's tool_use block is history, and
    # Claude Opus 5.5 checks thinking blocks against an edited earlier turn.
    proposal = next(m for m in messages if isinstance(m, AIMessage) and m.tool_calls)
    block = next(b for b in proposal.content if isinstance(b, dict) and b["type"] == "tool_use")
    assert block["input"]["headcount"] == 60


def test_an_approved_call_carries_no_edit_note(scripted):
    graph = _agent(scripted(_anthropic_hold_call(), AIMessage(content="Held.")))
    graph.invoke({"messages": [{"role": "user", "content": "Book it."}]}, THREAD, context=CTX)
    messages = _resume(graph, {"type": "approve"})["messages"]
    tool_msg = next(m for m in messages if getattr(m, "name", None) == "hold_venue")
    assert "Operator edit" not in tool_msg.content


def test_an_edit_note_needs_a_proposal_it_can_see(scripted):
    """With no `tool_use` block to compare against, nothing is claimed — the
    note must never assert a proposal this middleware did not read."""
    graph = _agent(scripted(_hold_call(), AIMessage(content="Held.")))
    graph.invoke({"messages": [{"role": "user", "content": "Book it."}]}, THREAD, context=CTX)
    corrected = dict(_hold_call().tool_calls[0]["args"], headcount=45)
    messages = _resume(
        graph, {"type": "edit", "edited_action": {"name": "hold_venue", "args": corrected}}
    )["messages"]
    tool_msg = next(m for m in messages if getattr(m, "name", None) == "hold_venue")
    assert "Operator edit" not in tool_msg.content


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

    This harness runs five middleware nodes — three `before_agent` once per
    invocation, two `after_model` on every model call (see `_FIXED_OVERHEAD`
    above) — so a tool round trip costs far more than the two steps
    (model + tools) you would expect — measured live, not guessed.
    `DEFAULT_MAX_STEPS` is a ceiling rather than a
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


def test_the_orchestrator_is_told_to_delegate_research_and_why():
    """Delegation is a security boundary here, so its instruction is pinned.

    Measured live: on Claude Opus 5.5 the previous wording — delegate research,
    but "do not delegate work you can finish yourself in one or two tool calls"
    — produced a full brief with zero `task` calls, the orchestrator running
    every `web_search` itself. That put live web content in the one context
    holding `hold_venue` and `send_invitations`. A stronger model judges more
    work finishable, so a capacity-based escape hatch erodes as models improve;
    the instruction now gives the reason instead, and this test holds both.
    """
    prompt = ORCHESTRATOR_PROMPT
    assert "work you can finish yourself" not in prompt
    assert "even when you could do it yourself" in prompt
    assert "do not run that research with your own\n`web_search`" in prompt
    for name in ("venue-researcher", "vendor-researcher"):
        assert f"`{name}`" in prompt
    # The reason, not just the rule: a model weighing whether to delegate
    # needs to know it is about where untrusted content may sit.
    assert "nothing a web page says can\nsit beside a booking" in prompt


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
