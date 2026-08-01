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

#: Two layers validate namespace labels and they do NOT agree, so this charset
#: is the intersection of both:
#:
#:   * `deepagents.backends.store` allows ``A-Za-z0-9-_.@+:~``
#:   * `langgraph.store.base` rejects any label containing a period, plus empty
#:     labels and a root label of "langgraph"
#:
#: The period is therefore excluded even though the deepagents regex permits
#: it. Trusting the more permissive layer passes construction, reads, and `ls`,
#: then raises `InvalidNamespaceError` on the first *write* — which is why the
#: separator below is a hyphen and why `test_namespaces_satisfy_both_validators`
#: checks both layers rather than one docstring.
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9\-_@+:~]")

#: Separator between the readable prefix and the digest. Must be legal in both
#: validators above; a period is not.
_SEPARATOR = "-"

#: Keep the readable prefix short; the digest carries uniqueness.
_MAX_READABLE = 40
_DIGEST_LEN = 12

#: Root label for every namespace. The kind ("memories"/"events") is appended
#: by `_scope`, so it must not be baked in here.
_ROOT = ("event_planner",)


@dataclass
class PlannerContext:
    """Per-invocation context, passed via `context=` on invoke/stream.

    `user_id` defaults to None, not to a placeholder string. A truthy default
    such as "default" silently defeats the fail-closed logic below: every
    caller that omits an id — `PlannerContext()`, `context={}` (LangGraph
    constructs the dataclass from an empty mapping), or a client sending
    partial context — would take the *identified* branch and share one bucket.
    None makes those callers fall through to thread scope, which is isolated.
    """

    user_id: str | None = None


def _component(raw: str) -> str:
    """Map an arbitrary identifier to a safe, collision-free namespace component.

    Sanitization alone is lossy, so the digest of the *raw* value is appended.
    Two ids that sanitize identically still produce different components.
    """
    readable = _SAFE_COMPONENT.sub("_", raw).strip("_")[:_MAX_READABLE]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
    return f"{readable}{_SEPARATOR}{digest}" if readable else digest


def _current_thread_id() -> str | None:
    """Best-effort read of the active thread id, for anonymous fallback."""
    try:
        from langgraph.config import get_config

        configurable = (get_config() or {}).get("configurable", {})
    except Exception:  # noqa: BLE001 - no ambient config; caller is not in a run
        return None
    thread_id = configurable.get("thread_id")
    return str(thread_id) if thread_id else None


def _scope(runtime: Any, kind: str) -> tuple[str, ...]:
    """Build a per-user namespace for one kind of stored data.

    Falls back to the conversation thread when no `user_id` is supplied, so an
    unidentified caller gets storage isolated to their own thread rather than
    inheriting everyone else's.
    """
    context = getattr(runtime, "context", None)
    user_id = getattr(context, "user_id", None)
    if user_id:
        return (*_ROOT, kind, "u", _component(str(user_id)))

    thread_id = _current_thread_id()
    if thread_id:
        return (*_ROOT, kind, "t", _component(thread_id))

    # Neither identity nor thread — nothing durable can be scoped safely. This
    # bucket is shared, which is why it is labelled: no identified user and no
    # threaded caller can ever land in it. In practice a checkpointer always
    # supplies a thread_id, so reaching this means the caller is unroutable.
    return (*_ROOT, kind, "unscoped")


def safe_component(raw: str) -> str:
    """Public form of the namespace component, for callers needing a safe name.

    Reusing this for on-disk export directories keeps them traceable back to
    the namespace they came from, and means an operator-supplied id can never
    act as a path segment — `--user ../../etc` becomes an inert string.
    """
    return _component(raw)


def namespace_for_user(user_id: str, kind: str) -> tuple[str, ...]:
    """Namespace for a known user id, without needing a LangGraph runtime.

    Callers that already know who they are — CLI inspection, export, tests —
    would otherwise each hand-roll a stand-in object with a `.context`
    attribute just to satisfy the runtime-shaped factories below.
    """
    return (*_ROOT, kind, "u", _component(user_id))


def memory_namespace(runtime: Any) -> tuple[str, ...]:
    """Namespace for durable client facts (`/memories/`)."""
    return _scope(runtime, "memories")


def events_namespace(runtime: Any) -> tuple[str, ...]:
    """Namespace for event working files (`/events/`).

    These carry client names, headcounts, guest details, and budgets, so they
    need the same per-user isolation as memory. They cannot live on the shared
    `FilesystemBackend` root: that root is a single static path (backend
    factories were removed in deepagents 0.7), and the agent has ls/read/glob/
    grep over it, so one planner could read another's brief.
    """
    return _scope(runtime, "events")
