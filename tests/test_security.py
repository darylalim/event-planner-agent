"""Tenant-isolation tests.

Each test here corresponds to a way one planner's data could reach another
planner's agent. They are separated from the harness tests because they guard a
different property: not "does the feature work" but "does the boundary hold".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from event_planner.agent import WORKSPACE, build_backend
from event_planner.cli import STATE_DIR, _check_db_outside_workspace
from event_planner.context import PlannerContext, events_namespace, memory_namespace


class _Runtime:
    def __init__(self, ctx):
        self.context = ctx


class _ScriptedWriter(GenericFakeChatModel):
    """Fake model that plays back a fixed reply sequence."""

    replies: list[AIMessage] = []

    def __init__(self, replies, **kwargs):
        super().__init__(messages=iter([]), **kwargs)
        self.replies = list(replies)

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        reply = self.replies.pop(0) if self.replies else AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=reply)])


def _ns(user_id):
    return memory_namespace(_Runtime(PlannerContext(user_id=user_id)))


# --------------------------------------------------------------------------- #
# the state database must be unreachable by the agent
# --------------------------------------------------------------------------- #


def test_state_dir_is_outside_the_agent_filesystem_root():
    """The agent has ls/read/glob/grep over its root.

    A checkpoint+memory database kept inside that root would let any session
    read every other user's memories and every other thread's history straight
    out of the raw file, bypassing namespacing entirely.
    """
    state = STATE_DIR.resolve()
    root = WORKSPACE.resolve()
    assert state != root
    assert root not in state.parents, f"{state} is readable by the agent"


def test_agent_cannot_see_a_state_directory_in_its_listing():
    backend = build_backend()
    entries = backend.ls("/").entries or []
    paths = {e["path"] for e in entries}
    assert not any(p.startswith("/.state") for p in paths), (
        f"state directory exposed to the agent: {paths}"
    )


def test_operator_supplied_db_inside_workspace_is_rejected():
    """`--db` must not be able to reintroduce the hole."""
    with pytest.raises(ValueError, match="inside the agent's filesystem root"):
        _check_db_outside_workspace(WORKSPACE / "planner.sqlite")
    with pytest.raises(ValueError, match="inside the agent's filesystem root"):
        _check_db_outside_workspace(WORKSPACE / "nested" / "deep" / "planner.sqlite")


def test_operator_supplied_db_outside_workspace_is_accepted():
    _check_db_outside_workspace(STATE_DIR / "planner.sqlite")
    _check_db_outside_workspace(Path("/tmp/planner.sqlite"))


# --------------------------------------------------------------------------- #
# namespace mapping must be injective
# --------------------------------------------------------------------------- #


def test_distinct_users_get_distinct_namespaces():
    assert _ns("alice@example.com") != _ns("bob@example.com")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("a/b", "a b"),  # both sanitize to "a_b"
        ("a*b", "a?b"),
        ("alice/x", "alice x"),
        ("../admin", "..%admin"),
    ],
)
def test_ids_that_sanitize_identically_still_do_not_collide(left, right):
    """Sanitization by replacement is lossy, so it cannot be the whole mapping.

    Without a digest of the raw id, these pairs would share a memory namespace
    — a cross-tenant leak triggered by nothing more exotic than a space.
    """
    assert _ns(left) != _ns(right), f"{left!r} and {right!r} collided"


_HOSTILE_IDS = [
    "a b/c*d",
    "../../etc/passwd",
    "emoji🎉id",
    "alice@example.com",  # the period here is the one that bit us
    "acme-planner",
    "user.with.dots",
    "-leading-hyphen-",
    "x" * 300,
    "default",
]


@pytest.mark.parametrize("user_id", _HOSTILE_IDS)
def test_namespaces_satisfy_both_validators(user_id):
    """Two layers validate namespaces and they disagree.

    `deepagents` permits periods; `langgraph.store.base` rejects them. Checking
    only the permissive one passes construction, reads, and `ls`, then raises
    InvalidNamespaceError on the first *write* — which is exactly how this
    escaped into a live run. Assert against both real validators, not a
    docstring.
    """
    from deepagents.backends.store import _validate_namespace as deepagents_validate
    from langgraph.store.base import _validate_namespace as langgraph_validate

    namespace = _ns(user_id)
    deepagents_validate(namespace)  # raises on failure
    langgraph_validate(namespace)  # raises on failure
    assert namespace[0] != "langgraph", "reserved root label"
    assert all(namespace), "empty namespace label"


def test_anonymous_and_thread_namespaces_also_validate():
    from langgraph.store.base import _validate_namespace as langgraph_validate

    langgraph_validate(memory_namespace(_Runtime(None)))


def test_memory_write_round_trips_through_a_real_store():
    """End-to-end proof, not a charset assertion.

    A namespace can satisfy every regex we know about and still be rejected by
    the store at write time. The only convincing check is writing through the
    real backend and reading it back.
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore

    from event_planner.agent import build_agent

    store = InMemoryStore()
    written = {
        "name": "write_file",
        "args": {
            "file_path": "/memories/AGENTS.md",
            "content": "Acme prefers venues with step-free access.",
        },
        "id": "mem-1",
    }
    model = _ScriptedWriter(
        [
            AIMessage(content="Saving.", tool_calls=[written]),
            AIMessage(content="Saved."),
        ]
    )
    graph = build_agent(model=model, checkpointer=InMemorySaver(), store=store)

    result = graph.invoke(
        {"messages": [{"role": "user", "content": "Remember that."}]},
        config={"configurable": {"thread_id": "mem-thread"}},
        context=PlannerContext(user_id="alice@example.com"),
    )

    tool_msgs = [m for m in result["messages"] if getattr(m, "name", None) == "write_file"]
    assert tool_msgs, "write_file never ran"
    assert "error" not in str(tool_msgs[-1].content).lower(), tool_msgs[-1].content

    stored = list(store.search(_ns("alice@example.com")))
    assert stored, "nothing persisted to the store"


