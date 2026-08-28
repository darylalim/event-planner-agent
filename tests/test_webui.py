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
    markdown_safe,
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
# costs must render as costs
# --------------------------------------------------------------------------- #


def test_two_costs_on_one_line_do_not_become_latex():
    """`st.markdown` reads `$...$` as LaTeX, and planner prose is full of costs.

    Verbatim from the live `hold_venue` recommendation this front end gates: the
    span between the two amounts was swallowed and re-set as italic mathematics,
    taking the figures the operator is asked to check with it.
    """
    rendered = markdown_safe("Venue $3,200 · AV $1,900 · Catering $4,050")
    assert rendered == r"Venue \$3,200 · AV \$1,900 · Catering \$4,050"
    # Nothing left that Streamlit would pair off into a maths span.
    assert "$" not in rendered.replace(r"\$", "")


def test_the_ceiling_comparison_survives():
    """The single most load-bearing sentence in an approval."""
    assert markdown_safe("$10,281 — $1,719 under your $12,000 ceiling") == (
        r"\$10,281 — \$1,719 under your \$12,000 ceiling"
    )


def test_an_already_escaped_dollar_is_left_alone():
    """Escaping twice would show the operator a stray backslash."""
    assert markdown_safe(r"\$3,200") == r"\$3,200"


def test_text_without_costs_is_untouched():
    assert markdown_safe("Dogpatch Studio, Thursday 12 March 2026.") == (
        "Dogpatch Studio, Thursday 12 March 2026."
    )


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


def _real_interrupt(scripted):
    """Drive the real graph to a real approval interrupt, offline.

    Every other test here hands `pending_reviews` a payload written in this file,
    which can only confirm the fixture matches itself. The middleware, not the
    model, builds the interrupt — so a scripted model is enough to get the
    genuine shape out of the installed package.
    """
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore

    from event_planner.agent import build_agent
    from event_planner.context import PlannerContext

    hold = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "hold_venue",
                "args": {
                    "venue_id": "v-loft-mission",
                    "event_date": "2026-09-19",
                    "headcount": 60,
                    "total_cost_usd": 18000.0,
                    "client_name": "Acme",
                },
                "id": "call_1",
            }
        ],
    )
    graph = build_agent(model=scripted(hold), checkpointer=InMemorySaver(), store=InMemoryStore())
    config = {"configurable": {"thread_id": "real-payload"}, "recursion_limit": 200}
    graph.invoke(
        {"messages": [{"role": "user", "content": "Book it."}]},
        config=config,
        context=PlannerContext(user_id="probe@example.com"),
    )
    return graph.get_state(config)


def test_the_real_middleware_payload_parses(scripted):
    """What the page reads has to match what the package actually emits.

    Caught two things the hand-written fixtures had wrong: `review_configs`
    entries also carry an `action_name`, and `description` is generated
    boilerplate repeating the tool name and a dict repr of the args rather than
    prose meant for a human.
    """
    snapshot = _real_interrupt(scripted)
    reviews = pending_reviews(snapshot.interrupts)

    assert len(reviews) == 1
    action, allowed = reviews[0]
    assert action["name"] == "hold_venue"
    assert action["args"]["headcount"] == 60
    assert allowed == ["approve", "edit", "reject"]


def test_widget_identity_works_against_a_real_snapshot(scripted):
    """`review_token` depends on a checkpoint id a real snapshot must supply."""
    from event_planner.webui import review_token

    snapshot = _real_interrupt(scripted)
    action, _ = pending_reviews(snapshot.interrupts)[0]

    assert snapshot.config["configurable"]["checkpoint_id"]
    assert review_token(snapshot.config, 0, action) != review_token({}, 0, action)


def test_a_real_interrupt_also_leaves_next_set(scripted):
    """Which is why the page checks for reviews before offering to resume.

    A pending approval and a stranded turn both show `next`; only the decision
    tells them apart.
    """
    snapshot = _real_interrupt(scripted)
    assert snapshot.next
    assert snapshot.interrupts


# --------------------------------------------------------------------------- #
# widget identity for a pending approval
# --------------------------------------------------------------------------- #

