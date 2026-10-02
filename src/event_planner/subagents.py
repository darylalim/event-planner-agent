"""Subagent roster.

Each subagent gets a narrow tool set and its own skills. Skills are NOT
inherited from the orchestrator — a subagent that needs one must list it
explicitly, which is why `venue-researcher` and `budget-analyst` repeat paths
that also appear in the orchestrator's `skills`.

The point of delegating here is context isolation: comparing eight venues
burns a lot of tokens on listings the orchestrator never needs to see again.
The subagent does that work in its own context and reports back a shortlist.

That isolation is also what makes a cheaper model safe here. Each `task` call
starts the subagent on a fresh conversation that is discarded once it reports,
so its model never shares a transcript with the orchestrator's — no thinking
block crosses between models, and nothing the orchestrator has checkpointed
depends on which model a subagent ran.
"""

from __future__ import annotations

from deepagents import SubAgent

from event_planner.models import ModelChoice
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

#: Model and effort per subagent, keyed by name. Applied by `build_agent`
#: rather than written into the specs above, so importing this module builds no
#: chat model. `test_every_subagent_has_a_model_choice` holds the keys to
#: `SUBAGENTS`: a new subagent missing here would otherwise fail at build time
#: with a bare KeyError, and one silently inheriting the orchestrator's Opus is
#: the outcome this table exists to make a decision rather than a default.
#:
#: Sonnet throughout: all three read material and weigh it, and the two
#: researchers read live `web_search` results, where the stronger model is the
#: better defence against instructions planted in a page.
#:
#: * The researchers run at `medium`, Anthropic's starting point for multi-step
#:   tool use on Sonnet 5.5 (its levels were recalibrated from Sonnet 5, so
#:   intuitions about the old ones do not carry over).
#: * The budget analyst runs at `high`. It makes one tool call and writes a short
#:   answer, so the extra effort costs little — and that answer is what the
#:   orchestrator weighs before proposing a booking.
SUBAGENT_MODELS: dict[str, ModelChoice] = {
    "venue-researcher": ModelChoice("claude-sonnet-5-5", "medium"),
    "vendor-researcher": ModelChoice("claude-sonnet-5-5", "medium"),
    "budget-analyst": ModelChoice("claude-sonnet-5-5", "high"),
}
