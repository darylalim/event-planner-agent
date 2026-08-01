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
uv sync                                    # install (Python >=3.11, uv required)
cp .env.example .env                       # then fill in ANTHROPIC_API_KEY

uv run pytest                              # 90 tests, ~1.5s, fully offline
uv run pytest tests/test_security.py       # one file
uv run pytest -k namespaces                # one pattern
uv run pytest tests/test_tools.py::test_hold_refuses_an_unknown_venue -v

uv run event-planner                       # interactive CLI
uv run event-planner --user alice@example.com --thread offsite-2026
uv run langgraph dev                       # LangGraph Studio (host supplies persistence)
```

No linter or type checker is configured, though the source carries ruff `# noqa` codes
(`BLE001`, `ANN001`). If you add one, run it via `uvx` rather than adding a dep.

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
as a tool error and retry.

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
`test_approval_cli.py`, `test_tools.py`.

Stub tools (`search_venues`, `check_availability`, `search_vendors`, `hold_venue`,
`send_invitations`) are deterministic on purpose so a behaviour regression is visible rather
than blamed on a vendor API. `estimate_budget` is real arithmetic; `web_search` is live
Tavily and degrades to an explanatory string without `TAVILY_API_KEY`.

Model-dependent behaviour that offline tests cannot reach — adaptive thinking blocks in a
resumed approval, skill application, cross-session recall — is verified against the live
model and recorded in README.md's "Verified live" section. Update it when those paths change.
