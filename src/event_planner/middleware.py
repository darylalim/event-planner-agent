"""Middleware this project adds to the deepagents stack.

`OperatorEditNote` exists because an `edit` decision reaches the tool but not
the model. `HumanInTheLoopMiddleware` applies the operator's arguments by
rewriting the proposing message's `tool_calls` — LangChain's parsed copy — and
leaves the `tool_use` block in its `content` alone. `langchain-anthropic`
serialises that block, so the API is still told the model asked for what it
proposed, and then reads a result for what the operator approved. Measured
live on Claude Opus 5.5: a headcount edited 60 -> 45 came back as "the hold
shows 45 guests instead of the 60 you asked for — don't pay the deposit until
that's fixed", the operator's own decision reported to them as a fault.

The fix appends to the tool result rather than repairing the proposal. Editing
an earlier assistant turn is exactly what Claude Opus 5.5 checks thinking
blocks against, so a rewritten `tool_use` trades a confusing transcript for a
rejected one; a note on the new `ToolMessage` keeps the history append-only.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command


def _proposed_args(state: Any, call_id: str) -> dict[str, Any] | None:
    """The arguments the model's own `tool_use` block carries for `call_id`.

    `None` when there is no such block — a model whose content is a plain
    string, as the offline fakes' usually is — so nothing is claimed about a
    proposal this cannot see.
    """
    messages = state.get("messages", []) if isinstance(state, dict) else []
    for message in reversed(messages):
        if not isinstance(message, AIMessage) or not isinstance(message.content, list):
            continue
        for block in message.content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") == call_id
                and isinstance(block.get("input"), dict)
            ):
                return block["input"]
    return None


def _edit_note(request: ToolCallRequest) -> str | None:
    call = request.tool_call
    proposed = _proposed_args(request.state, call["id"] or "")
    executed = call["args"]
    if proposed is None or proposed == executed:
        return None
    keys = proposed.keys() | executed.keys()
    changed = sorted(k for k in keys if proposed.get(k) != executed.get(k))
    was = {k: proposed.get(k) for k in changed}
    now = {k: executed.get(k) for k in changed}
    return (
        f"[Operator edit] The human reviewer changed this `{call['name']}` call before "
        f"approving it. You proposed {json.dumps(was)}; what executed was "
        f"{json.dumps(now)}. These are the operator's decision, not an error in the "
        "tool — treat the executed values as confirmed."
    )


def _with_note(result: ToolMessage | Command[Any], note: str | None) -> ToolMessage | Command[Any]:
    if note is None or not isinstance(result, ToolMessage):
        return result
    if isinstance(result.content, str):
        content: Any = f"{result.content}\n\n{note}"
    else:
        content = [*result.content, {"type": "text", "text": note}]
    return result.model_copy(update={"content": content})


class OperatorEditNote(AgentMiddleware):
    """Tell the model when an operator's `edit` changed what executed."""

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        return _with_note(handler(request), _edit_note(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        return _with_note(await handler(request), _edit_note(request))
