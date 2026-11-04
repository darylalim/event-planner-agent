# Plan: close two documented harness gaps

Status: done 2026-11-04, pending merge. Written 2026-11-04 for a new session to carry out.
Do `dependency-upgrade.md` first, for its uv version fix. No live model run is needed.

## Goal

Close two gaps `CLAUDE.md` records as untested: `build_agent` accepting a missing
`store`, and prompt file paths drifting from the routes in `build_backend()`.

## Gap 1: `build_agent` accepts a missing store

### Problem

With a checkpointer and no store, `build_agent` succeeds and then the first invoke fails
with `AttributeError: 'NoneType' object has no attribute 'get'`, from inside
`MemoryMiddleware.before_agent`. Nothing in that traceback names `store=`.

The `build_agent` docstring is also wrong. It says that without a store "memory does not
persist across threads", which describes a degraded mode that doesn't exist.

### Change

In `src/event_planner/agent.py`, add a guard directly **after** the checkpointer guard:

```python
if store is None and not hosted:
    msg = (
        "no store was supplied, so the first invoke would fail while loading "
        "/memories/AGENTS.md. Pass a store (e.g. InMemoryStore()), or hosted=True "
        "when the host supplies one."
    )
    raise ValueError(msg)
```

Order matters. `test_missing_checkpointer_is_rejected` calls `build_agent(model=scripted())`
with neither argument and expects "no checkpointer", so that guard must stay first.

Also update the docstring: the `store` argument, the `hosted` note ("Suppresses the guard
below", now two guards), and `Raises`.

### Tests

Add to `tests/test_harness.py`, beside the checkpointer tests:

- `test_missing_store_is_rejected`: a checkpointer and no store raises `ValueError`
  matching "no store".
- Extend `test_hosted_mode_allows_missing_checkpointer`, or add a sibling test, so it's
  clear `hosted=True` also passes with no store.

Before adding the guard, confirm the failure is still as described, since the upgrade may
have changed it. After adding it, any existing test or call site that builds without a
store fails, so fix those. All known real call sites already pass one.

## Gap 2: prompt paths can drift from routes

### Problem

Prompts name paths such as `/events/<event-slug>/brief.md` as literal strings. Nothing
checks them against `build_backend()`. Rename a route, or mistype a prompt, and the agent
is told to write somewhere that lands on the shared root, which refuses the write. That
fails at runtime, where the offline suite can't see it.

### Change

Add `test_every_prompt_path_is_routed` to `tests/test_harness.py`, next to the other
drift tests. Put any shared helper in `conftest.py`, as those tests do.

1. Collect text from every prompt in `prompts.py`, every subagent spec, and every
   `SKILL.md` under `src/event_planner/workspace/skills/`.
2. Extract absolute virtual paths. Use a pattern anchored on a leading `/`, a lower-case
   segment and a further `/`, so prose such as "and/or" doesn't match.
3. Read the allowed prefixes **from the real backend** rather than a hardcoded list,
   otherwise renaming a route can't fail the test. `CompositeBackend.routes` was a public
   attribute on 2026-11-04, so use `build_backend().routes`. The read-only default isn't
   in it, which is why step 4 handles `/skills/` separately.
4. Assert each path starts with a routed prefix or is a `/skills/` path.
5. For `/skills/` paths, assert the directory exists under `WORKSPACE`, since those are
   read from the shared root.

Follow `test_no_prompt_instructs_a_tool_its_agent_cannot_call`: keep a `permitted`
allowlist that starts empty, so an exception needs an explicit edit and a reason.

Also assert the collected list isn't empty and contains `/events/` and `/memories/`
paths. Otherwise a broken pattern would pass with nothing to check.

### Prove it can fail

Temporarily make each change below, confirm the test fails, and revert it:

- Change `/events/` to `/event/` in one prompt.
- Rename the `/events/` route in `build_backend()`.
- Change one `/skills/` path in `subagents.py` to a directory that doesn't exist.

Record the outcome in the pull request or the final report.

## Docs

Update `CLAUDE.md`:

- Replace "A `store` is required too, and — alone in this section — no test enforces it."
  and that bullet's closing lines. Keep the traceback, and name the guard and its test.
- Update the "Path drift is still uncovered" sentence in "The three-file loop".
- Update "the three drift tests" wording and the test count in Commands.

## Verification

```bash
uv run pytest
uvx ruff check .
uvx ruff format --check .
uvx ty check
```

Also smoke-check each entry point still builds, without calling a model:

- `uv run event-planner --help`
- `uv run --with "langgraph-cli[inmem]" langgraph dev`, which uses `hosted=True` and must
  not hit the new guard. It keeps running, so stop it once the graph has loaded

## Out of scope

Changing `MemoryMiddleware`, adding a fallback store, or checking relative paths.
