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
| `test_gate.sh` | PostToolUse | `src/event_planner/`, `tests/`, `workspace/`, `pyproject.toml`, `uv.lock` |

`_common.sh` is sourced by all three. It parses the payload once and exports
`HOOK_ROOT` and `HOOK_PATH` (absolute and lexically normalized). Parsing stdin
per-hook is what produced the `./workspace/x`, `workspace/skills/../x` and
`NotebookEdit` bypasses that used to exist. It also exported `HOOK_CMD`, the raw
Bash command string, until `guard_bash.sh` — its only reader — was deleted; the
export went with it rather than costing every edit a `jq` fork for a variable
nothing reads.

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

**`guard_workspace.sh` — cut, and covered elsewhere.** `test_security.py` holds
both halves: `test_shared_filesystem_root_holds_only_reference_material` reds on
a top-level entry under `workspace/` other than `skills`, and
`test_agent_cannot_see_a_state_directory_in_its_listing` covers `.state`. That
first test gained dot entries — its `startswith(".")` filter was dropped, because
`FilesystemBackend.ls("/")` enumerates them, so `workspace/.state/planner.sqlite`
reached every session exactly as `workspace/events/acme.md` would. It is still
one non-recursive `iterdir()`, not a walk of the tree; see "What these do not
cover" below. `test_gate.sh` now watches `workspace/` rather than
`workspace/skills/`, so a write there runs those tests locally within seconds.

Be precise about what that trade is, because the first version of this paragraph
was not. `test_gate.sh` runs under the **same** `Write|Edit|NotebookEdit` matcher
`guard_workspace.sh` had, so it is not wider coverage — it is the same coverage,
moved from blocking to after-the-fact, and reported as a test failure rather than
20 lines of stderr. What genuinely does see a write from any tool is the same
test running in CI, on every supported Python, for a contributor who has never
installed Claude Code. Locally, a `printf hi > workspace/events/acme.md` through
Bash still fires nothing at all.

**`check_prompt_drift.py` — cut, three of its four checks ported to pytest.**
`missing` and `misrouted` are `test_every_bound_tool_is_named_somewhere` and
`test_no_prompt_instructs_a_tool_its_agent_cannot_call` in `tests/test_harness.py`,
whose docstring already claimed this ground ("a prompt that instructs the model
to call a tool that was never bound"). `ungated` is
`test_every_irreversible_tool_is_actually_bound`, kept in `tests/test_security.py`
beside `test_every_irreversible_tool_is_gated` because what it protects is the
approval gate. Shared helpers live in `tests/conftest.py`, one copy.

Verified against planted defects, not just by passing: a half-rename (the tool
function and its imports renamed, the `IRREVERSIBLE_TOOLS` literal and the
prompts left stale) fails the first and third, naming `place_hold` and
`hold_venue`; appending "Once the budget clears, call `hold_venue` yourself." to
`BUDGET_ANALYST_PROMPT` fails the second with `('budget-analyst', 'hold_venue')`
while the first passes straight through it — which is the point of keeping both.

`misrouted` was cut in the pruning commit and is **restored** here. The stated
reason for cutting it was that legitimate prose would trip it: `"You never book.
The orchestrator calls `hold_venue`, which pauses for a human."` It is worth
recording that no prompt in this tree contains that sentence and none ever has —
it was a constructed example, not an observed failure. Run against the real
prompts the check reports **zero** misrouted pairs, so the test ships with an
empty `permitted` allowlist. Prose that genuinely needs to name another agent's
tool goes in that set with the sentence that justifies it; an exemption you must
type is cheaper than a check you deleted.

`unknown` is the one that is genuinely **not** ported, and its problem is
different from prose false positives: it flagged any backticked `snake_case`
token that is not a tool anywhere, so staying quiet required a hand-maintained
`HARNESS_TOOLS` list — a second copy of deepagents' tool names that fails open
the moment the package adds one. That is the failure class this repo keeps
eliminating, so it is not worth reintroducing to catch typos.

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

**Bash is not gated at all.** `settings.json` carries no Bash matcher since
`guard_bash.sh` was deleted, and `_common.sh` reads only `file_path` /
`notebook_path`, so a shell command reaches nothing here even if a matcher were
added back without also parsing `.tool_input.command`. Concretely: `sed -i ''
'2i\exit 0' .claude/hooks/protect_files.sh` disables the guard on the next tool
call, and `cp .env.example .env` clobbers live credentials — the two things
`protect_files.sh` exists to stop, both reachable. `guard_bash.sh` was deleted
because it blocked five of six benign commands while allowing the write it
existed to stop, which was the right call on the evidence; it left a gap, and
this is that gap stated rather than papered over.

**Reads are not gated.** Nothing stops `cat .env` or reading another user's
export. These hooks guard writes.

**`workspace/skills/` still takes writes, and nothing walks below it.** Skills
are shared reference material by design, so the subtree is writable, and
`test_shared_filesystem_root_holds_only_reference_material` is a single
non-recursive `WORKSPACE.iterdir()`. Client data parked at
`workspace/skills/acme/brief.md` passes both the hook and the test.

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

`lint_gate.sh` ~185 ms on any `.py` (ruff check 20 ms, ruff format --check
20 ms, ty 145 ms scoped; whole-project ty is 225 ms, so scoping buys less than
the earlier note claimed — it is kept for the misattribution argument, not the
speed). `test_gate.sh` ~5.8 s reported by pytest and ~6.6 s wall on watched
paths, of which 4.5 s is `test_streamlit_page.py`. The two run in parallel, so a
`src/` edit costs about the slower — call it seven seconds, and note that
`streamlit_app.py` is the one edited file where `lint_gate.sh` runs alone and
unmasked.

All three tool invocations in `lint_gate.sh` pass `--force-exclude`. Without it,
naming a path on the command line overrides the tool's own exclusions, and the
hook admits anything under the project root — so editing a vendored file under
`.venv/` to trace package behaviour blocked the edit with 17 `ty` diagnostics and
9 `ruff` errors against code the author does not own.
