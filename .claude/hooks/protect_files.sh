#!/usr/bin/env bash
# PreToolUse: refuse hand-edits to files that may only change via tooling.
set -uo pipefail

input=$(cat)
path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
[ -n "$path" ] || exit 0

case "$(basename "$path")" in
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
