#!/bin/bash
# MEMPALACE CURSOR SAVE HOOK — Auto-save every N stop events
#
# Cursor "stop" hook. After every agent loop ends, this hook:
#   1. Counts stop invocations per conversation_id (each stop ≈ one
#      assistant turn ≈ roughly one user message — see plan rationale).
#   2. Every SAVE_INTERVAL stops, mines the Cursor JSONL transcript
#      in the background (`mempalace mine --mode convos --wing`).
#      Silent by default — no followup in the chat window.
#   3. On the next stop, loop_count > 0 so we let the agent finish
#      without re-firing — Cursor's loop_count is the equivalent of
#      Claude Code's stop_hook_active flag.
#   4. If the preCompact hook has left a `.pending` marker, force a
#      save followup only when MEMPAL_VERBOSE is on, then clear the
#      marker either way.
#
# === WHY THE FOLLOWUP IS OPT-IN (matches the Claude hook) ===
#
# The Claude Code hook (hooks/mempal_save_hook.sh) is SILENT by default:
# its background `mempalace mine --mode convos` captures the verbatim
# transcript on its own, and the LLM-driven diary nudge is opt-IN behind
# MEMPAL_VERBOSE. That works because mempalace/normalize.py has a Claude
# Code JSONL parser.
#
# Cursor now has the same path. `_try_cursor_jsonl` in normalize.py
# unwraps `<user_query>`, drops injected Cursor blocks, and formats
# Cursor tool_use records, so the background mine files clean verbatim
# drawers. The followup_message is therefore opt-in via
# MEMPAL_VERBOSE=true (same as Claude). MEMPAL_CURSOR_SILENT is kept as
# a no-op alias for older installs that set it when the followup was
# the default.
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
    "MemPalace save checkpoint. Call mempalace_checkpoint ONCE with: "
    "items=[{wing: " + wing + ", room: <short topic>, content: <verbatim "
    "quote>}, ...] for the key topics, decisions, and verbatim quotes from "
    "this session; and diary={agent_name: cursor-ide, wing: " + wing + ", "
    "entry: <AAAK-format summary>}. It dedups, files non-duplicates, and "
    "writes the diary in one call. Then stop."
)
print(json.dumps({"followup_message": msg}))
' "$WING"
}

# ── Pending-save marker from preCompact ───────────────────────────
#
# preCompact cannot itself emit a followup_message (Cursor docs:
# preCompact is observational-only, output supports only user_message),
# so it drops a marker file and we consume it here. Forces a save
# nudge regardless of the counter — only when MEMPAL_VERBOSE is on.
if mempal_consume_pending "$MEMPAL_CONV_ID"; then
    mempal_log "stop" "$MEMPAL_CONV_ID" \
        "consumed pending-save marker (post-compaction)"
    if mempal_verbose; then
        _mempal_build_followup
        exit 0
    fi
    mempal_log "stop" "$MEMPAL_CONV_ID" \
        "followup silent by default; emitting {} (set MEMPAL_VERBOSE=true to nudge)"
    mempal_emit '{}'
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

# ── Background mine ───────────────────────────────────────────────
#
# Two independent targets — both run if both are set:
#   1. transcript_path → its parent directory, --mode convos --wing
#   2. MEMPAL_DIR (user-configured project) → --mode projects
#
# The --mode convos mine is the verbatim-capture path: normalize.py
# parses Cursor JSONL (`_try_cursor_jsonl`) and files the user's
# exact words. --wing keeps the drawers in the workspace wing the
# wake hook already searches (same as the Antigravity hook).
#
# Both run with stdout/stderr appended to the cursor log and are
# backgrounded so a slow mine cannot push the hook past its
# Cursor-configured timeout. Invoke via `"$MEMPAL_PYTHON_BIN" -m
# mempalace` (same as the Claude Code and Antigravity hooks) so a
# GUI-launched Cursor session whose PATH lacks the console script
# still mines through the interpreter that can import the package.
if "$MEMPAL_PYTHON_BIN" -m mempalace --version >/dev/null 2>&1; then
    if mempal_is_valid_transcript "$MEMPAL_TRANSCRIPT" \
        && [ -f "$MEMPAL_TRANSCRIPT" ]; then
        mempal_log "stop" "$MEMPAL_CONV_ID" \
            "spawning background mine wing=$WING"
        ( "$MEMPAL_PYTHON_BIN" -m mempalace mine \
            "$(dirname "$MEMPAL_TRANSCRIPT")" --mode convos \
            --wing "$WING" \
            >> "$MEMPAL_CURSOR_LOG" 2>&1 ) &
    elif [ -n "$MEMPAL_TRANSCRIPT" ]; then
        mempal_log "stop" "$MEMPAL_CONV_ID" \
            "skipping invalid transcript path: $MEMPAL_TRANSCRIPT"
    fi
    if [ -n "$MEMPAL_DIR" ] && [ -d "$MEMPAL_DIR" ]; then
        ( "$MEMPAL_PYTHON_BIN" -m mempalace mine "$MEMPAL_DIR" \
            --mode projects --wing "$WING" \
            >> "$MEMPAL_CURSOR_LOG" 2>&1 ) &
    fi
else
    mempal_log "stop" "$MEMPAL_CONV_ID" \
        "mempalace is not runnable via $MEMPAL_PYTHON_BIN -m mempalace; skipping background mine"
fi

# Followup is opt-in. Default is silent — the background mine is the
# verbatim path. Set MEMPAL_VERBOSE=true to also nudge a diary write.
if mempal_verbose; then
    _mempal_build_followup
    exit 0
fi

mempal_log "stop" "$MEMPAL_CONV_ID" \
    "followup silent by default; background mine only (set MEMPAL_VERBOSE=true to nudge)"
mempal_emit '{}'
