#!/bin/bash
# MEMPALACE ANTIGRAVITY SAVE HOOK — Stop event handler
#
# Antigravity fires the Stop event each time the agent's execution loop
# terminates. We use it to background-mine the active conversation
# transcript every Nth save into the user's MemPalace, and to write a
# diary checkpoint via `mempalace mine --mode convos`.
#
# Mirrors the Claude Code (hooks/mempal_save_hook.sh) and Codex
# (.codex-plugin/hooks/mempal-hook.sh) integrations as closely as the
# Antigravity stdin/stdout contract allows. Differences:
#
#   * Antigravity stdin uses camelCase: conversationId, transcriptPath,
#     workspacePaths, executionNum, terminationReason, fullyIdle.
#   * Antigravity stdout MUST be `{}` on every code path. Emitting
#     `{"decision":"continue"}` would force the agent to keep running
#     and create an infinite loop. We never call mempal_emit_stop_pass
#     with anything other than the literal empty object.
#   * Counter file is namespaced antigravity_save_count_<conversationId>
#     to coexist with Claude Code / Cursor / Codex state in the same
#     ~/.mempalace/hook_state/ directory.
#
# === STDIN (verified, camelCase) ===
# {
#   "executionNum": 1,
#   "terminationReason": "model_stop",
#   "error": "",
#   "fullyIdle": true,
#   "conversationId": "<uuid>",
#   "workspacePaths": ["/abs/path/..."],
#   "transcriptPath": "/abs/path/transcript.jsonl",
#   "artifactDirectoryPath": "/abs/path/artifacts/"
# }
#
# === STDOUT (always) ===
# {}
#
# `set -e` is intentionally NOT enabled — a broken hook must not block
# the user's conversation (constraint #2 in the integration brief).

# ── Locate this script + source common helpers ───────────────────────
MEMPAL_AGY_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
. "$MEMPAL_AGY_HOOK_DIR/lib/common.sh"

# ── Read all of stdin once ───────────────────────────────────────────
INPUT=$(cat)

# ── Kill switch: short-circuit cleanly if disabled ───────────────────
if mempal_kill_switch_tripped; then
    mempal_emit_stop_pass
    exit 0
fi

# ── Opportunistic GC of stale per-conversation state ─────────────────
#
# Self-throttled to at most once per 24h (see mempal_gc_stale_state),
# so this is a single mtime check on the overwhelming majority of
# fires. Runs after the kill switch so a disabled hook touches nothing.
mempal_gc_stale_state

# ── Parse stdin (camelCase, sentinel-guarded) ────────────────────────
_parsed=$(mempal_parse_stdin "$INPUT")
_marker=$(printf '%s\n' "$_parsed" | sed -n '1p')
CONVERSATION_ID=$(printf '%s\n' "$_parsed" | sed -n '2p')
TRANSCRIPT_PATH=$(printf '%s\n' "$_parsed" | sed -n '3p')
WORKSPACE_PATH=$(printf '%s\n' "$_parsed" | sed -n '4p')
# Line 5 (artifactDirectoryPath) is parsed but unused for save. Skip.
EXECUTION_NUM=$(printf '%s\n' "$_parsed" | sed -n '6p')
# Line 7 (terminationReason) is parsed but used only for logging.
TERMINATION_REASON=$(printf '%s\n' "$_parsed" | sed -n '7p')
FULLY_IDLE=$(printf '%s\n' "$_parsed" | sed -n '8p')

# ── Defense-in-depth: surface raw input on parse failure ─────────────
#
# When the sentinel is missing, Python crashed before reaching its
# print() calls. Persist the offending payload (capped at 4 KB, mode
# 0600) so the next debugger doesn't lose a day to log lines that say
# "Session unknown".
if [ -n "$INPUT" ] && [ "$_marker" != "__MEMPAL_PARSE_OK__" ]; then
    mempal_log "stop" "unknown" "input parse failed (sentinel missing); see antigravity_last_input.log + antigravity_last_python_err.log"
    (
        umask 077
        printf '%s' "$INPUT" | head -c 4096 > "$MEMPAL_STATE_DIR/antigravity_last_input.log"
    )
    chmod 600 "$MEMPAL_STATE_DIR/antigravity_last_input.log" 2>/dev/null
    # Continue with empty fields; the validators below will reject.
