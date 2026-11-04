# Plan: upgrade dependencies to latest

Status: done 2026-11-04, pending merge. Written 2026-11-04 for a new session to carry out.
Do this **before** `budget-analyst-effort.md`, so that experiment measures the
dependencies the project will actually ship.

## Goal

Move `uv.lock` to the latest compatible versions with `uv lock --upgrade`, fix
whatever that breaks, and confirm agent behaviour with live runs. Step 1 also closes
the `.gitignore` gaps found on 2026-11-04, and step 10 tightens `pyproject.toml`.

## Preview

`uv lock --upgrade --dry-run` on 2026-11-04 reported 55 changes. The list will
drift, so re-run it first.

| Package | From | To | Why it matters here |
| --- | --- | --- | --- |
| `deepagents` | 0.7.21 | 0.7.32 | Harness; CLAUDE.md records several version-specific behaviours |
| `langchain` | 1.4.3 | 1.4.8 | Owns the `edit` notice and `HumanInTheLoopMiddleware` |
| `langchain-core` | 1.6.6 | 1.7.5 | Minor bump under every agent |
| `langchain-anthropic` | 1.7.5 | 1.7.9 | Model profiles, `max_tokens`, effort payload |
| `anthropic` | 1.11.0 | 2.4.0 | **Major**, transitive |
| `langgraph` | 1.2.12 | 1.2.19 | Step budget, interrupts |
| `langgraph-checkpoint` | 4.2.0 | 4.3.1 | Persisted threads |
| `streamlit` | 1.65.0 | 1.66.1 | The last bump changed `AppTest` behaviour |

Other major bumps, all transitive: `pillow` 13, `python-dateutil` 4, `multidict`
7, `websockets` 17, `wcmatch` 12, `pyarrow` 26.

Added: `opentelemetry-api`. Removed: `httptools`, `six`, `toml`.

## Prerequisite: the uv version mismatch

`pyproject.toml` required uv `==0.12.22`, but this machine had 0.13.4, so every
`uv run`, `uv sync` and `uv lock` failed. The operator chose on 2026-11-04 to move
the pin to 0.13.7 and update the machine to match, rather than downgrade a global
tool for one repo. `uv.lock` came out byte-identical under 0.12.22, 0.13.4 and
0.13.7.

## Steps

1. Create a branch from `main`. Do not commit to `main`. Then tighten `.gitignore`
   before anything else, so a stray env file or database can't be swept into this
   work. Append this block, keeping the existing lines and comments:

   ```gitignore

   # Secrets — any local env file, matching protect_files.sh
   .env.*
   !.env.example

   # A database placed outside .state/ with --db or EVENT_PLANNER_DB
   *.sqlite
   *.sqlite-*

   # Claude Code's personal settings
   .claude/settings.local.json

   # LangGraph dev server state
   .langgraph_api/

   # Build output
   dist/
   build/

   # macOS
   .DS_Store
   ```

   Verify it. The first command must list all six paths, and the second must print
   nothing, because those files stay tracked:

   ```bash
   git check-ignore .env.local planner.sqlite planner.sqlite-wal \
     .langgraph_api/x dist/x .DS_Store
   git check-ignore .env.example .streamlit/config.toml .claude/settings.json uv.lock
   git ls-files -ci --exclude-standard   # must print nothing: no tracked file is now ignored
   ```

   Keep this as its own commit, separate from the lockfile change, if the operator
   asks for commits.

2. Upgrade and install:

   ```bash
   uv lock --upgrade
   uv sync
   ```

3. Run the offline checks by hand. `test_gate.sh` does not fire here, because the
   hooks do not see Bash:

   ```bash
   uv run pytest
   uvx ruff check .
   uvx ruff format --check .
   uvx ty check
   ```

4. Fix failures one at a time. Treat each as a possible behaviour change:

   - **`test_the_backend_surface_has_not_moved`:** read the new surface before
     touching the baseline. A new mutator on the read-only root is a security
     question.
   - **`test_an_edit_is_announced_to_the_model`:** the `HumanInTheLoopMiddleware`
     merge or the `edit_notice` argument may have moved.
   - **`test_step_budget_survives_a_long_planning_session`:** added middleware
     changes `6 + 4N`. Update the constant only after re-measuring.
   - **`test_every_role_compacts_against_its_real_context_window`:** model
     profiles may have changed.
   - **`test_streamlit_page.py`:** check `AppTest` routing first.

   If a package can't be fixed reasonably, hold it back with a `<` bound in
   `pyproject.toml`, add a comment saying why, and report it.

