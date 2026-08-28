# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

An event planning agent built on [Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview)
(`deepagents` 0.7.9), which wraps LangGraph. `create_deep_agent` returns a compiled
LangGraph graph, so checkpointers, `interrupt()`, streaming, and Studio all work underneath.

`README.md` is detailed and current — read it for the *why* behind the design, the
storage/tenant-isolation rationale, and the recorded live-run results. This file covers
what you need to *change code* safely.

## Commands

```bash
uv sync                                    # install (uv required; .python-version pins 3.14)
                                           # uv itself is pinned: pyproject's
                                           # [tool.uv] required-version gates
                                           # every `uv` line below (not `uvx`).
                                           # Mismatch -> `uv self update 0.12.5`
cp .env.example .env                       # then fill in ANTHROPIC_API_KEY

uv run pytest                              # 204 tests, ~6s, fully offline
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

`streamlit` is a **`web` extra**, not a core dependency. `langgraph.json` installs a plain
`.`, so leaving it in `[project.dependencies]` shipped Streamlit and ~27 transitive packages
(pandas, pyarrow, altair, pydeck) in a deployment image whose graph never imports them — 98
resolved packages against 65. The `dev` group self-references `event-planner-agent[web]`, so
a checkout still gets the front end from `uv sync` alone and CI's plain `uv sync --locked`
runs the AppTest suite unchanged. A **non-dev** install that wants the page needs
`uv sync --extra web` (or `pip install '.[web]'`); `uv run event-planner` never does.

That split is what the `deploy-shape` job in CI exists for. Every other job runs
`uv sync --locked` and therefore gets the dev group, so a module-scope `import streamlit`
in `src/` passes ruff, ty and all four pytest legs while breaking only the deployed graph.
That job installs `--no-dev`, asserts streamlit is *absent* — without which it would pass
vacuously the moment `--no-sync` came off — and imports `agent`, `cli` and `webui`. It is
the one check here that cannot be a test: pytest runs inside the dev environment and
cannot conjure one without it.

`.streamlit/config.toml` is committed app configuration. **Streamlit resolves it from the
current working directory, not from the script's directory** — measured: from another CWD
`config.get_option("server.address")` comes back `None`, Streamlit's bind-to-every-interface
default, and the theme silently vanishes too. So it is not the property of the app it reads
like; it is a property of being launched from the repo root. `streamlit_app.py` therefore
checks `server.address` at runtime and warns in the page when the bind is not loopback,
because a config file cannot enforce itself. That bind matters because the page has no
authentication — "User id" is a free-text field, so anyone who can reach the port can name
any tenant.

CWD-resolution also means the **test suite does read this file**: pytest runs from the repo
root, so `AppTest` picks up the project config. No server is started, so `server.*` is
inert there, but a `runner.*` or `global.*` option added here would change how tests
execute. `langgraph dev` and the CLI are unaffected — neither is `streamlit run`.

The theme defines **both** `[theme.light]` and `[theme.dark]`; a single `[theme]` block
locks the app to one mode and removes the toggle. `primaryColor` is `#5850EC` because
Streamlit renders white text on primary buttons, so that colour has to clear 4.5:1 against
white *and* 3:1 against each background — the obvious indigo-500 (`#6366F1`) fails the
first at 4.47:1, and the primary button here is the one that commits money. Note those
measurements do **not** describe badges: given only `redColor`, Streamlit derives the badge
fill at 10%/20% opacity and the badge text at ±15% lightness, so `st.badge(color="red")`
renders neither the configured colour nor the pairing that was measured.
`.streamlit/secrets.toml` is gitignored; credentials stay in `.env`.

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
| `/artifacts/` (`ARTIFACTS_ROOT`) | `StoreBackend(namespace=artifacts_namespace)` | Per user, across sessions |
| everything else | `FilesystemBackend(root_dir=workspace/, virtual_mode=True)` | **Shared across all sessions** |

`/artifacts/` is deepagents' territory, not ours, and nothing in this repo writes beneath it —
which is why it was missed. `FilesystemMiddleware` derives `<root>/large_tool_results/` and
`<root>/conversation_history/` from the composite's `artifacts_root` and offloads on its own
once a tool result passes `tool_token_limit_before_evict` (20k tokens) or a human message
passes `human_message_token_limit_before_evict` (50k). The two carry different things and both
belong to the client: a tool's output — a named account's shortlist or costed budget — and the
planner's own typed brief, which `_evict_and_truncate_messages` takes from the last
`HumanMessage`, never from a tool result.

