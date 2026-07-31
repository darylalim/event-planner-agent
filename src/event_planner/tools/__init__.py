"""Tool surface for the event planning agent."""

from event_planner.tools.bookings import hold_venue, send_invitations
from event_planner.tools.budget import estimate_budget
from event_planner.tools.catalog import check_availability, search_vendors, search_venues
from event_planner.tools.search import web_search

# Tools that spend money or contact guests. Gated behind human approval in
# agent.py — keep this list and the `interrupt_on` config in sync.
IRREVERSIBLE_TOOLS = ["hold_venue", "send_invitations"]

__all__ = [
    "IRREVERSIBLE_TOOLS",
    "check_availability",
    "estimate_budget",
    "hold_venue",
    "search_vendors",
    "search_venues",
    "send_invitations",
    "web_search",
]
