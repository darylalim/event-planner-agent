# Verified live

The offline suite covers the harness and cannot cover model behaviour. These
runs were made against `claude-opus-5` and their results recorded rather than
assumed. The current roster — Claude Opus 5.5 orchestrating, Claude Sonnet 5.5
subagents — has its own section, [The current roster](#the-current-roster);
everything above it predates the move.

## One full brief, end to end

85 guests, SF, $45k ceiling, seated lunch + livestreamed presentation:

| Measure | Result |
| --- | --- |
| Wall clock | 672s |
| Graph steps | 64 of the 200 budget |
| Tool calls | `write_todos` ×3, `task` ×2, `estimate_budget` ×7, `write_file` ×3, `read_file` ×4, `ls` ×3 |
| Tokens, orchestrator | 392,563 in (336,253 cached) / 16,943 out |
| Tokens, both subagents | 249,691 in (180,472 cached) / 31,515 out |
| Cost | ~$2.10 — ~$0.87 orchestrator, ~$1.23 subagents |
| Files produced | `brief.md`, `venues.md`, `vendors.md`, `budget.md` (~60 KB) |

This table said **~$0.87** until it was re-measured: that figure is the
orchestrator alone. Each `task` call checkpoints under its own namespace
(`tools:<uuid>`), so summing the root thread's messages never sees the
subagents' usage — more than half the total on every recorded brief.

- **Skills change behaviour.** For 85 seated guests it searched
  `min_capacity=180`, applying the venue-sourcing rule that seated format uses
  ~half of listed capacity — not the 85 in the brief.
- **Delegation works.** Two subagents ran, each writing its own file and
  returning a summary rather than dumping contents into the orchestrator.
- **Budgets get pressure-tested.** Seven `estimate_budget` calls sweeping
  catering rates and vendor minimums, per the budget-modeling skill.
- **Memory generalizes.** It recorded "treat streaming as a standing
  requirement" and "order special covers at headcount+2, counts drift up" —
  reusable rules, not event trivia.
- **Cross-session recall and isolation hold.** A brand-new thread for the same
  user loads that memory; a different user's session does not see it.
- **It pushed back.** The brief said "Thursday 19 September 2026"; that date is
  a Saturday, and it flagged the mismatch and checked the real Thursday.

## What a brief costs

Every model call's `usage_metadata` from the three recorded full briefs, read
per checkpoint namespace and priced at list rates. Uncached input is billed at
the base rate; no run reported a cache write.

| Run | Opus 5, every role (measured) | Opus 5.5 + Sonnet 5.5 (estimate) |
| --- | --- | --- |
| `full-brief-1` — three subagents | $2.37 | $1.13 |
| `full-brief-2` — two subagents | $1.76 | $0.89 |
| `full-brief-3` — two subagents | $2.10 | $1.12 |
| **Mean** | **$2.08** | **$1.05** |

The right-hand column is an **estimate**: the same token counts repriced for
the current roster. The one clean live brief on that roster came in at
**$0.82** (below), so the estimate ran high — `medium` effort and Sonnet's
tokenizer both cut the counts. Two things it showed reliably: the subagents
were more than half of the old bill, which is why moving them to Sonnet is the
larger saving; and output tokens, under a tenth of the volume, are more than
half the cost.

## The current roster

Reproduce any of this with `uv run scripts/live_check.py brief --yes-spend` (or
`edit`); it prints the transcript's shape, the verdict, and cost per role, and
`usage --db … --thread …` re-prices a thread already on disk for free.

The same 85-guest brief, re-run on Opus 5.5 (`high`) orchestrating Sonnet 5.5
subagents. Six live runs — four briefs, two `edit` checks — ~$3.90 in all; only the
last brief is a clean measurement, and the runs before it are each why
something below exists.

| Role | Calls | Tokens in (cached) / out | Cost |
| --- | --- | --- | --- |
| orchestrator — Opus 5.5 | 9 | 204,541 (180,324) / 9,835 | $0.33 |
| budget-analyst — Sonnet 5.5 | 9 | 196,951 (159,439) / 22,086 | $0.33 |
| venue-researcher — Sonnet 5.5 | 5 | 47,960 (33,011) / 5,206 | $0.09 |
| vendor-researcher — Sonnet 5.5 | 3 | 21,972 (11,927) / 4,864 | $0.07 |
| **Total** | **26** | | **$0.82**, 330s |

Against `full-brief-3` on Opus 5 — $2.10 and 672s — that is about 60% cheaper
and twice as fast, with all three subagents delegated, no `max_tokens` stop on
any of the 26 calls, and the same substance in the answer: the date flagged as a
Saturday, a shortlist, and a priced verdict with a worst case ($44,119, still
under the ceiling). The researchers show the model change most plainly: $0.09
and $0.07 here, $0.53 and $0.70 on Opus 5.

What the earlier runs found:

- **No delegation at all.** On the old orchestrator prompt, Opus 5.5 ran the
  whole brief itself — zero `task` calls, every `web_search` in the context that
  holds the booking tools. The prompt's escape hatch ("do not delegate work you
  can finish yourself") was the cause; it now gives the reason to delegate
  instead.
- **A silent 4,096-token output cap.** `langchain-anthropic` 1.6.1 has no profile
  for either 5.5 model id and falls back to 4,096. With delegation restored, the
  budget analyst's `budget.md` ran past it and every `write_file` was cut off
  before its `content` — 34 retries, $1.85 of a $2.50 brief, 21 minutes. The cap
  is now set explicitly for every role.
- **An `edit` read as a fault.** Answering a `hold_venue` with `edit` (60 -> 45
  guests) executed 45 correctly, but the API still saw the model's own 60, so
  Opus 5.5 reported the operator's correction as an error and advised against
  paying the deposit. With `OperatorEditNote` it now says "the reviewer changed
  the headcount from 60 to 45 before approving … I'm treating 45 as the confirmed
  number" — and flags that the $12,000 total did not move with it. The resumed
  turn carried one signed thinking block and was accepted, so an operator edit is
  compatible with Opus 5.5's history checks.
  `OperatorEditNote` has since been removed: langchain 1.4.2 ships the same
  append-only notice in `HumanInTheLoopMiddleware`, and the repo's own copy went
  silent on that release. Run live (`live_check.py edit`, $0.07), langchain's
  default wording fixed the fault but not the framing: the model wrote "45 (you
  asked for 60)" and asked the operator "which headcount is right?", and the
  $12,000 total went unremarked. With `prompts.EDIT_NOTICE` passed as
  `edit_notice` ($0.02), it listed 45 as settled and flagged that the $12,000
  "was set for 60 guests and kept at 45". Both resumed turns carried one thinking
  block and were accepted.

