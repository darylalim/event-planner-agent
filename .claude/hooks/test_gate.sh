#!/usr/bin/env bash
# PostToolUse: run the whole test suite after a source or test edit.
#
# Running everything rather than a related subset is affordable here: 91 tests,
# ~1.8s, fully offline, no API key. These tests are what guard the invariants
# that fail silently -- approval gating, storage routing, tool binding, the
# 6 + 4N step budget, namespace injectivity -- and breaking one of those
# produces working-looking code, which is exactly when fast feedback matters.
set -uo pipefail

input=$(cat)
path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
[ -n "$path" ] || exit 0

root=${CLAUDE_PROJECT_DIR:-$(printf '%s' "$input" | jq -r '.cwd // empty')}
[ -n "$root" ] || exit 0
case "$path" in /*) ;; *) path="$root/$path" ;; esac

case "$path" in
  "$root"/src/event_planner/* | "$root"/tests/*) ;;
  *) exit 0 ;;
esac

cd "$root" || exit 0

if ! out=$(uv run pytest -q 2>&1); then
  {
    echo "Tests failed after editing ${path#"$root"/}:"
    echo
    printf '%s\n' "$out" | tail -40
  } >&2
  exit 2
fi

exit 0
