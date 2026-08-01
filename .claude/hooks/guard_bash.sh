#!/usr/bin/env bash
# PreToolUse on Bash: best-effort cover for the shell bypass.
#
# The Write/Edit guards are exact -- they inspect a single resolved path. Bash
# is not amenable to that: `printf '%s' "$b" > workspace/events/acme.md` writes
# client data onto the shared filesystem root without ever touching Write, and
# `sed -i '' ... .env` edits credentials the same way.
#
# This is a HEURISTIC and is documented as one in README.md. It pairs a
# protected target with a write-shaped verb and refuses the combination. It
# will not catch an obfuscated path, a heredoc built at runtime, or a write
# performed inside a script this command invokes. It is a speed bump on the
# obvious route, not an equivalent of the path guards -- the enforceable
# boundary remains the test suite and code review.
set -uo pipefail
_hook_common="$(dirname "$0")/_common.sh"
[ -r "$_hook_common" ] || {
  echo "hook: cannot read $_hook_common; refusing rather than running unguarded." >&2
  exit 2
}
. "$_hook_common"

[ -n "$HOOK_CMD" ] || exit 0

# Protected targets. workspace/ is matched only when the next segment is not
# skills/, which is the one subtree that legitimately takes writes.
target=
case "$HOOK_CMD" in
  *.env*) target=".env" ;;
  *uv.lock*) target="uv.lock" ;;
  *.python-version*) target=".python-version" ;;
  *.claude/*) target=".claude/" ;;
esac
if [ -z "$target" ]; then
  case "$HOOK_CMD" in
    *workspace/skills/*) ;; # allowed subtree; check nothing else matched
    *workspace/*) target="workspace/" ;;
  esac
fi
[ -n "$target" ] || exit 0

# Write-shaped verbs. Redirections are matched loosely because `>`, `>>` and
# `>|` all appear with and without surrounding spaces.
verb=
case "$HOOK_CMD" in
  *'>'*) verb="a redirection" ;;
  *tee\ *) verb="tee" ;;
  *sed\ -i*) verb="sed -i" ;;
  *perl\ -i*) verb="perl -i" ;;
  *"cp "*) verb="cp" ;;
  *"mv "*) verb="mv" ;;
  *"rm "*) verb="rm" ;;
  *"dd "*) verb="dd" ;;
  *truncate\ *) verb="truncate" ;;
  *install\ -*) verb="install" ;;
  *touch\ *) verb="touch" ;;
  *python*-c*) verb="an inline python program" ;;
  *tofile*) verb="a file write" ;;
esac
[ -n "$verb" ] || exit 0

cat >&2 <<MSG
Blocked: this command pairs $verb with $target, which the Write/Edit guards
protect. Routing a write through the shell bypasses them.

  workspace/          shared filesystem root; readable by every planner's
                      session. Route user data through build_backend().
  .env                live credentials.
  uv.lock             regenerate with uv add / uv remove / uv lock.
  .python-version     repin with uv python pin.
  .claude/            the guard configuration itself.

If the write is legitimate, use the Write or Edit tool so the path guards can
see it, or have the operator run the command directly.

This check is a heuristic over the command string: it can misfire on a
read-only command that merely mentions one of these paths. If that is what
happened, say so rather than working around it.
MSG
exit 2
