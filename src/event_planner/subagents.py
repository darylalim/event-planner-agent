"""Subagent roster.

Each subagent gets a narrow tool set and its own skills. Skills are NOT
inherited from the orchestrator — a subagent that needs one must list it
explicitly, which is why `venue-researcher` and `budget-analyst` repeat paths
that also appear in the orchestrator's `skills`.

The point of delegating here is context isolation: comparing eight venues
burns a lot of tokens on listings the orchestrator never needs to see again.
The subagent does that work in its own context and reports back a shortlist.
"""

from __future__ import annotations

from deepagents import SubAgent

from event_planner.prompts import (
    BUDGET_ANALYST_PROMPT,
    VENDOR_RESEARCHER_PROMPT,
    VENUE_RESEARCHER_PROMPT,
)
from event_planner.tools import (
    check_availability,
    estimate_budget,
    search_vendors,
    search_venues,
    web_search,
)

VENUE_RESEARCHER: SubAgent = {
    "name": "venue-researcher",
    "description": (
        "Shortlists and compares event venues. Give it the city, date, "
        "headcount, budget ceiling, and the file path to write the comparison "
        "to. Returns a ranked shortlist with trade-offs."
    ),
    "system_prompt": VENUE_RESEARCHER_PROMPT,
    "tools": [search_venues, check_availability, web_search],
    "skills": ["/skills/venue-sourcing/"],
}

VENDOR_RESEARCHER: SubAgent = {
    "name": "vendor-researcher",
    "description": (
        "Researches catering, AV, and staffing vendors. Give it the city, "
        "headcount, dietary requirements, venue constraints (approved-caterer "
        "lists, kitchen availability), and the file path to write to."
    ),
    "system_prompt": VENDOR_RESEARCHER_PROMPT,
    "tools": [search_vendors, web_search],
}

BUDGET_ANALYST: SubAgent = {
    "name": "budget-analyst",
    "description": (
        "Costs a plan and pressure-tests it against the budget. Give it every "
        "known cost, the headcount, the budget ceiling, and the file path to "
        "write to. Returns totals plus where the plan breaks."
    ),
    "system_prompt": BUDGET_ANALYST_PROMPT,
    "tools": [estimate_budget],
    "skills": ["/skills/budget-modeling/"],
}

SUBAGENTS: list[SubAgent] = [VENUE_RESEARCHER, VENDOR_RESEARCHER, BUDGET_ANALYST]
