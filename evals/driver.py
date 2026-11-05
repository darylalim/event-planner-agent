"""Run one scenario against the graph and capture everything the checks read.

The capture is plain data, so it can be written to JSONL, re-scored later without
paying for the run again, and built by hand in `tests/test_evals.py`.

What actually executed comes from a spy on each gated tool's body rather than
from messages. A rejected call leaves a `ToolMessage` carrying the tool's name
too, so the transcript cannot prove a side effect happened, or that one did not.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from collections import Counter
from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

from langgraph.types import Command

from evals.scenarios import Answer, Scenario, WebResult
from event_planner import tools as planner_tools
from event_planner.agent import build_agent
from event_planner.cli import _check_db_outside_workspace, _stored, run_config
from event_planner.context import PlannerContext
from event_planner.tools import IRREVERSIBLE_TOOLS
from event_planner.webui import (
    approve_decision,
    close_persistence,
    edit_decision,
    message_text,
    open_persistence,
    pending_reviews,
    reject_decision,
)
from scripts.live_check import Usage, namespace_messages, subagent_role, summarise

FRONT_END = "eval"

#: A loop of proposals is a finding, not something to pay for indefinitely.
MAX_APPROVAL_ROUNDS = 8

_UNSCRIPTED = "The evaluation's script did not cover this action, so it was not approved."

#: Every kind of stored item, so a write to any of them shows up in a capture.
STORED_KINDS = ("memories", "events", "artifacts")


@dataclass
class ToolCall:
    """One tool call as the model made it, with the result it got back."""

    role: str
    name: str
    args: dict[str, Any]
    result: str | None = None


@dataclass
class Proposal:
    """One action that reached the approval gate, and what the script answered."""

    name: str
    args: dict[str, Any]
    decision: str
    #: The arguments the operator approved: the proposal's, or the edit's.
    approved_args: dict[str, Any] | None = None
    scripted: bool = True


@dataclass
class Execution:
    """A gated tool's body ran. Recorded by the spy, not read from a message."""

    name: str
    args: dict[str, Any]
    result: str


@dataclass
class Capture:
    """Everything observed in one run of one scenario."""

    scenario: str
    rep: int = 0
    user_id: str = ""
    threads: list[str] = field(default_factory=list)
    #: The last assistant text of each session; the checks read the last one.
    replies: list[str] = field(default_factory=list)
    calls: list[ToolCall] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    executions: list[Execution] = field(default_factory=list)
    #: role -> stop reason -> count, so a truncated subagent cannot hide.
    stops: dict[str, dict[str, int]] = field(default_factory=dict)
    #: "<kind>:<key>" -> file text.
    stored: dict[str, str] = field(default_factory=dict)
    web_queries: list[str] = field(default_factory=list)
    exception: str | None = None
    #: Nodes a session stopped on with no interrupt: an unfinished tool call.
    unfinished: list[str] = field(default_factory=list)
    wall_clock: float = 0.0
    usage: list[dict[str, Any]] = field(default_factory=list)

    @property
    def reply(self) -> str:
        return self.replies[-1] if self.replies else ""

    def cost(self) -> float | None:
        """List-price USD, or None when any role's model has no known price."""
        total = 0.0
        for row in self.usage:
            # A subagent that died before its first reply billed nothing, and
            # has no model name to price.
            if not row["calls"]:
                continue
            cost = _usage(row).cost()
            if cost is None:
                return None
            total += cost
        return total

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Capture:
        fields = dict(data)
        fields["calls"] = [ToolCall(**c) for c in data.get("calls", [])]
        fields["proposals"] = [Proposal(**p) for p in data.get("proposals", [])]
        fields["executions"] = [Execution(**e) for e in data.get("executions", [])]
        return cls(**fields)


class FixtureSearch:
    """Stands in for `TavilySearch`: fixed results, and a record of each query."""

    def __init__(self, results: tuple[WebResult, ...], queries: list[str]) -> None:
        self.results = results
        self.queries = queries

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.queries.append(str(payload.get("query", "")))
        return {"results": [asdict(result) for result in self.results]}


