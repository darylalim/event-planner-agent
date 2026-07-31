"""Shared fixtures.

The model is faked throughout. These tests are about the harness — approval
gating, memory routing, tool binding — not about model quality, and they must
run without an API key or network access.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")


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

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        reply = (
            self.responses.pop(0)
            if self.responses
            else AIMessage(content="done")
        )
        return ChatResult(generations=[ChatGeneration(message=reply)])


@pytest.fixture
def scripted() -> Any:
    """Factory for a model that plays back a scripted sequence of replies."""

    def _make(*responses: AIMessage) -> ScriptedModel:
        model = ScriptedModel(messages=iter([]))
        model.responses = list(responses)
        model.bound_tools = []
        return model

    return _make
