#!/usr/bin/env bash
set -euo pipefail

MODEL="${MODEL:-gemma4:26b}"
WAKE_FILE="$(mktemp /tmp/mempalace-wake.XXXXXX.txt)"
PROMPT_FILE="$(mktemp /tmp/mempalace-prompt.XXXXXX.txt)"

cleanup() {
  rm -f "$WAKE_FILE" "$PROMPT_FILE"
}
trap cleanup EXIT

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: required command not found: $1" >&2
    exit 1
  fi
}

require_cmd mempalace
require_cmd ollama

mempalace wake-up > "$WAKE_FILE"

printf '\n===== MEMPALACE WAKE-UP =====\n\n'
cat "$WAKE_FILE"
printf '\n===== END WAKE-UP =====\n\n'

if [ "$#" -gt 0 ]; then
  TASK="$*"
else
  printf 'Enter task for %s: ' "$MODEL"
  IFS= read -r TASK
fi

cat > "$PROMPT_FILE" <<EOP
You are the local MemPalace-assisted ArtistPro assistant.

Treat the retrieved context below as binding operating context.
Do not invent facts not supported by the retrieved context.
Prefer controlled, verified actions.
If the request is ambiguous, state what is missing instead of guessing.

=== RETRIEVED CONTEXT START ===
$(cat "$WAKE_FILE")
=== RETRIEVED CONTEXT END ===

=== USER TASK START ===
$TASK
=== USER TASK END ===
EOP

printf '\n===== OLLAMA RESPONSE (%s) =====\n\n' "$MODEL"
ollama run "$MODEL" < "$PROMPT_FILE"
printf '\n'