@contextlib.contextmanager
def _instrumented(scenario: Scenario, capture: Capture) -> Generator[None]:
    """Fix the web and spy on the gated tools, inside this process only."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"TAVILY_API_KEY": "eval-fixture"}))
        stack.enter_context(
            patch(
                "event_planner.tools.search._client",
                return_value=FixtureSearch(scenario.web, capture.web_queries),
            )
        )
        for name in IRREVERSIBLE_TOOLS:
            tool = getattr(planner_tools, name)
            stack.enter_context(patch.object(tool, "func", _spy(name, tool.func, capture)))
        yield


def _spy(name: str, body: Callable[..., str], capture: Capture) -> Callable[..., str]:
    def spied(**kwargs: Any) -> str:
        result = body(**kwargs)
        capture.executions.append(Execution(name, kwargs, result))
        return result

    return spied


def _answer(action: dict[str, Any], script: list[Answer], capture: Capture) -> dict[str, Any]:
    """Build one decision from the script, through the front ends' own builders."""
    name, args = action["name"], dict(action.get("args", {}))
    answer = script[0] if script and script[0].tool == name else None
    if answer is None:
        capture.proposals.append(Proposal(name, args, "reject", scripted=False))
        return reject_decision(_UNSCRIPTED)
    script.pop(0)
    if answer.decision == "approve":
        capture.proposals.append(Proposal(name, args, "approve", approved_args=args))
        return approve_decision()
    if answer.decision == "edit":
        edited = {**args, **answer.edits}
        capture.proposals.append(Proposal(name, args, "edit", approved_args=edited))
        return edit_decision(action, edited)
    capture.proposals.append(Proposal(name, args, "reject"))
    return reject_decision(answer.reason)


def _converse(
    graph: Any,
    prompt: str,
    config: dict[str, Any],
    context: PlannerContext,
    script: list[Answer],
    capture: Capture,
) -> None:
    """One session: the opening message, then every approval until it settles."""
    payload: Any = {"messages": [{"role": "user", "content": prompt}]}
    for _ in range(MAX_APPROVAL_ROUNDS):
        graph.invoke(payload, config, context=context)
        state = graph.get_state(config)
        if not state.interrupts:
            return
        reviews = pending_reviews(state.interrupts)
        if not reviews:
            msg = f"unparseable interrupt, refusing to continue: {state.interrupts!r}"
            raise RuntimeError(msg)
        decisions = [_answer(action, script, capture) for action, _ in reviews]
        payload = Command(resume={"decisions": decisions})
    msg = f"still asking for approval after {MAX_APPROVAL_ROUNDS} rounds"
    raise RuntimeError(msg)


def _calls_of(role: str, messages: list[Any]) -> list[ToolCall]:
    results = {
        m.tool_call_id: message_text(m) for m in messages if getattr(m, "type", None) == "tool"
    }
    return [
        ToolCall(role, call["name"], dict(call["args"]), results.get(call["id"]))
        for m in messages
        if getattr(m, "type", None) == "ai"
        for call in m.tool_calls
    ]


def _role_of(messages: list[Any], tasks: dict[str, str]) -> str:
    """Name a subagent run from the `task` call that started it.

    deepagents opens the run with the call's description as its only message, so
    this is exact where `subagent_role`'s inference from tools is not: a
    researcher that only searched the web matches two specs. That inference stays
    as the fallback for a run whose opening message does not match.
    """
    first = next((m for m in messages if getattr(m, "type", None) == "human"), None)
    if first is not None and (role := tasks.get(message_text(first))) is not None:
        return role
    return subagent_role(messages)


def _record_stops(role: str, messages: list[Any], capture: Capture) -> None:
    stops = Counter(
        str(m.response_metadata.get("stop_reason"))
        for m in messages
        if getattr(m, "type", None) == "ai"
    )
    bucket = capture.stops.setdefault(role, {})
    for reason, count in stops.items():
        bucket[reason] = bucket.get(reason, 0) + count


