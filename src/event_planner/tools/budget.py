"""Budget arithmetic.

This one is real, not stubbed. Costing is exactly the kind of work that should
not happen in the model's head: the numbers need to be reproducible, and a
plausible-looking total that is quietly wrong is worse than no total at all.
"""

from __future__ import annotations

from langchain.tools import tool


@tool
def estimate_budget(
    headcount: int,
    venue_total_usd: float,
    catering_per_person_usd: float,
    catering_minimum_headcount: int = 0,
    av_total_usd: float = 0.0,
    staffing_total_usd: float = 0.0,
    other_total_usd: float = 0.0,
    service_charge_pct: float = 0.0,
    contingency_pct: float = 15.0,
) -> str:
    """Build an auditable event budget breakdown.

    Args:
        headcount: Expected guest count.
        venue_total_usd: Venue cost for the event (day rate plus any surcharges).
        catering_per_person_usd: Catering cost per cover, before service charge.
        catering_minimum_headcount: Vendor's billing minimum. Catering is billed
            on whichever is greater, this or `headcount`.
        av_total_usd: AV cost.
        staffing_total_usd: Staffing cost.
        other_total_usd: Rentals, decor, permits, shipping, insurance.
        service_charge_pct: Service charge applied to catering only, e.g. 22.
        contingency_pct: Contingency on the subtotal. Defaults to 15.

    Returns a line-item breakdown with per-guest cost and a fixed-versus-variable
    split, so the effect of a headcount change is visible.
    """
    if headcount <= 0:
        return "headcount must be greater than zero."

    billed_covers = max(headcount, catering_minimum_headcount)
    catering_base = billed_covers * catering_per_person_usd
    service_charge = catering_base * (service_charge_pct / 100.0)
    catering_total = catering_base + service_charge

    variable = catering_total
    fixed = venue_total_usd + av_total_usd + staffing_total_usd + other_total_usd
    subtotal = fixed + variable
    contingency = subtotal * (contingency_pct / 100.0)
    total = subtotal + contingency

    minimum_note = ""
    if billed_covers > headcount:
        wasted = (billed_covers - headcount) * catering_per_person_usd
        minimum_note = (
            f"  ! Catering is billed at the {catering_minimum_headcount}-guest minimum,\n"
            f"    not {headcount}. You are paying ${wasted:,.0f} for covers you do not need —\n"
            f"    adding {catering_minimum_headcount - headcount} guests costs nothing extra."
        )

    # Below a vendor minimum the next guests are already paid for, so their
    # marginal cost is zero — quoting the per-head rate here would contradict
    # the minimum note printed a few lines above in this same output.
    marginal = (
        catering_per_person_usd
        * (1 + service_charge_pct / 100.0)
        * (1 + contingency_pct / 100.0)
    )
    free_headroom = max(0, catering_minimum_headcount - headcount)

    label_width = 32

    def row(label: str, amount: float, decimals: int = 0) -> str:
        return f"  {label:<{label_width}}${amount:>12,.{decimals}f}"

    lines = [
        f"EVENT BUDGET — {headcount} guests",
        "",
        "FIXED COSTS (do not move with headcount)",
        row("Venue", venue_total_usd),
        row("AV", av_total_usd),
        row("Staffing", staffing_total_usd),
        row("Other (rentals/decor/permits)", other_total_usd),
        row("Fixed subtotal", fixed),
        "",
        "VARIABLE COSTS (scale with headcount)",
        row(
            f"Catering ({billed_covers} covers @ ${catering_per_person_usd:,.2f})",
            catering_base,
        ),
        row(f"Service charge ({service_charge_pct:g}%)", service_charge),
        row("Variable subtotal", variable),
    ]
    if minimum_note:
        lines.append(minimum_note)
    lines += [
        "",
        "TOTALS",
        row("Subtotal", subtotal),
        row(f"Contingency ({contingency_pct:g}%)", contingency),
        row("TOTAL", total),
        "",
        row("Cost per guest", total / headcount, decimals=2),
    ]
    if free_headroom:
        lines += [
            row("Marginal cost per extra guest", 0.0, decimals=2),
            f"    (the next {free_headroom} guest(s) are already paid for under the "
            f"{catering_minimum_headcount}-guest minimum;",
            f"     beyond that each guest costs ${marginal:,.2f})",
        ]
    else:
        lines += [
            row("Marginal cost per extra guest", marginal, decimals=2),
            "    (catering + service charge + contingency; fixed costs unaffected)",
        ]
    lines += [
        "",
        'Not included unless entered under "Other": gratuity, load-in/out overtime,',
        "insurance, permits, shipping, and weather contingency. Confirm each before",
        "presenting this as final.",
    ]
    return "\n".join(lines)
