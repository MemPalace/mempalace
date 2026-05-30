#!/bin/bash
# MEMPALACE CURSOR SAVE HOOK — Auto-save every N stop events
#
# Cursor "stop" hook. After every agent loop ends, this hook:
#   1. Counts stop invocations per conversation_id (each stop ≈ one
#      assistant turn ≈ roughly one user message — see plan rationale).
#   2. Every SAVE_INTERVAL stops, returns a followup_message telling
#      the agent to file the session into MemPalace and write a diary
#      entry. Cursor auto-submits that as the next user message.
#   3. On the next stop, loop_count > 0 so we let the agent finish
#      without re-firing — Cursor's loop_count is the equivalent of
#      Claude Code's stop_hook_active flag.
#   4. If the preCompact hook has left a `.pending` marker, force a
#      save followup regardless of the counter and clear the marker.
#
# === WHY THE FOLLOWUP FIRES BY DEFAULT (differs from the Claude hook) ===
#
# The Claude Code hook (hooks/mempal_save_hook.sh) is SILENT by default:
# its background `mempalace mine --mode convos` captures the verbatim
# transcript on its own, and the LLM-driven diary nudge is opt-IN behind
# MEMPAL_VERBOSE. That works because mempalace/normalize.py has a Claude
# Code JSONL parser.
#
# Cursor is different. Cursor's transcript format is undocumented (see
# STDIN_SHAPE.md) and normalize.py has NO Cursor parser, so the
# background mine below is BEST-EFFORT only — it does not yet yield clean
# verbatim drawers for Cursor. The followup_message is therefore the
# load-bearing verbatim-capture path: it drives the agent to call
# mempalace_add_drawer / mempalace_diary_write from its in-context memory.
# That is why it is ON by default here — silencing it by default would
# leave a default Cursor install capturing nothing.
#
# Users who want the Claude-style "zero tokens in the chat window"
# behaviour can silence the followup (see MEMPAL_CURSOR_SILENT /
# MEMPAL_VERBOSE below). Once normalize.py learns to read Cursor
# transcripts, this default should flip to silent to match Claude.
#
# Companion files in this directory:
#   * lib/common.sh                       — shared helpers (sourced)
#   * mempal_precompact_hook_cursor.sh    — preCompact event
#   * mempal_wake_hook_cursor.sh          — sessionStart event
#
# === INSTALL ===
#
# Recommended path: run `hooks/cursor/install.sh` from a cloned repo,
# which copies the scripts to ~/.mempalace/hooks/cursor/ and merges
# the wiring into your ~/.cursor/hooks.json. See hooks/cursor/README.md
# for the full walkthrough, or website/guide/cursor-hooks.md for the
# rendered version.
#
# Manual wiring (user scope: ~/.cursor/hooks.json):
#
#   {
#     "version": 1,
#     "hooks": {
#       "stop": [
#         {
#           "command": "/absolute/path/to/mempal_save_hook_cursor.sh",
#           "loop_limit": 1
#         }
#       ]
#     }
#   }
#
# The `loop_limit: 1` cap is defense-in-depth — even if our own
# loop_count check below regresses, Cursor itself will stop emitting
# our followup after one auto-iteration.
#
# === KILL SWITCHES ===
#
#   MEMPAL_DISABLE_HOOK=1          — Cursor-prompt addition
#   MEMPALACE_HOOKS_AUTO_SAVE=false — matches the Claude Code hooks
#   ~/.mempalace/config.json "hooks.auto_save": false
#
# Any one of these short-circuits the hook to `{}` and exits 0.

# Resolve the directory this script lives in so we can source the
# sibling lib/common.sh whether the user invoked us by absolute path,
# by relative path, or via a symlink.
_mempal_self="${BASH_SOURCE[0]:-$0}"
_mempal_dir="$(cd "$(dirname "$_mempal_self")" 2>/dev/null && pwd)"
# shellcheck source=lib/common.sh
. "$_mempal_dir/lib/common.sh"

