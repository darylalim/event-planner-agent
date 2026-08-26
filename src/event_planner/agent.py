"""Deep Agent wiring for the event planner.

Backend layout — the load-bearing decision in this file:

    CompositeBackend
      "/memories/"   -> StoreBackend(namespace=per-user)       persists across sessions
      "/events/"     -> StoreBackend(namespace=per-user)       persists across sessions
      "/artifacts/"  -> StoreBackend(namespace=per-user)       persists across sessions
      default        -> FilesystemBackend(root_dir=workspace)  on disk, shared by everyone

`CompositeBackend` matches the longest route prefix first, and the filesystem
backend is the **default** rather than a route — so a path matching no route
does not fail, it lands on a root that every session can read. Anything the
agent writes under `/memories/`, `/events/` or `/artifacts/` goes to the
LangGraph store in that user's namespace and survives the thread; everything
else is an ordinary file in the workspace directory, visible to every other
planner. Only `/skills/` is meant to be there. See `build_backend` for why
`/events/` has to be routed, and why `/artifacts/` is routed at its root
rather than by the names deepagents derives beneath it.

Three things worth knowing if you change this:

* `FilesystemBackend` is rooted at `workspace/`, not the repo root, and runs
  with `virtual_mode=True`, which blocks `..`, `~`, and absolute paths outside
  the root. The agent therefore cannot read or write its own source. Those are
  path guardrails, not process isolation: do not repoint `root_dir` at the
  repo, and do not use this backend in a server process that handles untrusted
  input.
* `interrupt_on` silently does nothing without a checkpointer. The build below
  will refuse to hand back an un-gated agent rather than let that pass quietly.
* `TodoListMiddleware` is added explicitly. Despite what the Deep Agents docs
  say, `create_deep_agent` in 0.7.9 does not bind `write_todos` on its own —
  verified by inspecting the tools actually bound to the model. The
  orchestrator prompt tells the agent to plan with `write_todos`, so without
  this the model would be instructed to call a tool that does not exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StoreBackend
from langchain.agents.middleware import TodoListMiddleware
from langgraph.store.base import BaseStore

from event_planner.context import (
    PlannerContext,
    artifacts_namespace,
    events_namespace,
    memory_namespace,
)
from event_planner.prompts import ORCHESTRATOR_PROMPT
from event_planner.subagents import SUBAGENTS
from event_planner.tools import (
    IRREVERSIBLE_TOOLS,
    check_availability,
    estimate_budget,
    hold_venue,
    search_vendors,
    search_venues,
    send_invitations,
    web_search,
)

DEFAULT_MODEL = "claude-opus-5"

#: Repo root, i.e. the parent of `src/`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Everything the agent can see on disk. Deliberately not the repo root.
WORKSPACE = PROJECT_ROOT / "workspace"

#: Where deepagents offloads its own spill. `FilesystemMiddleware` derives
#: `<root>/large_tool_results/` and `<root>/conversation_history/` from the
#: composite's `artifacts_root`, so routing the root covers both — and covers
#: whatever a later release renames or adds beneath it. Named rather than
#: inlined so `build_backend` and the tests cannot drift on the literal.
ARTIFACTS_ROOT = "/artifacts/"

#: Decisions an operator may take on a gated tool. `respond` is omitted: a
#: free-text reply to a booking request invites the model to treat commentary
#: as confirmation. Approve it, fix it, or refuse it.
ALLOWED_DECISIONS = ["approve", "edit", "reject"]

#: Derived from IRREVERSIBLE_TOOLS rather than restated. Two hand-maintained
#: copies of "which tools spend money" means adding a third booking tool to one
#: and not the other silently ships it un-gated.
INTERRUPT_ON: dict[str, Any] = {
    name: {"allowed_decisions": ALLOWED_DECISIONS} for name in IRREVERSIBLE_TOOLS
}

ORCHESTRATOR_TOOLS = [
    search_venues,
    check_availability,
    search_vendors,
    estimate_budget,
    web_search,
    hold_venue,
    send_invitations,
]


def build_backend() -> CompositeBackend:
    """Compose shared read-only skills with per-user private storage.

    Only `/skills/` lives on the filesystem, and it is shared reference
    material rather than user data. `/events/`, `/memories/` and
    `/artifacts/` all route to per-user store namespaces.

    `/events/` has to be routed rather than left on disk: `backend` takes one
    static instance (backend factories were removed in deepagents 0.7), so a
    per-user filesystem root is impossible, and the agent has ls/read/glob/grep
    over whatever that root is. Event files carry client names, headcounts,
    guest details, and budgets — leaving them on a shared root lets one
    planner's session read another's brief.

    `ARTIFACTS_ROOT` is routed for the same reason and is easier to miss,
    because nothing here writes beneath it: `FilesystemMiddleware` derives
    `<root>/large_tool_results/` and `<root>/conversation_history/` from the
    composite's `artifacts_root` and offloads to them on its own once a tool
    result or a human message goes over its token limit. What spills is a
    tool's output and the planner's own brief, so left unrouted they would put
    exactly the data `/events/` is routed to protect back on the shared root.

    Routing the root rather than the two derived names is deliberate. Those
    names are deepagents' to change; `artifacts_root` is the seam it gives us,
    so one route covers every path it derives now and any it adds later. The
    alternative — two hardcoded prefixes — fails open on a rename, silently and
    with nothing to go red.

    No directories are created here. `workspace/memories` used to be made on
    disk and then permanently shadowed by the `/memories/` route, so it showed
    up twice in the agent's root listing and anything written to the on-disk
    copy was unreadable.
    """
    return CompositeBackend(
        default=FilesystemBackend(root_dir=WORKSPACE, virtual_mode=True),
        routes={
            "/memories/": StoreBackend(namespace=memory_namespace),
            "/events/": StoreBackend(namespace=events_namespace),
            ARTIFACTS_ROOT: StoreBackend(namespace=artifacts_namespace),
        },
        artifacts_root=ARTIFACTS_ROOT,
    )


def build_agent(
    *,
    model: str | Any = DEFAULT_MODEL,
    checkpointer: Any | None = None,
    store: BaseStore | None = None,
    hosted: bool = False,
) -> Any:
    """Construct the event planning agent.

    Args:
        model: Model id or a preconfigured chat model.
        checkpointer: Required for human-in-the-loop approval and for
            conversation state to survive across `invoke` calls.
        store: Backing store for `/memories/`. Without one, memory does not
            persist across threads.
        hosted: Set only when a host (LangGraph Platform, `langgraph dev`)
            injects its own checkpointer and store. Suppresses the guard below.

    Returns a compiled LangGraph graph.

    Raises:
        ValueError: If approval-gated tools are configured without a
            checkpointer, which would silently disable approval.
    """
    if INTERRUPT_ON and checkpointer is None and not hosted:
        msg = (
            "interrupt_on is configured but no checkpointer was supplied, so "
            "approval gates would be silently skipped and hold_venue / "
            "send_invitations would execute unreviewed. Pass a checkpointer "
            "(e.g. InMemorySaver()), or hosted=True when the host supplies one."
        )
        raise ValueError(msg)

    return create_deep_agent(
        model=model,
        tools=ORCHESTRATOR_TOOLS,
        system_prompt=ORCHESTRATOR_PROMPT,
        subagents=SUBAGENTS,
        # Not included by create_deep_agent in 0.7.9 — see module docstring.
        # Cast: TodoListMiddleware is generic over context, and the checker
        # treats that parameter as invariant against our PlannerContext.
        middleware=cast("Any", (TodoListMiddleware(),)),
        backend=build_backend(),
        skills=["/skills/"],
        # Loaded into the system prompt every turn, unlike skills which the
        # agent opens on demand. Routed to the store, so it outlives the thread.
        memory=["/memories/AGENTS.md"],
        interrupt_on=INTERRUPT_ON,
        context_schema=PlannerContext,
        checkpointer=checkpointer,
        store=store,
        name="event-planner",
    )


def hosted_agent() -> Any:
    """Factory referenced by `langgraph.json`.

    `langgraph dev` and LangGraph Platform inject their own checkpointer and
    store, so both are left unset here — passing our own would shadow the
    host's persistence. This is a factory rather than a module-level graph so
    that importing this module never builds an agent as a side effect.
    """
    return build_agent(hosted=True)
