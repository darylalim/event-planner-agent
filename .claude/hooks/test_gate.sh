#!/usr/bin/env bash
# PostToolUse: run the whole test suite after a change that can alter behaviour.
#
# Running everything rather than a related subset is affordable here: 201 tests,
# ~5.9s wall through `uv run`, fully offline, no API key. (This header said
# "91 tests, ~1.2s" for a long time; it was 5x stale, which is what a hook nobody
# re-justifies looks like. Re-measure it when you next touch this file.)
#
# 3.9s of that 5.9s is test_streamlit_page.py alone, so the only meaningful
# subset is "skip the AppTest file" -- and any src-file-to-test-file map would be
# a second hand-maintained copy that fails open, precisely the failure class this
# repo keeps eliminating (IRREVERSIBLE_TOOLS, the ruff/ty pins).
#
# These tests are what guard the invariants that fail silently -- approval
# gating, storage routing, tool binding, the 6 + 4N step budget, namespace
# injectivity -- and breaking one of those produces working-looking code, which
# is exactly when fast feedback matters. CI catches all of them too, but only at
# PR time, by which point the model has reasoned onward from the false premise.
set -uo pipefail
_hook_common="$(dirname "$0")/_common.sh"
[ -r "$_hook_common" ] || {
  echo "hook: cannot read $_hook_common; refusing rather than running unguarded." >&2
  exit 2
}
. "$_hook_common"

[ -n "$HOOK_PATH" ] || exit 0
[ -n "$HOOK_ROOT" ] || exit 0

# src/ and tests/ are the obvious triggers. The other two are not:
#
#   workspace/       the WHOLE tree, not just workspace/skills/. This is what
#                    replaces guard_workspace.sh: a write under workspace/ now
#                    runs test_shared_filesystem_root_holds_only_reference_material
#                    locally, which is strictly more than the old PreToolUse
#                    guard covered (it sees writes from any tool, not just
#                    Write/Edit) and reports as a test failure rather than 20
#                    lines of stderr. Skills are behaviour in their own right:
#                    they are loaded into context at runtime, and since the
#                    drift check moved into tests/, SKILL.md files are now read
#                    by test_every_bound_tool_is_named_somewhere.
#
#   pyproject.toml   test_webui.py parses it directly to assert streamlit is a
#                    `web` extra and that the dev group self-references [web],
#                    so editing it genuinely changes what that test sees.
#
# Dropped: uv.lock (a Write to it does not change the installed environment
# until `uv sync`, so running pytest straight afterwards exercises the OLD venv
# and proves nothing -- and real lock changes arrive via `uv add`/`uv lock` in
# Bash, which this matcher never sees) and langgraph.json (read by no test;
# test_webui.py only names it in a docstring).
watched=0
for base in src/event_planner tests workspace; do
  hook_under "$HOOK_PATH" "$HOOK_ROOT/$base" && watched=1
done
case "$HOOK_PATH" in
  "$HOOK_ROOT"/pyproject.toml) watched=1 ;;
esac
[ "$watched" -eq 1 ] || exit 0

cd "$HOOK_ROOT" || exit 0

rel=${HOOK_PATH#"$HOOK_ROOT"/}
out=$(uv run pytest -q 2>&1)
rc=$?
[ $rc -eq 0 ] && exit 0

# pytest exits 1 for test failures; 2-5 are usage/collection/internal errors,
# and `uv run` surfaces environment failures the same way. Framing a broken
# environment as "your edit broke the tests" sends the model editing source to
# fix a `uv sync` problem.
if [ $rc -ne 1 ]; then
  {
    echo "The test gate could not run cleanly (exit $rc). This usually means an"
    echo "environment or collection error rather than a failure caused by the"
    echo "edit to $rel. Check the output before changing source."
    echo
    printf '%s\n' "$out" | tail -40
  } >&2
  exit 2
fi

{
  echo "Tests failed after editing $rel:"
  echo
  printf '%s\n' "$out" | tail -40
} >&2
exit 2
