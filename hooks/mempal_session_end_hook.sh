#!/bin/bash
# MemPalace SessionEnd Hook — final save on clean exit.
#
# Claude Code documents a default SessionEnd hook timeout of 1.5s; a per-hook
# "timeout" in settings.local.json can raise it (up to 60s), but a
# plugin-provided timeout cannot (https://code.claude.com/docs/en/hooks). A cold
# `mempalace` start alone can exceed 1.5s, so we background the hook and return
# immediately; the detached child finishes the save after the session has
# exited. All logic lives in mempalace.hooks_cli for cross-harness extensibility;
# mempal_hook_run.sh picks the interpreter (MEMPAL_PYTHON first).
hook_run="$(dirname "$0")/mempal_hook_run.sh"

# Capture stdin (the SessionEnd JSON) before backgrounding — the parent's
# stdin is gone once we return. Forward it to the detached worker, which runs
# the final mine on its own time and outlives this process.
payload="$(cat)"
(
  printf '%s' "$payload" | bash "$hook_run" --hook session-end --harness "${MEMPALACE_HOOK_HARNESS:-claude-code}"
) >/dev/null 2>&1 </dev/null &
disown 2>/dev/null || true

# Return immediately so the harness never blocks on session exit.
printf '{}'
