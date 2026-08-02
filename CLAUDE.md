# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

An event planning agent built on [Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview)
(`deepagents` 0.7.1), which wraps LangGraph. `create_deep_agent` returns a compiled
LangGraph graph, so checkpointers, `interrupt()`, streaming, and Studio all work underneath.

`README.md` is detailed and current — read it for the *why* behind the design, the
storage/tenant-isolation rationale, and the recorded live-run results. This file covers
what you need to *change code* safely.

## Commands

```bash
uv sync                                    # install (uv required; .python-version pins 3.14)
cp .env.example .env                       # then fill in ANTHROPIC_API_KEY

uv run pytest                              # 176 tests, ~4s, fully offline
uv run pytest tests/test_security.py       # one file
uv run pytest -k namespaces                # one pattern
uv run pytest tests/test_tools.py::test_hold_refuses_an_unknown_venue -v

uv run event-planner                       # interactive CLI
uv run event-planner --user alice@example.com --thread offsite-2026
uv run streamlit run streamlit_app.py      # browser UI (EVENT_PLANNER_DB overrides the db)
uv run langgraph dev                       # LangGraph Studio (host supplies persistence)

uvx ruff check .                           # lint  — config in pyproject.toml, not a dep
uvx ruff format .                          # format — enforced by CI, run before committing
uvx ty check                               # types — config in pyproject.toml, not a dep
```

**When working with Python, invoke the relevant `/astral:<skill>` — `/astral:uv`,
`/astral:ty`, `/astral:ruff` — to ensure best practices are followed.** They carry the
current guidance for each tool, which is more reliable than working from memory:
`/astral:uv` for anything touching dependencies, the lockfile, the Python version, or
how a command is run; `/astral:ruff` before linting or formatting; `/astral:ty` before
type checking. All three tools are already configured for this repo — see below.

Ruff is configured in `pyproject.toml` but is **not** a dependency — run it with
`uvx ruff check .`. The rule set is chosen so the `# noqa` codes in the source
(`BLE001` on the three deliberate blind excepts) suppress rules that are actually
enabled; `RUF100` fails the check if one goes stale. `ANN401` is ignored because `Any`
is honest at the deepagents/langgraph boundary, and `tests/*` ignores `ANN`/`RUF012`
(the fake models are Pydantic subclasses, so their list defaults are fields, not shared
state). Formatting **is** enforced — run `uvx ruff format` before committing; CI runs
`uvx ruff format --check .`. It was adopted after the fact, so the reformat that made
the tree clean collapsed some hand-wrapped expressions; `line-length` drives the
formatter as well as E501, so raising it widens what gets joined onto one line.

ty is configured the same way and also not a dependency. Its defaults already pass, so
only `missing-type-argument` is raised to error — restating the defaults would be config
that checks nothing. `missing-override-decorator` is deliberately left off: satisfying it
means `@override` on the test fakes, which on 3.11 needs `typing_extensions`, available
here only transitively. Note ty checks against **3.11**, not the pinned 3.14 — it takes
the target from `requires-python`, so it catches 3.12+ syntax that would break the floor
this project claims to support. Both tools are clean; keep them that way rather than
adding suppressions.

## Architecture

```
build_agent()                     agent.py — the only place the harness is assembled
├── model            claude-opus-5
├── tools            ORCHESTRATOR_TOOLS (7)
├── subagents        SUBAGENTS from subagents.py (3, each with its own narrow tool set)
├── middleware       TodoListMiddleware()      — must be explicit, see gotchas
├── backend          build_backend() → CompositeBackend
├── skills           ["/skills/"]              — opened on demand
├── memory           ["/memories/AGENTS.md"]   — injected into system prompt every turn
├── interrupt_on     INTERRUPT_ON, derived from tools.IRREVERSIBLE_TOOLS
└── context_schema   PlannerContext            — carries user_id for namespacing
```

`hosted_agent()` in `agent.py` is the factory `langgraph.json` points at. It passes
`hosted=True` because the platform injects its own checkpointer and store.

### Storage: the default is shared, the routes are private

`CompositeBackend` matches the **longest route prefix first**, and `FilesystemBackend`
is the **default**, not a route:

| Path | Backend | Visibility |
| --- | --- | --- |
| `/memories/` | `StoreBackend(namespace=memory_namespace)` | Per user, across sessions |
| `/events/` | `StoreBackend(namespace=events_namespace)` | Per user, across sessions |
| everything else | `FilesystemBackend(root_dir=workspace/, virtual_mode=True)` | **Shared across all sessions** |

