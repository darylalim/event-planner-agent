#!/usr/bin/env bash
# PostToolUse: run the whole test suite after a change that can alter behaviour.
#
# Running everything rather than a related subset is affordable here: 91 tests,
# ~1.2s, fully offline, no API key. These tests are what guard the invariants
# that fail silently -- approval gating, storage routing, tool binding, the
# 6 + 4N step budget, namespace injectivity -- and breaking one of those
# produces working-looking code, which is exactly when fast feedback matters.
set -uo pipefail
_hook_common="$(dirname "$0")/_common.sh"
[ -r "$_hook_common" ] || {
  echo "hook: cannot read $_hook_common; refusing rather than running unguarded." >&2
  exit 2
}
. "$_hook_common"

[ -n "$HOOK_PATH" ] || exit 0
[ -n "$HOOK_ROOT" ] || exit 0

# src/ and tests/ are the obvious triggers. The other three are not, and each
# breaks a test without touching a .py file:
#   pyproject.toml   a deepagents bump breaks test_planning_tool_is_bound and
#                    test_step_budget_survives_a_long_planning_session, which
#                    exist precisely because 0.7.1 disagrees with its docs
#   uv.lock          same, via a transitive resolution change
#   workspace/skills anything the agent loads at runtime is behaviour
watched=0
for base in src/event_planner tests workspace/skills; do
  hook_under "$HOOK_PATH" "$HOOK_ROOT/$base" && watched=1
done
case "$HOOK_PATH" in
  "$HOOK_ROOT"/pyproject.toml | "$HOOK_ROOT"/uv.lock | "$HOOK_ROOT"/langgraph.json) watched=1 ;;
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
