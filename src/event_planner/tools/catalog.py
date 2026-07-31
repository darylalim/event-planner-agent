"""Stubbed venue and vendor directories.

These stand in for real supplier APIs. They are deterministic on purpose: the
agent loop is hard enough to evaluate without network flakiness underneath it,
and a fixed dataset means a regression in agent behaviour is visible instead of
being blamed on a vendor API.

Swapping in a real backend means replacing the bodies below. The tool
signatures and return shapes are the contract the prompts are written against,
so keep them stable.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any

from langchain.tools import tool

_VENUES: list[dict[str, Any]] = [
    {
        "id": "v-loft-mission",
        "name": "The Mission Loft",
        "city": "San Francisco",
        "capacity": 120,
        "day_rate_usd": 4800,
        "style": "industrial",
        "includes": ["tables", "chairs", "basic PA", "wifi"],
        "excludes": ["catering", "AV tech", "cleaning"],
        "notes": "Freight elevator only; load-in is slow. No kitchen, caterer must be off-site.",
        "booked_dates": ["2026-09-12", "2026-09-19", "2026-10-03"],
    },
    {
        "id": "v-presidio-hall",
        "name": "Presidio Officers' Club Hall",
        "city": "San Francisco",
        "capacity": 250,
        "day_rate_usd": 11500,
        "style": "historic",
        "includes": ["tables", "chairs", "AV package", "on-site coordinator", "parking"],
        "excludes": ["catering", "decor"],
        "notes": "Approved caterer list only. Hard 22:00 noise curfew.",
        "booked_dates": ["2026-09-19", "2026-11-14"],
    },
    {
        "id": "v-dogpatch-studio",
        "name": "Dogpatch Studio",
        "city": "San Francisco",
        "capacity": 80,
        "day_rate_usd": 3200,
        "style": "modern",
        "includes": ["chairs", "wifi", "projector"],
        "excludes": ["tables", "catering", "AV tech", "parking"],
        "notes": "Street parking only. Kitchenette suitable for drop-off catering.",
        "booked_dates": ["2026-10-03"],
    },
    {
        "id": "v-embarcadero-atrium",
        "name": "Embarcadero Atrium",
        "city": "San Francisco",
        "capacity": 400,
        "day_rate_usd": 18000,
        "style": "corporate",
        "includes": ["full AV", "staging", "security", "coordinator", "loading dock"],
        "excludes": ["catering", "decor", "valet"],
        "notes": "Union labour required for load-in. Budget for overtime past 20:00.",
        "booked_dates": ["2026-09-12"],
    },
    {
        "id": "v-brooklyn-greenpoint",
        "name": "Greenpoint Works",
        "city": "New York",
        "capacity": 180,
        "day_rate_usd": 7400,
        "style": "industrial",
        "includes": ["tables", "chairs", "PA", "freight elevator"],
        "excludes": ["catering", "AV tech", "heating (winter surcharge)"],
        "notes": "Winter events incur a $900 heating surcharge.",
        "booked_dates": ["2026-09-26"],
    },
    {
        "id": "v-tribeca-rooftop",
        "name": "Tribeca Rooftop",
        "city": "New York",
        "capacity": 300,
        "day_rate_usd": 21000,
        "style": "upscale",
        "includes": ["furniture", "AV package", "coordinator", "in-house bar"],
        "excludes": ["catering", "weather contingency tenting"],
        "notes": "Outdoor. Tenting runs $6-9k and must be decided 14 days out.",
        "booked_dates": ["2026-09-12", "2026-09-19"],
    },
]

_VENDORS: list[dict[str, Any]] = [
    {
        "id": "c-verde",
        "name": "Verde Catering",
        "city": "San Francisco",
        "category": "catering",
        "per_person_usd": 78,
        "minimum_headcount": 40,
        "notes": "Strong vegetarian and vegan menus. Service charge 22% not included.",
    },
    {
        "id": "c-ironpot",
        "name": "Iron Pot Kitchen",
        "city": "San Francisco",
        "category": "catering",
        "per_person_usd": 52,
        "minimum_headcount": 60,
        "notes": "Drop-off only, no service staff. Cheapest option above 60 guests.",
    },
    {
        "id": "c-lantern",
        "name": "Lantern Hospitality",
        "city": "San Francisco",
        "category": "catering",
        "per_person_usd": 135,
        "minimum_headcount": 25,
        "notes": "Plated service, includes staff. Handles allergen-restricted menus well.",
    },
    {
        "id": "a-brightline",
        "name": "Brightline AV",
        "city": "San Francisco",
        "category": "av",
        "flat_usd": 3400,
        "notes": "Two techs, full day. Overtime $220/hr past 10 hours.",
    },
    {
        "id": "a-signal",
        "name": "Signal Stage",
        "city": "San Francisco",
        "category": "av",
        "flat_usd": 1900,
        "notes": "Single tech, sound only. No video or livestream capability.",
    },
    {
        "id": "s-atlas",
        "name": "Atlas Event Staffing",
        "city": "San Francisco",
        "category": "staffing",
        "per_person_usd": 0,
        "flat_usd": 2600,
        "notes": "6 staff, 8 hours. Typical ratio is 1 staff per 20 guests for plated service.",
    },
    {
        "id": "c-hudson",
        "name": "Hudson Table",
        "city": "New York",
        "category": "catering",
        "per_person_usd": 96,
        "minimum_headcount": 50,
        "notes": "Includes service staff. 20% service charge additional.",
    },
    {
        "id": "a-eastriver",
        "name": "East River AV",
        "city": "New York",
        "category": "av",
        "flat_usd": 4800,
        "notes": "Union crew. Minimum 4-hour call.",
    },
]


@tool
def search_venues(
    city: str,
    min_capacity: int,
    max_day_rate_usd: float | None = None,
    style: str | None = None,
) -> str:
    """Search the venue directory.

    Args:
        city: City to search, e.g. "San Francisco".
        min_capacity: Minimum guest capacity the venue must support.
        max_day_rate_usd: Optional ceiling on the venue's day rate.
        style: Optional style filter, e.g. "industrial", "historic", "modern".

    Returns a formatted list of matching venues with rates, inclusions, and
    known gotchas. Returns a note when nothing matches.
    """
    matches = [
        v
        for v in _VENUES
        if v["city"].lower() == city.lower()
        and v["capacity"] >= min_capacity
        and (max_day_rate_usd is None or v["day_rate_usd"] <= max_day_rate_usd)
        and (style is None or v["style"].lower() == style.lower())
    ]
    if not matches:
        return (
            f"No venues in {city} match capacity >= {min_capacity}"
            + (f", day rate <= ${max_day_rate_usd:,.0f}" if max_day_rate_usd else "")
            + (f", style '{style}'" if style else "")
            + ". Try relaxing capacity or raising the rate ceiling."
        )

    lines = [f"{len(matches)} venue(s) in {city}:\n"]
    for v in sorted(matches, key=lambda x: x["day_rate_usd"]):
        lines.append(
            f"- {v['name']} (id: {v['id']})\n"
            f"    capacity: {v['capacity']} | day rate: ${v['day_rate_usd']:,}\n"
            f"    style: {v['style']}\n"
            f"    includes: {', '.join(v['includes'])}\n"
            f"    excludes: {', '.join(v['excludes'])}\n"
            f"    notes: {v['notes']}"
        )
    return "\n".join(lines)


@tool
def check_availability(venue_id: str, event_date: str) -> str:
    """Check whether a venue is free on a given date.

    Args:
        venue_id: Venue id from `search_venues`, e.g. "v-loft-mission".
        event_date: Date in ISO format, e.g. "2026-09-19".

    Returns availability plus the venue's other booked dates that month, so an
    alternative date can be proposed without a second lookup.
    """
    venue = next((v for v in _VENUES if v["id"] == venue_id), None)
    if venue is None:
        known = ", ".join(v["id"] for v in _VENUES)
        return f"Unknown venue_id '{venue_id}'. Known ids: {known}"

    try:
        parsed = _date.fromisoformat(event_date)
    except ValueError:
        return f"Could not parse event_date '{event_date}'. Use ISO format, e.g. 2026-09-19."

    booked = set(venue["booked_dates"])
    same_month = sorted(
        d for d in booked if d.startswith(f"{parsed.year:04d}-{parsed.month:02d}")
    )
    if event_date in booked:
        return (
            f"{venue['name']} is NOT available on {event_date}.\n"
            f"Other bookings that month: {', '.join(same_month) or 'none'}"
        )
    return (
        f"{venue['name']} IS available on {event_date} at ${venue['day_rate_usd']:,}/day.\n"
        f"Already booked that month: {', '.join(same_month) or 'none'}"
    )


@tool
def search_vendors(city: str, category: str, headcount: int | None = None) -> str:
    """Search the vendor directory for catering, AV, or staffing.

    Args:
        city: City to search, e.g. "San Francisco".
        category: One of "catering", "av", "staffing".
        headcount: Optional guest count, used to flag vendor minimums and to
            project a per-person total.

    Returns matching vendors with pricing, minimums, and exclusions.
    """
    valid = {"catering", "av", "staffing"}
    if category.lower() not in valid:
        return f"Unknown category '{category}'. Use one of: {', '.join(sorted(valid))}."

    matches = [
        v
        for v in _VENDORS
        if v["city"].lower() == city.lower() and v["category"] == category.lower()
    ]
    if not matches:
        return f"No {category} vendors found in {city}."

    lines = [f"{len(matches)} {category} vendor(s) in {city}:\n"]
    for v in matches:
        cost_line = ""
        if v.get("per_person_usd"):
            cost_line = f"${v['per_person_usd']}/person"
            if headcount:
                effective = max(headcount, v.get("minimum_headcount", 0))
                total = effective * v["per_person_usd"]
                cost_line += f" -> ${total:,.0f} for {effective} covers"
                if effective > headcount:
                    cost_line += (
                        f" (billed at the {v['minimum_headcount']}-guest minimum, "
                        f"not your {headcount})"
                    )
        if v.get("flat_usd"):
            cost_line = (cost_line + " + " if cost_line else "") + f"${v['flat_usd']:,} flat"

        lines.append(
            f"- {v['name']} (id: {v['id']})\n"
            f"    cost: {cost_line}\n"
            f"    notes: {v['notes']}"
        )
    return "\n".join(lines)
