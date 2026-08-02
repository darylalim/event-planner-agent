"""Tests for the Streamlit front end's logic layer.

The page itself is not exercised here — driving a Streamlit script needs a
runtime, and this suite stays offline. What is exercised is everything the page
delegates to `event_planner.webui`, which is where a second front end could
quietly diverge from the CLI on the decisions that matter.

The load-bearing test is `test_reject_matches_the_cli_byte_for_byte`. Two front
ends can now refuse a booking, and the wording of that refusal is behavioural:
a bare reason reads to the model as the tool erroring, and a failed tool invites
a retry where a human refusal must not be retried.
"""

from __future__ import annotations

import sqlite3

import pytest

from event_planner.agent import ALLOWED_DECISIONS
from event_planner.cli import _prompt_one
from event_planner.webui import (
    FALLBACK_DECISIONS,
    SUPPORTED_DECISIONS,
    approve_decision,
    download_name,
    edit_decision,
    message_text,
    open_persistence,
    parse_edited_args,
    pending_reviews,
    reject_decision,
    stored_items,
    tool_calls_of,
    unsupported_decisions,
)

ACTION = {
    "name": "hold_venue",
    "args": {"venue_id": "v-loft-mission", "headcount": 60},
}


class _Interrupt:
    """Stands in for `langgraph.types.Interrupt`, which is just a `.value` here."""

    def __init__(self, value):
        self.value = value


class _Message:
    """Minimal stand-in for a LangChain message."""

    def __init__(self, content, *, type="ai", name=None, tool_calls=None):
        self.content = content
        self.type = type
        self.name = name
        self.tool_calls = tool_calls or []


def _payload(actions, configs=None):
    payload = {"action_requests": actions}
    if configs is not None:
        payload["review_configs"] = configs
    return [_Interrupt(payload)]


# --------------------------------------------------------------------------- #
# the refusal path — must not drift from the CLI
# --------------------------------------------------------------------------- #


def test_reject_matches_the_cli_byte_for_byte(monkeypatch):
    """The two front ends must refuse a booking in exactly the same words.

    If either grows its own copy of the framing, this fails.
    """
    reason = "Client has not signed off on the deposit."
    queue = ["reject", reason]
    monkeypatch.setattr("builtins.input", lambda _prompt="": queue.pop(0))

    from_cli = _prompt_one(ACTION, ["approve", "edit", "reject"])
    assert reject_decision(reason) == from_cli


def test_rejection_reads_as_a_human_decision_not_a_tool_failure():
    message = reject_decision("Venue is over budget.")["message"]
    assert "human operator" in message.lower()
    assert "not be retried" in message.lower()
    assert "Venue is over budget." in message


def test_rejection_without_a_reason_still_explains_itself():
    assert "No reason given." in reject_decision("   ")["message"]


# --------------------------------------------------------------------------- #
# `respond` stays out
# --------------------------------------------------------------------------- #


def test_respond_is_not_a_supported_decision():
    """A free-text reply invites the model to read commentary as confirmation."""
    assert "respond" not in SUPPORTED_DECISIONS


def test_supported_decisions_do_not_exceed_what_the_agent_allows():
    assert set(SUPPORTED_DECISIONS) == set(ALLOWED_DECISIONS)


def test_an_allowed_but_unsupported_decision_is_reported_not_dropped():
    """Silently narrowing the operator's options is how the CLI's menu once hung."""
    assert unsupported_decisions(["approve", "edit", "reject", "respond"]) == ["respond"]


def test_nothing_is_reported_when_every_allowed_decision_is_supported():
    assert unsupported_decisions(list(ALLOWED_DECISIONS)) == []


# --------------------------------------------------------------------------- #
# decision payloads
# --------------------------------------------------------------------------- #


def test_approve_carries_no_arguments():
    assert approve_decision() == {"type": "approve"}


def test_edit_replaces_the_arguments_but_not_the_tool():
    decision = edit_decision(ACTION, {"headcount": 45})
    assert decision == {
        "type": "edit",
        "edited_action": {"name": "hold_venue", "args": {"headcount": 45}},
    }


def test_edit_matches_the_cli(monkeypatch):
    queue = ["edit", '{"headcount": 45}']
    monkeypatch.setattr("builtins.input", lambda _prompt="": queue.pop(0))

    from_cli = _prompt_one(ACTION, ["approve", "edit", "reject"])
    assert edit_decision(ACTION, {"headcount": 45}) == from_cli


@pytest.mark.parametrize("raw", ["[1, 2]", '"a string"', "42", "null"])
def test_edited_arguments_must_be_an_object(raw):
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_edited_args(raw)


def test_malformed_edited_arguments_explain_themselves():
    with pytest.raises(ValueError, match="Not valid JSON"):
        parse_edited_args("{headcount: 45}")


def test_well_formed_edited_arguments_parse():
    assert parse_edited_args('{"headcount": 45}') == {"headcount": 45}


# --------------------------------------------------------------------------- #
# interrupt normalisation
# --------------------------------------------------------------------------- #


def test_no_interrupt_means_no_review():
    assert pending_reviews(None) == []
    assert pending_reviews([]) == []


def test_one_review_is_produced_per_pending_action():
    """The middleware wants exactly one decision per action, in order."""
    actions = [ACTION, {"name": "send_invitations", "args": {"recipient_count": 60}}]
    configs = [{"allowed_decisions": ["approve", "reject"]}, {"allowed_decisions": ["approve"]}]

    reviews = pending_reviews(_payload(actions, configs))

    assert [action["name"] for action, _ in reviews] == ["hold_venue", "send_invitations"]
    assert [allowed for _, allowed in reviews] == [["approve", "reject"], ["approve"]]


