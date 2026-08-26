#!/usr/bin/env bash
# Sourced by every shell hook. Parses the hook payload on stdin exactly once
# and exports a normalized view of it:
#
#   HOOK_ROOT  project root
#   HOOK_PATH  absolute, lexically normalized target path ("" if none)
#
# Three behaviours worth knowing, each of which was a bypass before this file
# existed and the guards each parsed stdin their own way:
#
# * It reads `.tool_input.notebook_path` as well as `.tool_input.file_path`.
#   NotebookEdit supplies the former, so a guard reading only file_path
#   matches NotebookEdit in settings.json and then silently permits it.
#
# * It resolves `.` and `..`. Matching a raw path lexically means
#   `./workspace/x` and `workspace/skills/../x` both miss the `case` patterns
#   the guards test and sail through.
#
# * It FAILS CLOSED on a missing jq. `$(... | jq ...)` yields an empty string
#   when jq is absent, which every `[ -n "$path" ] || exit 0` reads as
#   "nothing to check" -- a guard that degrades to allow-everything while
#   still looking installed is worse than no guard.
#
# Written for bash 3.2, which is the /bin/bash macOS ships: no negative array
# subscripts, no associative arrays.

if ! command -v jq >/dev/null 2>&1; then
  echo "hook: jq is not on PATH, so this guard cannot inspect the tool call." >&2
  echo "hook: refusing it rather than passing it through unchecked." >&2
  echo "hook: install jq (brew install jq), or remove the hook from .claude/settings.json." >&2
  exit 2
fi

_hook_input=$(cat)

HOOK_ROOT=${CLAUDE_PROJECT_DIR:-$(printf '%s' "$_hook_input" | jq -r '.cwd // empty')}
_hook_raw=$(printf '%s' "$_hook_input" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty')

# Lexical resolution is the correct kind here: the target may not exist yet
# (it is usually about to be created), and `realpath -m` is GNU-only.
hook_normpath() {
  _np_in=$1
  case "$_np_in" in /*) ;; *) _np_in="$HOOK_ROOT/$_np_in" ;; esac
  _np_out=
  _np_ifs=$IFS
  set -f # a path containing * must not glob when we split it
  IFS=/
  # shellcheck disable=SC2086
  set -- $_np_in
  IFS=$_np_ifs
  set +f
  for _np_seg in "$@"; do
    case "$_np_seg" in
      '' | .) ;;
      ..) _np_out=${_np_out%/*} ;;
      *) _np_out="$_np_out/$_np_seg" ;;
    esac
  done
  printf '%s\n' "${_np_out:-/}"
}

HOOK_PATH=
if [ -n "$_hook_raw" ] && [ -n "$HOOK_ROOT" ]; then
  HOOK_PATH=$(hook_normpath "$_hook_raw")
fi

# True when $1 is $2 or sits underneath it.
hook_under() {
  case "$1" in
    "$2" | "$2"/*) return 0 ;;
    *) return 1 ;;
  esac
}
