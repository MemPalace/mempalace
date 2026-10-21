#!/bin/bash
# MemPalace SessionEnd hook for the Claude Code plugin.
#
# Claude Code documents a default SessionEnd hook timeout of 1.5s, and
# "timeouts set on plugin-provided hooks do not raise the budget"
# (https://code.claude.com/docs/en/hooks). A cold `mempalace` start alone
# exceeds 1.5s, so the final mine must NOT run in the foreground: it would be
# killed before it saved anything. This wrapper backgrounds the hook and
# returns immediately; the detached child finishes the save after the session
# has exited. mempal_hook_run.sh next to this file picks the interpreter.
here="${BASH_SOURCE[0]%/*}"
[ "$here" = "${BASH_SOURCE[0]}" ] && here=.
hook_run="$here/mempal_hook_run.sh"

# Capture stdin (the SessionEnd JSON) before backgrounding — the parent's
# stdin is gone once we return. Forward it to the detached worker, which runs
# the final mine on its own time and outlives this process.
payload="$(cat)"
(
  printf '%s' "$payload" | "${BASH:-bash}" "$hook_run" --hook session-end --harness "${MEMPALACE_HOOK_HARNESS:-claude-code}"
) >/dev/null 2>&1 </dev/null &
disown 2>/dev/null || true

# Return immediately so the harness never blocks on session exit.
printf '{}'