fi

CONVERSATION_ID="${CONVERSATION_ID:-unknown}"
TRANSCRIPT_PATH="${TRANSCRIPT_PATH:-}"
WORKSPACE_PATH="${WORKSPACE_PATH:-}"
EXECUTION_NUM="${EXECUTION_NUM:-0}"
TERMINATION_REASON="${TERMINATION_REASON:-}"
FULLY_IDLE="${FULLY_IDLE:-False}"

# Expand ~ in the transcript path
TRANSCRIPT_PATH="${TRANSCRIPT_PATH/#\~/$HOME}"

# ── Bail when fullyIdle is False ─────────────────────────────────────
#
# If background commands or async tasks are still running, the
# transcript is still in motion. Defer the save until the next Stop
# event when the agent is fully done — better to skip than to ingest a
# half-finished transcript and pollute the search index.
if [ "$FULLY_IDLE" != "True" ]; then
    mempal_log "stop" "$CONVERSATION_ID" "deferring save: fullyIdle=False (executionNum=$EXECUTION_NUM, terminationReason=$TERMINATION_REASON)"
    mempal_emit_stop_pass
    exit 0
fi

# ── Skip when terminationReason is `error` ───────────────────────────
#
# A model error termination usually means the transcript is corrupt or
# truncated. Don't ingest noise.
if [ "$TERMINATION_REASON" = "error" ]; then
    mempal_log "stop" "$CONVERSATION_ID" "skipping save: terminationReason=error"
    mempal_emit_stop_pass
    exit 0
fi

# ── Increment counter (per conversation) ─────────────────────────────
#
# The counter is a single integer, written via mempal_write_counter_atomic
# (same-dir temp + `mv`, which is an atomic rename on one filesystem).
# Concurrent Stop fires for the same conversation are unlikely
# (Antigravity serializes turns), but the atomic write means a
# concurrent reader always sees a complete value rather than a
# half-written / truncated file. The integer-only validation on read
# is a second guard: any garbled value resets the count to 0.
COUNTER_FILE="$MEMPAL_STATE_DIR/antigravity_save_count_${CONVERSATION_ID}"
COUNT=0
if [ -f "$COUNTER_FILE" ]; then
    raw=$(cat "$COUNTER_FILE" 2>/dev/null)
    case "$raw" in
        ''|*[!0-9]*) COUNT=0 ;;
        *) COUNT="$raw" ;;
    esac
fi
COUNT=$((COUNT + 1))
mempal_write_counter_atomic "$COUNTER_FILE" "$COUNT"

INTERVAL=$(mempal_save_interval)
mempal_log "stop" "$CONVERSATION_ID" "count=$COUNT interval=$INTERVAL executionNum=$EXECUTION_NUM workspace=$WORKSPACE_PATH"

# ── Modulo gate ──────────────────────────────────────────────────────
#
# `count % interval == 0` triggers a save. INTERVAL has been floored to
# >= 1 by mempal_save_interval, so the modulo cannot divide by zero
# even if the user explicitly set MEMPAL_SAVE_INTERVAL=0 or empty.
if [ $((COUNT % INTERVAL)) -ne 0 ]; then
    mempal_emit_stop_pass
    exit 0
fi

# ── Pending-marker guard ─────────────────────────────────────────────
#
# If a previous save is still running (the marker file exists), skip
# this fire. The mine subprocess removes the marker on exit, but a
# crashed mine could leave the marker forever — guard against that by
# treating markers older than 1 hour as stale and reclaiming them.
PENDING_FILE="$MEMPAL_STATE_DIR/antigravity_pending_${CONVERSATION_ID}"
if [ -f "$PENDING_FILE" ]; then
    # mtime in epoch seconds (portable; BSD/macOS `date -r` takes epoch, not a path).
    if mtime=$("$MEMPAL_PYTHON_BIN" -c 'import os, sys; print(int(os.path.getmtime(sys.argv[1])))' "$PENDING_FILE" 2>/dev/null) \
       && now=$(date '+%s') \
       && [ -n "$mtime" ] \
       && [ "$((now - mtime))" -lt 3600 ]; then
        mempal_log "stop" "$CONVERSATION_ID" "pending save still in flight; skipping"
        mempal_emit_stop_pass
        exit 0
    fi
    mempal_log "stop" "$CONVERSATION_ID" "stale pending marker reclaimed"
    rm -f "$PENDING_FILE" 2>/dev/null