**Route the root, never the names derived beneath it.** Those names are deepagents' to change,
so two hardcoded prefixes fail open on a rename with nothing to go red. Routing them
separately is worse than useless: `StoreBackend` strips the matched prefix before keying, so
two routes sharing `artifacts_namespace` flatten into one bucket and each directory lists and
reads the other's files — which matters because deepagents tells the model to grep
`/large_tool_results/` to recover an offload. `test_the_two_offload_paths_do_not_alias` and
`test_the_prefixes_deepagents_derives_stay_under_the_routed_root` guard both halves. The
namespace is kept apart from `/events/` so `cli._export` emits the planner's files rather than
the harness's overflow.

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
  doesn't exist. Nothing catches this at import time — the three drift tests below do,
  at test time. Path drift is still uncovered: nothing checks that `/events/...` in a
  prompt matches what `build_backend()` routes.
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

**A tool rename must land on every side of the three-file loop, and three tests say so.**
They were a PostToolUse hook (`check_prompt_drift.py`) until it was pruned; as tests they
run in CI, on all four Python versions, and for a contributor without Claude Code.
`test_every_bound_tool_is_named_somewhere` catches a tool the model was given but never
told about, checked **globally** — the orchestrator names only two of its seven tools and
delegates the rest, so a per-agent version reports five false positives.
`test_no_prompt_instructs_a_tool_its_agent_cannot_call` is its complement and catches what
a global check structurally cannot: binding is **per agent**, so budget-analyst's prompt
naming `hold_venue` passes the first test (bound somewhere, named somewhere) while the
subagent burns a turn on a tool it was never given. Its `permitted` allowlist is empty and
must be edited deliberately — prose that names another agent's tool goes there with the
sentence that justifies it, rather than the check being deleted. Both live in
`test_harness.py`, whose docstring already claims this ground.
`test_every_irreversible_tool_is_actually_bound` stays in `test_security.py` beside
`test_every_irreversible_tool_is_gated`, which cannot see it: that one asserts
`set(INTERRUPT_ON) == set(IRREVERSIBLE_TOOLS)` and `INTERRUPT_ON` is built from
`IRREVERSIBLE_TOOLS`, so it holds by construction. The shared helpers (`BACKTICKED`,
`agent_bindings`, `bound_tool_names`) live in `conftest.py` — one copy, since the rule
against two hand-maintained copies applies to tests too.

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

**Model prose reaches `st.markdown`, which renders `$...$` as LaTeX.** Any line quoting
two costs — which in this domain is most of them — has the span between them swallowed and
re-set as italic mathematics. Seen live on the `hold_venue` recommendation: "$10,281 —
$1,719 under your $12,000 ceiling" rendered as an equation. `webui.markdown_safe` escapes
bare `$` and every render path in the page goes through it (assistant prose, the operator's
own message on both the replay and the echo, and the tool-call captions). Adding a new
`st.markdown`/`st.caption` that carries model or operator text needs it too; `st.code` and
`st.json` do not, since neither parses markdown.

**A collapsed `st.expander` still computes and ships its body.** Closed is a frontend
state, not a guard — and since the page replays the whole checkpointed transcript on every
rerun, an ungated tool-result panel re-serialises every result in the thread on every
sidebar keystroke. Measured across the recorded threads in `.state/planner.sqlite`, tool
output is 37-61% of all transcript text: 70.8 KB on `full-brief-3`, whose largest single
result is 30.6 KB. `_render_tool` gates on `on_change="rerun"` plus `panel.open`, which
makes opening a panel a full app rerun — still the cheaper side, since that rerun no longer
carries the other bodies, and safe beside a pending approval because `review_token` does
not move, so an in-progress decision is restored rather than cleared.

Three things follow, and each one bites silently. **The key must identify the message**:
gating promotes the expander to a widget, widget keys must be unique, and an auto-generated
key derives from the label — which repeats seven times on `full-brief-3`. A positional
index will not do either, since `_render` is called from both the replay and mid-stream
with no shared counter. **A repeated key is fatal, not cosmetic**: it raises, and an
exception there takes the page down entirely — no transcript, no chat input, no approval
panel for a booking still parked. `_panel_key` namespaces `tool_call_id` and `id`
separately and returns `None` for anything already claimed in this run, so a duplicate
degrades to an ungated panel. **Panels must be inert on any run that also streams a turn**:
a widget toggle posts a rerun request, Streamlit raises `RerunException` at the next `st.*`
call, and that subclasses `BaseException` — so the turn's `except Exception` misses it and
`graph.stream` is abandoned. The transcript replays *before* the turn streams, so the
replay takes `gated=not turn_pending`; this is the same rule as `submit_mode="disable"` on
the chat box. Guarded by `test_a_collapsed_tool_result_is_not_sent_to_the_browser`,
`test_repeated_tool_names_get_distinct_panels`, `test_an_opened_panel_renders_its_body`,
`test_two_results_sharing_one_call_id_do_not_kill_the_page`, and
`test_panels_are_inert_on_a_run_that_streams_a_turn`.

