#!/usr/bin/env bash
set -euo pipefail
HOOK_NAME="${1:?Usage: mempal-hook.sh <hook-name>}"

# Resolve the Python interpreter. Same contract as mempal_save_hook.sh:
# MEMPAL_PYTHON (explicit override) → $(command -v python3) → bare python3.
MEMPAL_PYTHON_BIN="${MEMPAL_PYTHON:-}"
if [ -z "$MEMPAL_PYTHON_BIN" ] || [ ! -x "$MEMPAL_PYTHON_BIN" ]; then
    MEMPAL_PYTHON_BIN="$(command -v python3 2>/dev/null || echo python3)"
fi

# Run `mempalace hook run` with the best available invocation method.
# Resolution order:
#   1. $MEMPAL_PYTHON set  → "$MEMPAL_PYTHON_BIN" -m mempalace
#   2. mempalace on PATH   → bare mempalace
#   3. fallback            → "$MEMPAL_PYTHON_BIN" -m mempalace
run_mempalace_hook() {
  if [ -n "${MEMPAL_PYTHON:-}" ]; then
    "$MEMPAL_PYTHON_BIN" -m mempalace "$@"
    return $?
  fi

  if command -v mempalace >/dev/null 2>&1; then
    mempalace "$@"
    return $?
  fi

  "$MEMPAL_PYTHON_BIN" -m mempalace "$@"
}

INPUT_FILE=$(mktemp) || { echo "Failed to create temp file" >&2; exit 1; }
cat > "$INPUT_FILE"
run_mempalace_hook hook run --hook "$HOOK_NAME" --harness codex < "$INPUT_FILE"
EXIT_CODE=$?
rm -f "$INPUT_FILE" 2>/dev/null
exit $EXIT_CODE
