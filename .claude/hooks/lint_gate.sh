#!/usr/bin/env bash
# PostToolUse: keep ruff (lint and format) and ty clean on every Python edit.
#
# All three are configured in pyproject.toml and all three currently pass.
# CLAUDE.md: "Both tools are clean; keep them that way rather than adding
# suppressions." Measured cost: ruff check 27ms, ruff format --check 20ms,
# ty 93ms.
set -uo pipefail
_hook_common="$(dirname "$0")/_common.sh"
[ -r "$_hook_common" ] || {
  echo "hook: cannot read $_hook_common; refusing rather than running unguarded." >&2
  exit 2
}
. "$_hook_common"

# Pinned so an upstream release cannot turn every subsequent edit into a
# failure on code nobody touched -- ty in particular is pre-1.0 and its default
# diagnostic set still moves. The manual commands in CLAUDE.md are deliberately
# unpinned; bump these two deliberately when you bump those.
#
# .github/workflows/ci.yml pins the same two versions and its `static` job
# greps THIS file to fail when the two disagree, so bumping one and forgetting
# the other is caught rather than silently splitting local and CI behaviour.
# Keep the `RUFF="..."` / `TY="..."` spelling below; that canary matches on it.
RUFF="ruff@0.16.1"
TY="ty@0.0.65"

[ -n "$HOOK_PATH" ] || exit 0
[ -n "$HOOK_ROOT" ] || exit 0
case "$HOOK_PATH" in *.py) ;; *) exit 0 ;; esac
hook_under "$HOOK_PATH" "$HOOK_ROOT" || exit 0
[ -f "$HOOK_PATH" ] || exit 0

cd "$HOOK_ROOT" || exit 0

rel=${HOOK_PATH#"$HOOK_ROOT"/}

# "The tool ran and found something" and "the tool could not run" are both
# non-zero, and uvx exits 1 on an unresolvable version just as ruff exits 1 on
# a finding. Telling the model to fix a finding that is really a network
# failure sends it editing source to satisfy a resolver error, so the failure
# path re-probes the tool before framing its output as a result.
runnable() { uvx "$1" --version >/dev/null 2>&1; }

status=0
infra=
report=
unformatted=0

lint=$(uvx "$RUFF" check "$rel" 2>&1)
if [ $? -ne 0 ]; then
  if runnable "$RUFF"; then
    status=1
    report="$lint"
  else
    infra="${infra}ruff ($RUFF) could not be run:"$'\n'"$lint"$'\n'
  fi
fi

# Formatting IS enforced, as of .github/workflows/ci.yml -- see pyproject.toml,
# which used to say the opposite. `--check` reports rather than rewrites: a
# hook that reformatted the file underneath an in-flight edit would race the
# tool call that triggered it, and the model would be diffing against content
# it never wrote. The fix is one command, printed below when this trips.
fmt=$(uvx "$RUFF" format --check "$rel" 2>&1)
if [ $? -ne 0 ]; then
  if runnable "$RUFF"; then
    status=1
    unformatted=1
    report="${report:+$report$'\n\n'}$fmt"
  else
    infra="${infra}ruff ($RUFF) could not be run:"$'\n'"$fmt"$'\n'
  fi
fi

# ty infers its target from requires-python, so this checks against 3.11 (the
# declared floor), not the 3.14 in .python-version. That is the useful
# direction: it catches 3.12+ syntax that would break the claimed minimum.
types=$(uvx "$TY" check 2>&1)
if [ $? -ne 0 ]; then
  if runnable "$TY"; then
    status=1
    report="${report:+$report$'\n\n'}$types"
  else
    infra="${infra}ty ($TY) could not be run:"$'\n'"$types"$'\n'
  fi
fi

if [ -n "$infra" ]; then
  {
    echo "The lint/type gate could not run. This is a tooling or network"
    echo "failure, NOT a problem with the edit to $rel."
    echo "Do not change source to satisfy it; re-run once uvx can resolve."
    echo
    printf '%s' "$infra"
  } >&2
  exit 2
fi

if [ "$status" -ne 0 ]; then
  {
    echo "Lint/format/type gate failed after editing $rel:"
    echo
    printf '%s\n' "$report"
    echo
    echo "Fix the finding rather than adding a suppression."
    # Formatting is the one finding with a mechanical fix, and saying so beats
    # letting the model hand-wrap lines until ruff happens to agree with it.
    [ "$unformatted" -eq 1 ] && echo "For the formatting finding: uvx $RUFF format $rel"
  } >&2
  exit 2
fi

exit 0