SAVE_INTERVAL="${MEMPAL_SAVE_INTERVAL:-15}"
# Coerce empty, non-numeric, AND zero to the default. SAVE_INTERVAL=0
# would otherwise crash bash on the modulo check below ($((NEXT % 0))
# is "division by 0"). gh-PR review caught this edge case.
case "$SAVE_INTERVAL" in
    ''|*[!0-9]*|0) SAVE_INTERVAL=15 ;;
esac

# Optional additional project directory to mine on save (parity with
# the Claude Code hook's MEMPAL_DIR knob — purely additive, never an
# override for the transcript mine).
MEMPAL_DIR="${MEMPAL_DIR:-}"

# ── Followup opt-out ──────────────────────────────────────────────
#
# Returns 0 (true) when the user has asked to suppress the
# followup_message. See the header comment for why the followup is ON
# by default for Cursor. When silenced, the hook still runs the
# best-effort background mine and still maintains its counters/markers
# — it just emits `{}` instead of a followup. Two equivalent signals:
#   * MEMPAL_CURSOR_SILENT=1|true|yes|on  — dedicated Cursor opt-out
#   * MEMPAL_VERBOSE=false|0|no|off        — cross-hook silence signal
#     (mirror-image of the Claude hook, where MEMPAL_VERBOSE=true is
#      what turns its diary nudge ON)
mempal_followup_silenced() {
    case "${MEMPAL_CURSOR_SILENT:-}" in
        1|true|yes|on) return 0 ;;
    esac
    case "${MEMPAL_VERBOSE:-}" in
        false|0|no|off) return 0 ;;
    esac
    return 1
}

# Kill switch — emit `{}` so Cursor proceeds with normal stop.
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
    mempal_dump_bad_input "$INPUT" "stop"
    # Fail-open: don't block the host on a parse error.
    mempal_emit '{}'
    exit 0
fi

mempal_log "stop" "$MEMPAL_CONV_ID" \
    "loop_count=$MEMPAL_LOOP_COUNT status=${MEMPAL_STATUS:-?} workspace=$MEMPAL_WORKSPACE"

# ── Loop-prevention ────────────────────────────────────────────────
#
# Cursor's loop_count indicates how many times THIS stop hook has
# already triggered an automatic followup for this conversation
# (starts at 0). If it is > 0, our own previous followup is currently
# being consumed by the agent — let it finish without re-firing.
if [ "$MEMPAL_LOOP_COUNT" -gt 0 ] 2>/dev/null; then
    mempal_log "stop" "$MEMPAL_CONV_ID" "loop_count>0; letting agent stop"
    mempal_emit '{}'
    exit 0
fi

WING="$(mempal_infer_wing "$MEMPAL_WORKSPACE")"

# Build the followup message once; both the pending-marker branch and
# the threshold branch use it. Constructed via Python -c (rather than
# a heredoc) so we can pass the inferred wing as argv[1] and so the
# JSON encoding is correct even for wings whose name would otherwise
# need shell quoting.
_mempal_build_followup() {
    "$MEMPAL_PYTHON_BIN" -c '
import json, sys
wing = sys.argv[1] if len(sys.argv) > 1 else "cursor_session"
msg = (
    "MemPalace save checkpoint. "
    "(1) Call mempalace_check_duplicate on the key topics, decisions, "
    "and verbatim quotes from this session. "
    "(2) For each non-duplicate, call mempalace_add_drawer (wing="
    + wing + ", room=<short topic>, content=verbatim quote). "
    "(3) Call mempalace_diary_write (agent_name=cursor-ide, wing="
    + wing + ", entry=AAAK-format summary). "
    "Then stop."
)
print(json.dumps({"followup_message": msg}))
' "$WING"
}

# ── Pending-save marker from preCompact ───────────────────────────
#
# preCompact cannot itself emit a followup_message (Cursor docs:
# preCompact is observational-only, output supports only user_message),
# so it drops a marker file and we consume it here. Forces a save
# nudge regardless of the counter.
if mempal_consume_pending "$MEMPAL_CONV_ID"; then
    mempal_log "stop" "$MEMPAL_CONV_ID" \
        "consumed pending-save marker (post-compaction)"
    if mempal_followup_silenced; then
        mempal_log "stop" "$MEMPAL_CONV_ID" \
            "followup silenced (MEMPAL_CURSOR_SILENT/MEMPAL_VERBOSE); emitting {}"
        mempal_emit '{}'
        exit 0
    fi
    _mempal_build_followup
    exit 0
