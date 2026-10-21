#!/bin/bash
# MEMPALACE CURSOR WAKE HOOK — Session-start memory recall
#
# Cursor "sessionStart" hook. This is a Cursor-only capability —
# Claude Code's third-party-hooks compatibility layer does not have
# an equivalent event with the same "inject context into the agent's
# initial system message" semantics.
#
# Behaviour:
#   1. Parse Cursor's sessionStart payload (conversation_id /
#      session_id, is_background_agent, composer_mode, plus the
#      common workspace_roots field).
#   2. Infer the wing from basename(workspace_roots[0]).
#   3. Return {"additional_context": "..."} instructing the agent to
#      scope its memory recall by calling mempalace_search +
#      mempalace_diary_read with wing=<inferred>.
#
# sessionStart is documented as fire-and-forget — Cursor does not
# enforce a blocking response and does not consume "continue" /
# "user_message" — but "additional_context" is honoured and added to
# the conversation's initial system context. Verified in
# cursor.com/docs/hooks.md (fetched 2026-05-27).
#
# === INSTALL ===
#
# Add to ~/.cursor/hooks.json (or .cursor/hooks.json for project
# scope) under "sessionStart":
#
#   {
#     "version": 1,
#     "hooks": {
#       "sessionStart": [
#         { "command": "/absolute/path/to/mempal_wake_hook_cursor.sh" }
#       ]
#     }
#   }

_mempal_self="${BASH_SOURCE[0]:-$0}"
_mempal_dir="$(cd "$(dirname "$_mempal_self")" 2>/dev/null && pwd)"
# shellcheck source=lib/common.sh
. "$_mempal_dir/lib/common.sh"

if mempal_is_disabled; then
    mempal_emit '{}'
    exit 0
fi

INPUT="$(cat)"
mempal_parse_stdin "$INPUT"

if [ "$MEMPAL_PARSE_OK" != "1" ]; then
    mempal_dump_bad_input "$INPUT" "sessionStart"
    mempal_emit '{}'
    exit 0
fi

WING="$(mempal_infer_wing "$MEMPAL_WORKSPACE")"

mempal_log "sessionStart" "$MEMPAL_CONV_ID" \
    "workspace=$MEMPAL_WORKSPACE wing=$WING"

# Emit the additional_context payload via Python -c (rather than a
# heredoc) so the JSON encoding survives wings whose name contains
# characters that would otherwise need shell escaping, and so the
# inferred wing arrives as an argv positional. The MCP tool names
# referenced here are verified against mempalace/mcp_server.py:
# mempalace_search and mempalace_diary_read both exist and accept
# the wing parameter.
"$MEMPAL_PYTHON_BIN" -c '
import json, sys
wing = sys.argv[1] if len(sys.argv) > 1 else "cursor_session"
ctx = (
    "MemPalace wake-up. The Cursor workspace maps to wing=" + wing + ". "
    "Before answering anything that touches past work in this "
    "project, call mempalace_search (wing=" + wing + ", "
    "query=<relevant keywords>) and mempalace_diary_read "
    "(agent_name=cursor-ide, wing=" + wing + ", last_n=10). "
    "Use what you find verbatim where it answers the question; "
    "never summarise the user'"'"'s own words."
)
print(json.dumps({"additional_context": ctx}))
' "$WING"
