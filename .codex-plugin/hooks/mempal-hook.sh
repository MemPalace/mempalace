#!/usr/bin/env bash
set -euo pipefail
HOOK_NAME="${1:?Usage: mempal-hook.sh <hook-name>}"
INPUT_FILE=$(mktemp) || { echo "Failed to create temp file" >&2; exit 1; }
cat > "$INPUT_FILE"
cat "$INPUT_FILE" | mempalace hook run --hook "$HOOK_NAME" --harness codex
EXIT_CODE=$?
rm -f "$INPUT_FILE" 2>/dev/null
exit $EXIT_CODE