fi

# ── Normal counter path ───────────────────────────────────────────
COUNTER_FILE="$(_mempal_counter_path "$MEMPAL_CONV_ID")"
CURRENT="$(mempal_read_counter "$COUNTER_FILE")"
NEXT=$((CURRENT + 1))
mempal_write_counter_atomic "$COUNTER_FILE" "$NEXT" || {
    mempal_log "stop" "$MEMPAL_CONV_ID" \
        "WARN: counter write failed for $COUNTER_FILE; passing through"
    mempal_emit '{}'
    exit 0
}

mempal_log "stop" "$MEMPAL_CONV_ID" \
    "counter $CURRENT -> $NEXT (interval=$SAVE_INTERVAL)"

# Trigger when we hit a multiple of SAVE_INTERVAL. Modulo arithmetic
# keeps the counter monotonically growing (no reset) so the log file
# is greppable for total turns across a conversation.
if [ "$((NEXT % SAVE_INTERVAL))" -ne 0 ]; then
    mempal_emit '{}'
    exit 0
fi

mempal_log "stop" "$MEMPAL_CONV_ID" "TRIGGERING SAVE at counter=$NEXT"

# ── Background mine (best effort) ─────────────────────────────────
#
# Two independent targets — both run if both are set:
#   1. transcript_path → its parent directory, --mode convos
#   2. MEMPAL_DIR (user-configured project) → --mode projects
#
# IMPORTANT (Cursor caveat): the --mode convos mine is BEST-EFFORT for
# Cursor. Cursor's transcript format is undocumented and
# mempalace/normalize.py has no Cursor parser, so this call does not
# yet produce clean verbatim conversation drawers — at best it ingests
# raw bytes. The verbatim-capture guarantee for Cursor is carried by
# the followup_message below, which drives the agent to file its own
# in-context verbatim quotes. The --mode projects target (MEMPAL_DIR)
# is unaffected — normalize.py reads ordinary project files fine.
#
# Both run with stdout/stderr appended to the cursor log and are
# backgrounded so a slow mine cannot push the hook past its
# Cursor-configured timeout. `command -v mempalace` gates so a user
# without the CLI on PATH (e.g. a fresh GUI-launched session) does
# not see a noisy error.
if command -v mempalace >/dev/null 2>&1; then
    if mempal_is_valid_transcript "$MEMPAL_TRANSCRIPT" \
        && [ -f "$MEMPAL_TRANSCRIPT" ]; then
        ( mempalace mine "$(dirname "$MEMPAL_TRANSCRIPT")" --mode convos \
            >> "$MEMPAL_CURSOR_LOG" 2>&1 ) &
    elif [ -n "$MEMPAL_TRANSCRIPT" ]; then
        mempal_log "stop" "$MEMPAL_CONV_ID" \
            "skipping invalid transcript path: $MEMPAL_TRANSCRIPT"
    fi
    if [ -n "$MEMPAL_DIR" ] && [ -d "$MEMPAL_DIR" ]; then
        ( mempalace mine "$MEMPAL_DIR" --mode projects \
            >> "$MEMPAL_CURSOR_LOG" 2>&1 ) &
    fi
else
    mempal_log "stop" "$MEMPAL_CONV_ID" \
        "mempalace CLI not on PATH; skipping background mine"
fi

# The followup is the load-bearing verbatim path for Cursor (see header),
# so it fires by default. Honour the opt-out for users who want silence.
if mempal_followup_silenced; then
    mempal_log "stop" "$MEMPAL_CONV_ID" \
        "followup silenced (MEMPAL_CURSOR_SILENT/MEMPAL_VERBOSE); background mine only"
    mempal_emit '{}'
    exit 0
fi

_mempal_build_followup