def test_a_missing_review_config_falls_back_to_the_narrow_set():
    """Same fallback as the CLI: fail toward fewer options, never more."""
    reviews = pending_reviews(_payload([ACTION]))
    assert reviews[0][1] == list(FALLBACK_DECISIONS)
    assert "edit" not in reviews[0][1]


def test_a_short_review_config_list_still_falls_back():
    actions = [ACTION, {"name": "send_invitations", "args": {}}]
    reviews = pending_reviews(_payload(actions, [{"allowed_decisions": ["approve", "edit"]}]))
    assert reviews[1][1] == list(FALLBACK_DECISIONS)


def test_a_non_dict_interrupt_value_is_ignored_rather_than_raising():
    assert pending_reviews([_Interrupt("something unexpected")]) == []


# --------------------------------------------------------------------------- #
# message rendering
# --------------------------------------------------------------------------- #


def test_plain_string_content_round_trips():
    assert message_text(_Message("  hello  ")) == "hello"


def test_thinking_blocks_are_skipped_rather_than_flattened_to_nothing():
    """Adaptive thinking is on by default, so content arrives as mixed blocks."""
    content = [
        {"type": "thinking", "thinking": "the client said Thursday"},
        {"type": "text", "text": "That date is a Saturday."},
    ]
    assert message_text(_Message(content)) == "That date is a Saturday."


def test_a_message_with_only_tool_calls_renders_no_text():
    message = _Message([], tool_calls=[{"name": "search_venues", "args": {}}])
    assert message_text(message) == ""
    assert tool_calls_of(message) == [{"name": "search_venues", "args": {}}]


def test_a_message_without_tool_calls_reports_none():
    assert tool_calls_of(_Message("hi")) == []


# --------------------------------------------------------------------------- #
# downloads
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("/events/offsite-2026/brief.md", "events_offsite-2026_brief.md"),
        ("events/budget.md", "events_budget.md"),
        # Every leading dot goes, so a traversal-shaped key cannot come back as
        # a dotfile or a bare "..".
        ("../../etc/passwd", "_.._etc_passwd"),
        ("..", "event-file"),
        ("/", "event-file"),
        ("", "event-file"),
    ],
)
def test_download_names_are_flattened_and_never_empty(key, expected):
    """The key is chosen by the agent and ends up in a response header."""
    assert download_name(key) == expected


@pytest.mark.parametrize("key", ["/events/a/b.md", "../..", "a\\b", "x;rm -rf /"])
def test_download_names_carry_no_separators(key):
    name = download_name(key)
    assert "/" not in name
    assert "\\" not in name
    assert not name.startswith(".")


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def test_persistence_refuses_a_database_inside_the_agent_root():
    """Same guard the CLI applies to `--db`, reached through the web path."""
    from event_planner.agent import WORKSPACE

    with pytest.raises(ValueError, match="inside the agent's filesystem root"):
        open_persistence(WORKSPACE / "planner.sqlite")


def test_persistence_opens_a_usable_checkpointer_and_store(tmp_path):
    checkpointer, store = open_persistence(tmp_path / "nested" / "planner.sqlite")

    namespace = ("event_planner", "memories", "u", "alice-abc")
    store.put(namespace, "AGENTS.md", {"content": "hi"})

    item = store.get(namespace, "AGENTS.md")
    assert item is not None
    assert item.value == {"content": "hi"}
    assert checkpointer.conn is not None


def test_persistence_connections_are_usable_off_the_creating_thread(tmp_path):
    """`st.cache_resource` shares one instance across Streamlit's runner threads.

    Both classes serialise every statement behind an internal lock, which is why
    `check_same_thread=False` is safe — but only if it is actually set.
    """
    import threading

    checkpointer, store = open_persistence(tmp_path / "planner.sqlite")
    failures: list[sqlite3.Error] = []

    def touch():
        # Narrowly `sqlite3.Error`: cross-thread use of a connection opened with
        # `check_same_thread=True` raises `ProgrammingError`, and that specific
        # regression is the whole point of this test.
        try:
            store.put(("event_planner", "events", "u", "bob-def"), "brief.md", {"content": "x"})
            checkpointer.conn.execute("select 1").fetchone()
        except sqlite3.Error as exc:  # pragma: no cover - only on regression
            failures.append(exc)

    thread = threading.Thread(target=touch)
    thread.start()
    thread.join()

    assert failures == []


def test_persistence_leaves_the_store_in_autocommit(tmp_path):
    """`SqliteStore._cursor` issues its own BEGIN; implicit transactions nest."""
    _, store = open_persistence(tmp_path / "planner.sqlite")
    assert store.conn.isolation_level is None


def test_an_unidentified_caller_lists_nothing(tmp_path):
    """No user id means no cross-thread namespace to list, not a shared one."""
    _, store = open_persistence(tmp_path / "planner.sqlite")
    assert stored_items(store, None, "memories") == ((), ())


def test_stored_items_reads_back_what_the_agent_wrote(tmp_path):
    _, store = open_persistence(tmp_path / "planner.sqlite")
    from event_planner.context import namespace_for_user

    namespace = namespace_for_user("alice@example.com", "events")
    store.put(namespace, "/events/offsite/brief.md", {"content": "85 guests"})

    items, used = stored_items(store, "alice@example.com", "events")
    assert used == namespace
    assert [item.key for item in items] == ["/events/offsite/brief.md"]


def test_the_database_is_a_real_file_on_disk(tmp_path):
    """Memory that vanishes with the process is not cross-session memory."""
    db = tmp_path / "planner.sqlite"
    open_persistence(db)
    assert db.is_file()
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("select name from sqlite_master")}
    assert tables
