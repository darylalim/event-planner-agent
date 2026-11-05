# Plan: require a worst case in every budget

Status: not started. Written 2026-11-05 for a new session to carry out.
Read README's "The current roster" first: its 2026-11-05 experiment is why this exists.

## Goal

Make every costed brief state a worst-case total for the recommended plan and compare it
with the ceiling, whoever does the costing. Then, if the operator wants, retest
`budget-analyst` at `medium` now that the worst case no longer depends on wording.

## Why

Whether a brief gets a worst case is currently down to chance. In the six scored runs of
`budget-analyst-effort.md`, the orchestrator's task to the analyst asked for one in four
runs and left it out of two. All four that asked got one, and neither of the other two
did. A client told "$36,860, about $8,100 under" with no worst case can't see how far that
margin could move.

## Findings that shape the design

- **Nothing asks for it.** "Worst" appears in neither `prompts.py` nor the `budget-modeling`
  skill.
- **The analyst's prompt alone isn't enough.** The orchestrator may make an
  `estimate_budget` call directly, and in two recorded runs it did all the costing itself.
  Those runs never see `BUDGET_ANALYST_PROMPT`.
- **The skill alone isn't enough either.** `agent.py` passes `skills=["/skills/"]`,
  but skills are opened on demand, so holding the skill doesn't mean reading it. The
  requirement needs a line in a prompt as well as the skill.
- **"Worst case" needs a definition.** Runs that gave one meant different things: a pricier
  caterer, a Saturday at another venue, or 94 guests at 20% contingency. Only the recommended
  plan with its own uncertain lines pushed up tells a client what they'd actually be
  signing up for.

## Design

Define the worst case in one place, the `budget-modeling` skill: the recommended plan with
the same venue and vendors, every estimated line at the top of its plausible range, and
any unconfirmed minimum applied. Run it through `estimate_budget`, and report it beside the
realistic total, each compared with the ceiling.

Add a short requirement to `BUDGET_ANALYST_PROMPT`, and one to the orchestrator prompt
covering both the costing it does itself and what its final reply must carry.

## Steps

1. Get the operator's go-ahead on the budget below, then run the `smoke` baseline before
   any change: `uv run python -m evals.run --suite smoke --reps 1 --yes-spend --max-usd 5`.
2. Make the skill and prompt changes. Add an offline test that pins the requirement in both
   prompts, in the style of `test_the_orchestrator_is_told_to_delegate_research_and_why`.
3. Add a `worst_case_stated` check to `evals/`. Follow CLAUDE.md: test its pattern against
   real replies, try it with `rescore` on saved captures first, and return `unknown` when it
   can't decide. It must require a figure identified as a worst case, not just the word.
4. Run the candidate `smoke`, then `compare` it with step 1's baseline. Only `clear` passes,
   apart from the new check, which the baseline can't be expected to pass.
5. Run `uv run scripts/live_check.py brief --yes-spend` three times. All three replies must
   state a worst case for the recommended plan. Count any run where the orchestrator does
   the costing itself, because this plan has to hold there too.
6. Record the results in README's "The current roster".

### Optional: retest `medium`

Do this only if the operator asks after step 6. Repeat the method in
`budget-analyst-effort.md`, with fresh runs for both arms and the same adoption rule.
Step 5's three runs can serve as the `high` arm if nothing else has changed. The possible
saving is about $0.23 per brief, so this may not be worth doing.

## Verification

```bash
uv run pytest
uvx ruff check .
uvx ruff format --check .
uvx ty check
```

## Budget

Two `smoke` runs at about $1 and three briefs at about $0.92 come to about $5. The optional
retest adds three `medium` briefs at about $0.69, or about $2. Stop at $8.

Live spend so far is about $31, at most $32.70, against the $35 the operator authorised.
This plan needs that raised, and it would stay under the $45 limit.

## Out of scope

Changing any effort level outside the optional retest, the other budget checks, and
`estimate_budget` itself.
