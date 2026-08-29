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

uv run pytest                              # 224 tests, fully offline
uv run pytest tests/test_security.py       # one file
uv run pytest -k namespaces                # one pattern
uv run pytest tests/test_tools.py::test_hold_refuses_an_unknown_venue -v

uv run event-planner                       # interactive CLI
uv run event-planner --user alice@example.com --thread offsite-2026
uv run streamlit run streamlit_app.py      # browser UI (EVENT_PLANNER_DB overrides the db)
uv run --with "langgraph-cli[inmem]" langgraph dev
                                           # LangGraph Studio. `langgraph-cli` is NOT a
                                           # dependency and is in no lockfile, so a bare
                                           # `uv run langgraph dev` fails to spawn; the
                                           # `--with` form keeps the locked env the graph
                                           # needs. Host supplies persistence.

uvx ruff check .                           # lint  — config in pyproject.toml, not a dep
uvx ruff format .                          # format — enforced by CI, run before committing
uvx ty check                               # types — config in pyproject.toml, not a dep
                                           # These three resolve LATEST; the hook and CI
                                           # pin older ones — see two paragraphs below.
```

**Three Claude Code hooks are live**, configured in `.claude/settings.json`; the scripts and
their full rationale are in `.claude/hooks/README.md`. All three share the matcher
`Write|Edit|NotebookEdit` — a notebook write is gated exactly as a plain one is, which an
earlier bypass in this repo got wrong. PreToolUse `protect_files.sh` **blocks** such a write to
`.env*` (not `.env.example`) and to `.claude/hooks/` itself, so changing a guard
is an operator action taken outside a session — it deliberately does *not* guard
`settings.json`, which is where a hook is removed. PostToolUse `lint_gate.sh` runs ruff and ty
on any edited `.py` (~185 ms), and `test_gate.sh` runs the whole suite after an edit under
`src/event_planner/`, `tests/`, `pyproject.toml` or `uv.lock` (~6 s — the figures in that
script's header are deliberately not re-measured, so treat them as an order of magnitude).
Both exit 2, so their findings arrive as tool feedback rather than as a silently passing
edit. Hook *config* is snapshotted at session start, while the hook *scripts* are re-read
from disk on every call.
None of them see Bash — a shell redirect reaches nothing here.

**The lint and type versions are pinned in two files that must agree.**
`.claude/hooks/lint_gate.sh` carries `RUFF="ruff@0.16.1"` and `TY="ty@0.0.65"`;
`.github/workflows/ci.yml` sets the same two in `env:` and its `static` job greps the hook with
`grep -qxF` — a **whole-line** match, so those two assignments must stay exactly as written: no
`export`, no trailing comment, nothing else on the line. Bump both or neither, and note the
edit to `lint_gate.sh` is one `protect_files.sh` blocks. The `uvx` lines above are deliberately
unpinned and today resolve newer, so a finding you see locally is not necessarily one CI
reports, or vice versa.

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
That job installs `--no-dev --no-editable`, asserts streamlit is *absent* — without which
it would pass vacuously the moment `--no-sync` came off — imports `agent`, `cli` and
`webui`, and then asserts the installed copy resolves its skills and refuses a write to
its shared root. The *skills* assertion is the one pytest structurally cannot make: a checkout
cannot tell a package-relative root from one derived by counting levels up from
`__file__`, because in a checkout both land on a real directory. Only an installed copy
separates them, and this job is the only place that exists. The write refusal is not in that
class — `test_the_real_shared_root_refuses_a_write` makes it in a plain checkout, and the job
re-asserts it against the installed copy.

`.streamlit/config.toml` is committed app configuration and its header carries the rationale —
read it before changing the bind or the theme. It already covers config resolution — under
`streamlit run` the file is **script-level** and therefore applies from any CWD, while
**`pytest` reads it as project-level** (AppTest sets no script path, and the suite runs from
the repo root), so a `runner.*` or `global.*` key added there still changes how the suite
executes — plus why the bind matters with no authentication in front of the page, and why the
theme needs both `[theme.light]` and `[theme.dark]`. An earlier note in all four of these
files claimed config came from the *working directory*; it was wrong, and it aimed the runtime
warning's remedy at a knob that changes nothing. The thing the config cannot do is enforce
itself — a `--server.address` flag or `STREAMLIT_SERVER_ADDRESS` overrides it — so
`streamlit_app.py` re-checks `server.address` at runtime and warns in the page when the bind is
not loopback. `.streamlit/secrets.toml` is gitignored; credentials stay in `.env`.

Ruff is configured in `pyproject.toml` but is **not** a dependency — run it with
`uvx ruff check .`. The rule set is chosen so the `# noqa` codes in the source
(`BLE001` on the seven deliberate blind excepts — three under `src/`, four in
`streamlit_app.py`, two of them the guards on `graph.get_state`) suppress rules that are
actually enabled; `RUF100` fails the check if one goes stale. `ANN401` is ignored because `Any` is honest at the deepagents/langgraph
boundary, and `tests/*` ignores `ANN`/`RUF012`
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

