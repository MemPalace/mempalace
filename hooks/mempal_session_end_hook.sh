#!/bin/bash
# MemPalace SessionEnd Hook — final save on clean exit.
#
# Claude Code documents a default SessionEnd hook timeout of 1.5s; a per-hook
# "timeout" in settings.local.json can raise it (up to 60s), but a
# plugin-provided timeout cannot (https://code.claude.com/docs/en/hooks). A cold
# `mempalace` start alone can exceed 1.5s, so we background the hook and return
# immediately; the detached child finishes the save after the session has
# exited. All logic lives in mempalace.hooks_cli for cross-harness extensibility.
run_mempalace_hook() {
  if command -v mempalace >/dev/null 2>&1; then
    exec mempalace hook run "$@"
  fi

  MEMPAL_PYTHON_BIN="${MEMPAL_PYTHON:-}"
  if [ -z "$MEMPAL_PYTHON_BIN" ] || [ ! -x "$MEMPAL_PYTHON_BIN" ]; then
    MEMPAL_PYTHON_BIN="$(command -v python3 2>/dev/null || echo python3)"
  fi
  if "$MEMPAL_PYTHON_BIN" -c "import mempalace" >/dev/null 2>&1; then
    exec "$MEMPAL_PYTHON_BIN" -m mempalace hook run "$@"
  fi

  if command -v python >/dev/null 2>&1 && python -c "import mempalace" >/dev/null 2>&1; then
    exec python -m mempalace hook run "$@"
  fi

  echo "MemPalace hook error: could not find a runnable mempalace command or module" >&2
  exit 1
}

# Capture stdin (the SessionEnd JSON) before backgrounding — the parent's
# stdin is gone once we return. Forward it to the detached worker, which runs
# the final mine on its own time and outlives this process.
payload="$(cat)"
(
  printf '%s' "$payload" | run_mempalace_hook --hook session-end --harness "${MEMPALACE_HOOK_HARNESS:-claude-code}"
) >/dev/null 2>&1 </dev/null &
disown 2>/dev/null || true

# Return immediately so the harness never blocks on session exit.
printf '{}'