SEND = {"name": "send_invitations", "args": {"recipient_count": 250}}


def _token(checkpoint, index=0, action=ACTION):
    from event_planner.webui import review_token

    return review_token({"configurable": {"checkpoint_id": checkpoint}}, index, action)


def test_widget_identity_is_stable_within_one_pending_approval():
    """It has to survive the reruns that happen while a decision is being made.

    Selecting `approve` reruns the page; if the key moved, the selection would be
    lost and the submit button could never enable.
    """
    assert _token("ck-7") == _token("ck-7")


def test_widget_identity_changes_when_the_checkpoint_advances():
    """The bug this exists to prevent, at its root.

    Resolving one approval advances the checkpoint, so the next interrupt's
    widgets must not be the same ones — otherwise Streamlit restores the previous
    decision and the new action renders pre-approved.
    """
    assert _token("ck-7") != _token("ck-8")


def test_widget_identity_changes_with_the_action():
    assert _token("ck-7", action=ACTION) != _token("ck-7", action=SEND)


def test_widget_identity_changes_with_the_arguments():
    other = {"name": "hold_venue", "args": {"venue_id": "v-loft-mission", "headcount": 45}}
    assert _token("ck-7", action=ACTION) != _token("ck-7", action=other)


def test_widget_identity_changes_per_pending_action():
    """Two actions in one interrupt share a checkpoint, so index must separate."""
    assert _token("ck-7", index=0) != _token("ck-7", index=1)


def test_widget_identity_survives_a_config_without_a_checkpoint():
    """Falls back to action identity rather than raising or collapsing to one key."""
    from event_planner.webui import review_token

    assert review_token(None, 0, ACTION) != review_token(None, 0, SEND)
    assert review_token({}, 0, ACTION) == review_token(None, 0, ACTION)


def test_widget_identity_is_a_plain_key_safe_string():
    token = _token("ck-7")
    assert token.isalnum()
    assert len(token) == 16


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


# --------------------------------------------------------------------------- #
# packaging — the front end must not ride along into the deployment image
# --------------------------------------------------------------------------- #


def test_the_checkout_warning_is_silent_in_a_checkout():
    """It must not fire on a normal `uv run event-planner`."""
    from event_planner.cli import checkout_warning

    assert checkout_warning() is None


def test_the_checkout_warning_names_only_the_knob_the_cli_reads(monkeypatch, tmp_path):
    """`EVENT_PLANNER_DB` is the page's knob; this process never reads it.

    Naming it would be `_check_db_outside_workspace(..., knob=...)` in reverse —
    telling an operator to set something that changes nothing for them.
    """
    from event_planner import cli

    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    warning = cli.checkout_warning()
    assert warning is not None
    assert str(tmp_path) in warning
    assert "--db" in warning
    assert "EVENT_PLANNER_DB" not in warning


def test_both_front_ends_share_one_checkout_warning():
    """The page must show the CLI's note, not an equivalent of its own."""
    from event_planner import cli, webui

    assert webui.checkout_warning is cli.checkout_warning


def _pyproject():
    import tomllib

    from event_planner.cli import PROJECT_ROOT

    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_streamlit_is_an_extra_not_a_core_dependency():
    """`langgraph.json` installs a plain `.`, so a core dep ships to the platform.

    The deployed graph never imports Streamlit, but carrying it there pulled in
    ~27 transitive packages — pandas, pyarrow, altair, pydeck. A bare
    `uv add streamlit` would silently put it back.
    """
    project = _pyproject()["project"]

    assert "streamlit" not in " ".join(project["dependencies"])
    assert "streamlit" in " ".join(project["optional-dependencies"]["web"])


def test_the_dev_group_pulls_the_web_extra_in():
    """CI runs a plain `uv sync --locked`, and the suite drives the real page.

    Without this self-reference the AppTest tests cannot import Streamlit and CI
    fails on a checkout that looks correctly configured.
    """
    dev = " ".join(_pyproject()["dependency-groups"]["dev"])
    assert "event-planner-agent[web]" in dev


