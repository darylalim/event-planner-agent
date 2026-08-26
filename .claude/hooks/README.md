# Claude Code hooks

Configured in `../settings.json`, which loads at **session start** — edits there
take effect on the next `claude` invocation, and `/hooks` shows what is loaded.
The scripts in *this* directory are different: `bash` re-reads them from disk on
every invocation, so a change to one is live on the next tool call. That
asymmetry is why `protect_files.sh` guards `.claude/hooks/` and deliberately
does not guard `.claude/settings.json`.

| Hook | Event | Fires on |
| --- | --- | --- |
| `protect_files.sh` | PreToolUse | `.env*` (except `.env.example`), and `.claude/hooks/` |
| `lint_gate.sh` | PostToolUse | any `.py` in the project |
| `test_gate.sh` | PostToolUse | `src/`, `tests/`, `workspace/`, `pyproject.toml` |

`_common.sh` is sourced by both shell hooks. It parses the payload once and
exports `HOOK_ROOT`, `HOOK_PATH` (absolute and lexically normalized) and
`HOOK_CMD`. Parsing stdin per-hook is what produced the `./workspace/x`,
`workspace/skills/../x` and `NotebookEdit` bypasses that used to exist.

Exit codes follow the hook protocol: `0` allows, `2` blocks (PreToolUse) or
feeds stderr back to the model (PostToolUse). Anything else is a non-blocking
error the model never sees — which is why the guards fail closed.

## What was removed

The set was six hooks and is now three. Each removal moved the check somewhere
that covers strictly more, or removed a check that was net-negative.

**`guard_bash.sh` — cut.** It paired a protected path with a "write-shaped verb"
in the raw command string. The verb list counted `2>/dev/null` as a write and
the substring `rm ` inside "confi**rm**"; the target list matched bare
substrings with no argument position. Measured, it blocked five of six benign
read-only commands — including `cp .env.example .env`, which is CLAUDE.md's own
documented setup line — while *allowing* the write it existed to stop:

```
ALLOWED  printf hi > workspace/events/acme.md # see workspace/skills/venue-sourcing
ALLOWED  cp workspace/skills/a.md workspace/events/b.md
```

The `*workspace/skills/*) ;;` arm cleared the target if that string appeared
anywhere in the command, a trailing comment included. A guard that is inverted
on both axes is worse than none: it teaches the model to strip `2>/dev/null` and
to stop naming paths, degrading its own diagnostics, and it makes the other
hooks untestable from inside a session.

**`guard_workspace.sh` — cut, and over-covered.** `test_security.py` already
holds both halves: `test_shared_filesystem_root_holds_only_reference_material`
reds on any entry under `workspace/` other than `skills`, and
`test_agent_cannot_see_a_state_directory_in_its_listing` covers `.state`. That
first test now walks the full tree — its `startswith(".")` filter was dropped,
because `FilesystemBackend.ls("/")` enumerates dot entries, so
`workspace/.state/planner.sqlite` reached every session exactly as
`workspace/events/acme.md` would. `test_gate.sh` now watches `workspace/` rather
than `workspace/skills/`, so a write there runs those tests locally within
seconds. That is strictly more than the hook covered: it sees writes from any
tool, not just `Write`/`Edit`/`NotebookEdit`.

**`check_prompt_drift.py` — cut, two of its four checks ported to pytest.**
`missing` and `ungated` are now `test_every_bound_tool_is_named_somewhere` and
`test_every_irreversible_tool_is_actually_bound` in `tests/test_security.py`.
Verified against a planted half-rename (the tool function and its imports
renamed, the `IRREVERSIBLE_TOOLS` literal and the prompts left stale): both
fail, naming `place_hold` and `hold_venue` respectively.

`misrouted` and `unknown` are deliberately **not** ported. Both keyed off the
same premise — that a backticked `snake_case` token is a claimed tool call — and
both refused legitimate prose. `"You never book. The orchestrator calls
`hold_venue`, which pauses for a human."` was blocked, with a printed remedy
suggesting `hold_venue` be bound to that subagent. Six of six prose probes were
refused across two independent reviews.

As tests rather than a hook, the surviving checks run on every path instead of
five, on every supported Python, and for a contributor without Claude Code — and
`.github/workflows/ci.yml` no longer needs its planted-drift canary, because a
hook returning `0` proves nothing while pytest reporting a pass proves it ran.

## Also enforced in CI

`.github/workflows/ci.yml` runs `ruff check`, `ruff format --check`, `ty check`
and `pytest`, so a contributor without Claude Code gets this feedback on a pull
request rather than not at all. Two things about that pairing are worth knowing:

**`protect_files.sh` has no CI counterpart.** It blocks writes rather than
inspecting them, and a pull request has no equivalent. A PR is not a control
over a local `.env` clobber — the file is gitignored and never reaches a runner
— nor over a session that splices `exit 0` into a guard script, which takes
effect on the next tool call.

**The pins are asserted, not documented.** `ci.yml` pins `ruff@0.16.1` and
`ty@0.0.65` in `env:` and its `static` job greps `lint_gate.sh` to fail when the
two disagree. Keep the `RUFF="..."` / `TY="..."` spelling in that file; the
canary matches on it.

## What these do not cover

Stated plainly, because a guard that is trusted beyond its reach is worse than
no guard.

**Reads are not gated.** Nothing stops `cat .env` or reading another user's
export. These hooks guard writes.

**`workspace/skills/` still takes writes.** Skills are shared reference material
by design, so the subtree is writable, and
`test_shared_filesystem_root_holds_only_reference_material` inspects only the
top level of `workspace/`. Client data parked at `workspace/skills/acme/brief.md`
passes both.

**These are development-time guards, not the tenant boundary.** They inspect
Claude Code's own tool calls while you work on the repo. What the *running* agent
writes is governed by `build_backend()`'s routing and the tests in
`tests/test_security.py` — that is the enforceable boundary, and it is unchanged
by anything in this directory.

**`lint_gate.sh` is scoped to the edited file.** It will not see a caller you
broke elsewhere; `test_gate.sh` will. Run whole-project, `ty` reported
diagnostics from untouched files under a header asserting the current edit
caused them, which is a worse failure than the one scoping gives up.

## Turning them off

Remove the entry from `../settings.json` and restart — `protect_files.sh` does
not block that file, deliberately. It *does* block the scripts here, so changing
a guard is an operator action taken outside a session. Debug with `claude
--debug`.

## Cost per edit

`lint_gate.sh` ~110 ms on any `.py` (ruff check 29 ms, ruff format --check
20 ms, ty 62 ms scoped). `test_gate.sh` ~5.9 s on watched paths, of which 3.9 s
is `test_streamlit_page.py`. The two run in parallel, so a `src/` edit costs
about the slower — call it six seconds, and note that `streamlit_app.py` is the
one edited file where `lint_gate.sh` runs alone and unmasked.