The same gate is on the approval panel's "Middleware note", keyed on the action token —
inside the fragment `rerun` reruns the fragment, and no turn is in flight to interrupt
because the graph is parked waiting on that panel.

**The approval panel is an `st.fragment`, so it must not read fresh graph state.**
`_approval_panel` reruns in isolation on every widget change — that is the point, since
the alternative replays the whole checkpointed transcript to redraw one segmented
control. It is only sound because everything it needs is already resolved into its
`reviews` argument and cannot change while the graph is parked. Adding a `graph.get_state`
or a `snapshot.` read *inside* it reintroduces staleness that no test will catch:
`AppTest._run` builds a fresh `LocalScriptRunner` per call and never passes a
`fragment_id_queue`, so under test the fragment only ever executes inline during a full
run. Submitting escapes deliberately — `st.rerun()` defaults to `scope="app"`, which is
what lets the turn run from the main script against freshly read state.

**A disabled Streamlit button became a guard, and the reason to distrust it still stands.**
On 1.60 `disabled=` was presentation: it stopped a click in the browser and said nothing about
what reached the branch behind it, and `test_submitting_with_no_decision_sends_nothing` caught
the first version resuming the graph with an empty decision list. Streamlit 1.62 enforces it
server-side — `WidgetMetadata` carries `disabled`, and the runtime drops an incoming value for
a disabled widget as stale or forged. The submit handler still re-checks `ready`, and should:
that copy is what holds if the pin ever moves back. The upgrade did cost
`test_submitting_with_no_decision_sends_nothing` its teeth: `ready` and `disabled=` derive
from the same value, so no real click can now reach the branch with `ready` false, and it
passes whether or not the guard is there.
`test_a_submit_that_slips_past_the_disabled_button_sends_nothing` restores the coverage by
forcing the button to report a click — the only way left to exercise the case the guard is
for.

**Approval widget identity also carries the turn attempt, not just the checkpoint.**
`review_token` mixes in the checkpoint id, which only advances when the graph does — so a
resume that raises *before* any state change (locked database, 429, server reaped mid-turn)
rebuilds the panel under an identical token, and Streamlit restores the decision the
operator just submitted: primary button live, one reflexive click from executing a booking
nobody re-confirmed. `streamlit_app.py` bumps `st.session_state.turn_attempt` on every
attempt and prefixes the token with it, so a failed turn costs a deliberate re-decision.
`test_a_failed_resume_does_not_leave_the_panel_pre_armed` drives it with a graph that
raises before advancing.

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
LangGraph's own default of 25 never applies: `create_deep_agent` binds
`recursion_limit: 9_999` onto the compiled graph, so the front ends' `DEFAULT_MAX_STEPS = 200`
*lowers* that ceiling rather than raising it from 25. Dropping the explicit limit uncaps a
runaway session to 9999; it does not strand one at 25. Adding middleware changes this
constant — `test_step_budget_survives_a_long_planning_session` guards it.

## deepagents 0.7.9 vs. published docs

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

One `AppTest` trap: **an expander with an `icon` is not in `at.expander`.** `element_tree`
sorts `expandable` blocks by whether they carry an icon and routes those that do to
`Status`, so every panel this page renders — tool results and the middleware note both pass
`icon=` — is reachable only through `at.status`. An assertion written against `at.expander`
gets an empty list and fails for a reason that has nothing to do with the page;
`_panels()` in `test_streamlit_page.py` exists to keep that in one place.

Neither block exposes `.open`, which reads like "a test cannot open a panel" and is wrong —
that was claimed here and in README once, and it hid a real coverage gap. A *gated* expander
is a widget, so it registers its key in session state: `at.session_state[key] = True`
followed by `at.run()` opens it. `_panel_keys()` collects those keys.

Stub tools (`search_venues`, `check_availability`, `search_vendors`, `hold_venue`,
`send_invitations`) are deterministic on purpose so a behaviour regression is visible rather
than blamed on a vendor API. `estimate_budget` is real arithmetic; `web_search` is live
Tavily and degrades to an explanatory string without `TAVILY_API_KEY`.

Model-dependent behaviour that offline tests cannot reach — adaptive thinking blocks in a
resumed approval, skill application, cross-session recall — is verified against the live
model and recorded in README.md's "Verified live" section. Update it when those paths change.