# --------------------------------------------------------------------------- #
# missing identity must not fail open
# --------------------------------------------------------------------------- #


def test_anonymous_callers_do_not_land_in_a_shared_user_bucket():
    """A missing user_id must not merge anonymous callers together.

    The dangerous version of this fell back to a single "default" namespace, so
    every context-less invocation inherited everyone else's memory.
    """
    anonymous = memory_namespace(_Runtime(None))
    identified = _ns("alice@example.com")
    assert anonymous != identified
    # Anonymous callers are tagged distinctly from identified ones, so no
    # identified user can ever occupy the anonymous bucket.
    assert anonymous[2] != "u"


def test_explicit_default_user_is_distinct_from_anonymous():
    """Someone who explicitly chooses "default" is identified, not anonymous."""
    assert _ns("default") != memory_namespace(_Runtime(None))


def test_namespace_survives_a_runtime_without_context():
    assert memory_namespace(object())  # must not raise


@pytest.mark.parametrize(
    "ctx", [PlannerContext(), PlannerContext(**{}), PlannerContext(user_id=None)]
)
def test_a_context_without_an_id_is_not_treated_as_an_identified_user(ctx):
    """The default must not be a truthy placeholder.

    `user_id: str = "default"` silently defeated the whole fail-closed design:
    every caller that omitted an id took the *identified* branch and shared one
    bucket. LangGraph builds the dataclass from `context={}`, so this was the
    common path, not an edge case. The only fallback test passed a runtime with
    no context at all — a state the production path never reaches.
    """
    namespace = memory_namespace(_Runtime(ctx))
    assert "u" not in namespace, f"unidentified caller landed in a user bucket: {namespace}"


def test_unidentified_callers_are_separated_by_thread(monkeypatch):
    import event_planner.context as ctx_mod

    seen = []
    for thread in ("thread-a", "thread-b"):
        monkeypatch.setattr(ctx_mod, "_current_thread_id", lambda t=thread: t)
        seen.append(memory_namespace(_Runtime(PlannerContext())))
    assert seen[0] != seen[1], "different threads shared a namespace"


# --------------------------------------------------------------------------- #
# event files must not leak between users
# --------------------------------------------------------------------------- #


def test_event_files_are_scoped_per_user():
    """Event files carry client names, headcounts, guest details, and budgets.

    They used to live on the single shared FilesystemBackend root, where the
    agent's ls/read/glob/grep could reach another planner's brief.
    """
    a = events_namespace(_Runtime(PlannerContext(user_id="alice@example.com")))
    b = events_namespace(_Runtime(PlannerContext(user_id="bob@example.com")))
    assert a != b


def test_events_and_memories_do_not_share_a_namespace():
    who = _Runtime(PlannerContext(user_id="alice@example.com"))
    assert events_namespace(who) != memory_namespace(who)


