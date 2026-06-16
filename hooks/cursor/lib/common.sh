# shellcheck shell=bash
# MEMPALACE CURSOR HOOK — shared helpers
#
# Sourced by the three Cursor hooks (stop / preCompact / sessionStart).
# Mirrors the conventions of the existing Claude Code hook scripts
# (hooks/mempal_save_hook.sh, hooks/mempal_precompact_hook.sh) so a
# user who already debugs one knows how to debug the other:
#
#   * STATE_DIR layout under ~/.mempalace/hook_state/
#   * MEMPAL_PYTHON resolution order (override → $PATH → bare python3)
#   * MEMPALACE_HOOKS_AUTO_SAVE=false kill switch (config.json fallback)
#   * sentinel-guarded Python parser via `sed -n 'Np'` (bash 3.2 safe)
#   * fail-open on internal errors: emit `{}` and log, never crash the
#     hook host
#
# Cursor-specific additions on top of that contract:
#
#   * MEMPAL_DISABLE_HOOK=1 as an additional kill-switch alias
#   * MEMPAL_STATE_DIR env override for the state directory
#   * conversation_id (Cursor's stable per-conversation ID) replaces
#     Claude Code's session_id in the counter file names — Cursor `stop`
#     events do not carry a session_id, only conversation_id
#   * loop_count is the loop-prevention signal in place of Claude Code's
#     stop_hook_active flag (Cursor docs, "stop" event)
#
# This file is sourced, not executed, so it intentionally has no
# shebang. The `# shellcheck shell=bash` directive above tells
# shellcheck to treat it as bash when run standalone.

# ── State directory + log path ────────────────────────────────────────
#
# Honour MEMPAL_STATE_DIR (additive override introduced for Cursor)
# while keeping the default identical to the Claude Code hooks so a
# user running both keeps a single state directory.
MEMPAL_STATE_DIR="${MEMPAL_STATE_DIR:-$HOME/.mempalace/hook_state}"
mkdir -p "$MEMPAL_STATE_DIR" 2>/dev/null
MEMPAL_CURSOR_LOG="$MEMPAL_STATE_DIR/cursor_hook.log"

# ── Python interpreter resolution ─────────────────────────────────────
#
# Same contract as the Claude Code hooks:
#   1. $MEMPAL_PYTHON        — explicit user override (absolute path)
#   2. $(command -v python3) — first python3 on the hook's PATH
#   3. bare "python3"        — last-resort fallback
mempal_resolve_python() {
    local p="${MEMPAL_PYTHON:-}"
    if [ -n "$p" ] && [ -x "$p" ]; then
        printf '%s' "$p"
        return 0
    fi
    p="$(command -v python3 2>/dev/null || true)"
    if [ -n "$p" ]; then
        printf '%s' "$p"
        return 0
    fi
    printf '%s' "python3"
}
MEMPAL_PYTHON_BIN="$(mempal_resolve_python)"

# ── Logging ───────────────────────────────────────────────────────────
#
# Lines are `[ISO8601Z] [event=...] [conv=...] message`. ISO8601 keeps
# the format greppable across timezones (the Claude Code log uses
# %H:%M:%S which loses the date — we improve on that here without
# changing the existing log file).
mempal_log() {
    local event="${1:-?}"
    local conv="${2:-unknown}"
    local msg="${3:-}"
    local ts
    ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf '[%s] [event=%s] [conv=%s] %s\n' "$ts" "$event" "$conv" "$msg" \
        >> "$MEMPAL_CURSOR_LOG" 2>/dev/null
}

# ── Kill switch ───────────────────────────────────────────────────────
#
# Disabled if ANY of:
#   * MEMPAL_DISABLE_HOOK is a truthy string (Cursor-prompt addition)
#   * MEMPALACE_HOOKS_AUTO_SAVE is false/0/no (Claude Code convention)
#   * ~/.mempalace/config.json has hooks.auto_save == false
#
# Returns 0 (true in shell) when disabled, 1 when enabled.
mempal_is_disabled() {
    case "${MEMPAL_DISABLE_HOOK:-}" in
        1|true|yes|on) return 0 ;;
    esac
    case "${MEMPALACE_HOOKS_AUTO_SAVE:-}" in
        false|0|no|off) return 0 ;;
    esac
    local cfg="$HOME/.mempalace/config.json"
    if [ -f "$cfg" ]; then
        local result
        # Use python -c '...' with the config path as argv[1] rather
        # than a heredoc. A heredoc body that contains parens inside a
        # $(...) command substitution trips the bash 3.2.57 parser
        # bug (macOS /bin/bash default) — gh-PR review caught this.
        # The -c form is also consistent with mempal_parse_stdin below.
        result="$("$MEMPAL_PYTHON_BIN" -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    print(str(cfg.get("hooks", {}).get("auto_save", True)).lower())