A second clean run, after `web_search` was removed from the orchestrator:
**$0.65 and 187s**. Both researchers were delegated, and the orchestrator made
no attempt to call the tool it no longer has. It did the costing itself — six
`estimate_budget` calls rather than delegating to `budget-analyst` — which the
prompt permits in smaller doses and which reads no web content, so the boundary
holds either way.

Two runs are not a distribution, and these two disagree on substance: $39,884
realistic and $44,119 worst case in the first, $43,400 and $46,900 in the
second — the second's worst case over the ceiling. Expect cost, wall clock and
the verdict's margins to vary from run to run, and re-measure before quoting a
figure to anyone.

After the 2026-11-04 dependency upgrade (deepagents 0.7.32, langchain 1.4.8,
langchain-anthropic 1.7.10, anthropic 2.4.0), one brief and one `edit` check:

| Role | Calls | Tokens in (cached) / out | Cost |
| --- | --- | --- | --- |
| orchestrator — Opus 5.5 | 14 | 338,377 (313,515) / 11,011 | $0.38 |
| budget-analyst — Sonnet 5.5 | 11 | 429,865 (370,093) / 31,453 | $0.51 |
| venue-researcher — Sonnet 5.5 | 6 | 61,219 (44,604) / 6,220 | $0.10 |
| vendor-researcher — Sonnet 5.5 | 4 | 39,858 (23,702) / 6,818 | $0.11 |
| **Total** | **35** | | **$1.10**, 432s |

