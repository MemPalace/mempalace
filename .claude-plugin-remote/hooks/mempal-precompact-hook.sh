#!/bin/bash
# MemPalace remote PreCompact hook — SSH-wraps to the host's mempalace CLI
HOST="${MEMPALACE_REMOTE_HOST:?MEMPALACE_REMOTE_HOST must be set in env}"
BIN="${MEMPALACE_REMOTE_BIN:-mempalace}"

[[ "$HOST" =~ ^[A-Za-z0-9_./@-]+$ ]] || { echo "MemPalace: MEMPALACE_REMOTE_HOST has unsafe characters" >&2; exit 1; }
[[ "$BIN" =~ ^[A-Za-z0-9_./-]+$ ]] || { echo "MemPalace: MEMPALACE_REMOTE_BIN has unsafe characters" >&2; exit 1; }

INPUT=$(cat)
echo "$INPUT" | ssh -- "$HOST" "$BIN hook run --hook precompact --harness claude-code"