except Exception:
    print("true")
' "$cfg" 2>/dev/null)"
        if [ "$result" = "false" ]; then
            return 0
        fi
    fi
    return 1
}

# ── Stdin parser ──────────────────────────────────────────────────────
#
# Reads Cursor's hook JSON from $1 and exports:
#   MEMPAL_CONV_ID    — conversation_id, falls back to "unknown"
#   MEMPAL_LOOP_COUNT — integer (0 if absent / non-numeric)
#   MEMPAL_TRANSCRIPT — transcript_path, may be empty
#   MEMPAL_WORKSPACE  — first workspace_roots entry, falls back to
#                       CURSOR_PROJECT_DIR env var, then $PWD
#   MEMPAL_TRIGGER    — preCompact trigger ("auto" | "manual"), empty otherwise
#   MEMPAL_STATUS     — stop status ("completed" | "aborted" | "error"),
#                       empty otherwise
#   MEMPAL_PARSE_OK   — "1" if parser ran cleanly, "0" otherwise
#
# Uses the same sentinel + `sed -n 'Np'` extraction as the Claude Code
# hooks for bash 3.2 compatibility (mapfile/readarray are unavailable
# on macOS /bin/bash 3.2.57; #1440 regression). Each line of output is
# pre-sanitised by the Python side to a shell-safe character set.
mempal_parse_stdin() {
    local input="${1:-}"
    local parsed
    # We invoke Python via -c with a single-quoted multi-line string
    # rather than ``python3 - <<'PYEOF'`` because the heredoc form
    # would shadow Python's stdin with the heredoc body, leaving
    # ``json.load(sys.stdin)`` to read nothing and silently fail. The
    # parser body deliberately uses only double-quoted Python strings
    # so the surrounding bash single-quote is safe verbatim, and uses
    # only the shell-safe character set (alphanumeric, underscore,
    # dash, slash, dot, tilde) matching the Claude Code hook's
    # sanitiser so a hostile transcript_path cannot splice
    # metacharacters into the parsed output.
    parsed="$(
        umask 077
        printf '%s' "$input" | "$MEMPAL_PYTHON_BIN" -c '
import json, re, sys

def safe_str(value):
    return re.sub(r"[^a-zA-Z0-9_/.\-~]", "", str(value or ""))

def safe_int(value):
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "0"

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(data, dict):
    sys.exit(1)

conv = safe_str(data.get("conversation_id") or data.get("session_id"))
loop_count = safe_int(data.get("loop_count", 0))
transcript = safe_str(data.get("transcript_path", ""))
trigger = safe_str(data.get("trigger", ""))
status = safe_str(data.get("status", ""))

roots = data.get("workspace_roots") or []
workspace = ""
if isinstance(roots, list) and roots:
    workspace = safe_str(roots[0])

print("__MEMPAL_PARSE_OK__")
print(conv)
print(loop_count)
print(transcript)
print(workspace)
print(trigger)
print(status)
' 2>"$MEMPAL_STATE_DIR/cursor_last_python_err.log"
    )"

    # Drop empty stderr capture on success; lock it to 0600 on failure
    # (mirrors the privacy contract in the Claude Code hooks — the
    # traceback can echo back transcript_path / home layout).
    if [ -s "$MEMPAL_STATE_DIR/cursor_last_python_err.log" ]; then
        chmod 600 "$MEMPAL_STATE_DIR/cursor_last_python_err.log" 2>/dev/null
    else
        rm -f "$MEMPAL_STATE_DIR/cursor_last_python_err.log" 2>/dev/null
    fi

    local marker
    marker="$(printf '%s\n' "$parsed" | sed -n '1p')"
    if [ "$marker" = "__MEMPAL_PARSE_OK__" ]; then
        MEMPAL_PARSE_OK="1"
        MEMPAL_CONV_ID="$(printf '%s\n' "$parsed" | sed -n '2p')"
        MEMPAL_LOOP_COUNT="$(printf '%s\n' "$parsed" | sed -n '3p')"
        MEMPAL_TRANSCRIPT="$(printf '%s\n' "$parsed" | sed -n '4p')"
        MEMPAL_WORKSPACE="$(printf '%s\n' "$parsed" | sed -n '5p')"
        MEMPAL_TRIGGER="$(printf '%s\n' "$parsed" | sed -n '6p')"
        MEMPAL_STATUS="$(printf '%s\n' "$parsed" | sed -n '7p')"
    else
        MEMPAL_PARSE_OK="0"
        MEMPAL_CONV_ID=""
        MEMPAL_LOOP_COUNT="0"
        MEMPAL_TRANSCRIPT=""
        MEMPAL_WORKSPACE=""
        MEMPAL_TRIGGER=""
        MEMPAL_STATUS=""
    fi

    # Defaults and environment fallbacks. The Cursor docs guarantee
    # CURSOR_TRANSCRIPT_PATH and CURSOR_PROJECT_DIR env vars are set
    # for every hook execution; if JSON parsing failed for any reason
    # (sentinel missing, malformed payload, missing interpreter) we
    # still have a usable workspace.
    MEMPAL_CONV_ID="${MEMPAL_CONV_ID:-unknown}"
    case "$MEMPAL_LOOP_COUNT" in
        ''|*[!0-9]*) MEMPAL_LOOP_COUNT="0" ;;
    esac
    if [ -z "$MEMPAL_TRANSCRIPT" ] && [ -n "${CURSOR_TRANSCRIPT_PATH:-}" ]; then
        MEMPAL_TRANSCRIPT="${CURSOR_TRANSCRIPT_PATH}"
    fi
    if [ -z "$MEMPAL_WORKSPACE" ]; then
        if [ -n "${CURSOR_PROJECT_DIR:-}" ]; then
            MEMPAL_WORKSPACE="${CURSOR_PROJECT_DIR}"
        elif [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
            MEMPAL_WORKSPACE="${CLAUDE_PROJECT_DIR}"
        else
            MEMPAL_WORKSPACE="${PWD:-/}"
        fi
    fi

    # Expand a leading ~ in the transcript path so downstream
    # ``[ -f "$path" ]`` checks resolve correctly.
    case "$MEMPAL_TRANSCRIPT" in
        '~/'*) MEMPAL_TRANSCRIPT="$HOME/${MEMPAL_TRANSCRIPT#~/}" ;;
    esac
}

