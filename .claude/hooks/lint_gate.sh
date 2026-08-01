#!/usr/bin/env bash
# PostToolUse: keep ruff and ty clean on every Python edit.
#
# Both are configured in pyproject.toml and both currently pass. CLAUDE.md:
# "Both tools are clean; keep them that way rather than adding suppressions."
# Measured cost: ruff 27ms, ty 93ms.
set -uo pipefail

input=$(cat)
path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
case "$path" in *.py) ;; *) exit 0 ;; esac

root=${CLAUDE_PROJECT_DIR:-$(printf '%s' "$input" | jq -r '.cwd // empty')}
[ -n "$root" ] || exit 0
case "$path" in /*) ;; *) path="$root/$path" ;; esac
case "$path" in "$root"/*) ;; *) exit 0 ;; esac
[ -f "$path" ] || exit 0

cd "$root" || exit 0

# Statuses are tracked per tool: grouping both in one $( ... ) would report only
# the exit code of the last command, so a ruff failure with a clean ty would
# pass silently.
status=0
report=""

# `ruff check`, never `ruff format`. Formatting is deliberately unenforced here
# and would rewrite files the linter is happy with -- see pyproject.toml:32.
if ! lint=$(uvx ruff check "$path" 2>&1); then
  status=1
  report="$lint"
fi

# ty infers its target from requires-python, so this checks against 3.11 (the
# declared floor), not the 3.14 in .python-version. That is the useful
# direction: it catches 3.12+ syntax that would break the claimed minimum.
if ! types=$(uvx ty check 2>&1); then
  status=1
  report="${report:+$report$'\n\n'}$types"
fi

if [ "$status" -ne 0 ]; then
  {
    echo "Lint/type gate failed after editing ${path#"$root"/}:"
    echo
    printf '%s\n' "$report"
    echo
    echo "Fix the finding rather than adding a suppression."
  } >&2
  exit 2
fi

exit 0
