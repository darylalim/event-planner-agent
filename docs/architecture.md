# Architecture

How the harness is put together and why. The diagram is in the [README](../README.md#how-it-works).

`web_search` belongs to the two researchers and not to the orchestrator, by design:
the agent that can book venues and contact guests never reads arbitrary web
pages. Anything only the web can answer, it asks a researcher for — in a
context that is thrown away once the researcher reports.

Each role's model and effort is chosen, not inherited: the orchestrator's lives
in `models.py`, the subagents' in `SUBAGENT_MODELS` beside their specs. Effort is
always sent explicitly, because the API's default is per model and moves —
Claude Opus 5.5 defaults to `medium`, below the `high` Claude Opus 5 used. The
subagents run on Sonnet because each `task` call starts them on a fresh
conversation that is thrown away once they report, so a second model never
shares a transcript with the orchestrator's.

## Storage

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

## Tenant isolation

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

## Skills vs memory

Both shape behaviour, but they load differently:

- **Skills** (`src/event_planner/workspace/skills/*/SKILL.md`) — opened on demand when the task
  calls for them. Good for long domain guidance.
- **Memory** (`/memories/AGENTS.md`) — injected into the system prompt every
  turn. Good for compact, always-relevant client facts.

Subagents do **not** inherit skills. `venue-researcher` and `budget-analyst`
list theirs explicitly in `subagents.py`.

## Human-in-the-loop

`hold_venue` and `send_invitations` are gated with `approve` / `edit` /
`reject`. `respond` is deliberately excluded — a free-text reply to a booking
request invites the model to read commentary as confirmation.

`interrupt_on` silently does nothing without a checkpointer, so `build_agent`
raises rather than handing back an agent whose approval gates don't gate. Pass
`hosted=True` only when LangGraph Platform supplies its own persistence.

## Step budget

LangGraph counts every node as a super-step, and this harness runs seven
middleware nodes at two different rates. Three `before_agent` (Skills,
PatchToolCalls, Memory) and one `after_agent` (ParentNotifier) run once per
invocation. One `before_model` (Skills) and two `after_model` (HumanInTheLoop,
TodoList) run on every model call. A single tool round trip costs **5 steps**,
not the 2 you'd expect from `model → tools`:

```
3 × before_agent → before_model → model → 2 × after_model → tools
  → before_model → model → 2 × after_model → after_agent
= 13 steps for one tool call and a final answer
```

The two extra node types arrived with deepagents 0.7.32. Neither does anything
here: `before_model` pins skills only if a caller names them, and `after_agent`
reports only to a parent that started the run with `start_async_task`.

LangGraph's default `recursion_limit` of 25 would strand a session after
roughly three tool calls — but it never applies here, because
`create_deep_agent` binds `recursion_limit: 9_999` onto the compiled graph. So
the 200 both front ends pass is a deliberate cap on a runaway session, not a
rescue from 25: removing it uncaps to 9999 rather than restoring the default.
Tune with `--max-steps`.

## Tracing

The graph is LangGraph, so [LangSmith](https://docs.smith.langchain.com/)
tracing needs no code: set `LANGSMITH_API_KEY` and `LANGSMITH_TRACING=true`
(both are in `.env.example`) and every model call, tool call and subagent
`task` arrives as one tree per turn in the `LANGSMITH_PROJECT` project.
`langsmith` is already installed as a dependency of `langchain-core`.

What the code adds is the ability to *find* a trace. All three entry points
build their run config with `cli.run_config`, so each root run is named
`event-planner` and carries:

| Field | Value | Where it comes from |
| --- | --- | --- |
| `thread_id` | the conversation | LangGraph copies it from `configurable`; LangSmith's Threads view groups on it |
| `user_id` | the `--user` / sidebar id, omitted when there is none | `run_config` — it travels in `context=`, which no tracer reads |
| `front_end` (also a tag) | `cli`, `web` or `live_check` | `run_config` |

`langgraph dev` and a platform deployment pass their own config, so their traces
carry `thread_id` but not the other two.

**Tracing sits outside tenant isolation.** Namespacing keeps one planner's
briefs, guest lists and memories away from another's *session*. A trace, though,
records every prompt, tool result and file write in full and sends them to one
LangSmith project, where anyone with access to that project can read every
tenant's data. Turn it on only where that is acceptable.

The test suite never traces. `tests/conftest.py` pins `LANGSMITH_TRACING_V2=false`,
the name langsmith reads first. Without that line a narrowed run such as
`pytest tests/test_streamlit_page.py tests/test_webui.py` tried 45 uploads with the
real key, because a page test's real `_load_env` puts `.env`'s values into the
environment. A full run escaped only because langsmith caches the lookup and had
already read "off". `test_the_suite_never_traces` guards it.

## The browser front end

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

## Layout

```
streamlit_app.py  the browser front end — the page, and nothing else of consequence
src/event_planner/
  agent.py        harness wiring — backend, memory, skills, approval gates
  models.py       the orchestrator's model and effort, and the ModelChoice type
  subagents.py    the three researcher subagents, and the model each runs on
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
  test_live_check.py     the live script's cost accounting, offline
  test_evals.py          the evaluation checks and driver, offline
scripts/
  live_check.py          run the real model and report cost per role (spends money)
evals/                   scored comparison of model behaviour (spends money)
  scenarios.py           briefs, scripted approvals, expected facts, planted pages
  driver.py              runs a scenario, answers approvals, captures everything
  checks.py              deterministic gates and scored checks
  judge.py               rubric checks, usable only after calibration
  weak.py                flawed rewrites of real replies, to calibrate the judge
  run.py                 suites, spend cap, compare, rescore
  results/               JSONL output                (gitignored)
```