# ── Defense-in-depth: dump unparseable stdin ──────────────────────────
#
# Same shape as the Claude Code hooks' last_input.log: bounded to 4096
# bytes, overwritten (never appended) so a misconfiguration loop cannot
# grow disk usage, 0600 perms because the dump mirrors the raw hook
# payload (transcript_path reveals the user's home + project layout).
mempal_dump_bad_input() {
    local input="${1:-}"
    if [ -z "$input" ]; then
        return 0
    fi
    mempal_log "${2:-?}" "${MEMPAL_CONV_ID:-unknown}" \
        "WARN: input parse failed (sentinel missing); see $MEMPAL_STATE_DIR/cursor_last_input.log + cursor_last_python_err.log"
    (
        umask 077
        printf '%s' "$input" | head -c 4096 > "$MEMPAL_STATE_DIR/cursor_last_input.log"
    )
    chmod 600 "$MEMPAL_STATE_DIR/cursor_last_input.log" 2>/dev/null
}

# ── Counter helpers ───────────────────────────────────────────────────
#
# One counter file per conversation_id. Atomic write via temp file
# inside the same directory (rename is atomic on POSIX) so concurrent
# hook invocations cannot half-write the file. Read tolerates a
# corrupted or empty file by returning 0, never crashing.
_mempal_counter_path() {
    local conv="${1:-unknown}"
    # Sanitise the conv id one more time: it has already been through
    # the Python sanitiser, but be defensive in case a caller passes a
    # raw string. Strip any character outside [a-zA-Z0-9_.-].
    local safe_conv
    safe_conv="$(printf '%s' "$conv" | tr -cd 'a-zA-Z0-9_.-')"
    if [ -z "$safe_conv" ]; then
        safe_conv="unknown"
    fi
    printf '%s/cursor_%s.count' "$MEMPAL_STATE_DIR" "$safe_conv"
}

