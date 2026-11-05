# Plan: test `budget-analyst` at `medium` effort

Status: done 2026-11-05. `high` kept: `medium` cost 58% less but passed every check in only
1 of 3 runs. $5.37 spent. README's "The current roster" has the results and the confound.
Written 2026-11-04 for a new session to carry out.
Do `dependency-upgrade.md` first, including its uv version fix: without that fix,
every `uv run` below fails.

## Goal

Find out whether `budget-analyst` can drop from `high` to `medium` effort, on
`claude-sonnet-5-5`, without a worse budget verdict. Keep every other role as it
is. Correct the stale comment whichever way the result goes.

## Why

`src/event_planner/subagents.py:88` justifies `high` by saying the analyst "makes
one tool call and writes a short answer, so the extra effort costs little". The
clean brief in README's "The current roster" disagrees:

| Role | Calls | Output tokens | Cost |
| --- | --- | --- | --- |
| orchestrator | 9 | 9,835 | $0.33 |
| budget-analyst | 9 | 22,086 | $0.33 |

The analyst was 40% of that $0.82 run. This is one run, so it is a reason to
test, not a result.

## Constraints

- **Live runs cost money.** Get the operator's go-ahead on the budget below
  before the first `--yes-spend`. Stop and report if spend reaches $8.
- **Do not change `BRIEF`, the prompts, or any other role** during the
  experiment. Effort must be the only difference between the two arms.
- **Do not compare against the README figures.** Recorded runs differ on
  substance: worst case was $44,119 in one and $46,900 in another, and the
  second run never delegated to `budget-analyst`. Both arms must be run fresh.
  `BRIEF`'s date (19 September 2026) has also passed since those runs, which may
  change what the model says.
- Commit or push only if the operator asks.

## Steps

### 1. Run the `high` arm (no code change)

Run this three times:

```bash
uv run scripts/live_check.py brief --yes-spend
```

Each run prints its `db:` and `thread:`. Save those and the per-role report.

### 2. Run the `medium` arm

Change one line in `SUBAGENT_MODELS` (`src/event_planner/subagents.py:94`):

```python
"budget-analyst": ModelChoice("claude-sonnet-5-5", "medium"),
```

Then run the same command three times. No test pins this value: the tests in
`tests/test_harness.py` compare against the table, not a literal.

### 3. Discard runs that don't test the analyst

A run counts only if the `budget-analyst` row appears in its report. The
orchestrator may do the costing itself, as it did in the $0.65 run. Replace a
discarded run at most twice per arm, within the $8 limit. If an arm still has
fewer than two valid runs, stop and report that the experiment is inconclusive.

### 4. Review the full output

`run_brief` cuts the final reply to 1,500 characters, so read `budget.md` too. To
export a run's files:

```bash
uv run event-planner --db <db> --user live-check@example.com --thread <thread>
# then type /export
```

Score each valid run on these checks, reading the runs without regard to arm as
far as practical:

- The line items add up to the stated totals.
- The totals match what `estimate_budget` returned.
- A realistic and a worst-case figure are both stated.
- Both are compared with the $45,000 ceiling, with a clear verdict on fit.
- AV, the livestream and the dietary needs are all priced or explicitly flagged.
- No `max_tokens` in the orchestrator stop reasons, and `budget.md` is complete.

### 5. Decide

Adopt `medium` only if **both** hold:

- Every valid `medium` run passes every check in step 4.
- Mean `budget-analyst` cost is at least 20% below the `high` arm.

Otherwise restore `high`. Do not judge on the verdict's dollar figures alone,
because they vary between runs at the same effort.

### 6. Update the code and docs

Whichever way it goes:

- Rewrite the comment at `src/event_planner/subagents.py:88-90`. It must describe
  the measured work, not "one tool call", and give the reason for the final level.
- Add the results to README's "The current roster", including run counts and any
  discarded runs. CLAUDE.md requires this after a model or effort change.

If `medium` is adopted, also:

- Update the architecture diagram at `README.md:128`.
- Check the `~$0.65-$1` estimate in `scripts/live_check.py` and `CLAUDE.md` against
  the new mean.

## Verification

```bash
uv run pytest
uvx ruff check .
uvx ruff format --check .
uvx ty check
```

The experiment is finished when the final code matches the decision, the checks
above pass, and README records the runs.

## Budget

Six runs at about $0.65-$1 each come to $3.90-$6. Up to four replacement runs
could add more, so the $8 limit is the hard stop.

## Out of scope

Haiku 5.5 for the researchers, `xhigh` on the orchestrator, and Fable 5.1 on the
orchestrator. None has measured evidence behind it yet.