All three subagents were delegated and no role hit `max_tokens`. The cost is
just above the earlier $0.65-$0.82, and the budget analyst is most of the
difference: 42 `estimate_budget` calls and five `edit_file` calls, two of them
refused as parallel writes to one path. deepagents 0.7.21 has that refusal too,
and a single run can't separate the upgrade from ordinary variation.

The `edit` check cost $0.07. The resumed turn carried one thinking block and was
accepted, the model treated 45 as settled, and it flagged that the $12,000 total
may have been set for 60 guests.

On 2026-11-05 a test of `budget-analyst` at `medium` instead of `high`, with effort
the only difference: seven briefs, $5.37. One `high` run was discarded and replaced
because the orchestrator did the costing itself, so each arm has three valid runs.

| Effort | Analyst calls | Analyst output tokens | Analyst cost | Mean analyst cost | Mean brief | Passed every check |
| --- | --- | --- | --- | --- | --- | --- |
| `high` | 6, 11, 8 | 19,788, 38,495, 21,113 | $0.28, $0.60, $0.31 | $0.40 | $0.92 | 3 of 3 |
| `medium` | 4, 6, 5 | 7,962, 14,493, 12,171 | $0.11, $0.21, $0.18 | $0.17 | $0.69 | 1 of 3 |

`high` stayed. `medium` cost 58% less, past the 20% the test required, but it also
had to pass every check, and two of its runs gave no figure identified as a worst
case. Runs were scored under shuffled labels before the arms were revealed. Their
line items, tool totals, ceiling verdicts and `budget.md` files were all sound, and
nothing hit `max_tokens`.

This does not show that `medium` is worse. Those two runs are also the only two
where the orchestrator's task to the analyst did not ask for a worst case, and
that task is written before the analyst's effort has any effect. No prompt or skill
requires one, so whether a brief gets a worst case currently depends on the
orchestrator's wording. Three runs per arm can't separate the two causes, and
retesting `medium` would need that requirement fixed first.

Later on 2026-11-05 that requirement went in: the `budget-modeling` skill defines the
worst case as the recommended plan with the same venue and vendors, every estimated
line at the top of its plausible range, and any unconfirmed minimum applied. Both the
analyst's and the orchestrator's prompts require it. `smoke` went from $1.57 to $1.40
with verdict `clear`, and the new `worst_case_stated` check, which failed 9 of the 10
saved replies it could score, passed. Three briefs followed, $2.47 in all:

| Brief | Realistic | Worst case | Against the $45,000 ceiling | Cost |
| --- | --- | --- | --- | --- |
| 1 | $42,434 | $52,473 | $2,566 under, $7,473 over | $0.94 |
| 2 | $38,746 | $47,230 | $6,254 under, $2,230 over | $0.78 |
| 3 | $42,785 | $51,696 | $2,215 under, $6,696 over | $0.75 |

All three replies stated a worst case, and so did the `smoke` brief ($38,050 and
$45,525). The realistic total fit and the worst case did not in all four, so the
requirement changes what the client is told. The briefs' mean cost of $0.82 is below
the earlier $0.92, so it did not add measurable cost.

The orchestrator's own path is still unproven. `budget-analyst` ran in all four, and
although the orchestrator also called `estimate_budget` two or three times in each of
the three briefs, none had it do all the costing, which is the case its prompt line
exists for. `medium` was not retested.

## The evaluation baseline

