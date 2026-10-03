"""Tests for `scripts/live_check.py`'s accounting — offline, like the rest.

The script's live modes spend money and are never run here. What is tested is
the part that decides what a live run *reports*: per-role usage and cost. That
is where this repo has already been wrong once — README put a full brief at
~$0.87 by summing the root thread, which never sees a subagent's usage, when the
brief had cost ~$2.10.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from conftest import ScriptedModel
from langchain_core.messages import AIMessage

from event_planner.agent import build_agent
from event_planner.webui import close_persistence, open_persistence

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_check.py"


@pytest.fixture(scope="module")
def live() -> ModuleType:
    """The script, imported from its path: `scripts/` is not a package, by
    design, so it ships in no wheel."""
    spec = importlib.util.spec_from_file_location("live_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `@dataclass` resolves string annotations through
    # `sys.modules[cls.__module__]`, and a module loaded by path is not there.
    sys.modules["live_check"] = module
    spec.loader.exec_module(module)
    return module


def _ai(model, *, tool_calls=(), content="", inp=0, cached=0, out=0):
    return AIMessage(
        content=content,
        tool_calls=list(tool_calls),
        usage_metadata={
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": inp + out,
            "input_token_details": {"cache_read": cached},
        },
        response_metadata={"model_name": model},
    )


def _scripted(*responses):
    model = ScriptedModel(messages=iter([]))
    model.responses = list(responses)
    model.bound_tools = []
    return model


def test_cost_reproduces_the_recorded_figure(live):
    """README's `full-brief-3` orchestrator, priced at Opus 5 rates, is $0.87.
    Pinned so a pricing or formula change cannot quietly move every figure."""
    usage = live.Usage("orchestrator", ("claude-opus-5",), 13, 392_563, 336_253, 0, 16_943)
    assert round(usage.cost(), 2) == 0.87


def test_a_model_with_no_known_price_is_not_guessed(live):
    assert live.Usage("x", ("claude-unknown",), 1, 100, 0, 0, 10).cost() is None
    assert live.Usage("x", ("claude-opus-5-5", "?"), 1, 100, 0, 0, 10).cost() is None


@pytest.mark.parametrize(
    ("tools", "role"),
    [
        (["search_vendors", "web_search"], "vendor-researcher"),
        (["search_venues", "check_availability", "read_file"], "venue-researcher"),
        (["estimate_budget", "write_file", "edit_file"], "budget-analyst"),
        # Both researchers have `web_search`, so on its own it identifies nobody.
        (["web_search"], "subagent (unidentified)"),
        (["ls", "read_file"], "subagent (unidentified)"),
    ],
)
def test_a_subagent_is_named_by_its_domain_tools(live, tools, role):
    calls = [{"name": n, "args": {}, "id": f"c{i}"} for i, n in enumerate(tools)]
    assert live.subagent_role([_ai("m", tool_calls=calls)]) == role


def test_a_subagents_usage_is_counted(live, tmp_path, monkeypatch):
    """The mistake this script exists to not make: a delegated run's tokens live
    in the subagent's own checkpoint namespace, not on the root thread."""
    subagent = _scripted(
        _ai(
            "claude-sonnet-5-5",
            tool_calls=[
                {
                    "name": "search_venues",
                    "args": {"city": "San Francisco", "min_capacity": 100},
                    "id": "s1",
                }
            ],
            inp=1_000,
            cached=600,
            out=50,
        ),
        _ai("claude-sonnet-5-5", content="Shortlist written.", inp=1_500, cached=900, out=80),
    )

    class _Choice:
        def build(self):
            return subagent

    monkeypatch.setattr(
        "event_planner.agent.SUBAGENT_MODELS",
        {
            "venue-researcher": _Choice(),
            "vendor-researcher": _Choice(),
            "budget-analyst": _Choice(),
        },
    )
    orchestrator = _scripted(
        _ai(
            "claude-opus-5-5",
            tool_calls=[
                {
                    "name": "task",
                    "args": {
                        "description": "Shortlist SF venues.",
                        "subagent_type": "venue-researcher",
                    },
                    "id": "t1",
                }
            ],
            inp=5_000,
            cached=4_000,
            out=200,
        ),
        _ai("claude-opus-5-5", content="Here is the plan.", inp=6_000, cached=5_000, out=300),
    )
    checkpointer, store = open_persistence(tmp_path / "live.sqlite", knob="--db")
    try:
        graph = build_agent(model=orchestrator, checkpointer=checkpointer, store=store)
        graph.invoke(
            {"messages": [{"role": "user", "content": "Plan it."}]},
            live._config("t"),
            context=live.USER,
        )
        usages = {u.role: u for u in live.thread_usage(graph, checkpointer, "t")}
    finally:
        close_persistence(checkpointer, store)

    assert set(usages) == {"orchestrator", "venue-researcher"}
    assert (usages["orchestrator"].calls, usages["orchestrator"].output_tokens) == (2, 500)
    venue = usages["venue-researcher"]
    assert (venue.calls, venue.input_tokens, venue.cache_read, venue.output_tokens) == (
        2,
        2_500,
        1_500,
        130,
    )
    assert venue.models == ("claude-sonnet-5-5",)
    # The total is what README quotes, so it must include the subagent.
    assert "TOTAL" in live.report(list(usages.values()))


def test_spending_modes_refuse_without_the_flag(live, monkeypatch, capsys):
    """`brief` and `edit` bill the API; the guard must hold before any env is
    loaded or any graph built."""
    monkeypatch.setattr("sys.argv", ["live_check.py", "brief"])
    monkeypatch.setattr(live, "_load_env", lambda: pytest.fail("loaded env without --yes-spend"))
    assert live.main() == 2
    assert "--yes-spend" in capsys.readouterr().out
