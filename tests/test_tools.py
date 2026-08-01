"""Tool correctness.

Every case here is a bug that shipped: output that contradicted itself, or a
tool that returned confident success for input it should have refused. The
agent grounds its cost and availability claims in these return values, so a
wrong-but-plausible string propagates straight into advice given to a client.
"""

from __future__ import annotations

import pytest

from event_planner.tools import (
    check_availability,
    estimate_budget,
    hold_venue,
    search_venues,
)


def _call(tool, **kwargs) -> str:
    return tool.invoke(kwargs)


# --------------------------------------------------------------------------- #
# availability
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("form", ["2026-09-19", "20260919", "2026-W38-6"])
def test_booked_date_is_detected_in_every_iso_form(form):
    """`fromisoformat` accepts basic and week forms; comparison must normalize.

    Comparing the raw input against canonical booked_dates reported a booked
    venue as available while listing that same date as booked two lines below
    — one self-contradicting tool result.
    """
    result = _call(check_availability, venue_id="v-presidio-hall", event_date=form)
    assert "is NOT available" in result, result
    assert "2026-09-19" in result


def test_free_date_still_reads_as_available():
    result = _call(check_availability, venue_id="v-presidio-hall", event_date="2026-09-17")
    assert "IS available" in result


def test_unparseable_date_is_refused():
    assert "Could not parse" in _call(
        check_availability, venue_id="v-presidio-hall", event_date="next Thursday"
    )


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #


def test_zero_rate_ceiling_is_reported_not_hidden():
    """The filter treats 0 as a real ceiling, so the message must show it.

    A truthiness test dropped the clause and told the model only that nothing
    matched, hiding the constraint that caused the empty result.
    """
    result = _call(search_venues, city="San Francisco", min_capacity=50, max_day_rate_usd=0)
    assert "day rate <= $0" in result


def test_omitted_ceiling_is_not_mentioned():
    result = _call(search_venues, city="San Francisco", min_capacity=9999)
    assert "day rate" not in result


# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #


def test_marginal_cost_is_zero_below_a_vendor_minimum():
    """The tool used to print 'adding 20 guests costs nothing extra' and a
    per-guest marginal cost of $72.96 in the same output."""
    result = _call(
        estimate_budget,
        headcount=40,
        venue_total_usd=3200,
        catering_per_person_usd=52,
        catering_minimum_headcount=60,
        service_charge_pct=22,
    )
    marginal = next(l for l in result.splitlines() if "Marginal cost" in l)
    assert "$        0.00" in marginal or "0.00" in marginal.split("$")[-1]
    assert "already paid for" in result
    assert "beyond that each guest costs" in result


def test_marginal_cost_is_the_per_head_rate_at_or_above_the_minimum():
    result = _call(
        estimate_budget,
        headcount=80,
        venue_total_usd=3200,
        catering_per_person_usd=52,
        catering_minimum_headcount=60,
        service_charge_pct=22,
    )
    marginal = next(l for l in result.splitlines() if "Marginal cost" in l)
    assert "72.96" in marginal


@pytest.mark.parametrize(
    ("service", "contingency", "expect_service", "expect_contingency"),
    [(22.5, 12.5, "22.5%", "12.5%"), (22, 15, "22%", "15%")],
)
def test_percentage_labels_match_the_arithmetic(
    service, contingency, expect_service, expect_contingency
):
    """Labels were formatted :.0f while the maths used the full float, so a
    22.5% charge was billed at 22.5% and captioned '22%'."""
    result = _call(
        estimate_budget,
        headcount=50,
        venue_total_usd=1000,
        catering_per_person_usd=100,
        service_charge_pct=service,
        contingency_pct=contingency,
    )
    assert f"Service charge ({expect_service})" in result
    assert f"Contingency ({expect_contingency})" in result


def test_zero_headcount_is_refused():
    assert "greater than zero" in _call(
        estimate_budget, headcount=0, venue_total_usd=1, catering_per_person_usd=1
    )


# --------------------------------------------------------------------------- #
# booking — the tool that commits the client's money
# --------------------------------------------------------------------------- #


def test_hold_refuses_an_unknown_venue():
    """check_availability validates its venue_id; the tool that spends money
    must be at least as strict, or an approved hold lands on a hallucination."""
    result = _call(
        hold_venue,
        venue_id="v-does-not-exist",
        event_date="2026-09-17",
        headcount=50,
        total_cost_usd=1000.0,
        client_name="Acme",
    )
    assert result.startswith("REFUSED")
    assert "Provisional hold placed" not in result


def test_hold_refuses_a_date_the_venue_is_already_booked():
    result = _call(
        hold_venue,
        venue_id="v-presidio-hall",
        event_date="2026-09-19",
        headcount=50,
        total_cost_usd=1000.0,
        client_name="Acme",
    )
    assert result.startswith("REFUSED")
    assert "already booked" in result


def test_hold_refuses_a_headcount_over_capacity():
    result = _call(
        hold_venue,
        venue_id="v-dogpatch-studio",  # capacity 80
        event_date="2026-09-17",
        headcount=500,
        total_cost_usd=1000.0,
        client_name="Acme",
    )
    assert result.startswith("REFUSED")
    assert "capacity" in result


def test_hold_refuses_an_unparseable_date():
    result = _call(
        hold_venue,
        venue_id="v-loft-mission",
        event_date="whenever",
        headcount=50,
        total_cost_usd=1000.0,
        client_name="Acme",
    )
    assert result.startswith("REFUSED")


def test_valid_hold_still_succeeds_and_normalizes_the_date():
    result = _call(
        hold_venue,
        venue_id="v-loft-mission",
        event_date="20260917",
        headcount=50,
        total_cost_usd=8000.0,
        client_name="Acme",
    )
    assert "Provisional hold placed" in result
    assert "2026-09-17" in result
