"""Tenant-isolation tests.

Each test here corresponds to a way one planner's data could reach another
planner's agent. They are separated from the harness tests because they guard a
different property: not "does the feature work" but "does the boundary hold".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from event_planner.agent import WORKSPACE, build_backend
from event_planner.cli import STATE_DIR, _check_db_outside_workspace
from event_planner.context import PlannerContext, memory_namespace


class _Runtime:
    def __init__(self, ctx):
        self.context = ctx


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
        ("a/b", "a b"),          # both sanitize to "a_b"
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


def test_namespace_components_are_store_safe():
    """StoreBackend rejects components outside this charset and raises mid-run."""
    for user_id in ["a b/c*d", "../../etc/passwd", "emoji🎉id", "alice@example.com"]:
        for component in _ns(user_id):
            assert component, "empty namespace component"
            assert all(ch.isalnum() or ch in "-_.@+:~" for ch in component), (
                f"unsafe component {component!r} from {user_id!r}"
            )


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