The agent has `ls`/`read_file`/`glob`/`grep` over that filesystem root, and the root is a
single static path — backend factories were removed in deepagents 0.7, so it cannot vary
per user. **Any new path holding user data needs a route in `build_backend()` plus a
namespace factory in `context.py`**, or it lands on the shared root and one planner's
session can read another's brief.

### The three-file loop

Changing behaviour usually touches all three, and they drift silently:

- `prompts.py` names tools (`search_venues`, `estimate_budget`) and file paths
  (`/events/<event-slug>/brief.md`) as literal strings. Renaming a tool or restructuring
  paths without updating prompts produces an agent instructed to call something that
  doesn't exist. Nothing catches this at import time.
- `tools/` signatures are the contract prompts are written against. Keep them stable when
  swapping stubs for real backends.
- `subagents.py` — subagents do **not** inherit the orchestrator's skills. Each lists its
  own (`venue-researcher` and `budget-analyst` repeat paths the orchestrator also has).

Skills live in `workspace/skills/*/SKILL.md` as plain markdown with YAML frontmatter
(`name`, `description`). They are shared reference material, versioned in git, and read by
the agent at runtime — they're behaviour, not documentation.

## Invariants that fail silently

Each is enforced by a test; breaking one usually produces working-looking code.

**`interrupt_on` is a no-op without a checkpointer.** `build_agent` raises rather than
returning an agent whose approval gates don't gate. Only `hosted=True` suppresses this.

**Approval-gated tools come from one list.** `IRREVERSIBLE_TOOLS` in `tools/__init__.py`
is the source; `INTERRUPT_ON` is derived from it. A new money-spending or guest-contacting
tool goes in that list — never in two hand-maintained copies.

**`respond` is excluded from `ALLOWED_DECISIONS`.** Only `approve`/`edit`/`reject`. A
free-text reply to a booking request invites the model to read commentary as confirmation.
Relatedly, `cli._resolve_choice` never matches ambiguously (`reject`/`respond` share `r`),
and `_decline_message` frames refusals as a human decision so the model doesn't read them
as a tool error and retry. `webui.SUPPORTED_DECISIONS` holds the same three and reports
anything else as unsupported rather than rendering it.

**Two front ends can now refuse a booking, and they must do it identically.**
`webui.py` imports everything shared from `cli.py` rather than restating it —
`_decline_message`, `_check_db_outside_workspace`, `credentials_problem`,
`DEFAULT_MAX_STEPS`, and `_stored`/`_brief_args` (re-exported as `stored_items`/`brief_args`,
so they are the CLI's objects, not equivalents — `test_the_shared_helpers_are_the_clis_own_objects`
asserts identity). The wording of a refusal is behavioural, not cosmetic, and a divergent
copy would surface only as a booking retried after a human said no;
`test_reject_matches_the_cli_byte_for_byte` and `test_edit_matches_the_cli` drive
`cli._prompt_one` with scripted stdin and compare its payload against the web builder's.
Messages that name an operator-facing knob take it as a parameter
(`_check_db_outside_workspace(..., knob=...)`), since `--db` is meaningless to someone who
set `EVENT_PLANNER_DB`.

What the web UI deliberately does *not* mirror is `cli._export`: it offers downloads, which
construct no server-side path, so the traversal check that makes `_export` safe has exactly
one copy.

**The two front ends share one database file, so its connections need WAL.** `open_persistence`
sets `journal_mode=WAL` and a 30s busy timeout; in rollback-journal mode a CLI turn holding the
write lock makes a concurrent browser turn fail outright with "database is locked", which the
page can only report as a lost turn. Connections are also closed on cache eviction via
`close_persistence` — `st.cache_resource(max_entries=...)` bounds how many entries it keeps but
does not close what it drops, and the cache key includes a free-text model field.

**A disabled Streamlit button is not a guard.** `disabled=` stops a click in the browser
and says nothing about what reaches the branch behind it. The approval submit button
checks `ready` again in the handler; the first version did not, and
`test_submitting_with_no_decision_sends_nothing` caught it resuming the graph with an
empty decision list.

**Approval widget keys follow the action, never its position.** Streamlit restores a
keyed widget's value whenever a widget with that key renders again. With `key=f"choice-{index}"`,
resolving one approval and immediately interrupting for a *different* action reused the key,
so the new panel rendered pre-approved with submit enabled — one click executing something
nobody reviewed. `edit` was worse: a stored value beats the `value=` argument, so the new
action's box came back holding the previous action's arguments and would have executed the
wrong tool with them. `webui.review_token` mixes in the checkpoint id (fresh per interrupt,
stable across the reruns *within* one approval, which is what lets a selection survive long
enough to submit) plus the action name and args.
`test_a_second_interrupt_is_not_pre_approved` guards it.