mempal_read_counter() {
    local path="$1"
    if [ ! -f "$path" ]; then
        printf '0'
        return 0
    fi
    local raw
    raw="$(cat "$path" 2>/dev/null)"
    case "$raw" in
        ''|*[!0-9]*) printf '0' ;;
        *) printf '%s' "$raw" ;;
    esac
}

mempal_write_counter_atomic() {
    local path="$1"
    local value="$2"
    case "$value" in
        ''|*[!0-9]*) value="0" ;;
    esac
    local tmp="${path}.tmp.$$"
    printf '%s' "$value" > "$tmp" 2>/dev/null || return 1
    mv "$tmp" "$path" 2>/dev/null || {
        rm -f "$tmp" 2>/dev/null
        return 1
    }
}

# ── Pending-save marker ───────────────────────────────────────────────
#
# Dropped by the preCompact hook (which cannot itself emit a
# followup_message — Cursor's preCompact is observational-only) and
# consumed by the next stop invocation so the LLM still gets a diary
# nudge after compaction.
_mempal_pending_path() {
    local conv="${1:-unknown}"
    local safe_conv
    safe_conv="$(printf '%s' "$conv" | tr -cd 'a-zA-Z0-9_.-')"
    if [ -z "$safe_conv" ]; then
        safe_conv="unknown"
    fi
    printf '%s/cursor_%s.pending' "$MEMPAL_STATE_DIR" "$safe_conv"
}

mempal_set_pending() {
    local conv="${1:-unknown}"
    local path
    path="$(_mempal_pending_path "$conv")"
    : > "$path" 2>/dev/null || return 1
    chmod 600 "$path" 2>/dev/null
}

mempal_consume_pending() {
    local conv="${1:-unknown}"
    local path
    path="$(_mempal_pending_path "$conv")"
    if [ -f "$path" ]; then
        rm -f "$path" 2>/dev/null
        return 0
    fi
    return 1
}

# ── State-file TTL ────────────────────────────────────────────────────
#
# Per-conversation state artifacts (cursor_<conv>.count and
# cursor_<conv>.pending) accumulate one set per conversation and are
# never otherwise removed (igorls review, PR #1632 — unbounded state
# growth). Reads MEMPAL_STATE_TTL_DAYS (default 30), validated
# digits-only and leading-zero-stripped (mirrors the SAVE_INTERVAL
# sanitiser) so `find -mtime` never sees a bad or octal token. Empty or
# non-numeric floors to 30; a value of 0 means "sweep everything older
# than today".
mempal_state_ttl_days() {
    local raw="${MEMPAL_STATE_TTL_DAYS:-30}"
    case "$raw" in
        ''|*[!0-9]*) printf '30'; return 0 ;;
    esac
    while [ "${raw}" != "${raw#0}" ] && [ "${#raw}" -gt 1 ]; do
        raw="${raw#0}"
    done
    printf '%s' "$raw"
}