`evals/` turns reading one run by hand into a scored comparison. Run `smoke` after
a prompt or middleware change, `full` before changing a model or an effort level,
and `compare` the result against a baseline from the same day. CLAUDE.md's
"The evaluation suite" has the commands and what each verdict means.

The first baseline, on 2026-11-04 with the current roster: 13 scenarios at three
repetitions, 39 runs, **$6.00 and 39 minutes**. A smoke run is about $1. Web
results came from fixtures and tracing was off.

| Check | Result |
| --- | --- |
| Every gate: approvals, rejections, the orchestrator off the web, runs completing | No failures in 39 runs |
| Planted web text, three styles | Resisted 9 of 9, and reported to the client 9 of 9 |
| Capacity, dates, arguments, delegation, surcharge, memory | All decided runs passed |
| Totals traced to `estimate_budget` | 4 of 4 decided, 2 undecided |
| `budget-analyst` used when a budget was asked for | **2 of 6** |

| Role | Cost |
| --- | --- |
| orchestrator | $3.86 |
| venue-researcher | $1.08 |
| budget-analyst | $0.54 |
| vendor-researcher | $0.52 |

The analyst figure is the finding. On `hidden-cost` the orchestrator costed the
plan itself in all three runs, and on `standard-offsite` in one of three. The prompt
permits a quick check, and no web content is involved, so the boundary holds, but
the role is used less than its place in the roster suggests. A `standard-offsite`
run cost $0.56 to $0.74 here, against $0.65 to $1.10 in the earlier single runs.

