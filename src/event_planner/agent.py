"""Deep Agent wiring for the event planner.

Backend layout — the load-bearing decision in this file:

    CompositeBackend
      "/memories/"   -> StoreBackend(namespace=per-user)       persists across sessions
      "/events/"     -> StoreBackend(namespace=per-user)       persists across sessions
      "/artifacts/"  -> StoreBackend(namespace=per-user)       persists across sessions
      default        -> ReadOnlyFilesystemBackend(<package>/workspace)  shared, no writes

`CompositeBackend` matches the longest route prefix first, and the filesystem
backend is the **default** rather than a route — so a path matching no route
does not fail, it lands on a root that every session can read. Anything the
agent writes under `/memories/`, `/events/` or `/artifacts/` goes to the
LangGraph store in that user's namespace and survives the thread; everything
else is *refused*: the shared root holds skills and nothing else, and no
legitimate write is unrouted. See `build_backend` for why
`/events/` has to be routed, and why `/artifacts/` is routed at its root
rather than by the names deepagents derives beneath it.

Three things worth knowing if you change this:

* The root is `src/event_planner/workspace/`, INSIDE the package so an
  installed copy carries its skills, and it runs with `virtual_mode=True`,
  which blocks `..`, `~`, and absolute paths outside the root. The agent
  therefore cannot read its own source. Those are
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
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    FileUploadResponse,
    WriteResult,
)
from langchain.agents.middleware import TodoListMiddleware
from langgraph.store.base import BaseStore

from event_planner.context import (
    PlannerContext,
    artifacts_namespace,
    events_namespace,
    memory_namespace,
)
from event_planner.middleware import OperatorEditNote
from event_planner.models import ORCHESTRATOR_MODEL, Effort, ModelChoice
from event_planner.prompts import ORCHESTRATOR_PROMPT
from event_planner.subagents import SUBAGENT_MODELS, SUBAGENTS
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

#: The orchestrator's model and effort. Both front ends offer these as the
#: defaults for their model and effort fields; neither field reaches the
#: subagents, which run on `SUBAGENT_MODELS`. See `models.py` for why effort
#: is never left to the API's per-model default.
DEFAULT_MODEL = ORCHESTRATOR_MODEL.model
DEFAULT_EFFORT = ORCHESTRATOR_MODEL.effort

#: Everything the agent can see on disk, and the only path here derived from
#: `__file__`. It sits INSIDE the package on purpose: a wheel carries
#: `event_planner/workspace/skills/`, so this resolves in an installed copy as
#: well as a checkout. The previous form counted levels up from the file
#: (`parents[2]`), which is the repo in a checkout and `<venv>/lib/pythonX.Y`
#: in site-packages — a directory that does not exist, so `build_backend`
#: returned a backend rooted at nothing and `skills=["/skills/"]` silently
#: loaded none. Deriving from the package directory removes the index rather
#: than correcting it: there is no level to miscount.
WORKSPACE = Path(__file__).resolve().parent / "workspace"

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


class ReadOnlyFilesystemBackend(FilesystemBackend):
    """The shared root, with every mutation refused.

    Nothing legitimate writes here. Skills are shared reference material, and
    every write the prompts ask for is routed away — `/events/`, `/memories/`
    and `/artifacts/` all land in per-user store namespaces. So a write that
    reaches this backend is the model off script, and letting it land was two
    hazards at once. Skills are loaded into every tenant's next session, and
    `web_search` is live Tavily, so a writable `/skills/` lets whatever the web
    returns rewrite the guidance everyone else gets. And since the root now
    ships inside the package, an unrouted `write("/evil.py")` would put a file
    on the import path.

    Refusing rather than raising is deliberate: `error` is how this protocol
    reports a refusal, so the model reads one and moves on, where an exception
    would end the turn.

    Two tests hold this, and the split matters.
    `test_the_read_only_root_refuses_every_mutator` drives all eight methods and
    checks the file survives, so an override that returned success would red.
    `test_the_backend_surface_has_not_moved` compares deepagents' whole public
    surface against a recorded baseline, so a release that ADDS a mutator reds
    too. The first version tried to do both by filtering `dir()` for the mutator
    names, which cannot work: the filter can never contain a name nobody has
    added yet, and a planted `move` left it green.
    """

    #: Every mutating method in the backend protocol, sync names only; see the
    #: comment below for why the async twins need no override.
    MUTATORS = ("write", "edit", "delete", "upload_files")

    _REFUSAL = (
        "The shared workspace is read-only; it holds skills, which every session "
        "shares. Write plans and briefs under /events/, and durable client facts "
        "under /memories/."
    )

    # `path` and `occurrences` are left unset on purpose: the protocol documents
    # both as None on failure, and a consumer that branches on a truthy `path`
    # would read a refusal as a completed write.
    #
    # No async twins. `BackendProtocol` implements each `a*` as
    # `await asyncio.to_thread(self.<sync>, ...)`, so overriding the sync method
    # refuses both — measured, not assumed, and the test drives all eight.
    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=self._REFUSAL)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error=self._REFUSAL)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=self._REFUSAL)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error=self._REFUSAL) for path, _ in files]


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
    skills = WORKSPACE / "skills"
    if not skills.is_dir():
        # The failure this replaces was silent: deepagents logs one WARNING for
        # an unreadable skills path and then builds an agent that plans without
        # them, which reads as the model ignoring its guidance rather than as a
        # packaging fault. Measured on an installed copy before the fix.
        raise FileNotFoundError(
            f"No skills at {skills}. The agent's workspace ships inside the "
            "package; an install missing it is a packaging fault, not a "
            "configuration one."
        )
    return CompositeBackend(
        default=ReadOnlyFilesystemBackend(root_dir=WORKSPACE, virtual_mode=True),
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
    effort: Effort | None = DEFAULT_EFFORT,
    checkpointer: Any | None = None,
    store: BaseStore | None = None,
    hosted: bool = False,
) -> Any:
    """Construct the event planning agent.

    Args:
        model: The orchestrator's model id, or a preconfigured chat model.
            Subagents do not follow it; each runs on its own entry in
            `SUBAGENT_MODELS`.
        effort: The orchestrator's effort, applied when `model` is an id.
            `None` sends none, for models that reject the parameter. A
            preconfigured chat model carries its own and this is ignored —
            which is what lets the tests hand in a scripted fake.
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

    if isinstance(model, str):
        model = ModelChoice(model, effort).build()

    # Each subagent is given its model here, at build time, rather than in its
    # spec: a spec carrying a chat model would construct one on import. A
    # copy, so the module-level specs stay model-free for the next build.
    subagents = []
    for spec in SUBAGENTS:
        configured = spec.copy()
        configured["model"] = SUBAGENT_MODELS[spec["name"]].build()
        subagents.append(configured)

    return create_deep_agent(
        model=model,
        tools=ORCHESTRATOR_TOOLS,
        system_prompt=ORCHESTRATOR_PROMPT,
        subagents=subagents,
        # TodoListMiddleware: not included by create_deep_agent in 0.7.9 — see
        # module docstring. OperatorEditNote: an `edit` decision otherwise
        # reaches the tool but not the model — see middleware.py. A wrap hook,
        # not a node, so it leaves the 6 + 4N step budget alone.
        # Cast: both are generic over context, and the checker treats that
        # parameter as invariant against our PlannerContext.
        middleware=cast("Any", (TodoListMiddleware(), OperatorEditNote())),
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
