"""Shared fixtures.

The model is faked throughout. These tests are about the harness — approval
gating, memory routing, tool binding — not about model quality, and they must
run without an API key or network access.
"""

from __future__ import annotations

import os
import re
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")

# No traces leave the suite. A page test runs the real `_load_env`, which puts
# `.env`'s LANGSMITH_TRACING=true and real API key into os.environ for the rest
# of the session; a full run escaped only because langsmith's lru_cached env
# lookup had already cached "off". `pytest tests/test_streamlit_page.py
# tests/test_webui.py` did not, and attempted 45 uploads. TRACING_V2 is read
# before TRACING in both namespaces, so this beats a stray LANGCHAIN_TRACING_V2
# in the shell too, and load_dotenv never overrides a variable already set.
os.environ["LANGSMITH_TRACING_V2"] = "false"


class ScriptedModel(GenericFakeChatModel):
    """Returns a fixed list of AIMessages, one per model call.

    `GenericFakeChatModel` has no `bind_tools`, which `create_agent` requires,
    so it is stubbed out as a no-op that records what was bound.
    """

    responses: list[AIMessage] = []
    bound_tools: list[str] = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        self.bound_tools.clear()
        self.bound_tools.extend(getattr(t, "name", str(t)) for t in tools)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        reply = self.responses.pop(0) if self.responses else AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=reply)])


#: Backticked lowercase snake_case tokens. Subagent names (`venue-researcher`)
#: carry hyphens and file paths (`/events/...`) start with a slash, so neither
#: is picked up.
BACKTICKED = re.compile(r"`([a-z_][a-z0-9_]*)`")


def agent_bindings() -> list[tuple[str, str, set[str]]]:
    """(name, system prompt, callable tool names) for the orchestrator and each
    subagent.

    Read from the same structures `build_agent` passes to `create_deep_agent`,
    so the pairing cannot drift from what is actually bound. Lives here because
    test_harness.py and test_security.py both need it and CLAUDE.md's rule
    against two hand-maintained copies applies to tests as much as to source.
    """
    from event_planner.agent import ORCHESTRATOR_TOOLS
    from event_planner.prompts import ORCHESTRATOR_PROMPT
    from event_planner.subagents import SUBAGENTS

    pairs = [("orchestrator", ORCHESTRATOR_PROMPT, {t.name for t in ORCHESTRATOR_TOOLS})]
    pairs += [
        (str(s["name"]), str(s.get("system_prompt", "")), {t.name for t in s.get("tools", [])})
        for s in SUBAGENTS
    ]
    return pairs


def bound_tool_names() -> set[str]:
    """Every tool name the model can actually call, across all four agents."""
    return {name for _, _, own in agent_bindings() for name in own}


@pytest.fixture
def scripted() -> Any:
    """Factory for a model that plays back a scripted sequence of replies."""

    def _make(*responses: AIMessage) -> ScriptedModel:
        model = ScriptedModel(messages=iter([]))
        model.responses = list(responses)
        model.bound_tools = []
        return model

    return _make