def test_user_data_paths_are_routed_to_per_user_stores():
    """`/events/` and `/memories/` must not resolve to the shared filesystem.

    The shared root is a single static path — backend factories were removed in
    deepagents 0.7 — and the agent has ls/read/glob/grep over it, so anything
    left there is readable by every session.
    """
    from deepagents.backends import StoreBackend

    routes = build_backend().routes
    for path in ("/events/", "/memories/"):
        assert path in routes, f"{path} falls through to the shared filesystem"
        assert isinstance(routes[path], StoreBackend), f"{path} is not store-backed"


def test_shared_filesystem_root_holds_only_reference_material():
    """Whatever sits on disk under the root is visible to every user."""
    on_disk = {p.name for p in WORKSPACE.iterdir() if not p.name.startswith(".")}
    assert on_disk <= {"skills"}, f"user data on the shared root: {on_disk - {'skills'}}"


def test_root_listing_has_no_duplicate_entries():
    """`workspace/memories` was created on disk and then permanently shadowed
    by the /memories/ route, so it appeared twice and anything written to the
    on-disk copy was unreadable."""
    paths = [e["path"] for e in (build_backend().ls("/").entries or [])]
    assert len(paths) == len(set(paths)), f"duplicate entries: {paths}"


# --------------------------------------------------------------------------- #
# gate configuration
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# /export writes agent-chosen paths to disk
# --------------------------------------------------------------------------- #


@pytest.fixture
def exporter(tmp_path):
    """A store plus an export destination, for exercising `_export`."""
    from langgraph.store.sqlite import SqliteStore

    from event_planner.cli import _export
    from event_planner.context import namespace_for_user

    db = str(tmp_path / "export.sqlite")

    def _run(user_id: str, entries: dict[str, str]):
        with SqliteStore.from_conn_string(db) as store:
            store.setup()
            namespace = namespace_for_user(user_id, "events")
            for key, content in entries.items():
                store.put(namespace, key, {"content": content})
            _export(store, user_id, tmp_path / "exports")
        escaped = [
            p
            for p in tmp_path.rglob("*")
            if p.is_file()
            and p.suffix in {".txt", ".md"}
            and (tmp_path / "exports") not in p.parents
        ]
        exported = [p for p in (tmp_path / "exports").rglob("*") if p.is_file()]
        return exported, escaped

    return _run


def test_export_refuses_traversal_in_an_agent_chosen_path(exporter):
    """Store keys are paths the *agent* picked, so they are untrusted here.

    A key of "/events/../../../x" escaped the export directory and wrote
    outside it — silently, since the escaped file then sat outside the tree
    the function reports on.
    """
    exported, escaped = exporter(
        "alice",
        {
            "/events/../../../OUTSIDE.txt": "escaped",
            "/events/ok/brief.md": "legit",
        },
    )
    assert not escaped, f"wrote outside the export directory: {escaped}"
    assert [p.name for p in exported] == ["brief.md"]


def test_export_of_an_absolute_looking_key_stays_inside(tmp_path, exporter):
    """A key like "/etc/passwd.txt" must land under the export tree, not at /."""
    exported, escaped = exporter("alice", {"/etc/passwd.txt": "nope"})
    assert not escaped
    base = tmp_path / "exports"
    assert exported, "file was dropped entirely"
    for path in exported:
        assert base in path.parents, f"{path} is outside {base}"


def test_export_does_not_use_the_operator_id_as_a_raw_path_segment(exporter):
    """`--user ../../x` must not relocate the export tree."""
    exported, escaped = exporter("../../ESCAPED", {"/events/a.md": "x"})
    assert not escaped, f"--user escaped the export directory: {escaped}"
    assert exported, "legitimate file was not exported"


def test_export_writes_ordinary_files_unchanged(exporter):
    exported, _ = exporter("alice", {"/events/trip/brief.md": "hello"})
    assert len(exported) == 1
    assert exported[0].read_text() == "hello"


def test_every_irreversible_tool_is_gated():
    """Two hand-maintained copies of "which tools spend money" means adding a
    third booking tool to one and not the other ships it un-gated."""
    from event_planner.agent import INTERRUPT_ON
    from event_planner.tools import IRREVERSIBLE_TOOLS

    assert set(INTERRUPT_ON) == set(IRREVERSIBLE_TOOLS)
    for config in INTERRUPT_ON.values():
        assert "reject" in config["allowed_decisions"]
