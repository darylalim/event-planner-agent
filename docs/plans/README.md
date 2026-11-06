# Plans

Direction: this project is heading towards a real tool, so real integrations, guest data
and authentication outrank polish.

Each plan is self-contained and sized for one session, one branch and one pull request.
This file holds only the order and status. The details live in each plan, and where
they disagree, the plan wins.

To start one, open a new session and ask it to carry out that plan. Read only the plan
you're working on. When one finishes, update its row here and the status line at the top
of the plan.

| Order | Plan | Who acts | Estimated live spend | Needs first | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | [required-ci-checks.md](required-ci-checks.md) | Operator | None | Nothing | Done |
| 2 | [dependency-upgrade.md](dependency-upgrade.md) | Session | $0.70-$1.05 | 1 | Done |
| 3 | [harness-gaps.md](harness-gaps.md) | Session | None | 2 | Done |
| 4 | [agent-evaluation.md](agent-evaluation.md) | Session, then operator labels | About $25.60 spent (at most $27.30); about $1 smoke, $6 full | 2 | Phase 1 done; judge calibrated for `verdict_clear` only |
| 5 | [real-invitations.md](real-invitations.md) | Session, after operator decisions | Provider costs; live sends need go-ahead | 2, 3, ideally 4 | Not started |
| 6 | [budget-analyst-effort.md](budget-analyst-effort.md) | Session | $5.37 spent | 2, ideally 4 | Done; `high` kept |
| 6b | [worst-case-budget.md](worst-case-budget.md) | Session | $5.44 spent | 4, 6 | Done; optional `medium` retest not run |
| 7 | [artifacts-retention.md](artifacts-retention.md) | Session | None | 2 | Parked 2026-11-05: `/artifacts/` is empty, see the plan for what reopens it |
| 7b | [checkpoint-retention.md](checkpoint-retention.md) | Session | None | 2 | Not started; not urgent until real client threads exist |
| 8 | [streamlit-theme.md](streamlit-theme.md) | Session | None; reviewed on a scripted model | 2 | Done; $0 spent |
| 9 | [streamlit-layout.md](streamlit-layout.md) | Session | None; reviewed on a scripted model | 2, 8 | Phase 1 done, $0 spent; phase 2 stopped at its spike |
| 10 | [readme-restructure.md](readme-restructure.md) | Session | None | All others | Not started |

Plans 3-9 can move around as long as their "Needs first" column holds. Plan 10 stays last,
because the others add to README sections it moves.

For now this runs only on the operator's machine, on loopback, so no authentication plan
is needed yet. Write one before anyone else uses it: `user_id` is an unverified text
field, and `real-invitations.md` blocks shared use on it.

## Rules for every plan

- **Spending:** get the operator's go-ahead before any live model run. Planned spend
  across all plans is about $44. Do not pass $45 in total without
  the operator raising that limit.
- **Real sends:** get the operator's go-ahead before any real email leaves the system.
- **Public changes:** get the operator's go-ahead before anything that changes GitHub
  settings or the public repo page.
- **Commits:** commit or push only if the operator asks.
- **uv:** the uv version fix in plan 2 comes before any `uv run`, `uv sync` or `uv lock`.

Costs are estimates from recorded runs. Line numbers and version previews were correct on
2026-11-04 and will drift, so re-check them when a plan starts.
