# Event Planner Agent

[![CI](https://github.com/darylalim/event-planner-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/darylalim/event-planner-agent/actions/workflows/ci.yml)

Event planning agent on [Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview).

Takes an event brief and drives it to a bookable plan — venue shortlist,
catering and AV, budget, run-of-show — delegating research to subagents,
keeping durable artifacts on disk, and pausing for human approval before
anything spends money or reaches a guest's inbox.

## Why Deep Agents

Event planning hits nearly every condition the harness exists for:

| Capability | Why this task needs it |
| --- | --- |
| Planning (`write_todos`) | Headcount gates venue, venue gates catering, venue gates run-of-show |
| File management | Guest lists and vendor quotes outgrow a context window |
| Subagent delegation | Comparing eight venues shouldn't pollute the orchestrator's context |
| Persistent memory | Planning spans days or weeks of separate sessions |

`create_deep_agent` returns a compiled LangGraph graph, so checkpointers,
`interrupt()`, streaming, and Studio all still work underneath.

## Setup

Requires [uv](https://docs.astral.sh/uv/). Two Python versions are declared and
they do different jobs: `pyproject.toml` sets `>=3.11` as the compatibility
floor, while `.python-version` pins *development* to 3.14 so every checkout
builds the same environment. `uv sync` provisions the pinned interpreter
automatically; change it with `uv python pin <version>`.

uv itself is pinned too, by `[tool.uv] required-version` in `pyproject.toml`.
`astral-sh/setup-uv` reads that key, so one line pins the CI runner and your
checkout alike — and a mismatch is a hard error naming the version rather than a
silent split between what your machine checks and what CI does. If `uv sync`
greets you with *"Required uv version `==0.12.5` does not match the running
version"*, that is this, working as intended — `uv self update 0.12.5` puts you
back in step. It gates the project-aware commands only; `uvx ruff` and
`uv self update` are deliberately untouched, the latter so the way out is never
the thing being blocked.

```bash
uv sync
cp .env.example .env   # then fill in ANTHROPIC_API_KEY
```

`ANTHROPIC_API_KEY` is required. `TAVILY_API_KEY` is optional — without it,
`web_search` degrades gracefully, telling the agent to rely on the structured
directory and to flag that reputation data went unchecked.

The browser UI's dependency is a `web` **extra** rather than a core one, so the
CLI and the LangGraph Platform image — neither of which imports Streamlit — do
not carry it or its ~27 transitive packages. A checkout still gets it from
`uv sync` alone, because the `dev` group asks for `event-planner-agent[web]`; a
non-dev install that wants the page needs `uv sync --extra web`.

## Run

```bash
uv run event-planner                                  # interactive CLI
uv run event-planner --user alice@example.com         # scoped memory
uv run event-planner --thread offsite-2026            # named conversation
uv run event-planner --max-steps 400                  # longer planning session
uv run streamlit run streamlit_app.py                 # browser UI
uv run --with "langgraph-cli[inmem]" langgraph dev    # LangGraph Studio
uv run pytest                                         # harness tests
```

In the CLI: `/state` lists what is stored for the current user, `/export` writes
their event files to `exports/`, and `/exit` quits.

Both front ends read the same `.env` and share `.state/planner.sqlite` unless
`EVENT_PLANNER_DB` points elsewhere, so a plan started in the terminal resumes
in the browser on the same thread.

## Releases

Bump `version` in `pyproject.toml`, run `uv lock`, and push to main.
`.github/workflows/release.yml` waits for CI to go green on that commit, then
tags `v<version>` and publishes a GitHub release with generated notes. Nothing
else is a trigger — not a tag you push, not an edit to `pyproject.toml` that
leaves the version alone.

The `uv lock` is not optional and it fails in the quiet direction. `uv.lock`
records the project's own version alongside every dependency, so a bare bump
makes `uv sync --locked` exit with *"The lockfile at `uv.lock` needs to be
updated"* in all six CI jobs — and a red CI run emits no `workflow_run` success,
so the release does not fail, it simply never happens.

The gate asks *"does the version at this commit have a tag yet?"*, not *"did this
push change pyproject.toml?"*. Every diff-shaped alternative breaks on some push
shape and breaks silently: `HEAD~1` misses a bump in the middle commit of a
multi-commit push, and `github.event.before` is unreachable after a force-push and
is replayed verbatim on a re-run. Asking about the tag is correct on all of them,
and makes re-runs, reverts, and no-op edits no-ops for one reason rather than three.

It is a separate workflow rather than a third job in `ci.yml` so that file keeps
`permissions: contents: read` and the README badge keeps reporting lint, types and
tests rather than release outcomes. The cost is `workflow_run`, whose edges are
recorded in the file: `github.sha` is the branch tip rather than the commit CI
tested, the workflow only ever runs from main's copy of itself, and a tag pushed
with `GITHUB_TOKEN` triggers no further workflows.

Releases carry no build artifacts. The reason recorded here — that an installed
wheel resolved its workspace to `<venv>/lib` and loaded no skills — is fixed: the
workspace ships inside the package and CI's `deploy-shape` job asserts an
installed copy resolves its skills. Attaching a wheel is now a decision rather
than a blocked one, and publishing to PyPI remains separate: the name is
unclaimed, a first upload takes it permanently, and README documents a
checkout-and-`uv run` workflow that nobody has tested as an installed package.

If you delete a release but leave its tag, the gate will say "already released"
for good; the workflow's `workflow_dispatch` trigger is the recovery path.

## Architecture

```
orchestrator (claude-opus-5)
├── tools      search_venues · check_availability · search_vendors
│               estimate_budget · web_search · hold_venue* · send_invitations*
├── subagents  venue-researcher · vendor-researcher · budget-analyst
├── skills     venue-sourcing · budget-modeling        (loaded on demand)
└── memory     /memories/AGENTS.md                     (loaded every turn)

                                        * gated behind human approval
```

### Storage

A `CompositeBackend` routes by path prefix, longest match first. The filesystem
backend is the **default**, not a route, so an unrouted path doesn't fail — it
lands on the shared root:

| Path | Backend | Lifetime |
| --- | --- | --- |
| `/memories/` | `StoreBackend`, namespaced per user | Across sessions |
| `/events/` | `StoreBackend`, namespaced per user | Across sessions |
| `/artifacts/` | `StoreBackend`, namespaced per user | Across sessions |
| everything else | `ReadOnlyFilesystemBackend` rooted in the package | On disk, shared, read-only |

`/artifacts/` is deepagents' own territory. `FilesystemMiddleware` derives
`large_tool_results/` and `conversation_history/` beneath it and offloads a tool result over
20k tokens, or a human message over 50k, without anything here naming either path. What
spills is a named client's shortlist or costed budget, and the planner's own typed brief.
Routing is the same rule as `/events/`, applied to paths the harness writes rather than we
do — and applied to the *root*, because the names below it are deepagents' to rename.

Only `/skills/` lands on that shared root, and the root **refuses writes**.
It is `src/event_planner/workspace/`, inside the package so that an installed
copy carries it, and it runs with `virtual_mode=True` — the agent cannot read
or write its own source. Read-only is what makes a root inside the package
safe: an unrouted `write("/evil.py")` would otherwise land on the import path,
and a writable `/skills/` would let anything `web_search` returns rewrite the
guidance every tenant's next session loads. Those are still path guardrails,
not process isolation: don't repoint `root_dir` at the repo, and don't use this
backend in a server process handling untrusted input.

Event files are store-backed rather than on disk, so they aren't browsable by
default. `/export` in the CLI writes the current user's files to `exports/`.

### Tenant isolation

The agent has `ls` / `read_file` / `glob` / `grep` over its filesystem root, and
that root is a **single static path** — backend factories were removed in
deepagents 0.7, so it cannot vary per user. Anything reachable from it is
therefore readable by every session. Four rules follow, each enforced by a test
in `tests/test_security.py`:

**Only shared reference material sits on the root.** `/skills/` is on disk;
`/events/` and `/memories/` are routed to per-user store namespaces. Event
files carry client names, headcounts, guest lists, and budgets, so leaving them
on the shared root let one planner's session read another's brief.

**State lives outside the root.** Checkpoints and the store go in `.state/` at
the repo root, which the agent's root — now inside the package — is nowhere
near. A database under
the agent's root would let any session read every user's memories and every
thread's history straight out of the raw file, bypassing namespacing
completely. `--db` is validated against this too, so an operator can't
reintroduce it.

**Namespace mapping is injective.** Storage is scoped per user via
`("event_planner", <kind>, "u", <component>)`. The store rejects namespace
components outside `[A-Za-z0-9\-_@+:~]` — note the **period is excluded**,
because `langgraph.store.base` rejects it even though the `deepagents` regex
permits it, and only at write time. Sanitizing by replacement is also lossy
(`"a/b"` and `"a b"` both collapse to `"a_b"`), so a digest of the raw id is
appended: `alice@example.com` becomes `alice@example_com-ff8d9819fc0e`.

**Missing identity does not fail open.** `PlannerContext.user_id` defaults to
`None`, never to a placeholder string — a truthy default such as `"default"`
sends every unidentified caller down the *identified* branch and into one
shared bucket, which is precisely the failure this rule exists to prevent
(LangGraph builds the dataclass from `context={}`, so that is the common path).
With no `user_id`, storage scopes to the conversation thread, and `--user`
likewise defaults to nothing. Pass `--user <id>` for storage that carries
across threads.

The one genuinely shared bucket is `("event_planner", <kind>, "unscoped")`,
reached only when there is neither an id nor a resolvable thread. A
checkpointer always supplies a `thread_id`, so reaching it means the caller is
unroutable; it is labelled so no identified or threaded caller can land there.

### Skills vs memory

Both shape behaviour, but they load differently:

- **Skills** (`src/event_planner/workspace/skills/*/SKILL.md`) — opened on demand when the task
  calls for them. Good for long domain guidance.
- **Memory** (`/memories/AGENTS.md`) — injected into the system prompt every
  turn. Good for compact, always-relevant client facts.

Subagents do **not** inherit skills. `venue-researcher` and `budget-analyst`
list theirs explicitly in `subagents.py`.

### Human-in-the-loop

`hold_venue` and `send_invitations` are gated with `approve` / `edit` /
`reject`. `respond` is deliberately excluded — a free-text reply to a booking
request invites the model to read commentary as confirmation.

`interrupt_on` silently does nothing without a checkpointer, so `build_agent`
raises rather than handing back an agent whose approval gates don't gate. Pass
`hosted=True` only when LangGraph Platform supplies its own persistence.

### Step budget

LangGraph counts every node as a super-step, and this harness runs five
middleware nodes at two different rates — three `before_agent` (Skills,
PatchToolCalls, Memory) once per invocation, and two `after_model`
(HumanInTheLoop, TodoList) on every model call. Measured against the
live model, a single tool round trip costs about **4 steps**, not the 2 you'd
expect from `model → tools`:

```
3 × before_agent → model → 2 × after_model → tools → model → 2 × after_model
= 10 steps for one tool call and a final answer
```

LangGraph's default `recursion_limit` of 25 would strand a session after
roughly five tool calls — but it never applies here, because
`create_deep_agent` binds `recursion_limit: 9_999` onto the compiled graph. So
the 200 both front ends pass is a deliberate cap on a runaway session, not a
rescue from 25: removing it uncaps to 9999 rather than restoring the default.
Tune with `--max-steps`.

### The browser front end

`streamlit_app.py` is the same graph, the same persistence, and the same
approval gates behind a web page. Thread, user id, and model are sidebar fields
rather than flags, and stored memories and event files are listed there as
downloads. Four differences from the CLI are deliberate rather than incidental.

**The turn loop is inverted.** `cli._run_turn` blocks on `input()` until the
operator decides; a Streamlit script cannot block — it runs top to bottom and
ends, then reruns on the next interaction. So the transcript is replayed from
the checkpointer on every rerun and the pending approval is re-derived from
`StateSnapshot.interrupts`, rather than either being accumulated in session
state. A second copy would drift from the graph the first time a turn failed
halfway through; re-deriving also means a half-answered booking survives a
browser refresh instead of being stranded.

**Replaying is why two things are lazy.** The approval gate is an `st.fragment`,
because picking a decision or editing arguments would otherwise replay an entire
planning session — 15.7 KB of venue comparison in the recorded run — to redraw
one segmented control. It is safe to isolate because the interrupt is already
resolved into the panel's arguments and cannot change while the graph is parked;
submitting escapes on purpose, since `st.rerun()` defaults to `scope="app"`.

Tool-result panels are lazy for the same reason. Streamlit computes and ships a
collapsed expander's body anyway, so every rerun was re-serialising every tool
result in the thread — 37-61% of all transcript text across the recorded
threads, and 70.8 KB on the largest. `_render_tool` gates the body on
`on_change="rerun"` and `panel.open`, keyed on `tool_call_id`: gating promotes
the panel to a widget, and a label-derived key collides seven ways on a thread
holding seven `estimate_budget` calls.

**Artifacts download rather than export.** `/export` in the CLI writes store
keys to `exports/`, which is why it validates those agent-chosen keys against
traversal. The browser has no reason to write to the server's disk, so it
doesn't — and a second copy of that check is a second thing to get wrong.

**Approval takes two steps**, pick then submit. A single-click *Approve* is much
easier to hit by accident than `a` + Enter is in a terminal, and `hold_venue`
starts a deposit clock. `respond` is not offered, for the same reason it is
absent from `ALLOWED_DECISIONS`; if config ever allows it, the UI says so rather
than silently narrowing the operator's options.

One operational note. `.streamlit/config.toml` binds the server to `127.0.0.1`,
since Streamlit's default is every interface and this page has no
authentication — "User id" names a tenant, it does not prove one. Under
`streamlit run` that bind travels with the script: Streamlit appends the
script-level config last, so the file beside `streamlit_app.py` outranks project
and global config from any working directory. A `--server.address` flag or
`STREAMLIT_SERVER_ADDRESS` outranks it in turn, and a config file cannot enforce
itself, so the page checks the effective `server.address` at startup and warns in
the browser when it is not loopback. The theme
defines both light and dark so the mode toggle works; `primaryColor` was picked
by measuring rather than by eye, since Streamlit puts white text on primary
buttons and the primary button here is the one that commits money.

## What's real and what's stubbed

| Component | Status |
| --- | --- |
| `web_search` | **Live** (Tavily) |
| `estimate_budget` | **Real** arithmetic — reproducible and auditable |
| `search_venues`, `check_availability`, `search_vendors` | Stubbed, deterministic |
| `hold_venue`, `send_invitations` | Stubbed — return a reference, take no action |

Stubs are deterministic on purpose: a regression in agent behaviour should be
visible, not blamed on a vendor API. The tool signatures are the contract the
prompts are written against, so keep them stable when swapping in real
backends.

## Verified live

The offline suite covers the harness and cannot cover model behaviour. These
runs were made against `claude-opus-5` and their results recorded rather than
assumed.

### One full brief, end to end

85 guests, SF, $45k ceiling, seated lunch + livestreamed presentation:

| Measure | Result |
| --- | --- |
| Wall clock | 672s |
| Graph steps | 64 of the 200 budget |
| Tool calls | `write_todos` ×3, `task` ×2, `estimate_budget` ×7, `write_file` ×3, `read_file` ×4, `ls` ×3 |
| Tokens | 392,563 in (336,253 cached) / 16,943 out |
| Cost | ~$0.87 |
| Files produced | `brief.md`, `venues.md`, `vendors.md`, `budget.md` (~60 KB) |

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

### The approval gate

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

### The browser UI

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

### What the suite reaches, and what it can't

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

## Notes on `deepagents` 0.7.9

Three places where the published guidance and the installed package disagree.
All were found by inspecting the package, and all are covered by tests:

1. **`write_todos` is not bound by default.** The docs describe
   `TodoListMiddleware` as always present; it is not. `agent.py` adds it
   explicitly, since the orchestrator prompt instructs the model to plan with
   it. `test_planning_tool_is_bound` guards this.
2. **Backend constructors changed.** The docs show `StateBackend(rt)` and
   `lambda rt: StoreBackend(rt)`. The installed signatures are `StateBackend()`
   and `StoreBackend(*, namespace, store=None)`, and `backend` takes a direct
   instance rather than a factory.
3. **`create_deep_agent` gained a `memory=[...]` parameter** backed by
   `MemoryMiddleware`, which loads memory files into the system prompt. It is
   not mentioned in the current guidance.

## Layout

```
streamlit_app.py  the browser front end — the page, and nothing else of consequence
src/event_planner/
  agent.py        harness wiring — backend, memory, skills, approval gates
  subagents.py    the three researcher subagents
  prompts.py      orchestrator + subagent system prompts
  context.py      per-user memory namespacing
  cli.py          interactive REPL with approval prompts
  webui.py        logic the page delegates to, testable without a Streamlit runtime
  tools/          catalog (stub) · budget (real) · bookings (stub) · search (live)
  workspace/      the agent's filesystem view — ships in the wheel, read-only
    skills/       venue-sourcing · budget-modeling
.state/           checkpoints, memory, event files   (gitignored, out of reach)
exports/          /export output                     (gitignored)
tests/
  test_harness.py        approval gates, tool binding, config guards, step budget
  test_security.py       tenant isolation — reachability, namespaces, fail-closed
  test_approval_cli.py   the operator's approve/edit/reject prompt
  test_webui.py          the web front end's decisions, incl. parity with the CLI
  test_streamlit_page.py the real page driven through Streamlit's AppTest
  test_tools.py          tool correctness — dates, budgets, booking refusals
```

## License

[MIT](LICENSE).
