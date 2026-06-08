#!/bin/bash
# MemPalace SessionEnd Hook — thin wrapper calling Python CLI
# All logic lives in mempalace.hooks_cli for cross-harness extensibility
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

run_mempalace_hook --hook session-end --harness "${MEMPALACE_HOOK_HARNESS:-claude-code}"
