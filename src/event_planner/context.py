"""Runtime context and memory scoping.

`StoreBackend` takes a namespace factory that receives the LangGraph runtime,
so every planner gets their own slice of the store without running a separate
agent per user. Two rules govern the mapping from identity to namespace:

1. **It must be injective.** Distinct users must never land in the same
   namespace. Naive sanitization (replacing unsafe characters) is lossy —
   `"a/b"` and `"a b"` both collapse to `"a_b"` — so a digest of the raw id is
   appended to guarantee separation while keeping the readable part readable.

2. **It must not fail open.** If no user identity is supplied, falling back to
   a single shared `"default"` bucket would silently merge every anonymous
   caller's memory. We scope to the conversation thread instead, which is
   isolated by construction.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

#: `StoreBackend` validates every namespace component against this character
#: set and raises on anything else, so a user id containing a space, slash, or
#: glob character would blow up mid-run.
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9\-_.@+:~]")

#: Keep the readable prefix short; the digest carries uniqueness.
_MAX_READABLE = 40
_DIGEST_LEN = 12

_ROOT = ("event_planner", "memories")


@dataclass
class PlannerContext:
    """Per-invocation context, passed via `context=` on invoke/stream."""

    user_id: str = "default"


def _component(raw: str) -> str:
    """Map an arbitrary identifier to a safe, collision-free namespace component.

    Sanitization alone is lossy, so the digest of the *raw* value is appended.
    Two ids that sanitize identically still produce different components.
    """
    readable = _SAFE_COMPONENT.sub("_", raw).strip("_")[:_MAX_READABLE]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
    return f"{readable}.{digest}" if readable else digest


def _current_thread_id() -> str | None:
    """Best-effort read of the active thread id, for anonymous fallback."""
    try:
        from langgraph.config import get_config

        configurable = (get_config() or {}).get("configurable", {})
    except Exception:  # noqa: BLE001 - no ambient config; caller is not in a run
        return None
    thread_id = configurable.get("thread_id")
    return str(thread_id) if thread_id else None


def memory_namespace(runtime: Any) -> tuple[str, ...]:
    """Namespace factory for `StoreBackend`.

    Returns a per-user namespace so one deployed agent can serve many planners
    without leaking memory between them.

    When no `user_id` is supplied the namespace falls back to the conversation
    thread rather than a shared bucket. An anonymous caller therefore gets
    memory isolated to their own thread instead of inheriting everyone else's.
    """
    context = getattr(runtime, "context", None)
    user_id = getattr(context, "user_id", None)
    if user_id:
        return (*_ROOT, "u", _component(str(user_id)))

    thread_id = _current_thread_id()
    if thread_id:
        return (*_ROOT, "t", _component(thread_id))

    # Neither identity nor thread. Nothing durable can be scoped safely, so use
    # an explicitly-labelled bucket that no identified user can ever occupy.
    return (*_ROOT, "anonymous")
