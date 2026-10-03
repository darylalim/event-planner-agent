"""Run the planner against the real API, and report what it cost per role.

**This spends money.** `brief` and `edit` call the live model on your
`ANTHROPIC_API_KEY` (and Tavily, if `TAVILY_API_KEY` is set), and refuse to run
without `--yes-spend`. `usage` reads a database and spends nothing.

    uv run scripts/live_check.py brief --yes-spend      # the 85-guest brief, ~$0.65-$1
    uv run scripts/live_check.py edit --yes-spend       # hold_venue answered with `edit`, ~$0.05
    uv run scripts/live_check.py usage --db PATH --thread NAME

The offline suite fakes the model, so it cannot see what only the real one
does. Each finding in README's "The current roster" came from a run like this:
a silent 4,096-token output cap, an orchestrator that stopped delegating, an
operator `edit` read as a fault. Run `brief` after changing a model, an effort
level, a prompt, or the middleware stack, and record the result there.

Two things this gets right that are easy to get wrong:

* **Subagents are counted.** Each `task` call checkpoints under its own
  namespace (`tools:<uuid>`), so summing the root thread's messages sees only
  the orchestrator. README's first cost figure was 2.4x low for exactly that
  reason.
* **A fresh database by default**, never `.state/planner.sqlite`: a live run
  writes memories, and those load into every later session for the same user.
  It is opened through `webui.open_persistence` — two connections, because the
  store issues its own `BEGIN` and sharing one with the checkpointer fails
  under parallel tool calls.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.types import Command

from event_planner.agent import build_agent
from event_planner.cli import (
    DEFAULT_MAX_STEPS,
    _check_db_outside_workspace,
    _load_env,
    credentials_problem,
)
from event_planner.context import PlannerContext
from event_planner.subagents import SUBAGENTS
from event_planner.webui import open_persistence

#: The brief every recorded run in README uses, so runs stay comparable. Note
#: the date is deliberately wrong — 19 September 2026 is a Saturday — and
#: flagging it is part of what a good answer does.
BRIEF = (
    "Plan our company offsite. Acme Robotics, 85 guests, San Francisco, Thursday 19 "
    "September 2026. Format is a seated lunch followed by a 90-minute presentation "
    "that needs AV, livestreamed to about 20 remote staff. Roughly a dozen attendees "
    "are vegan or gluten-free. Hard budget ceiling is $45,000 all-in. Shortlist "
    "venues, price the whole thing, and tell me straight whether it fits the budget. "
    "Do not book anything or send anything yet."
)

EDIT_PROMPT = (
    "Place a hold on venue v-loft-mission for client Acme on 2026-09-26: 60 guests, "
    "total cost $12,000. Call hold_venue with exactly those values; my approval comes "
    "through the approval step."
)

#: USD per million tokens: (uncached input, cache read, output). List prices as
#: of 2026-09; check them before quoting a figure. Cache writes, when a run
#: reports any, are billed at 1.25x uncached input.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.0, 0.20, 20.0),
    "claude-sonnet-5-5": (2.0, 0.20, 10.0),
    "claude-opus-5": (5.0, 0.50, 25.0),
    "claude-sonnet-5": (2.0, 0.20, 10.0),
    "claude-haiku-4-5": (1.0, 0.10, 5.0),
}

USER = PlannerContext(user_id="live-check@example.com")


@dataclass
class Usage:
    """Token totals for one role on one thread."""

    role: str
    models: tuple[str, ...]
    calls: int
    input_tokens: int
    cache_read: int
    cache_write: int
    output_tokens: int

    def cost(self) -> float | None:
        """List-price cost in USD, or None for a model with no known price."""
        if len(self.models) != 1 or self.models[0] not in PRICES:
            return None
        base, cache_read, output = PRICES[self.models[0]]
        # `input_tokens` already includes the cached and cache-written parts.
        uncached = self.input_tokens - self.cache_read - self.cache_write
        return (
            uncached * base
            + self.cache_read * cache_read
            + self.cache_write * base * 1.25
            + self.output_tokens * output
        ) / 1e6


def summarise(role: str, messages: list[Any]) -> Usage:
    """Total the `usage_metadata` of every AI message in `messages`."""
    ai = [m for m in messages if getattr(m, "type", None) == "ai"]

    def field(m: Any, key: str) -> int:
        return int((m.usage_metadata or {}).get(key) or 0)

    def detail(m: Any, key: str) -> int:
        return int(((m.usage_metadata or {}).get("input_token_details") or {}).get(key) or 0)

    return Usage(
        role=role,
        models=tuple(sorted({m.response_metadata.get("model_name") or "?" for m in ai})),
        calls=len(ai),
        input_tokens=sum(field(m, "input_tokens") for m in ai),
        cache_read=sum(detail(m, "cache_read") for m in ai),
        cache_write=sum(detail(m, "cache_creation") for m in ai),
        output_tokens=sum(field(m, "output_tokens") for m in ai),
    )


def subagent_role(messages: list[Any]) -> str:
    """Name the subagent a namespace belongs to, from the tools it called.

    Matched against each spec's own tools rather than hard-coded names, so a
    renamed or added subagent is identified without editing this file. Only the
    domain tools count: the filesystem and planning tools every agent shares say
    nothing. A namespace that matches no spec, or more than one — a researcher
    that only called `web_search`, which both have — is reported as such rather
    than guessed.
    """
    owned = {str(s["name"]): {t.name for t in s.get("tools", [])} for s in SUBAGENTS}
    domain = set().union(*owned.values())
    used = {
        c["name"]
        for m in messages
        if getattr(m, "type", None) == "ai"
        for c in m.tool_calls
        if c["name"] in domain
    }
    matches = [name for name, own in owned.items() if used and used <= own]
    return matches[0] if len(matches) == 1 else "subagent (unidentified)"


def thread_usage(graph: Any, checkpointer: Any, thread: str) -> list[Usage]:
    """Usage for the orchestrator and for every subagent run on `thread`.

    The orchestrator's messages come from graph state. A subagent's live only in
    its own checkpoint namespace, read here by taking the longest message list
    any checkpoint in that namespace holds.
    """
    root = graph.get_state(_config(thread)).values.get("messages", [])
    usages = [summarise("orchestrator", root)]
    namespaces = checkpointer.conn.execute(
        "select distinct checkpoint_ns from checkpoints"
        " where thread_id = ? and checkpoint_ns != ''",
        (thread,),
    ).fetchall()
    for (namespace,) in namespaces:
        longest: list[Any] = []
        for saved in checkpointer.list(
            {"configurable": {"thread_id": thread, "checkpoint_ns": namespace}}
        ):
            messages = saved.checkpoint["channel_values"].get("messages") or []
            if len(messages) > len(longest):
                longest = messages
        usages.append(summarise(subagent_role(longest), longest))
    return usages


def report(usages: list[Usage]) -> str:
    lines = []
    for u in usages:
        cost = u.cost()
        lines.append(
            f"{u.role:24} {','.join(u.models):18} calls={u.calls:3}  "
            f"in={u.input_tokens:>9,} (cached {u.cache_read:>9,})  out={u.output_tokens:>7,}  "
            + (f"${cost:.2f}" if cost is not None else "$?")
        )
    costs = [u.cost() for u in usages]
    total = sum(c for c in costs if c is not None)
    lines.append(f"{'TOTAL':24} " + (f"${total:.2f}" if None not in costs else f">= ${total:.2f}"))
    return "\n".join(lines)


def _config(thread: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread}, "recursion_limit": DEFAULT_MAX_STEPS}


def _text(message: Any) -> str:
    if isinstance(message.content, str):
        return message.content
    return " ".join(b.get("text", "") for b in message.content if isinstance(b, dict))


def _open(db: Path) -> tuple[Any, Any]:
    _check_db_outside_workspace(db, knob="--db")
    db.parent.mkdir(parents=True, exist_ok=True)
    checkpointer, store = open_persistence(db, knob="--db")
    return build_agent(checkpointer=checkpointer, store=store), checkpointer


def run_brief(db: Path, thread: str) -> None:
    graph, checkpointer = _open(db)
    start = time.monotonic()
    graph.invoke({"messages": [{"role": "user", "content": BRIEF}]}, _config(thread), context=USER)
    elapsed = time.monotonic() - start
    state = graph.get_state(_config(thread))
    messages = state.values["messages"]
    tools = Counter(c["name"] for m in messages if m.type == "ai" for c in m.tool_calls)
    stops = Counter(m.response_metadata.get("stop_reason") for m in messages if m.type == "ai")
    print(f"wall clock: {elapsed:.0f}s   pending: {state.next or 'none'}")
    print(f"orchestrator tools: {dict(tools)}")
    print(f"orchestrator stop reasons: {dict(stops)}  <- any max_tokens here is a truncation")
    print(f"\nfinal reply:\n{_text(messages[-1])[:1500]}\n")
    print(report(thread_usage(graph, checkpointer, thread)))


def run_edit(db: Path, thread: str) -> None:
    graph, checkpointer = _open(db)
    config = _config(thread)
    graph.invoke({"messages": [{"role": "user", "content": EDIT_PROMPT}]}, config, context=USER)
    state = graph.get_state(config)
    if not state.interrupts:
        print("no interrupt: the model did not propose hold_venue")
        print(_text(state.values["messages"][-1])[:800])
        return
    proposal = state.values["messages"][-1]
    call = proposal.tool_calls[0]
    thinking = sum(
        1
        for b in (proposal.content if isinstance(proposal.content, list) else [])
        if isinstance(b, dict) and b.get("type") == "thinking"
    )
    print(f"proposed: {call['name']} {call['args']}")
    print(f"thinking blocks in the proposing turn: {thinking}")
    edited = {**call["args"], "headcount": 45}
    decision = {"type": "edit", "edited_action": {"name": call["name"], "args": edited}}
    graph.invoke(Command(resume={"decisions": [decision]}), config, context=USER)
    messages = graph.get_state(config).values["messages"]
    executed = next(m for m in messages if m.type == "tool" and m.name == "hold_venue")
    print("executed:", _text(executed)[:300])
    print("\nfinal reply — should treat 45 as the operator's decision:")
    print(f"{_text(messages[-1])[:800]}\n")
    print(report(thread_usage(graph, checkpointer, thread)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("brief", "edit", "usage"))
    parser.add_argument(
        "--db",
        type=Path,
        help="SQLite file. Default for brief/edit: a fresh one under the system temp dir.",
    )
    parser.add_argument("--thread", help="Thread id. Default: a timestamped one.")
    parser.add_argument(
        "--yes-spend", action="store_true", help="Required for brief and edit: they bill the API."
    )
    args = parser.parse_args()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    thread = args.thread or f"live-{args.mode}-{stamp}"

    if args.mode == "usage":
        if args.db is None or args.thread is None:
            parser.error("usage needs both --db and --thread")
        graph, checkpointer = _open(args.db)
        print(report(thread_usage(graph, checkpointer, thread)))
        return 0

    if not args.yes_spend:
        estimate = "~$0.65-$1" if args.mode == "brief" else "~$0.05"
        print(f"`{args.mode}` calls the live model ({estimate}). Re-run with --yes-spend.")
        return 2
    _load_env()
    if (problem := credentials_problem()) is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    db = args.db or Path(tempfile.gettempdir()) / "event-planner-live" / f"{thread}.sqlite"
    print(f"db: {db}   thread: {thread}\n")
    (run_brief if args.mode == "brief" else run_edit)(db, thread)
    return 0


if __name__ == "__main__":
    sys.exit(main())