`CompositeBackend` matches the **longest route prefix first**, and the read-only
filesystem backend is the **default**, not a route:

| Path | Backend | Visibility |
| --- | --- | --- |
| `/memories/` | `StoreBackend(namespace=memory_namespace)` | Per user, across sessions |
| `/events/` | `StoreBackend(namespace=events_namespace)` | Per user, across sessions |
| `/artifacts/` (`ARTIFACTS_ROOT`) | `StoreBackend(namespace=artifacts_namespace)` | Per user, across sessions |
| everything else | `ReadOnlyFilesystemBackend(root_dir=<package>/workspace/, virtual_mode=True)` | Shared, and refuses writes |

`/artifacts/` is deepagents' territory: `FilesystemMiddleware` derives
`<root>/large_tool_results/` and `<root>/conversation_history/` from the composite's
`artifacts_root` and offloads client data there on its own, so nothing in this repo names
those paths at a call site — which is why the route was missed. README's Storage section has
the token limits and what spills into each.

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

That root is `src/event_planner/workspace/` — **inside the package**, derived from the package
directory rather than by counting `parents[N]` up from `agent.py`, so a wheel carries it and an
installed copy resolves its skills (`agent.py`'s `WORKSPACE` comment records the install that
silently loaded none). Two consequences follow.
`build_backend` **raises** when `skills/` is missing, on the same rule as `build_agent`
refusing an un-gated agent. And the root **refuses every write**
(`ReadOnlyFilesystemBackend`), because a writable directory inside the package is a file
on the import path, and a writable `/skills/` lets whatever `web_search` returns rewrite
the guidance every tenant's next session loads. Nothing legitimate writes there — every
write the prompts ask for is routed.

`virtual_mode=True` is the **read** half of that same boundary, and the table above describes
only the write half. Under `virtual_mode=False` deepagents documents the backend as letting an
absolute path bypass `root_dir` entirely and a relative `..` escape it, so the agent's
`ls`/`read_file`/`glob`/`grep` would reach the repo's own source and anything else on disk. Be
precise about the hazard: 0.7.9 already **defaults** it to `True`, so deleting the keyword is a
no-op today. It is passed explicitly because that default is deepagents' to change, and because
no test asserts `build_backend()` sets it — a release that flipped it would break nothing
visible here.

Two tests hold the write half:
`test_the_read_only_root_refuses_every_mutator` drives all eight methods and checks the
file survives, and `test_the_backend_surface_has_not_moved` compares deepagents' whole
public surface against a recorded baseline — because a check that filters `dir()` for the
mutator names it already knows can never see one that was just added.

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

Skills live in `src/event_planner/workspace/skills/*/SKILL.md` as plain markdown with YAML frontmatter
(`name`, `description`). They are shared reference material, versioned in git, and read by
the agent at runtime — they're behaviour, not documentation.

## Invariants that fail silently

Each is enforced by a test except where it says otherwise; breaking one usually produces
working-looking code.

**`interrupt_on` is a no-op without a checkpointer.** `build_agent` raises rather than
returning an agent whose approval gates don't gate. Only `hosted=True` suppresses this.

