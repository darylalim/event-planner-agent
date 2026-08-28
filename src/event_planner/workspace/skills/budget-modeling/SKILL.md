---
name: budget-modeling
description: How to build and stress-test an event budget — fixed vs variable costs, vendor minimums, the line items that are always forgotten, and how to propose cuts when a plan is over.
---

# Budget Modeling

## Overview

Producing an event budget that survives contact with reality. The job is not to
make the numbers fit — it is to say clearly whether they do, and where the plan
breaks if they do not.

## When to Use

Costing a shortlist, checking a plan against a ceiling, or deciding what to cut.

## Instructions

### 1. Split fixed from variable before anything else

- **Fixed**: venue, AV, staffing, permits, insurance, decor. Unmoved by headcount.
- **Variable**: catering, rentals per head, welcome gifts, beverage per head.

This split is what makes the budget useful. "We are $4,000 over" has completely
different answers depending on which side the money is on: trimming 30 guests
does nothing to a fixed overage.

Always report the marginal cost of one additional guest. It is the number the
client actually needs when the RSVP list moves.

### 2. Respect vendor minimums

Catering minimums bill on `max(headcount, minimum)`. Below the minimum you pay
for covers you do not serve — and adding guests up to the minimum is free. Say
so explicitly; clients routinely cut headcount to save money and save nothing.

### 3. The line items that get forgotten

Check every one of these before calling a budget complete:

- Service charge (often 18–24%) and whether gratuity is separate
- Load-in / load-out labour and overtime
- Delivery, pickup, and rental damage waivers
- Event insurance and permits
- Shipping for anything branded, plus customs if crossing a border
- Weather contingency for outdoor space (tenting decisions have deadlines)
- Taxes — service charge is often taxed, gratuity often is not

### 4. Contingency is not padding

Default to 15% on the subtotal. Go to 20% when the venue is outdoors, the date
is inside 60 days, or any major line item is an estimate rather than a written
quote. Never present a budget with no contingency.

Label each line as **quoted** or **estimated**. A budget of estimates with a
precise-looking total is misleading.

### 5. When the plan is over budget

Do not silently shave numbers to fit. Propose specific cuts with their savings,
ranked by how much they damage the event. State the damage honestly:

1. Cuts guests do not notice — decor, printed collateral, upgraded linens
2. Cuts guests notice but tolerate — beer/wine instead of full bar, buffet
   instead of plated, shorter AV window
3. Cuts that change the event — fewer guests, a cheaper venue, a weekday date

Give the client the choice. Do not make it for them.

## Output

Use `estimate_budget` for the arithmetic rather than computing totals yourself,
so the numbers are reproducible. Write the breakdown to the file path you were
given and return the totals plus your assessment of whether the plan fits.