5. Check the deployed shape. CI's `deploy-shape` job is the real test, so also
   confirm where the new package comes from and that streamlit stays out. Do not use
   `uv tree --no-dev` for this: it lists `streamlit` under the `web` extra either way.

   ```bash
   uv tree --invert --package opentelemetry-api
   uv export --frozen --no-dev --no-emit-project --no-hashes | grep -c '^streamlit=='
   ```

   The second command must print `0`. Without `--no-dev` it prints `1`, which is a quick
   way to confirm the check works.

6. Re-check the "deepagents vs. published docs" notes in CLAUDE.md against the
   installed 0.7.32, since each is version-specific.

7. Run the live checks. **These cost money, so get the operator's go-ahead:**

   ```bash
   uv run scripts/live_check.py edit --yes-spend    # ~$0.05
   uv run scripts/live_check.py brief --yes-spend   # ~$0.65-$1
   ```

   - `edit`: the resumed turn is accepted, and 45 guests is treated as settled.
   - `brief`: no `max_tokens` stops, research is delegated, and costs are in the
     recorded range.

8. Update floors in `pyproject.toml` only where the code now needs a newer
   version, as the last upgrade did (`dfe0e2f`). Re-run `uv lock` after any change.

9. Update version references, found with:

   ```bash
   grep -rn "0\.7\.21" CLAUDE.md README.md src tests
   ```

   Also update the test count in CLAUDE.md if it changed, and add the live results
   to README's "The current roster". Some references say "the same in 0.7.9 and
   0.7.21", so extend those only after step 4 confirms it.

10. Tighten `pyproject.toml`, once everything above passes. Keep this as its own
    commit if the operator asks for commits, so a lint change can't hide an upgrade
    fix.

    - **Ruff rules:** extend `select` and add `S` to the test ignores:

      ```toml
      select = ["E", "F", "I", "UP", "B", "BLE", "ANN", "RUF",
                "S", "DTZ", "PTH", "C4", "RET", "SIM", "PT"]
      ```

      ```toml
      "tests/*" = ["ANN", "RUF012", "S"]
      ```

      Measured on 2026-11-04 with ruff 0.16.1: `S` found 0 outside tests and 408
      across the repo; `DTZ`, `PTH`, `C4` and `RET` found 0; `SIM` and `PT` found 4
      (`SIM300` x2, `SIM118`, `PT018`). Fix those four rather than suppressing them.
      Re-measure first, since the upgrade step may have changed code. Do not add
      `T20` (54 findings) or `ERA`.

    - **Pytest config:** add this section:

      ```toml
      [tool.pytest.ini_options]
      testpaths = ["tests"]
      addopts = ["--strict-markers", "--strict-config"]
      ```

    - **Build backend:** change `requires = ["hatchling"]` to
      `requires = ["hatchling>=1.27"]`, which the license comment already says it
      needs.

    - **Stale comment:** the `[tool.ruff.lint]` comment says "five" `# noqa: BLE001`
      directives, but there are seven. Remove the number so it can't drift again.

    - **Metadata:** add to `[project]`, plus a `[project.urls]` table:

      ```toml
      authors = [{ name = "Daryl Lim" }]

      [project.urls]
      Repository = "https://github.com/darylalim/event-planner-agent"
      ```

      `[project.urls]` must come after the `[project]` keys. Do not add an email
      address, `keywords` or `classifiers`.

    - **Description:** set `description` in `[project]` to exactly:

      ```text
      Event planning agent built on Deep Agents: turns a brief into a costed plan, with human approval before anything is booked or sent.
      ```

      GitHub's description must match it. After this work reaches `main`, and only
      with the operator's go-ahead, because it changes the public repo page, update
      GitHub and add the three new topics:

      ```bash
      gh repo edit darylalim/event-planner-agent \
        --description "Event planning agent built on Deep Agents: turns a brief into a costed plan, with human approval before anything is booked or sent." \
        --add-topic multi-agent --add-topic llm --add-topic python
      gh repo view darylalim/event-planner-agent --json description,repositoryTopics
      ```

      The existing nine topics stay. Confirm the result shows twelve topics and the
      same description as `pyproject.toml`.

    Then run `uv lock` and re-run the step 3 checks, using the pinned linter as well,
    because CI uses that version rather than the unpinned `uvx ruff`:

    ```bash
    uvx ruff@0.16.1 check .
    uvx ruff@0.16.1 format --check .
    ```

    Update the rule-set description in CLAUDE.md to match.

## Verification

- All step 3 checks pass.
- `edit` and `brief` meet the step 7 criteria.
- If the operator asks to push, CI passes on Python 3.11-3.14 and in `deploy-shape`.
  Only CI runs that full matrix.

## Out of scope

Bumping the ruff and ty pins or the CI action SHAs. Adding
`exclude-newer`, which would hold back the newest releases this plan is asking for.