**A `store` is required too, and — alone in this section — no test enforces it.** With a
checkpointer alone `build_agent` constructs fine and then dies on the first invoke:
`AttributeError: 'NoneType' object has no attribute 'get'`, from `store.get(namespace, path)`
inside `MemoryMiddleware.before_agent`, because `memory=["/memories/AGENTS.md"]` downloads
through `StoreBackend`. Every real call site passes one (`cli.py`, `streamlit_app.py`, and
`tests/test_harness.py`'s `_agent`, which defaults to `InMemoryStore()`), so a new entry point,
repro script, or test must too — nothing in that traceback names `build_agent` or its `store=`
parameter. The guard belongs beside the checkpointer's in `build_agent`; until it is written,
this bullet is what stands in for it.

**Approval-gated tools come from one list.** `IRREVERSIBLE_TOOLS` in `tools/__init__.py`
is the source; `INTERRUPT_ON` is derived from it. A new money-spending or guest-contacting
tool goes in that list — never in two hand-maintained copies.

**A tool rename must land on every side of the three-file loop, and three tests say so.**
`test_every_bound_tool_is_named_somewhere` catches a tool bound but named by no prompt,
checked **globally** — the orchestrator names only two of its seven tools and delegates the
rest, so a per-agent version reports five false positives.
`test_no_prompt_instructs_a_tool_its_agent_cannot_call` is its complement and catches what a
global check structurally cannot: binding is **per agent**, so budget-analyst's prompt naming
`hold_venue` passes the first test while the subagent burns a turn on a tool it was never
given. Its `permitted` allowlist is empty and must be edited deliberately — prose that names
another agent's tool goes there with the sentence that justifies it, rather than the check
being deleted. Both live in `test_harness.py`.
`test_every_irreversible_tool_is_actually_bound` stays in `test_security.py` beside
`test_every_irreversible_tool_is_gated`, which cannot see it: that one asserts
`set(INTERRUPT_ON) == set(IRREVERSIBLE_TOOLS)`, which holds by construction. Shared helpers
(`BACKTICKED`, `agent_bindings`, `bound_tool_names`) live in `conftest.py`, one copy.

**`respond` is excluded from `ALLOWED_DECISIONS`.** Only `approve`/`edit`/`reject`. A
free-text reply to a booking request invites the model to read commentary as confirmation.
Relatedly, `cli._resolve_choice` never matches ambiguously (`reject`/`respond` share `r`),
and `_decline_message` frames refusals as a human decision so the model doesn't read them
as a tool error and retry. `webui.SUPPORTED_DECISIONS` holds the same three and reports
anything else as unsupported rather than rendering it.

**Two front ends can refuse a booking, and they must do it identically.** `webui.py` imports
everything shared from `cli.py` rather than restating it — nine names: `_decline_message`,
`_check_db_outside_workspace`, `UnsafeDatabaseLocation`, `credentials_problem`,
`checkout_warning`, `degraded_capability_note`, `DEFAULT_MAX_STEPS`, and `_stored`/`_brief_args`.
Its module docstring gives the reasons for five of them; the other four are recorded only here.
Identity, not equivalence: the re-exports are the CLI's own objects and
`test_the_shared_helpers_are_the_clis_own_objects` asserts it. The wording of a refusal is
behavioural, not cosmetic, and a divergent copy would surface only as a booking retried after
a human said no; `test_reject_matches_the_cli_byte_for_byte` and `test_edit_matches_the_cli`
drive `cli._prompt_one` with scripted stdin and compare its payload against the web builder's.
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
does not close what it drops, and the cache key includes a free-text model field. That cache is
`scope="session"` rather than the default `"global"` for the same reason: process-wide, the
fifth distinct model string typed in *any other* browser session evicts this session's entry,
and `on_release` then closes both connections out from under a `graph.stream` still running on
them. Nothing refcounts them, `validate=` does not help (it runs on the entry still in cache),
and a larger `max_entries` only postpones it.

**Model prose reaches `st.markdown`, which renders `$...$` as LaTeX.** Any line quoting
two costs — which in this domain is most of them — has the span between them swallowed and
re-set as italic mathematics. Seen live on the `hold_venue` recommendation: "$10,281 —
$1,719 under your $12,000 ceiling" rendered as an equation. `webui.markdown_safe` escapes
bare `$` on every prose render path (assistant text, the operator's own message on both the
replay and the echo).

It stops at code spans, fenced blocks and URLs, and stopping there is not a gap: CommonMark
does not process escapes inside code, so the backslash was being *rendered* — a fenced budget
table came out as `\$3,200` — and the maths tokenizer never fires inside code either, so
there was nothing to prevent.

**Interpolated agent values take the other helper.** `webui.markdown_literal` escapes the GFM
inline metacharacters, and the tool-call captions and the download-button labels use it rather
than `markdown_safe`: those are the model's own argument values and store keys, which nobody
intends to be formatted. Left as markdown, `query='rooftop *loft*'` showed the operator
italics and no asterisks — arguments that differ from the ones that would execute, in the
panel whose whole job is checking them. So a new `st.markdown`/`st.caption` carrying model or
operator text needs one of the two — prose takes `markdown_safe`, values take
`markdown_literal`; `st.code` and `st.json` need neither, since neither parses markdown.

It is *not* "everything markdown reads": a bare `https://…` still autolinks, and escaping a
URL out of that would mean mangling the value. That residue is deliberate and it is the line
worth holding — an autolink renders its own text unchanged, so the operator still reads the
value that is there, while `*loft*` losing its asterisks means they do not.

**`markdown_safe`'s region arms have to be conservative, and each one was a leak first.** A
region arm that matches too much is worse than no arm at all: the old unconditional escape
was merely ugly inside code, while a greedy region ships real costs to KaTeX. Three arms are
written the way they are for measured reasons — the closing fence is `{3,}` rather than a
backreference (CommonMark lets ``` close with ````), both closing fences carry `\r?` (`$`
under MULTILINE matches only before `\n`, so CRLF closed no fence at all), and the code-span
arm cannot cross a blank line (backticks in two paragraphs never pair in CommonMark, and one
unbalanced backtick — guaranteed in a message truncated mid-span by streaming — otherwise
made whole paragraphs of money "code"). In each case the `|\Z` arm then swallowed the rest of
the message. `test_a_malformed_region_never_swallows_the_money` is the parametrised guard.

**A collapsed `st.expander` still computes and ships its body.** Closed is a frontend
state, not a guard — and since the page replays the whole checkpointed transcript on every
rerun, an ungated tool-result panel re-serialises every result in the thread on every
sidebar keystroke. Tool output dominates transcript size — the measured share and the worst
single result are in `_render_tool`'s docstring, taken against threads in a local
`.state/planner.sqlite` that no fresh clone has. `_render_tool` gates on `on_change="rerun"`
plus `panel.open`, which makes opening a panel a full app rerun — still the cheaper side,
since that rerun no longer
carries the other bodies, and safe beside a pending approval because `review_token` does
not move, so an in-progress decision is restored rather than cleared.

Three things follow, and each one bites silently. **The key must identify the message**:
gating promotes the expander to a widget, widget keys must be unique, and an auto-generated
key derives from the label — which repeats many times in any real thread. A positional
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

**Nothing that queues a rerun may stay live while a turn streams, and the rule is wider than
the tool panels it was written for.** `RerunException` subclasses `BaseException`, so the
turn's `except Exception` misses it and `graph.stream` is abandoned with nothing shown — on a
turn that ran 672s live. `submit_mode="disable"` and `gated=not turn_pending` were the first
two answers; the sidebar's three text inputs and the stored-file downloads were both still
live in that window. The downloads now take `on_click="ignore"`, which is the clean fix: it
removes the rerun at the source rather than the click, so nothing has to be lifted afterwards
and a download needs no server-side rerun anyway (the bytes come over a separate media URL).

**The sidebar fields deliberately keep no such gate, and the reason generalises.**
`disabled=turn_pending` is the obvious fix and it cannot lift: `turn_pending` is read once per
run, and the run that consumes `pending_input` draws the sidebar disabled from top to bottom,
so the only way back to a live sidebar is a second run after the turn. That rerun makes the
*entire streaming run* invisible to `AppTest` — which is where this page's invariants are
pinned — taking `test_panels_are_inert_on_a_run_that_streams_a_turn` and
`test_a_resumed_proposal_is_replaced_rather_than_drawn_twice` green-for-the-wrong-reason with
it. Both were measured doing exactly that before the gate was reverted. Trading a tested
invariant for an untested one is the wrong direction here, so the hazard is documented instead:
changing **Thread** mid-turn abandons the stream and leaves `next` set on the thread the
operator just left, and the "Resume unfinished turn" button is the affordance that recovers it.
The same reasoning is why that button takes no gate either — it renders *after* `_stream_turn`,
so it never exists inside the window at all, and gating it only killed it on the one run a
failed resume produces.

**The replay and the live stream can carry the same message, and appending both misleads.**
`HumanInTheLoopMiddleware.after_model` re-emits the proposing message as its node update, and
the model node that produced it was checkpointed a super-step earlier — so the run that
resumes a decision draws it twice. For approve and reject that is two identical bubbles; for
`edit` the replayed copy carries the model's arguments and the streamed copy the operator's,
with nothing saying which executed. `_replay` gives each checkpointed message its own
`st.empty()` keyed by message id and `_stream_turn` writes back into it, so the re-emitted
copy wins. It self-corrects on the next rerun (`add_messages` dedupes on id) — but the run it
is wrong on is the run that commits a booking.
`test_a_resumed_proposal_is_replaced_rather_than_drawn_twice` guards it.

The same gate is on the approval panel's "Middleware note", keyed on the action token —
inside the fragment `rerun` reruns the fragment, and no turn is in flight to interrupt
because the graph is parked waiting on that panel.

**The approval panel is an `st.fragment`, so it must not read graph state *while rendering*.**
`_approval_panel` reruns in isolation on every widget change — that is the point, since the
alternative replays the whole checkpointed transcript to redraw one segmented control. It is
sound only because everything it *draws* is already resolved into its `reviews` argument. A
`graph.get_state` or `snapshot.` read on the render path would serve a panel built from state
the fragment never refreshes. `test_the_panel_reads_no_graph_state_while_rendering` pins it by
counting reads rather than inspecting the source, since the hazard is a call anywhere in the
panel including inside a helper — and counting is what stands in for the fragment-scoped rerun
`AppTest` cannot drive (it builds a fresh `LocalScriptRunner` per call and passes no
`fragment_id_queue`, so under test the fragment only ever executes inline during a full run).

**The one graph read that belongs there is at submit time, and it is load-bearing.** `reviews`
is as old as the last *full* run, so another browser tab or a CLI turn on the same thread can
answer this interrupt first. After the operator commits, the panel re-reads live state,
compares `_review_tokens` against its own, and sets `stale_approval` — surfaced as a warning
on the next full run — instead of resuming a graph that is no longer asking. That is a
compare-and-swap, not a render, which is why it does not violate the rule above; the `graph`
and `config` arguments the panel takes exist for it. `st.rerun()` then defaults to
`scope="app"`, which is what lets the turn run from the main script against fresh state.

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

**Approval widget identity follows the action and the attempt — never position alone.**
Streamlit restores a keyed widget's value whenever a widget with that key renders again. With
`key=f"choice-{index}"`, resolving one approval and immediately interrupting for a *different*
action reused the key, so the new panel rendered pre-approved with submit enabled — one click
executing something nobody reviewed. `edit` was worse: a stored value beats the `value=`
argument, so the new action's box came back holding the previous action's arguments and would
have executed the wrong tool with them. `webui.review_token` therefore hashes four things —
checkpoint id, index, action name, args. The checkpoint id is fresh per interrupt but stable
across the reruns *within* one approval, which is what lets a selection survive long enough to
submit; the index is still needed because two actions in one interrupt share a checkpoint, so
dropping it collides their widgets. `streamlit_app.py` additionally prefixes
`st.session_state.turn_attempt`, because a resume that raises *before* the graph advances
(locked database, 429, server reaped mid-turn) leaves the checkpoint id identical and would
rebuild the panel pre-armed with the decision just submitted. Guarded by
`test_a_second_interrupt_is_not_pre_approved`,
`test_widget_identity_changes_per_pending_action`, and
`test_a_failed_resume_does_not_leave_the_panel_pre_armed`.

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

**State lives outside the agent's root.** `.state/` sits at the repo root, and the root is now inside the package. A SQLite file under the
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

**`store.search` returns 10 rows unless you ask for more, and nothing at the call site says
so.** `langgraph.store.base` defaults `limit=10`, so `cli._stored`'s
`tuple(store.search(namespace))` read as "everything" and meant "ten" — silently, with no
error and no truncation marker. It was invisible because `_stored` backs *both* front ends:
the browser's "Stored" panel and the CLI's `/state` and `/export` under-reported by exactly
the same amount, and the two front ends agreeing is normally this repo's proof of
correctness. `/export` even printed the count as though it were the whole set. `_stored` now
pages with `limit=_STORE_PAGE, offset=len(items)`; `artifacts` crosses ten first, since it
holds deepagents' offload spill and nothing evicts it. Guarded by
`test_stored_items_lists_past_the_stores_default_page`, which asserts the bare call still
returns 10 so the test cannot pass by the default quietly changing.

**Store keys are untrusted paths.** `cli._export` treats them as agent-chosen input and
validates against traversal before writing to `exports/`.

**The step budget is `6 + 4N` for N tool round trips.** LangGraph counts every node as a
super-step, and the two kinds of middleware node do not run at the same rate: the three
`before_agent` nodes (Skills, PatchToolCalls, Memory) run **once per invocation**, the two
`after_model` nodes (HumanInTheLoop, TodoList) run **per model call**. That asymmetry is what
`6 + 4N` encodes — counting all five per turn predicts 13 steps for one round trip against the
10 measured. `test_harness.py`'s `_FIXED_OVERHEAD` comment records the correct decomposition.
README's
"Step budget" has the trace, and explains why the front ends' `DEFAULT_MAX_STEPS = 200`
*lowers* `create_deep_agent`'s `recursion_limit: 9_999` rather than raising LangGraph's
default 25. Adding middleware changes this constant —
`test_step_budget_survives_a_long_planning_session` guards it.

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