**An interrupt payload the page cannot parse must fail closed.** `pending_reviews` returns
`[]` for an unrecognised shape, and an empty result rendered the ordinary chat input — so a
follow-up would run against a thread holding a `tool_use` with no `tool_result` while a
booking sat un-gated behind a UI that looked idle. The page compares `snapshot.interrupts`
against the parsed reviews and refuses loudly when they disagree, matching
`cli._collect_decisions`, which raises rather than continuing.

**A turn can stop with `next` set and no interrupt.** That is not an approval waiting to
be answered — it is an unfinished tool call, and LangGraph continues it by streaming
`None`. The web UI shows a resume button for it, keyed off `snapshot.next`; without one
the thread sits on the pending node and every later message queues behind it. Found live
when the host reaped the server mid-`task`. The approval branch takes precedence, since a
real interrupt also leaves `next` set — `test_a_pending_approval_takes_precedence_over_resume`
guards that ordering.

**State lives outside `workspace/`.** `.state/` is a *sibling*. A SQLite file under the
agent's root would expose every user's memories and every thread's checkpoints in raw
form, bypassing namespacing entirely. `_check_db_outside_workspace` enforces this for
operator-supplied `--db` too.

**Namespace components exclude the period.** `context._SAFE_COMPONENT` is the
*intersection* of two disagreeing validators: `deepagents.backends.store` allows `.`,
`langgraph.store.base` rejects it — and only at **write** time, after construction, reads,
and `ls` all succeed. The separator is a hyphen for the same reason. A digest of the raw id
is appended because sanitization is lossy (`a/b` and `a b` both become `a_b`).

**`PlannerContext.user_id` must default to `None`, never a placeholder.** LangGraph builds
the dataclass from `context={}`, so a truthy default sends every unidentified caller down
the *identified* branch into one shared bucket. `--user` defaults to nothing for the same
reason. With no id, storage scopes to the thread.

**Store keys are untrusted paths.** `cli._export` treats them as agent-chosen input and
validates against traversal before writing to `exports/`.

**The step budget is `6 + 4N` for N tool round trips.** Five middleware nodes run per model
turn (three `before_agent`, two `after_model`), and LangGraph counts each as a super-step.
Its default `recursion_limit` of 25 strands a session after ~5 tool calls; the CLI sets 200.
Adding middleware changes this constant — `test_step_budget_survives_a_long_planning_session`
guards it.

## deepagents 0.7.1 vs. published docs

Three documented behaviours don't match the installed package:

1. **`write_todos` is not bound by default.** `TodoListMiddleware()` is added explicitly in
   `agent.py`; the orchestrator prompt instructs the model to plan with it.
   `test_planning_tool_is_bound` guards this.
2. **Backend constructors changed.** Actual signatures are `StateBackend()` and
   `StoreBackend(*, namespace, store=None)`; `backend=` takes an instance, not a factory.
3. **`create_deep_agent` gained `memory=[...]`** (via `MemoryMiddleware`), undocumented.

When package behaviour and docs conflict, inspect the installed package and add a test.

## Testing conventions

The model is faked throughout (`ScriptedModel` in `conftest.py`, which stubs `bind_tools`
to record what was bound). Tests cover the harness — approval gating, storage routing, tool
binding, step budget — not model quality, and must keep running without an API key or
network. Files are split by concern: `test_harness.py`, `test_security.py`,
`test_approval_cli.py`, `test_webui.py`, `test_streamlit_page.py`, `test_tools.py`.

`test_streamlit_page.py` runs the real page through `streamlit.testing.v1.AppTest`,
which execs the script and exposes its widgets. Two things make that work: the page
imports `build_agent` at exec time, so patching `event_planner.agent.build_agent`
before `.run()` substitutes a fake graph; and `st.cache_resource.clear()` between cases
is required, since the page caches the graph across reruns by design. `ANTHROPIC_API_KEY`
is set to a dummy value only to clear the credential gate — no model is built and nothing
leaves the process. Prefer this over asserting on rendering helpers: the bugs live in the
wiring, and the disabled-button bug above was invisible to every unit-level test.

Stub tools (`search_venues`, `check_availability`, `search_vendors`, `hold_venue`,
`send_invitations`) are deterministic on purpose so a behaviour regression is visible rather
than blamed on a vendor API. `estimate_budget` is real arithmetic; `web_search` is live
Tavily and degrades to an explanatory string without `TAVILY_API_KEY`.

Model-dependent behaviour that offline tests cannot reach — adaptive thinking blocks in a
resumed approval, skill application, cross-session recall — is verified against the live
model and recorded in README.md's "Verified live" section. Update it when those paths change.
