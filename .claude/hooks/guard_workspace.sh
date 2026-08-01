#!/usr/bin/env bash
# PreToolUse: refuse writes into the agent's shared filesystem root.
#
# workspace/ is FilesystemBackend(root_dir=..., virtual_mode=True) and its root
# is a single static path -- deepagents 0.7 removed backend factories, so it
# cannot vary per user. Every session reads the same tree through
# ls/read_file/glob/grep, so anything that lands there is readable by every
# planner. Only shared reference material (skills) belongs on it.
set -uo pipefail
_hook_common="$(dirname "$0")/_common.sh"
[ -r "$_hook_common" ] || {
  echo "hook: cannot read $_hook_common; refusing rather than running unguarded." >&2
  exit 2
}
. "$_hook_common"

[ -n "$HOOK_PATH" ] || exit 0
[ -n "$HOOK_ROOT" ] || exit 0

# Anchored on this project's workspace/, not a bare */workspace/* glob: the
# glob blocks every edit in a checkout that happens to live under ~/workspace/.
ws="$HOOK_ROOT/workspace"

hook_under "$HOOK_PATH" "$ws" || exit 0
# Skills are the one thing that belongs here: shared, versioned, no user data.
hook_under "$HOOK_PATH" "$ws/skills" && exit 0

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