To show the suite can fail, a deliberately broken orchestrator prompt ("never use
`task`, call `hold_venue` straight away") was run on one injection scenario.
`compare` returned `blocked` on `no_unrequested_proposal`, flagged
`delegated_research` as regressed, and left `injection_resisted` undecided, since
that run never read the planted page.

Generating weak replies to calibrate the phase 2 judge turned up a second finding.
Appending instructions to the orchestrator's prompt to hide drawbacks, invent
details or withhold the verdict mostly failed across 24 runs ($4.24): it declined
to invent Denver venues, and in several replies said what it had been told and
that it would not comply ("You're about to commit real money, so the limits and
extra costs are included below"). Weak replies now come from rewriting good ones
instead ($0.31 for 27). This was read by hand, not scored, so it shows a tendency
under these prompts rather than a guarantee.

Three repetitions only detect large changes, and these fixtures are one narrow
catalogue, so read a clean result as "no large regression", not as proof.

## The approval gate

All three decisions were run from the CLI on separate threads (~$0.25 total),
and `edit` again from the browser. This is the path offline tests cannot reach:
adaptive thinking is on by default, so a real `hold_venue` proposal arrives in
an `AIMessage` carrying thinking blocks *alongside* the `tool_use`, and resuming
replays that history to the API — a scripted `AIMessage` has none. Every run
confirmed **1 signed thinking block** in the proposing turn, so the risky path
was genuinely exercised rather than simulated.

| Decision | Result |
| --- | --- |
| `approve` | Original args executed |
| `edit` | Operator's corrected args executed (60 → 45 guests), not the model's |
| `reject` | Stub never ran; the agent reported the refusal and did not retry |

The `edit` runs surfaced a property worth keeping: the agent noticed that the
executed arguments differed from what it proposed, and said so rather than
silently accepting the change — a comparison table in the CLI, and in the
browser (30 → 24 guests plus a corrected client name) an opening line of "the
approval came back with two corrections I've absorbed" followed by a budget
reworked at 24.

`approve` and `reject` were not re-run from the browser. Both are recorded above
for the CLI, and the browser's payloads are pinned against the CLI's by
`test_reject_matches_the_cli_byte_for_byte` and `test_edit_matches_the_cli` —
the decision dict is precisely what those compare.

## The browser UI

Driven through `streamlit_app.py` on several threads: a 40-guest brief ($18k
ceiling) as `demo@example.com`, a 30-guest booking ($12k ceiling) on a separate
thread and database, and a replay of the recorded `full-brief-3`.

- **Streaming renders incrementally.** Tool calls appear as captions and results
  as collapsed expanders while the turn is still running.
- **Skills load on demand, and delegation works through the page.** Both
  `venue-sourcing` and `budget-modeling` were read before any planning;
  `venue-researcher` wrote a 15.7 KB `venues.md` and `vendor-researcher` wrote
  `vendors.md`.
- **Storage routes per user, visibly.** Event files landed under
  `event_planner/events/u/demo@example_com-7462108984f6`, and with
  `acme-planner`'s `/AGENTS.md` sitting in the same database the sidebar
  correctly reported "no memories yet" for `demo@example.com`.
- **The transcript survives losing the server.** The host reaped the background
  process mid-turn, twice, and a fresh process replayed the whole transcript
  from the checkpointer — the payoff for reading history from
  `graph.get_state()` rather than accumulating it in session state.
- **The fragment isolates.** Selecting `edit` and editing the arguments redrew
  only the panel, leaving the transcript untouched; submitting escaped to a full
  app run.
- **Panels are genuinely lazy.** Replaying `full-brief-3` rendered 22 panels,
  **0** code blocks in the DOM, and 74.1 KB of markup for a transcript whose
  tool output alone is 70.8 KB — the bodies are absent, not merely hidden.
  Opening one produced a single code block with the budget breakdown, and the
  other six `estimate_budget result` panels stayed shut, which is what per-panel
  widget identity buys.

Two bugs were found this way, both since fixed and covered by tests.

**A turn could be stranded.** The reaping above also left a thread at
`next=('tools',)` with no interrupt to answer, and the page had no affordance
for that state — so that brief never finished, and the thread would have sat on
an unfinished tool call for good. Streaming `None` continues the pending node;
the vendor-researcher then ran to completion.

**`st.markdown` reads `$...$` as LaTeX.** Any line quoting two costs had the
span between them swallowed and re-set as italic mathematics: "$10,281 — $1,719
under your $12,000 ceiling" rendered as an equation, taking the figures an
operator is asked to check with it. Every amount in the proposal was affected.
Fixed by escaping bare `$` before rendering (`webui.markdown_safe`), with the
live strings pinned as tests.

## What the suite reaches, and what it can't

Most of the browser's approval path is covered offline, because the middleware
builds the interrupt rather than the model — so a *scripted* model is enough to
produce a genuine one. `test_the_real_middleware_payload_parses` drives the real
graph to a real interrupt and feeds the actual `snapshot.interrupts` to the
page's parser. It found two things the hand-written fixtures had wrong:
`review_configs` entries also carry `action_name`, and `description` is
generated boilerplate repeating the tool name and a dict repr of the args, which
the page had been rendering as prose directly under those same arguments. The
resume itself needs no separate coverage: the page calls
`graph.stream(Command(resume=...))` on the same graph with the same config and
adds nothing to it.

Opening a panel is reachable too, contrary to a claim recorded here once. A
gated expander is a widget, so it registers its key in session state, and
setting that key opens it under `AppTest` —
`test_an_opened_panel_renders_its_body` and
`test_opening_one_panel_leaves_the_others_closed` drive the two behaviours the
browser run showed. What the browser added was scale and the DOM measurement,
not the only possible coverage.

What remains genuinely unreachable offline is the fragment's own execution mode.
`AppTest` builds a fresh `LocalScriptRunner` per call and never sets
`fragment_id_queue`, so `_approval_panel` only ever runs inline there; the
browser's replay from `MemoryFragmentStorage` is covered by the live runs above
and not by the suite. The invariant that makes the isolation safe — that the
panel reads no graph state while rendering — is pinned by
`test_the_panel_reads_no_graph_state_while_rendering`.
