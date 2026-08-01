# event-planner-agent

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

Requires [uv](https://docs.astral.sh/uv/). Two separate Python versions are
declared, and they do different jobs: `pyproject.toml` sets `>=3.11` as the
compatibility floor, while `.python-version` pins *development* to 3.14 so every
checkout builds the same environment. `uv sync` provisions the pinned
interpreter automatically; change it with `uv python pin <version>`.

```bash
uv sync
cp .env.example .env   # then fill in ANTHROPIC_API_KEY
```

`ANTHROPIC_API_KEY` is required. `TAVILY_API_KEY` is optional — without it,
`web_search` degrades gracefully and tells the agent to rely on the structured
directory and flag that reputation data went unchecked.

## Run

```bash
uv run event-planner                                  # interactive CLI
uv run event-planner --user alice@example.com         # scoped memory
uv run event-planner --thread offsite-2026            # named conversation
uv run event-planner --max-steps 400                  # longer planning session
uv run langgraph dev                                  # LangGraph Studio
uv run pytest                                         # harness tests
```

In the CLI: `/state` lists what is stored for the current user, `/export` writes
their event files to `exports/`, and `/exit` quits.

Without `--user`, storage scopes to the conversation thread. That is
deliberate — a shared placeholder id would merge every unidentified operator's
memory into one bucket. Pass `--user <id>` for storage that carries across
threads.

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

A `CompositeBackend` routes by path prefix, longest match first:

| Path | Backend | Lifetime |
| --- | --- | --- |
| `/memories/` | `StoreBackend`, namespaced per user | Across sessions |
| `/events/` | `StoreBackend`, namespaced per user | Across sessions |
| `/skills/` | `FilesystemBackend` rooted at `workspace/` | On disk, shared |

The filesystem backend is rooted at `workspace/`, **not** the repo root, and
runs with `virtual_mode=True` — the agent cannot read or write its own source.
Don't repoint `root_dir` at the repo, and don't use this backend in a server
process handling untrusted input.

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
the repo root — a *sibling* of `workspace/`, never inside it. A database under
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
With no `user_id`, storage scopes to the conversation thread. `--user` likewise
defaults to nothing rather than to a placeholder.

The one genuinely shared bucket is `("event_planner", <kind>, "unscoped")`,
reached only when there is neither an id nor a resolvable thread. A
checkpointer always supplies a `thread_id`, so reaching it means the caller is
unroutable; it is labelled so no identified or threaded caller can land there.

### Skills vs memory

Both shape behaviour, but they load differently:

- **Skills** (`workspace/skills/*/SKILL.md`) — opened on demand when the task
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
middleware nodes per model turn — three `before_agent` (Skills, PatchToolCalls,
Memory) and two `after_model` (HumanInTheLoop, TodoList). Measured against the
live model, a single tool round trip costs about **4 steps**, not the 2 you'd
expect from `model → tools`:

```
3 × before_agent → model → 2 × after_model → tools → model → 2 × after_model
= 10 steps for one tool call and a final answer
```

LangGraph's default `recursion_limit` of 25 therefore strands a session after
roughly five tool calls. The CLI sets 200 instead; tune with `--max-steps`.

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

One full brief end-to-end against `claude-opus-5` (85 guests, SF, $45k ceiling,
seated lunch + livestreamed presentation):

| | |
| --- | --- |
| Wall clock | 672s |
| Graph steps | 64 of the 200 budget |
| Tool calls | `write_todos` ×3, `task` ×2, `estimate_budget` ×7, `write_file` ×3, `read_file` ×4, `ls` ×3 |
| Tokens | 392,563 in (336,253 cached) / 16,943 out |
| Cost | ~$0.87 |
| Files produced | `brief.md`, `venues.md`, `vendors.md`, `budget.md` (~60KB) |

Behaviours confirmed rather than assumed:

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

### Approval gate, verified live

All three resume paths were run against `claude-opus-5` on separate threads
(~$0.25 total). This matters because offline tests cannot reach it: adaptive
thinking is on by default, so a real `hold_venue` proposal arrives in an
`AIMessage` carrying thinking blocks *alongside* the `tool_use`, and resuming
replays that history to the API. A scripted `AIMessage` has no thinking blocks.

Each run confirmed **1 signed thinking block** in the proposing turn, so the
risky path was genuinely exercised rather than simulated:

| Decision | Result |
| --- | --- |
| `edit` | Operator's corrected args executed (60 → 45 guests), not the model's |
| `approve` | Original args executed |
| `reject` | Stub never ran; the agent reported the refusal and did not retry |

The `edit` run surfaced a nice property: the agent noticed the executed
arguments differed from what it proposed and flagged the discrepancy in a
comparison table rather than silently accepting the change.

## Notes on `deepagents` 0.7.1

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
src/event_planner/
  agent.py        harness wiring — backend, memory, skills, approval gates
  subagents.py    the three researcher subagents
  prompts.py      orchestrator + subagent system prompts
  context.py      per-user memory namespacing
  cli.py          interactive REPL with approval prompts
  tools/          catalog (stub) · budget (real) · bookings (stub) · search (live)
workspace/        the agent's filesystem view — shared, so skills only
  skills/         venue-sourcing · budget-modeling
.state/           checkpoints, memory, event files   (gitignored, out of reach)
exports/          /export output                     (gitignored)
tests/
  test_harness.py      approval gates, tool binding, config guards, step budget
  test_security.py     tenant isolation — reachability, namespaces, fail-closed
  test_approval_cli.py the operator's approve/edit/reject prompt
  test_tools.py        tool correctness — dates, budgets, booking refusals
```
