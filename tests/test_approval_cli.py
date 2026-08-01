"""Tests for the operator-facing approval prompt.

This is the code an operator uses to refuse a booking. Until now it had never
executed — the harness tests drive `Command(resume=...)` directly and skip the
terminal layer entirely, so a bug here would only appear the first time someone
actually tried to say no.
"""

from __future__ import annotations

from typing import Any

import pytest

from event_planner.cli import (
    _collect_decisions,
    _prompt_one,
    _resolve_choice,
    _unique_prefix,
)

ACTION = {
    "name": "hold_venue",
    "args": {"venue_id": "v-loft-mission", "headcount": 60},
}
ALL_FOUR = ["approve", "edit", "reject", "respond"]
DEFAULT = ["approve", "edit", "reject"]


@pytest.fixture
def answers(monkeypatch):
    """Feed scripted terminal input to `input()`."""

    def _install(*lines: str) -> None:
        queue = list(lines)

        def fake_input(_prompt: str = "") -> str:
            if not queue:
                raise EOFError
            return queue.pop(0)

        monkeypatch.setattr("builtins.input", fake_input)

    return _install


# --------------------------------------------------------------------------- #
# choice resolution
# --------------------------------------------------------------------------- #


def test_reject_is_reachable_when_respond_is_also_allowed():
    """Regression: 'reject' and 'respond' share a first letter.

    The original map was {d[0]: d for d in allowed}, so 'respond' overwrote
    'reject' and typing 'r' sent a free-text reply instead of refusing — which
    the model can read as consent. The worst possible way for this to fail.
    """
    assert _resolve_choice("rej", ALL_FOUR) == "reject"
    assert _resolve_choice("res", ALL_FOUR) == "respond"


def test_ambiguous_input_never_guesses():
    assert _resolve_choice("r", ALL_FOUR) is None


def test_unambiguous_single_letter_still_works():
    assert _resolve_choice("r", DEFAULT) == "reject"
    assert _resolve_choice("a", DEFAULT) == "approve"
    assert _resolve_choice("e", DEFAULT) == "edit"


@pytest.mark.parametrize("raw", ["", "x", "zzz", "appro ve"])
def test_unrecognised_input_resolves_to_nothing(raw):
    assert _resolve_choice(raw, DEFAULT) is None


def test_exact_word_always_wins():
    for option in ALL_FOUR:
        assert _resolve_choice(option, ALL_FOUR) == option


def test_unique_prefix_disambiguates():
    assert _unique_prefix("reject", ALL_FOUR) == "rej"
    assert _unique_prefix("respond", ALL_FOUR) == "res"
    assert _unique_prefix("approve", ALL_FOUR) == "a"
    assert _unique_prefix("reject", DEFAULT) == "r"


# --------------------------------------------------------------------------- #
# prompting
# --------------------------------------------------------------------------- #


def test_approve(answers):
    answers("a")
    assert _prompt_one(ACTION, DEFAULT) == {"type": "approve"}


def test_reject_carries_the_operator_reason(answers):
    answers("r", "Budget not signed off.")
    decision = _prompt_one(ACTION, DEFAULT)
    assert decision["type"] == "reject"
    assert "Budget not signed off." in decision["message"]


def test_reject_without_a_reason_still_explains_itself(answers):
    answers("r", "")
    assert "No reason given." in _prompt_one(ACTION, DEFAULT)["message"]


def test_rejection_reads_as_a_human_decision_not_a_tool_failure(answers):
    """The reason reaches the model as the tool's return value.

    A bare reason gets reported as "the tool returned an error" — observed
    live. That mislabels a refusal as a malfunction, and the two warrant
    opposite responses: a failed tool invites a retry, a refusal must not be
    retried. The message has to say who decided and that nothing happened.
    """
    answers("r", "Finance has not signed off.")
    message = _prompt_one(ACTION, DEFAULT)["message"]
    lowered = message.lower()
    assert "operator" in lowered, "does not attribute the decision to a human"
    assert "not performed" in lowered, "does not state the action did not happen"
    assert "retried" in lowered, "does not warn against retrying"


def test_eof_rejection_is_also_framed_as_a_decision(answers):
    answers()
    message = _prompt_one(ACTION, DEFAULT)["message"]
    assert "operator" in message.lower()
    assert "not performed" in message.lower()


def test_ambiguous_then_specific_reaches_reject(answers):
    """'r' is ambiguous with respond allowed; the retry must land on reject."""
    answers("r", "rej", "Not yet.")
    decision = _prompt_one(ACTION, ALL_FOUR)
    assert decision["type"] == "reject"


def test_edit_rewrites_arguments(answers):
    answers("e", '{"venue_id": "v-presidio-hall", "headcount": 45}')
    decision = _prompt_one(ACTION, DEFAULT)
    assert decision["type"] == "edit"
    assert decision["edited_action"]["name"] == "hold_venue"
    assert decision["edited_action"]["args"]["headcount"] == 45


def test_edit_survives_malformed_json(answers):
    """A typo must re-prompt, not crash and not silently approve."""
    answers("e", "{not json", "e", '{"headcount": 45}')
    decision = _prompt_one(ACTION, DEFAULT)
    assert decision["edited_action"]["args"] == {"headcount": 45}


def test_edit_rejects_non_object_json(answers):
    answers("e", "[1, 2, 3]", "e", '{"headcount": 45}')
    decision = _prompt_one(ACTION, DEFAULT)
    assert decision["edited_action"]["args"] == {"headcount": 45}


def test_blank_edit_returns_to_the_menu(answers):
    answers("e", "", "a")
    assert _prompt_one(ACTION, DEFAULT) == {"type": "approve"}


def test_eof_fails_closed(answers):
    """No operator present must mean refusal, never silent approval."""
    answers()  # empty queue -> EOFError on first read
    decision = _prompt_one(ACTION, DEFAULT)
    assert decision["type"] == "reject"


# --------------------------------------------------------------------------- #
# multi-action payloads
# --------------------------------------------------------------------------- #


class _Interrupt:
    def __init__(self, value: Any) -> None:
        self.value = value


def test_one_decision_is_collected_per_pending_action(answers):
    """The middleware raises unless decisions match interrupted calls exactly."""
    answers("a", "r", "Not this one.")
    payload = _Interrupt(
        {
            "action_requests": [
                {"name": "hold_venue", "args": {"venue_id": "v-1"}},
                {"name": "send_invitations", "args": {"recipient_count": 60}},
            ],
            "review_configs": [
                {"action_name": "hold_venue", "allowed_decisions": DEFAULT},
                {"action_name": "send_invitations", "allowed_decisions": DEFAULT},
            ],
        }
    )
    decisions = _collect_decisions([payload])
    assert [d["type"] for d in decisions] == ["approve", "reject"]


def test_missing_review_config_falls_back_to_safe_decisions(answers):
    answers("a")
    payload = _Interrupt(
        {"action_requests": [{"name": "hold_venue", "args": {}}], "review_configs": []}
    )
    assert _collect_decisions([payload]) == [{"type": "approve"}]
