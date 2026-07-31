"""Runtime context for the event planning agent.

The `user_id` here is what scopes persistent memory. `StoreBackend` takes a
namespace factory that receives the LangGraph runtime, so every planner gets
their own slice of the store without running a separate agent per user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class PlannerContext:
    """Per-invocation context, passed via `context=` on invoke/stream."""

    user_id: str = "default"


#: `StoreBackend` validates every namespace component against this character
#: set and raises on anything else, so a user id containing a space, slash, or
#: glob character would blow up mid-run rather than at startup. Sanitize here.
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9\-_.@+:~]")


def memory_namespace(runtime: Any) -> tuple[str, ...]:
    """Namespace factory for `StoreBackend`.

    Returns a per-user namespace so one deployed agent can serve many planners
    without leaking memory between them. Falls back to "default" when no
    context was supplied (e.g. a bare `invoke` with no `context=`).
    """
    context = getattr(runtime, "context", None)
    user_id = getattr(context, "user_id", None) or "default"
    safe = _SAFE_COMPONENT.sub("_", str(user_id)).strip("_")
    return ("event_planner", "memories", safe or "default")