# ── Stale state GC ────────────────────────────────────────────────────
#
# Opportunistic sweep of per-conversation Cursor state older than the
# TTL. Throttled to at most once per 24h via the cursor_last_sweep
# marker, so it costs a single mtime comparison on the vast majority of
# fires. When it does run, two `find` passes remove the stale counter
# files and pending markers.
#
# The globs are Cursor-specific and suffix-anchored (cursor_*.count,
# cursor_*.pending), so the shared logs (cursor_hook.log,
# cursor_last_input.log, cursor_last_python_err.log), the
# cursor_last_sweep marker itself, and any antigravity_*/Claude state
# sharing the same directory are never touched. BSD find (macOS default)
# and GNU find both accept -maxdepth, -mtime +N, and -exec ... +.
#
# Fail-open: every step is best-effort; a missing state dir, a find that
# errors, or a permission problem must never abort the caller.
mempal_gc_stale_state() {
    [ -d "$MEMPAL_STATE_DIR" ] || return 0

    local marker="$MEMPAL_STATE_DIR/cursor_last_sweep"
    if [ -f "$marker" ]; then
        local mtime now
        if mtime=$("$MEMPAL_PYTHON_BIN" -c 'import os, sys; print(int(os.path.getmtime(sys.argv[1])))' "$marker" 2>/dev/null) \
           && now=$(date '+%s' 2>/dev/null) \
           && [ -n "$mtime" ] \
           && [ "$((now - mtime))" -lt 86400 ]; then
            return 0
        fi
    fi
    # Touch the marker first so a crash mid-sweep still throttles the
    # next fire (better to skip a sweep than to hammer the disk).
    : > "$marker" 2>/dev/null

    local ttl
    ttl=$(mempal_state_ttl_days)

    find "$MEMPAL_STATE_DIR" -maxdepth 1 -type f \
        -name 'cursor_*.count' -mtime +"$ttl" \
        -exec rm -f {} + 2>/dev/null
    find "$MEMPAL_STATE_DIR" -maxdepth 1 -type f \
        -name 'cursor_*.pending' -mtime +"$ttl" \
        -exec rm -f {} + 2>/dev/null

    return 0
}

# ── Workspace → wing inference ────────────────────────────────────────
#
# basename(workspace_root), normalised to [a-z0-9_-]. Edge cases:
#   /              → "root"
#   /path/         → trailing slash stripped, then basename
#   "/foo bar/"    → "foo_bar" (spaces collapsed to underscores)
#   ""             → "cursor_session"
#   "C:\\proj"     → "proj" (Windows-style path; basename via tr fallback)
#
# We intentionally keep this in pure bash + POSIX tools so the
# inference is identical across the hook scripts and the test suite
# can target it as a function via `bash -c 'source ...; ...'`.
mempal_infer_wing() {
    local raw="${1:-}"
    if [ -z "$raw" ]; then
        printf 'cursor_session'
        return 0
    fi
    # Strip trailing slashes (but preserve the lone "/" case).
    while [ "$raw" != "/" ] && [ "${raw%/}" != "$raw" ]; do
        raw="${raw%/}"
    done
    if [ "$raw" = "/" ]; then
        printf 'root'
        return 0
    fi
    local base="${raw##*/}"
    # On Windows-style paths with backslashes, fall back to splitting
    # on backslash too so we don't return the whole path verbatim.
    case "$base" in
        *\\*) base="${base##*\\}" ;;
    esac
    # Lowercase + replace anything outside [a-z0-9_-] with underscore.
    # Collapse runs of underscores so "foo  bar" doesn't become
    # "foo__bar".
    base="$(printf '%s' "$base" \
        | tr '[:upper:]' '[:lower:]' \
        | tr -c 'a-z0-9_-' '_' \
        | tr -s '_' \
        | sed 's/^_//; s/_$//')"
    if [ -z "$base" ]; then
        printf 'cursor_session'
        return 0
    fi
    printf '%s' "$base"
}

# ── Transcript path validation ────────────────────────────────────────
#
# Mirrors hooks/mempal_save_hook.sh::is_valid_transcript_path so the
# Cursor and Claude Code hooks reject the same shapes:
#   * non-empty
#   * .json or .jsonl suffix
#   * no .. traversal segments
mempal_is_valid_transcript() {
    local path="${1:-}"
    [ -n "$path" ] || return 1
    case "$path" in
        *.json|*.jsonl) ;;
        *) return 1 ;;
    esac
    case "/$path/" in
        */../*) return 1 ;;
    esac
    return 0
}

# ── JSON emit ─────────────────────────────────────────────────────────
#
# Final stdout write. Uses ``printf '%s'`` instead of ``echo`` because
# echo interprets ``-n``/``-e``/``-E`` as flags and varies in backslash
# handling between builtin and /bin/echo (xpg_echo shopt). Matches the
# Claude Code hook's documented rationale.
mempal_emit() {
    printf '%s\n' "${1:-{\}}"
}
