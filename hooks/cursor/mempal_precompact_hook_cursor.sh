#!/bin/bash
# MEMPALACE CURSOR PRE-COMPACT HOOK — Snapshot transcript before compaction
#
# Cursor "preCompact" hook. Cursor's preCompact is documented as
# OBSERVATIONAL ONLY (cursor.com/docs/hooks.md fetched 2026-05-27):
#
#   * It cannot block compaction.
#   * Its only output field is `user_message` (no `followup_message`,
#     no `decision: block`).
#
# So unlike the Claude Code PreCompact hook (which can block the AI
# and force a save before compaction proceeds), the Cursor preCompact
# hook can only do two useful things at this moment:
#
#   1. Run `mempalace mine` SYNCHRONOUSLY against the transcript file
#      so whatever Cursor's transcript contains is ingested BEFORE
#      Cursor summarises the conversation — zero LLM cost, no agent
#      interaction needed. NOTE: this is BEST-EFFORT for Cursor.
#      Cursor's transcript format is undocumented and normalize.py has
#      no Cursor parser, so this does not yet produce clean verbatim
#      drawers; it is a safety net, not the primary capture path.
#
#   2. Drop a `.pending` marker file keyed on conversation_id. The
#      next `stop` hook reads that marker and forces a save followup
#      regardless of its counter, so the AI still gets a "write a
#      diary entry now" nudge on the very next turn. THIS followup is
#      the load-bearing verbatim-capture path for Cursor (the agent
#      files its own in-context verbatim quotes via the MCP tools).
#
# === INSTALL ===
#
# Add to ~/.cursor/hooks.json (or .cursor/hooks.json for project
# scope) under "preCompact":
#
#   {
#     "version": 1,
#     "hooks": {
#       "preCompact": [
#         { "command": "/absolute/path/to/mempal_precompact_hook_cursor.sh" }
#       ]
#     }
#   }
#
# No loop_limit is needed; preCompact is not a looping hook.

_mempal_self="${BASH_SOURCE[0]:-$0}"
_mempal_dir="$(cd "$(dirname "$_mempal_self")" 2>/dev/null && pwd)"
# shellcheck source=lib/common.sh
. "$_mempal_dir/lib/common.sh"

# Optional additional project directory to mine before compaction
# (parity with the Claude Code hook's MEMPAL_DIR knob).
MEMPAL_DIR="${MEMPAL_DIR:-}"

if mempal_is_disabled; then
    mempal_emit '{}'
    exit 0
fi

# Opportunistic, daily-throttled GC of stale per-conversation state.
# Placed after the kill switch so a disabled hook touches nothing.
mempal_gc_stale_state

INPUT="$(cat)"
mempal_parse_stdin "$INPUT"

if [ "$MEMPAL_PARSE_OK" != "1" ]; then
    mempal_dump_bad_input "$INPUT" "preCompact"
    mempal_emit '{}'
    exit 0
fi

mempal_log "preCompact" "$MEMPAL_CONV_ID" \
    "trigger=${MEMPAL_TRIGGER:-?} transcript=$MEMPAL_TRANSCRIPT"

# ── Synchronous mine ──────────────────────────────────────────────
#
# This intentionally blocks the hook. Compaction is irreversible —
# once Cursor summarises the conversation we cannot get the verbatim
# text back — so we must finish ingesting before returning. Background
# mining would race the compaction and lose data.
#
# TIMEOUT TRADEOFF (igorls review, PR #1632): on a very large transcript
# this synchronous mine can exceed Cursor's per-hook timeout, in which
# case Cursor kills the process mid-mine. That is acceptable and safe
# here: `mempalace mine` is incremental and append-only (a crash mid-
# operation leaves the existing palace untouched — see CLAUDE.md
# "Incremental only"), so a killed mine simply resumes on the next mine
# invocation rather than corrupting the palace. We deliberately do NOT
# wrap this in a shorter timeout, because truncating the mine would
# trade a recoverable partial-ingest for guaranteed silent data loss
# right before the irreversible compaction. The pending-save marker
# below is the backstop: the next `stop` hook re-mines and nudges a
# verbatim save regardless of whether this mine completed.
if command -v mempalace >/dev/null 2>&1; then
    if mempal_is_valid_transcript "$MEMPAL_TRANSCRIPT" \
        && [ -f "$MEMPAL_TRANSCRIPT" ]; then
        mempalace mine "$(dirname "$MEMPAL_TRANSCRIPT")" --mode convos \
            >> "$MEMPAL_CURSOR_LOG" 2>&1 || \
            mempal_log "preCompact" "$MEMPAL_CONV_ID" \
                "WARN: mempalace mine convos returned non-zero"
    elif [ -n "$MEMPAL_TRANSCRIPT" ]; then
        mempal_log "preCompact" "$MEMPAL_CONV_ID" \
            "skipping invalid transcript path: $MEMPAL_TRANSCRIPT"
    fi
    if [ -n "$MEMPAL_DIR" ] && [ -d "$MEMPAL_DIR" ]; then
        mempalace mine "$MEMPAL_DIR" --mode projects \
            >> "$MEMPAL_CURSOR_LOG" 2>&1 || \
            mempal_log "preCompact" "$MEMPAL_CONV_ID" \
                "WARN: mempalace mine projects returned non-zero"
    fi
else
    mempal_log "preCompact" "$MEMPAL_CONV_ID" \
        "mempalace CLI not on PATH; skipping synchronous mine"
fi

# ── Drop the pending-save marker ──────────────────────────────────
mempal_set_pending "$MEMPAL_CONV_ID" || \
    mempal_log "preCompact" "$MEMPAL_CONV_ID" \
        "WARN: could not write pending-save marker"

# Surface a short user-visible note that compaction is about to
# happen and we've already captured the verbatim text. user_message
# is the only output field Cursor's preCompact accepts.
"$MEMPAL_PYTHON_BIN" -c '
import json
print(json.dumps({
    "user_message": (
        "MemPalace: transcript snapshotted before compaction. "
        "A diary nudge is queued for the next agent turn."
    )
}))
'
