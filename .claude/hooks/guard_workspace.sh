#!/usr/bin/env bash
# PreToolUse: refuse writes into the agent's shared filesystem root.
#
# workspace/ is FilesystemBackend(root_dir=..., virtual_mode=True) and its root
# is a single static path -- deepagents 0.7 removed backend factories, so it
# cannot vary per user. Every session reads the same tree through
# ls/read_file/glob/grep, so anything that lands there is readable by every
# planner. Only shared reference material (skills) belongs on it.
set -uo pipefail

input=$(cat)
path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
[ -n "$path" ] || exit 0

root=${CLAUDE_PROJECT_DIR:-$(printf '%s' "$input" | jq -r '.cwd // empty')}
[ -n "$root" ] || exit 0
case "$path" in /*) ;; *) path="$root/$path" ;; esac

# Anchored on this project's workspace/, not a bare */workspace/* glob: a
# checkout living under ~/workspace/ would otherwise block every edit in it.
ws="$root/workspace"

case "$path" in
  "$ws"/skills/*) exit 0 ;;
  "$ws" | "$ws"/*)
    cat >&2 <<'MSG'
Blocked: workspace/ is the agent's filesystem root and is SHARED across every
session and every user. The agent has ls/read_file/glob/grep over it, and the
root is a single static path, so anything placed here is readable by every
planner's session.

Only shared reference material belongs here -- workspace/skills/.

  User data?        Add a route in build_backend() (agent.py) plus a namespace
                    factory in context.py, so it lands in a per-user store
                    namespace like /events/ and /memories/ do.

  Runtime state?    .state/ at the repo root -- a SIBLING of workspace/, never
                    inside it. A database under the agent's root exposes every
                    user's memories and every thread's checkpoints in raw form,
                    bypassing namespacing entirely.

Enforced at test time by test_shared_filesystem_root_holds_only_reference_material
in tests/test_security.py; this hook stops it before the file exists.
MSG
    exit 2
    ;;
esac

exit 0
