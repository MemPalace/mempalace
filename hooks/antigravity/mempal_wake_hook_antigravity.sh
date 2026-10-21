#!/bin/bash
# MEMPALACE ANTIGRAVITY WAKE HOOK — PreInvocation event handler
#
# Antigravity fires the PreInvocation event before every model
# invocation, with `invocationNum` carrying the sequence number of the
# call. We use the first invocation (invocationNum == 1) as our
# session-start equivalent and inject a verbatim memory pointer into
# the agent's context via the `injectSteps[].ephemeralMessage` output
# field — the message lives for one turn and does not persist into the
# transcript, so it doesn't pollute future invocations of this same
# conversation.
#
# === STDIN (verified, camelCase) ===
# {
#   "invocationNum": 1,
#   "initialNumSteps": 0,
#   "conversationId": "<uuid>",
#   "workspacePaths": ["/abs/path/..."],
#   "transcriptPath": "/abs/path/transcript.jsonl",
#   "artifactDirectoryPath": "/abs/path/artifacts/"
# }
#
# === STDOUT ===
# Either:
#   {}                                                  — no injection
# Or:
#   {"injectSteps":[{"ephemeralMessage":"..."}]}        — verbatim memory pointer
#
# Verbatim guarantee: the ephemeralMessage carries the exact text
# emitted by `mempalace wake-up`, never paraphrased or summarized.
#
# Performance budget: the integration brief sets a 100ms ceiling for
# startup injection. We enforce a 500ms hard timeout on the
# `mempalace wake-up` subprocess (more generous than 100ms because
# cold ChromaDB connections can dominate, and missing the budget is
# strictly better than blocking the user) — if it doesn't return in
# time we emit `{}` and let the conversation start without injection.
#
# `set -e` is intentionally NOT enabled — fail-open is mandatory.

# ── Locate this script + source common helpers ───────────────────────
MEMPAL_AGY_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
. "$MEMPAL_AGY_HOOK_DIR/lib/common.sh"

# ── Read all of stdin once ───────────────────────────────────────────
INPUT=$(cat)

# ── Kill switch ──────────────────────────────────────────────────────
if mempal_kill_switch_tripped; then
    mempal_emit_stop_pass
    exit 0
fi

# ── Parse stdin ──────────────────────────────────────────────────────
_parsed=$(mempal_parse_stdin "$INPUT")
_marker=$(printf '%s\n' "$_parsed" | sed -n '1p')
CONVERSATION_ID=$(printf '%s\n' "$_parsed" | sed -n '2p')
# Lines 3-5 (transcriptPath, workspacePath, artifactDirectoryPath) are
# parsed; we use workspacePath for wing inference. transcriptPath and
# artifactDirectoryPath are unused by the wake flow.
# Line 4: workspacePath
WORKSPACE_PATH=$(printf '%s\n' "$_parsed" | sed -n '4p')
INVOCATION_NUM=$(printf '%s\n' "$_parsed" | sed -n '9p')

# Defense-in-depth on parse failure
if [ -n "$INPUT" ] && [ "$_marker" != "__MEMPAL_PARSE_OK__" ]; then
    mempal_log "preInvocation" "unknown" "input parse failed (sentinel missing)"
    mempal_emit_stop_pass
    exit 0
fi

CONVERSATION_ID="${CONVERSATION_ID:-unknown}"
WORKSPACE_PATH="${WORKSPACE_PATH:-}"
INVOCATION_NUM="${INVOCATION_NUM:-0}"

# ── Gate: only inject on the FIRST invocation ────────────────────────
#
# PreInvocation fires before every model call. Without this gate we'd
# inject memory on every single turn — both expensive and visually
# noisy. invocationNum == 1 means "first model call of this
# conversation", which is the closest thing Antigravity exposes to
# Cursor's `sessionStart`.
if [ "$INVOCATION_NUM" != "1" ]; then
    mempal_emit_stop_pass
    exit 0
fi

# ── Loop guard ───────────────────────────────────────────────────────
#
# Defense in depth: even within the first invocation, we only ever
# want to inject once per conversation. mkdir is atomic and works on
# bash 3.2 / macOS / Linux without flock or other GNU coreutils
# extensions.
WOKE_MARKER="$MEMPAL_STATE_DIR/antigravity_woke_${CONVERSATION_ID}"
if ! mkdir "$WOKE_MARKER" 2>/dev/null; then
    mempal_log "preInvocation" "$CONVERSATION_ID" "already woke this conversation; skipping"
    mempal_emit_stop_pass
    exit 0
fi

# ── Run wake-up with a hard timeout ──────────────────────────────────
#
# `timeout` is GNU coreutils — present on most Linux installs but
# missing from stock macOS. Wrap the subprocess in a Python timeout
# (subprocess.run(timeout=...)) which is cross-platform. The Python
# script also constructs the final JSON envelope for stdout, so the
# bash side just passes the result through.
WING=$(mempal_infer_wing "$WORKSPACE_PATH")
mempal_log "preInvocation" "$CONVERSATION_ID" "WAKE injection wing=$WING invocationNum=$INVOCATION_NUM"

OUTPUT=$("$MEMPAL_PYTHON_BIN" -c "
import json, subprocess, sys

wing = sys.argv[1]
timeout_s = 0.5  # 500 ms

# Invoke as ``[sys.executable, '-m', 'mempalace', ...]`` rather than
# the bare ``mempalace`` console script. sys.executable is the same
# Python that resolved MEMPAL_PYTHON in lib/common.sh, so this binds
# the wake-up call to the correct interpreter (and its installed
# mempalace package) even when the venv's bin/ isn't on PATH.
try:
    completed = subprocess.run(
        [sys.executable, '-m', 'mempalace', 'wake-up', '--wing', wing],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if completed.returncode != 0:
        print('{}')
        sys.exit(0)
    body = (completed.stdout or '').strip()
    if not body:
        print('{}')
        sys.exit(0)
    # Verbatim — pass the wake-up text exactly as emitted, wrapped in
    # the Antigravity injectSteps envelope. json.dumps escapes embedded
    # control chars and quotes correctly.
    print(json.dumps({'injectSteps': [{'ephemeralMessage': body}]}))
except FileNotFoundError:
    print('{}')
except subprocess.TimeoutExpired:
    print('{}')
except Exception:
    print('{}')
" "$WING" 2>/dev/null)

if [ -z "$OUTPUT" ]; then
    OUTPUT='{}'
fi

# Sanity-check: never emit `decision` from a PreInvocation hook (that
# field belongs to the Stop event). The Python helper only ever
# constructs `{"injectSteps": [...]}` or `{}`, so this is belt-and-
# suspenders against a future edit ever leaking a Stop-shaped object.
case "$OUTPUT" in
    *\"decision\"*)
        mempal_log "preInvocation" "$CONVERSATION_ID" "ERROR: refused to emit decision field from wake hook"
        mempal_emit_stop_pass
        exit 0
        ;;
esac

printf '%s\n' "$OUTPUT"
exit 0
