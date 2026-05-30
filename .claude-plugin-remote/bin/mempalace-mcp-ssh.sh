#!/bin/bash
# MemPalace remote MCP server — SSH-wraps to the host's mempalace-mcp
HOST="${MEMPALACE_REMOTE_HOST:?MEMPALACE_REMOTE_HOST must be set in env}"
MCP_BIN="${MEMPALACE_REMOTE_MCP_BIN:-mempalace-mcp}"

[[ "$HOST" =~ ^[A-Za-z0-9_./@-]+$ ]] || { echo "MemPalace: MEMPALACE_REMOTE_HOST has unsafe characters" >&2; exit 1; }
[[ "$MCP_BIN" =~ ^[A-Za-z0-9_./-]+$ ]] || { echo "MemPalace: MEMPALACE_REMOTE_MCP_BIN has unsafe characters" >&2; exit 1; }

exec ssh -- "$HOST" "$MCP_BIN"