def _collect_session(
    graph: Any, checkpointer: Any, config: dict[str, Any], capture: Capture
) -> None:
    """Read one thread back: root messages, then each subagent's namespace."""
    thread = config["configurable"]["thread_id"]
    state = graph.get_state(config)
    root = state.values.get("messages", [])
    if state.next and not state.interrupts:
        capture.unfinished.extend(state.next)

    ai_texts = [
        text for m in root if getattr(m, "type", None) == "ai" and (text := message_text(m).strip())
    ]
    capture.replies.append(ai_texts[-1] if ai_texts else "")

    orchestrator = _calls_of("orchestrator", root)
    capture.calls.extend(orchestrator)
    capture.usage.append(_usage_row(summarise("orchestrator", root)))
    _record_stops("orchestrator", root, capture)

    tasks = {
        str(c.args.get("description")): str(c.args.get("subagent_type"))
        for c in orchestrator
        if c.name == "task"
    }
    for messages in namespace_messages(checkpointer, thread):
        role = _role_of(messages, tasks)
        capture.calls.extend(_calls_of(role, messages))
        capture.usage.append(_usage_row(summarise(role, messages)))
        _record_stops(role, messages, capture)


def _usage(row: dict[str, Any]) -> Usage:
    """Rebuild a `Usage` from its JSON row, where the models tuple became a list."""
    fields = dict(row)
    fields["models"] = tuple(fields["models"])
    return Usage(**fields)


def _usage_row(usage: Usage) -> dict[str, Any]:
    row = asdict(usage)
    row["models"] = list(usage.models)
    return row


def _file_text(value: Any) -> str:
    """A stored file's text. deepagents has used both a string and a line list."""
    content = value.get("content", value) if isinstance(value, dict) else value
    if isinstance(content, list):
        return "\n".join(str(line) for line in content)
    return content if isinstance(content, str) else json.dumps(content)


def _collect_stored(store: Any, capture: Capture) -> None:
    for kind in STORED_KINDS:
        items, _ = _stored(store, capture.user_id, kind)
        for item in items:
            capture.stored[f"{kind}:{item.key}"] = _file_text(item.value)


def run_scenario(
    scenario: Scenario,
    *,
    db_dir: Path,
    rep: int = 0,
    build: Callable[..., Any] = build_agent,
) -> Capture:
    """Run every session of `scenario` on a fresh database and a unique user.

    A model or graph error is recorded rather than raised: it is a result, and
    whatever the run spent before it still has to reach the spend cap.
    """
    run_id = uuid.uuid4().hex[:10]
    capture = Capture(scenario.name, rep=rep, user_id=f"eval-{run_id}@example.com")
    context = PlannerContext(user_id=capture.user_id)
    db = db_dir / f"{scenario.name}-{rep}-{run_id}.sqlite"
    _check_db_outside_workspace(db, knob="--db-dir")
    db.parent.mkdir(parents=True, exist_ok=True)
    checkpointer, store = open_persistence(db, knob="--db-dir")
    script = list(scenario.answers)
    start = time.monotonic()
    try:
        with _instrumented(scenario, capture):
            graph = build(checkpointer=checkpointer, store=store)
            for index, prompt in enumerate(scenario.sessions):
                thread = f"eval-{run_id}-s{index}"
                config = run_config(thread, user_id=capture.user_id, front_end=FRONT_END)
                capture.threads.append(thread)
                try:
                    _converse(graph, prompt, config, context, script, capture)
                except Exception as exc:  # noqa: BLE001 - a failed run is a result
                    capture.exception = f"{type(exc).__name__}: {exc}"
                _collect_session(graph, checkpointer, config, capture)
                if capture.exception:
                    break
            _collect_stored(store, capture)
    finally:
        capture.wall_clock = time.monotonic() - start
        close_persistence(checkpointer, store)
    return capture
