"""Booking and outreach actions — stubbed, and gated behind human approval.

These are the tools that would spend the client's money or put mail in a guest's
inbox. They are stubs today, but they are wired into `interrupt_on` in
`agent.py` so the approval path is exercised now rather than being retrofitted
on the day the stubs are swapped for real APIs.
"""

from __future__ import annotations

import hashlib
from datetime import date as _date

from langchain.tools import tool

from event_planner.tools.catalog import is_booked, known_venue_ids, venue_by_id


def _reference(prefix: str, *parts: str) -> str:
    """Deterministic pseudo-reference so repeated runs are diffable."""
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:8].upper()
    return f"{prefix}-{digest}"


@tool
def hold_venue(
    venue_id: str,
    event_date: str,
    headcount: int,
    total_cost_usd: float,
    client_name: str,
) -> str:
    """Place a provisional hold on a venue. Requires human approval.

    Args:
        venue_id: Venue id from `search_venues`.
        event_date: ISO date, e.g. "2026-09-19".
        headcount: Confirmed guest count for the booking.
        total_cost_usd: Total the client is committing to, including the venue
            deposit and any confirmed vendor costs.
        client_name: Who the hold is under.

    Returns a hold reference and the terms. A hold is not a booking, but it does
    start a deposit clock — confirm the date and cost before calling this.
    """
    # Validate before returning a confident success string. `check_availability`
    # rejects an unknown venue_id; the tool that commits the client's money must
    # be at least as strict, or an approved hold can land on a hallucinated
    # venue or a date the venue is already booked — and the operator approving
    # it sees only well-formed confirmation text.
    venue = venue_by_id(venue_id)
    if venue is None:
        return (
            f"REFUSED: unknown venue_id '{venue_id}'. No hold was placed.\n"
            f"Known ids: {', '.join(known_venue_ids())}"
        )
    try:
        canonical = _date.fromisoformat(event_date).isoformat()
    except ValueError:
        return (
            f"REFUSED: could not parse event_date '{event_date}'. No hold was "
            f"placed. Use ISO format, e.g. 2026-09-19."
        )
    if is_booked(venue_id, canonical):
        return (
            f"REFUSED: {venue['name']} is already booked on {canonical}. "
            f"No hold was placed. Re-check availability and pick another date."
        )
    if headcount > venue["capacity"]:
        return (
            f"REFUSED: {headcount} guests exceeds {venue['name']}'s capacity of "
            f"{venue['capacity']}. No hold was placed."
        )
    if total_cost_usd <= 0:
        return f"REFUSED: total_cost_usd must be positive, got {total_cost_usd}."

    ref = _reference("HOLD", venue_id, canonical, client_name)
    deposit = total_cost_usd * 0.25
    return (
        f"[STUB] Provisional hold placed.\n"
        f"  Reference:   {ref}\n"
        f"  Venue:       {venue['name']} ({venue_id})\n"
        f"  Date:        {canonical}\n"
        f"  Headcount:   {headcount}\n"
        f"  Client:      {client_name}\n"
        f"  Total quoted: ${total_cost_usd:,.2f}\n"
        f"  Deposit due:  ${deposit:,.2f} within 5 business days\n"
        f"  Hold expires: 14 days from today unless the deposit is received.\n"
        f"No real booking was made — this is a stub."
    )


@tool
def send_invitations(
    event_name: str,
    event_date: str,
    venue_name: str,
    recipient_count: int,
    rsvp_deadline: str,
) -> str:
    """Send invitations to the guest list. Requires human approval.

    Args:
        event_name: Name of the event as guests will see it.
        event_date: ISO date of the event.
        venue_name: Venue name as guests will see it.
        recipient_count: How many invitations will go out.
        rsvp_deadline: ISO date by which guests must reply.

    Returns a send reference. Invitations cannot be recalled once sent — verify
    the date, venue, and spelling of the event name before calling this.
    """
    ref = _reference("SEND", event_name, event_date, str(recipient_count))
    return (
        f"[STUB] Invitations queued.\n"
        f"  Reference:  {ref}\n"
        f"  Event:      {event_name}\n"
        f"  Date:       {event_date}\n"
        f"  Venue:      {venue_name}\n"
        f"  Recipients: {recipient_count}\n"
        f"  RSVP by:    {rsvp_deadline}\n"
        f"No real email was sent — this is a stub."
    )
