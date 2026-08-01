#!/usr/bin/env bash
# PreToolUse: refuse hand-edits to files that may only change via tooling, and
# to the guard configuration itself.
set -uo pipefail
_hook_common="$(dirname "$0")/_common.sh"
[ -r "$_hook_common" ] || {
  echo "hook: cannot read $_hook_common; refusing rather than running unguarded." >&2
  exit 2
}
. "$_hook_common"

[ -n "$HOOK_PATH" ] || exit 0

# The guards must not be editable by the thing they guard: one edit to
# settings.json emptying `hooks`, or an `exit 0` at the top of any script here,
# silently disables every other rule in this directory. Changing them is a
# deliberate operator action.
if [ -n "$HOOK_ROOT" ] && hook_under "$HOOK_PATH" "$HOOK_ROOT/.claude"; then
  cat >&2 <<'MSG'
Blocked: .claude/ configures the guards themselves. Editing it from inside a
session is how every other rule in this directory gets disabled by accident --
emptying `hooks` in settings.json, or an early `exit 0` in a guard script,
leaves .env, uv.lock, .python-version and workspace/ unprotected from the next
tool call onward, with nothing in the output to say so.

If the change is intended, the operator should make it directly (the files are
ordinary text), or temporarily remove the hook from .claude/settings.json.
MSG
  exit 2
fi

case "$(basename "$HOOK_PATH")" in
  .env.example)
    # The tracked template is the file you are meant to edit.
    exit 0
    ;;
  .env | .env.*)
    cat >&2 <<'MSG'
Blocked: .env holds live credentials (ANTHROPIC_API_KEY, TAVILY_API_KEY,
LANGSMITH_API_KEY) and is gitignored, so a mistake here leaks quietly.

Edit .env.example instead to document a new variable, and let the operator
fill in the real value.
MSG
    exit 2
    ;;
  uv.lock)
    cat >&2 <<'MSG'
Blocked: uv.lock is generated. Hand-editing it desyncs the lockfile from
pyproject.toml, and the next `uv sync` silently reverts the change.

  add a dependency      uv add <pkg>
  remove one            uv remove <pkg>
  refresh the lock      uv lock
MSG
    exit 2
    ;;
  .python-version)
    cat >&2 <<'MSG'
Blocked: repin with `uv python pin <version>` rather than editing by hand.

Note this is the DEVELOPMENT pin (3.14), which is deliberately distinct from
the compatibility floor in pyproject.toml (requires-python = ">=3.11,<4.0").
ty checks against the floor, not this pin. If you meant to change the range of
Python versions the project supports, edit requires-python instead.
MSG
    exit 2
    ;;
esac

exit 0
