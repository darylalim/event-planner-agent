# Plan: restructure `README.md`

Status: not started. Written 2026-11-04 for a new session to carry out.
**Do this last,** after every other plan in `README.md` in this folder, because they add
to README sections this plan moves. Line numbers below are from 2026-11-04 and will have
shifted.

## Goal

Turn the 653-line README into a short front door of about 100-150 lines, and move
the depth into `docs/`. Keep every recorded fact.

## Why

- Architecture and Verified live are 72% of the file.
- "What's real and what's stubbed" starts at line 356, so a reader can miss that
  `hold_venue` and `send_invitations` do nothing real. The intro mentions spending
  money and reaching guests, so this belongs beside it.
- `## Run` starts at line 61, after a long explanation of Python and uv versions.

## Rules

- **Move text, don't rewrite it.** The recorded figures, dates and failure stories
  are the value. Do not re-derive, round or summarise them away.
- Only new prose is the short README itself, and a one-line intro per docs file.
- The README must state only what the moved docs or the code support.
- Keep the CI badge. `ci.yml` and `release.yml` comments depend on it.
- `.claude/hooks/*.sh` mention `README.md`, but mean `.claude/hooks/README.md`, so
  they need no change. `protect_files.sh` blocks edits there anyway.

## Target layout

### `README.md`

1. Title, CI badge, what it does (keep the current intro).
2. **Status:** which tools are stubs and which are real, moved from "What's real and
   what's stubbed".
3. **Quickstart:** `uv sync`, copy `.env.example`, set `ANTHROPIC_API_KEY`, run
   `uv run event-planner`. Include the pinned uv remedy in one line.
4. **Running it:** CLI, Streamlit and Studio.
5. **How it works:** the existing architecture diagram and a short paragraph.
6. **Safety in brief:** approval gates, per-user storage, and no web access for the
   agent that holds the booking tools. Mention the Streamlit page has no
   authentication, so it should stay on loopback.
7. **Development:** tests, lint, format and types commands.
8. **Documentation:** links to the files below.
9. **License.**

"Why Deep Agents" can stay if the result fits the target length. Otherwise move it to
`docs/architecture.md`.

### Moved sections

| Current README section | Destination |
| --- | --- |
| Setup: the Python and uv version rationale | `docs/development.md` |
| Releases | `docs/releases.md` |
| Storage, Tenant isolation, Skills vs memory, Human-in-the-loop, Step budget, Tracing, The browser front end | `docs/architecture.md` |
| Verified live, and all its subsections | `docs/verified-live.md` |
| Notes on `deepagents` | `docs/deepagents-notes.md` |
| Layout | `docs/architecture.md` |

Keep the heading text unchanged, so section names stay searchable. Demote heading
levels only where the new file needs it. `docs/plans/` stays as it is.

## Steps

1. Create a branch. Save the current README for comparison:

   ```bash
   git show HEAD:README.md > <scratch>/README.before.md
   ```

2. Create the docs files by moving sections verbatim.

3. Write the new README.

4. Fix links:

   - `README.md:52` links to `#tracing`, and `README.md:375` to
     `#the-current-roster`. Point them at the new file, or at the right heading
     within it.
   - Fix relative links in moved text. For example, `LICENSE` becomes `../LICENSE`.

5. Update references elsewhere. Find them with:

   ```bash
   grep -rIn --exclude-dir=.venv --exclude-dir=.git --exclude=uv.lock README .
   ```

   - **`CLAUDE.md`:** repoint each section reference, such as "README's Storage",
     "The current roster" and "Step budget", at its new file. Update the sentence
     near the top that calls `README.md` detailed and points readers there for the
     design rationale.
   - **`scripts/live_check.py:12`** and **`:56`:** repoint at
     `docs/verified-live.md`.
   - **`.env.example`:** check its reference.
   - **Leave as they are:** historical notes such as "README put a full brief at"
     in `tests/test_live_check.py` and `scripts/live_check.py:21`, which describe the
     past. Check `tests/test_live_check.py:63` and `:163` and repoint them only if
     they direct a reader somewhere.
   - **Finished plans:** update any plan still pending that names a moved section.

## Verification

No test reads `README.md`, so nothing goes red if this is done wrong. Check by hand:

1. **Nothing was lost.** Every line of the old README must exist in the new set,
   apart from the deliberate rewrites. Review what this prints:

   ```bash
   sort -u <scratch>/README.before.md > <scratch>/before.txt
   cat README.md docs/architecture.md docs/verified-live.md docs/releases.md \
     docs/development.md docs/deepagents-notes.md | sort -u > <scratch>/after.txt
   comm -23 <scratch>/before.txt <scratch>/after.txt
   ```

2. **Every figure survived.** Run the same comparison on dollar amounts:

   ```bash
   grep -oE '\$[0-9][0-9,.]*' <scratch>/README.before.md | sort -u > <scratch>/m1.txt
   cat README.md docs/*.md | grep -oE '\$[0-9][0-9,.]*' | sort -u > <scratch>/m2.txt
   comm -23 <scratch>/m1.txt <scratch>/m2.txt   # must print nothing
   ```

3. **Links work.** Check every relative link and anchor in `README.md` and `docs/`,
   by script or by viewing the files on GitHub.

4. **Quickstart works.** Follow it from a fresh clone as far as `uv run event-planner`
   starting up.

5. **The build still works**, because `pyproject.toml` uses `README.md` as the package
   readme:

   ```bash
   uv build
   ```

   Remove the resulting `dist/` afterwards if `.gitignore` doesn't cover it yet.

6. **The usual checks pass:**

   ```bash
   uv run pytest
   uvx ruff check .
   uvx ruff format --check .
   uvx ty check
   ```

No live model run is needed.

## Out of scope

Rewriting or correcting the moved content, a documentation site, and restructuring
`CLAUDE.md` beyond its references.