# --------------------------------------------------------------------------- #
# shared with the CLI rather than copied
# --------------------------------------------------------------------------- #


def test_the_shared_helpers_are_the_clis_own_objects():
    """Not equivalent implementations — the same ones, so they cannot drift.

    `cli._stored`'s concrete typing is load-bearing (callers reach `item.key`,
    and `cli._export` treats that key as untrusted input), and `_brief_args`
    owns the truncation arithmetic.
    """
    from event_planner import cli
    from event_planner.webui import brief_args

    assert stored_items is cli._stored
    assert brief_args is cli._brief_args


def test_both_front_ends_apply_the_same_credential_test(monkeypatch):
    from event_planner.cli import _check_credentials
    from event_planner.webui import credentials_problem

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _check_credentials() == credentials_problem()


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_required_credential_counts_as_missing(monkeypatch, value):
    from event_planner.webui import credentials_problem

    monkeypatch.setenv("ANTHROPIC_API_KEY", value)
    assert "ANTHROPIC_API_KEY" in (credentials_problem() or "")


def test_a_present_required_credential_reports_nothing(monkeypatch):
    from event_planner.webui import credentials_problem

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    assert credentials_problem() is None


def test_the_optional_credential_note_appears_only_when_it_is_missing(monkeypatch):
    from event_planner.webui import degraded_capability_note

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert "TAVILY_API_KEY" in (degraded_capability_note() or "")

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-not-a-real-key")
    assert degraded_capability_note() is None


# --------------------------------------------------------------------------- #
# the error message names the knob the operator actually set
# --------------------------------------------------------------------------- #


def test_the_web_path_names_its_environment_variable():
    """Telling a browser operator to fix `--db` points at a flag they cannot pass."""
    from event_planner.agent import WORKSPACE
    from event_planner.webui import UnsafeDatabaseLocation

    with pytest.raises(UnsafeDatabaseLocation, match="EVENT_PLANNER_DB"):
        open_persistence(WORKSPACE / "planner.sqlite")


def test_the_cli_path_still_names_its_own_flag():
    from event_planner.agent import WORKSPACE
    from event_planner.cli import _check_db_outside_workspace

    with pytest.raises(ValueError, match=r"\-\-db"):
        _check_db_outside_workspace(WORKSPACE / "planner.sqlite")


def test_the_unsafe_location_error_is_still_a_value_error():
    """Existing callers catch `ValueError`; the subclass only adds a name."""
    from event_planner.webui import UnsafeDatabaseLocation

    assert issubclass(UnsafeDatabaseLocation, ValueError)


# --------------------------------------------------------------------------- #
# connections are shared with the CLI, and reclaimed
# --------------------------------------------------------------------------- #


def test_persistence_uses_wal_so_the_front_ends_do_not_lock_each_other(tmp_path):
    """Both are documented as sharing one file; rollback-journal mode blocks."""
    _, store = open_persistence(tmp_path / "planner.sqlite")
    mode = store.conn.execute("pragma journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_closing_persistence_closes_both_connections(tmp_path):
    """`st.cache_resource` evicts entries but does not close what it drops."""
    from event_planner.webui import close_persistence

    checkpointer, store = open_persistence(tmp_path / "planner.sqlite")
    close_persistence(checkpointer, store)

    for conn in (checkpointer.conn, store.conn):
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("select 1")


def test_closing_persistence_twice_does_not_raise(tmp_path):
    """It runs from a cache-release callback, where a raise is a page error."""
    from event_planner.webui import close_persistence

    checkpointer, store = open_persistence(tmp_path / "planner.sqlite")
    close_persistence(checkpointer, store)
    close_persistence(checkpointer, store)


def test_closing_persistence_tolerates_objects_without_connections():
    from event_planner.webui import close_persistence

    close_persistence(None, None)


def test_the_database_is_a_real_file_on_disk(tmp_path):
    """Memory that vanishes with the process is not cross-session memory."""
    db = tmp_path / "planner.sqlite"
    open_persistence(db)
    assert db.is_file()
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("select name from sqlite_master")}
    assert tables
