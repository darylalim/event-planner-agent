# event-planner-agent

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

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

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

In the CLI, `/state` lists what the agent has memorized about the current user
and `/exit` quits.

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
| everything else | `FilesystemBackend` rooted at `workspace/` | On disk |

The filesystem backend is rooted at `workspace/`, **not** the repo root, and
runs with `virtual_mode=True` — the agent cannot read or write its own source.
Don't repoint `root_dir` at the repo, and don't use this backend in a server
process handling untrusted input.

### Tenant isolation

The agent has `ls` / `read_file` / `glob` / `grep` over its filesystem root, so
anything reachable from that root is readable by every session. Three rules
follow, each enforced by a test in `tests/test_security.py`:

**State lives outside the root.** Checkpoints and the memory store go in
`.state/` at the repo root — a *sibling* of `workspace/`, never inside it. A
database under the agent's root would let any session read every user's
memories and every thread's history straight out of the raw file, bypassing
namespacing completely. `--db` is validated against this too, so an operator
can't reintroduce it.

**Namespace mapping is injective.** Memory is scoped per user via
`("event_planner", "memories", "u", <component>)`. The store rejects namespace
components outside `[A-Za-z0-9\-_.@+:~]`, but sanitizing by replacement is
lossy — `"a/b"` and `"a b"` both collapse to `"a_b"`. A digest of the raw id is
appended so distinct users never share a namespace, while the readable part
stays readable (`alice_example.com.9f2a1c…`).

**Missing identity does not fail open.** With no `user_id`, memory falls back
to the conversation thread rather than a shared `default` bucket, so anonymous
callers get isolated memory instead of inheriting each other's. Anonymous
namespaces are tagged distinctly from identified ones, so no real user can ever
occupy one.

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
workspace/        the agent's entire filesystem view
  skills/         venue-sourcing · budget-modeling
  events/         agent working files        (gitignored)
.state/           checkpoints + memory       (gitignored, outside agent reach)
tests/
  test_harness.py   approval gates, tool binding, config guards
  test_security.py  tenant isolation — state reachability, namespace collisions
```