fi

# ── Validate transcript path ─────────────────────────────────────────
if ! mempal_is_valid_transcript_path "$TRANSCRIPT_PATH"; then
    mempal_log "stop" "$CONVERSATION_ID" "invalid transcriptPath rejected: $TRANSCRIPT_PATH"
    mempal_emit_stop_pass
    exit 0
fi
if [ ! -f "$TRANSCRIPT_PATH" ]; then
    mempal_log "stop" "$CONVERSATION_ID" "transcriptPath does not exist: $TRANSCRIPT_PATH"
    mempal_emit_stop_pass
    exit 0
fi

# ── Trigger save ─────────────────────────────────────────────────────
WING=$(mempal_infer_wing "$WORKSPACE_PATH")
TRANSCRIPT_DIR=$(dirname "$TRANSCRIPT_PATH")

mempal_log "stop" "$CONVERSATION_ID" "TRIGGERING SAVE wing=$WING transcript_dir=$TRANSCRIPT_DIR"

# Drop the pending marker BEFORE spawning so a near-simultaneous fire
# sees it. If the spawn fails, remove the marker so the next fire can
# retry.
: > "$PENDING_FILE" 2>/dev/null

# Detach EVERYTHING heavy into a single background subshell: the
# runnability probe, the mine itself, and the pending-marker cleanup.
# The foreground returns immediately after spawning, so the hook's
# stdout (`{}`) reaches Antigravity within milliseconds.
#
# Why the probe must NOT run in the foreground: `mempalace --version`
# is NOT cheap. Building the `mine` argument parser imports
# `mempalace.miner` (-> palace -> backends -> chromadb/onnx) before
# argparse ever processes `--version`, so the probe pays the full
# cold-start import cost. Running it in the foreground would block the
# hook for that entire import and blow the <500ms save budget. Moving
# it inside the backgrounded subshell keeps the foreground instant.
#
# Folding the cleanup into this same subshell also removes the need for
# a separate process-liveness polling watcher: the `rm -f
# "$PENDING_FILE"` simply runs after the mine returns, in the same
# shell that owns the mine — no sibling-PID `wait` hazard, no polling
# loop.
#
# We invoke mempalace as `"$MEMPAL_PYTHON_BIN" -m mempalace` rather than
# the bare `mempalace` console script so a user with the package
# installed only inside a venv (and the venv's bin/ not on the hook's
# PATH, e.g. `uv tool install` in some distributions, or a manually
# managed virtualenv) still hits a working mine. MEMPAL_PYTHON honours
# user override; sees ``mempalace/__main__.py`` which dispatches to
# ``mempalace.cli:main`` — identical to the console script.
mempal_log "stop" "$CONVERSATION_ID" "spawning background mine wing=$WING transcript_dir=$TRANSCRIPT_DIR"
(
    if "$MEMPAL_PYTHON_BIN" -m mempalace --version >/dev/null 2>&1; then
        "$MEMPAL_PYTHON_BIN" -m mempalace mine "$TRANSCRIPT_DIR" \
            --mode convos \
            --wing "$WING" \
            >> "$MEMPAL_AGY_LOG" 2>&1 < /dev/null
        mempal_log "stop" "$CONVERSATION_ID" "background mine finished wing=$WING"
    else
        mempal_log "stop" "$CONVERSATION_ID" "ERROR: mempalace is not runnable via $MEMPAL_PYTHON_BIN -m mempalace; install mempalace or set MEMPAL_PYTHON"
    fi
    rm -f "$PENDING_FILE" 2>/dev/null
) >/dev/null 2>&1 < /dev/null &

# ── Always emit `{}` ─────────────────────────────────────────────────
#
# Never `{"decision":"continue"}`. That would force the agent into an
# infinite re-execution loop. mempal_emit_stop_pass hard-codes `{}`.
mempal_emit_stop_pass
exit 0
