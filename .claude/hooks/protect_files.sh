#!/usr/bin/env bash
# PreToolUse: the only write guard left. Two clauses, each defending something
# with no other backstop anywhere in the repo.
#
# What used to be here and is not any more, with the reason:
#
#   uv.lock          git-tracked, so a hand-edit is one `git checkout` away, and
#                    the block message itself conceded "the next `uv sync`
#                    silently reverts the change". CI runs `uv sync --locked`
#                    in both jobs.
#   .python-version  git-tracked, 5 bytes, and `uv sync` fails loudly on a bogus
#                    pin. Its message was really a paragraph about the 3.14 dev
#                    pin vs. the requires-python floor -- prose that CLAUDE.md
#                    already carries, and a block you can only read by tripping
#                    it is a bad place to keep prose.
#   .claude/settings.json
#                    Hook CONFIG is snapshotted at session start (see README.md),
#                    so an edit here is inert until the next `claude` invocation
#                    -- the old message's claim that it took effect "from the
#                    next tool call onward" was false. Guarding it charged every
#                    hook change an out-of-session round trip and bought nothing;
#                    .github/workflows/ci.yml carried a stale cross-reference for
#                    exactly that reason. It is also the file you must edit to
#                    remove a hook.
set -uo pipefail
_hook_common="$(dirname "$0")/_common.sh"
[ -r "$_hook_common" ] || {
  echo "hook: cannot read $_hook_common; refusing rather than running unguarded." >&2
  exit 2
}
. "$_hook_common"

[ -n "$HOOK_PATH" ] || exit 0

# The guard SCRIPTS, unlike settings.json, are re-read from disk by bash on
# every invocation -- settings.json runs `bash "$CLAUDE_PROJECT_DIR/.claude/
# hooks/<name>.sh"`, so an `exit 0` spliced in at the top of one is live on the
# very next tool call, with nothing in the output to say so. That is the half of
# the old .claude/ clause that was actually true, and it is the half kept.
if [ -n "$HOOK_ROOT" ] && hook_under "$HOOK_PATH" "$HOOK_ROOT/.claude/hooks"; then
  cat >&2 <<'MSG'
Blocked: .claude/hooks/ holds the guard scripts themselves, and bash re-reads
them from disk on every invocation -- so an early `exit 0` spliced in here
disables that guard on the very next tool call, unreviewed.

Changing them is a deliberate operator action: edit them directly (they are
ordinary text), or remove the hook from .claude/settings.json, which this guard
deliberately does NOT block.
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
LANGSMITH_API_KEY) and is GITIGNORED -- so an overwriting write destroys the
operator's keys with no git history to recover them from, and a mistake leaks
quietly.

Nothing else in this repo stands behind that. CI never sees the file (no
secrets are passed, deliberately), no test in tests/ names it, and Claude Code's
own sensitive-path list covers .ssh, id_rsa, .aws/credentials, .netrc, .npmrc
and .git-credentials -- not .env.

Edit .env.example instead to document a new variable, and let the operator
fill in the real value.
MSG
    exit 2
    ;;
esac

exit 0
