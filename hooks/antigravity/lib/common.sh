# shellcheck shell=bash
# MEMPALACE ANTIGRAVITY HOOK — shared helpers
#
# Sourced by the two Antigravity hook scripts:
#   * mempal_save_hook_antigravity.sh   (Stop event)
#   * mempal_wake_hook_antigravity.sh   (PreInvocation event, gated to invocationNum==1)
#
# Mirrors the conventions of the existing Claude Code hook scripts
# (hooks/mempal_save_hook.sh, hooks/mempal_precompact_hook.sh):
#
#   * STATE_DIR layout under ~/.mempalace/hook_state/
#   * MEMPAL_PYTHON resolution order (override -> $PATH -> bare python3)
#   * MEMPALACE_HOOKS_AUTO_SAVE=false kill switch (config.json fallback)
#   * sentinel-guarded Python parser via `sed -n 'Np'` (bash 3.2 safe)
#   * fail-open on internal errors: emit valid JSON and log, never crash
#     the hook host
#
# Antigravity-specific contract differences from Claude / Cursor:
#
#   * Antigravity stdin uses camelCase (transcriptPath, conversationId,
#     workspacePaths, executionNum, terminationReason, fullyIdle,
#     invocationNum, initialNumSteps), not the snake_case Claude Code
#     format (session_id, transcript_path, stop_hook_active).
#   * Antigravity stdout for Stop event MUST be {} on every success path
#     because { "decision": "continue" } would force the agent into an
#     infinite re-execution loop. The save hook explicitly refuses to
#     ever emit the "continue" decision.
#   * Antigravity stdout for PreInvocation can carry an "injectSteps"
#     array of { "ephemeralMessage": "..." } objects to inject memory
#     into the agent's first turn.
#
# This file is sourced, not executed, so it intentionally has no
# shebang. The `# shellcheck shell=bash` directive above tells
# shellcheck to treat it as bash when run standalone.

# bash 3.2.57 (the macOS default) is the lower bound. Do not use
# `mapfile`, `readarray`, `declare -A`, or `${var^^}` — none of those
# exist in 3.2. Use `sed -n 'Np'` for line extraction and case-folding
# via `tr` instead.

# ── State directory + log path ────────────────────────────────────────
#
# Honour MEMPAL_STATE_DIR while keeping the default identical to the
# Claude Code hooks so a user running both keeps a single state directory
# (constraint #7 in the integration brief).
MEMPAL_STATE_DIR="${MEMPAL_STATE_DIR:-$HOME/.mempalace/hook_state}"
mkdir -p "$MEMPAL_STATE_DIR" 2>/dev/null
MEMPAL_AGY_LOG="$MEMPAL_STATE_DIR/antigravity_hook.log"

# ── Python interpreter resolution ─────────────────────────────────────
#
# The hooks run mempalace as `"$MEMPAL_PYTHON_BIN" -m mempalace`, so the
# resolved interpreter MUST be one that has the mempalace package
# importable. The single most common install path —
# `uv tool install mempalace` (and `pipx install`) — puts the
# `mempalace` / `mempalace-mcp` *console scripts* on PATH inside an
# ISOLATED environment whose interpreter is NOT the system `python3`.
# So naively resolving `command -v python3` lands on a system Python
# that can't import mempalace, the `-m mempalace` probe fails, and
# mining silently never fires. (This bit a real user on PR #1633.)
#
# Resolution order — first hit wins:
#   1. $MEMPAL_PYTHON                         — explicit operator override
#   2. shebang of the mempalace-mcp / mempalace console script on PATH
#        — pip/uv write these with an absolute-path shebang pointing at
#          the exact interpreter that owns the package. This is the SAME
#          console script mcp_config.json launches, so if the MCP server
#          can start, the hooks resolve a working interpreter too — no
#          MEMPAL_PYTHON needed for the common install paths.
#   3. $(command -v python3)                  — an activated dev venv /
#          editable install where python3 itself owns the package
#   4. bare "python3"                         — last-resort fallback
#
# Steps 2-4 are pure string parsing + stat (no Python subprocess), so
# resolution stays cheap enough to run at source time on every hook
# fire, including gated-out / kill-switched ones. We deliberately do
# NOT run an `import mempalace` probe here: building that import pays
# the chromadb/onnx cold-start cost, which a recent perf fix
# (df295bd) moved OFF the hook foreground on purpose. The downstream
# `-m mempalace --version` probe (backgrounded in the save hook,
# subprocessed in the wake hook) is the safety net that catches a
# shebang interpreter whose package is genuinely broken.
mempal_resolve_python() {
    # 1. Explicit override always wins.
    local p="${MEMPAL_PYTHON:-}"
    if [ -n "$p" ] && [ -x "$p" ]; then
        printf '%s' "$p"
        return 0
    fi

    # 2. Derive the interpreter from a mempalace console-script shebang.
    local script_path shebang interp
    for script_path in mempalace-mcp mempalace; do
        script_path="$(command -v "$script_path" 2>/dev/null || true)"
        [ -n "$script_path" ] || continue
        [ -r "$script_path" ] || continue
        shebang="$(sed -n '1p' "$script_path" 2>/dev/null)"
        case "$shebang" in
            '#!'*)
                interp="${shebang#\#!}"     # drop the leading '#!'
                interp="${interp%$'\r'}"    # strip a trailing CR (CRLF files)
                interp="${interp# }"        # drop one leading space
                interp="${interp%% *}"      # first whitespace-delimited token
                # Guard against `#!/usr/bin/env python` wrappers: only
                # accept a token whose basename looks like a Python
                # interpreter and is executable. An `env`-style shebang
                # yields `/usr/bin/env` here, which we skip.
                case "${interp##*/}" in
                    python*)
                        if [ -x "$interp" ]; then
                            printf '%s' "$interp"
                            return 0
                        fi
                        ;;
                esac
                ;;
        esac
    done

    # 3. First python3 on PATH.
    p="$(command -v python3 2>/dev/null || true)"
    if [ -n "$p" ]; then
        printf '%s' "$p"
        return 0
    fi

    # 4. Last-resort bare name.
    printf '%s' "python3"
}
MEMPAL_PYTHON_BIN="$(mempal_resolve_python)"

# ── Logging ───────────────────────────────────────────────────────────
#
# ISO8601Z timestamps are greppable across timezones.
mempal_log() {
    local event="${1:-?}"
    local conv="${2:-unknown}"
    local msg="${3:-}"
    local ts
    ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf '[%s] [event=%s] [conv=%s] %s\n' "$ts" "$event" "$conv" "$msg" \
        >> "$MEMPAL_AGY_LOG" 2>/dev/null
}

# ── Kill switch ───────────────────────────────────────────────────────
#
# Disabled if ANY of:
#   * MEMPAL_DISABLE_HOOK is a truthy string
#   * MEMPALACE_HOOKS_AUTO_SAVE is false/0/no
#   * ~/.mempalace/config.json sets hooks.auto_save: false
#   * ~/.mempalace/ directory does not exist (user nuked the palace)
#
# Returns 0 (kill switch tripped, hook should short-circuit) or non-zero
# (proceed normally).
mempal_kill_switch_tripped() {
    # Palace nuke is the strongest signal: respect it before touching
    # disk for state, logging, etc.
    if [ ! -d "$HOME/.mempalace" ]; then
        return 0
    fi

    case "${MEMPAL_DISABLE_HOOK:-}" in
        1|true|TRUE|yes|YES) return 0 ;;
    esac

    case "${MEMPALACE_HOOKS_AUTO_SAVE:-}" in
        false|FALSE|0|no|NO) return 0 ;;
    esac

    local cfg="$HOME/.mempalace/config.json"
    if [ -f "$cfg" ]; then
        local auto
        auto=$("$MEMPAL_PYTHON_BIN" -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    print(str(cfg.get('hooks', {}).get('auto_save', True)).lower())
except Exception:
    print('true')
" "$cfg" 2>/dev/null)
        if [ "$auto" = "false" ]; then
            return 0
        fi
    fi

    return 1
}

# ── camelCase JSON parser (Antigravity stdin) ────────────────────────
#
# Reads JSON from stdin once and prints a sanitized, sentinel-bracketed
# block of fields the bash side can grab via `sed -n 'Np'`. Why a
# sentinel and per-line layout: bash 3.2 doesn't have `mapfile` or
# `readarray`, and `eval`-on-shell-var is the wrong shape (every value
# is user-controllable JSON). Sentinel + line offset is the same pattern
# the existing Claude Code hook (hooks/mempal_save_hook.sh) uses.
#
# Output layout (one field per line; line numbers are stable and the
# fields are documented in STDIN_SHAPE.md):
#
#   line 1: __MEMPAL_PARSE_OK__       — sentinel (parse success marker)
#   line 2: conversationId            — sanitized to [A-Za-z0-9._-]
#   line 3: transcriptPath            — sanitized to a safe path charset
#   line 4: workspacePath             — workspacePaths[0], sanitized
#   line 5: artifactDirectoryPath     — sanitized
#   line 6: executionNum              — integer, default 0
#   line 7: terminationReason         — sanitized to [a-z_]
#   line 8: fullyIdle                 — "True" or "False" (string)
#   line 9: invocationNum             — integer, default 0
#   line 10: initialNumSteps          — integer, default 0
#
# The sanitizers are defense-in-depth: every field is also vetted by
# the Python json.load step, but we still strip shell-meaningful chars
# from any field a downstream bash variable might interpolate, so that
# a hostile / malformed harness payload cannot inject command tokens.
#
# Stderr from Python is captured to last_python_err.log at mode 0600 so
# operators can debug parse failures without re-firing the hook. The
# umask 077 on the inner subshell creates the file at 0600 atomically;
# the explicit chmod 600 below is a belt-and-suspenders guard if a
# future edit ever drops the umask.
mempal_parse_stdin() {
    local input="$1"
    (
        umask 077
        printf '%s' "$input" | "$MEMPAL_PYTHON_BIN" -c "
import sys, json, re

# IMPORTANT: do NOT wrap json.load in a try/except. If the input is
# not valid JSON we want Python to exit non-zero BEFORE printing the
# __MEMPAL_PARSE_OK__ sentinel — the bash caller looks for the
# sentinel on line 1 to decide whether to engage its defense-in-depth
# 'failed to parse' branch. Catching the exception and falling back
# to data={} would let the sentinel print, masking parse failures
# from the bash side. The traceback lands in
# antigravity_last_python_err.log so operators can debug.
data = json.load(sys.stdin)

def safe(s, allowed=r'[^a-zA-Z0-9_/.\-~]'):
    return re.sub(allowed, '', str(s))

def safe_id(s):
    return re.sub(r'[^a-zA-Z0-9._-]', '', str(s))

def safe_int(v, default=0):
    try:
        n = int(v)
        return n if n >= 0 else default
    except Exception:
        return default

def safe_lower_alpha_underscore(s):
    return re.sub(r'[^a-z_]', '', str(s).lower())

conv_id = safe_id(data.get('conversationId', ''))
transcript = safe(data.get('transcriptPath', ''))
wp_arr = data.get('workspacePaths', [])
if isinstance(wp_arr, list) and wp_arr:
    workspace = safe(wp_arr[0])
else:
    workspace = ''
artifact = safe(data.get('artifactDirectoryPath', ''))
execution_num = safe_int(data.get('executionNum', 0))
termination_reason = safe_lower_alpha_underscore(data.get('terminationReason', ''))
fully_idle_raw = data.get('fullyIdle', None)
if fully_idle_raw is True or str(fully_idle_raw).lower() in ('true', '1', 'yes'):
    fully_idle = 'True'
else:
    fully_idle = 'False'
invocation_num = safe_int(data.get('invocationNum', 0))
initial_num_steps = safe_int(data.get('initialNumSteps', 0))

print('__MEMPAL_PARSE_OK__')
print(conv_id)
print(transcript)
print(workspace)
print(artifact)
print(execution_num)
print(termination_reason)
print(fully_idle)
print(invocation_num)
print(initial_num_steps)
" 2>"$MEMPAL_STATE_DIR/antigravity_last_python_err.log"
    )
    # Tidy up the err log: keep it iff non-empty (failure happened).
    if [ -s "$MEMPAL_STATE_DIR/antigravity_last_python_err.log" ]; then
        chmod 600 "$MEMPAL_STATE_DIR/antigravity_last_python_err.log" 2>/dev/null
    else
        rm -f "$MEMPAL_STATE_DIR/antigravity_last_python_err.log" 2>/dev/null
    fi
}

# ── Transcript path validator ─────────────────────────────────────────
#
# Mirrors mempalace.hooks_cli._validate_transcript_path: rejects empty,
# non-jsonl/json suffixes, and any `..` traversal segment.
mempal_is_valid_transcript_path() {
    local path="$1"
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

# ── Wing inference ────────────────────────────────────────────────────
#
# Takes the first workspace path from workspacePaths[] (already
# extracted into $1) and derives a `wing_<slug>` name from its leaf
# directory. Hyphens become underscores; spaces become underscores.
# Empty input yields wing_sessions, matching mempalace.hooks_cli's
# fallback.
mempal_infer_wing() {
    local workspace="$1"
    if [ -z "$workspace" ]; then
        printf 'wing_sessions'
        return 0
    fi
    # Strip trailing slashes
    while [ "${workspace}" != "${workspace%/}" ]; do
        workspace="${workspace%/}"
    done
    if [ -z "$workspace" ]; then
        printf 'wing_sessions'
        return 0
    fi
    local leaf="${workspace##*/}"
    if [ -z "$leaf" ]; then
        printf 'wing_sessions'
        return 0
    fi
    # Lowercase + hyphens-to-underscores. tr is bash 3.2 safe; ${var^^}
    # / ${var//-/_} on a fresh expansion are bash 4+ only.
    local slug
    slug=$(printf '%s' "$leaf" | tr 'A-Z' 'a-z' | tr ' -' '__')
    printf 'wing_%s' "$slug"
}

# ── Save-interval floor ───────────────────────────────────────────────
#
# Reads MEMPAL_SAVE_INTERVAL from the environment, floors it to >= 1
# so that `count % interval` cannot divide by zero. We hit the
# divide-by-zero shape on the Cursor PR review; this guards explicitly.
mempal_save_interval() {
    local raw="${MEMPAL_SAVE_INTERVAL:-15}"
    case "$raw" in
        ''|*[!0-9]*) printf '15'; return 0 ;;
    esac
    # Strip leading zeros. bash arithmetic ($((...))) parses any token
    # starting with `0` as octal, so MEMPAL_SAVE_INTERVAL=08 would
    # crash $((COUNT % INTERVAL)) with "value too great for base".
    # Loop while the value still starts with 0 AND has length > 1, so
    # the literal string "0" is preserved (then floored to 15 below).
    while [ "${raw}" != "${raw#0}" ] && [ "${#raw}" -gt 1 ]; do
        raw="${raw#0}"
    done
    if [ "$raw" -lt 1 ] 2>/dev/null; then
        printf '15'
        return 0
    fi
    printf '%s' "$raw"
}

# ── Atomic counter write ──────────────────────────────────────────────
#
# Writes $value to $file via a same-directory temp file + `mv -f`.
# `mv` (rename) is atomic on a single filesystem, so a concurrent
# reader either sees the old contents or the new contents — never a
# half-written / truncated file. A plain `printf > file` truncates
# first and then writes, leaving a window where a concurrent Stop fire
# could read an empty / partial value. Concurrent fires for one
# conversation are unlikely (Antigravity serializes turns) but the
# previous "written atomically" comment was simply false; this makes
# it true.
#
# bash 3.2 safe. The temp lives in the same directory as the target so
# the rename stays on one filesystem (a cross-device mv would fall back
# to copy+unlink and lose atomicity). On any failure we degrade to a
# direct write rather than leaving the counter unwritten.
mempal_write_counter_atomic() {
    local file="$1"
    local value="$2"
    local tmp
    tmp="$(mktemp "${file}.XXXXXX" 2>/dev/null)" || {
        printf '%s' "$value" > "$file"
        return
    }
    printf '%s' "$value" > "$tmp"
    mv -f "$tmp" "$file" 2>/dev/null || {
        rm -f "$tmp" 2>/dev/null
        printf '%s' "$value" > "$file"
    }
}

# ── State-file TTL ────────────────────────────────────────────────────
#
# Per-conversation state artifacts (antigravity_save_count_<conv>,
# antigravity_pending_<conv>, antigravity_woke_<conv>/) accumulate one
# set per conversation and are never otherwise removed. Reads
# MEMPAL_STATE_TTL_DAYS (default 30), validated digits-only and
# leading-zero-stripped like mempal_save_interval so `find -mtime`
# never sees a bad token. A value of 0 means "sweep aggressively"
# (everything older than today); we floor empty/garbage to 30.
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
# Opportunistic sweep of per-conversation state older than the TTL.
# Gated to run at most once per 24h via the antigravity_last_sweep
# marker so it costs nothing on the vast majority of fires (a single
# mtime comparison). When it does run, three `find` passes remove the
# stale counter files, pending markers, and woke marker directories.
#
# The name globs are specific (antigravity_save_count_*, _pending_*,
# _woke_*), so the shared log files (antigravity_hook.log,
# antigravity_last_input.log, antigravity_last_python_err.log) and the
# antigravity_last_sweep marker itself are never touched. BSD find
# (macOS default) and GNU find both accept `-maxdepth`, `-mtime +N`,
# and `-exec ... +`.
#
# Fail-open: every step is best-effort; a missing state dir, a find
# that errors, or a permission problem must never abort the caller.
mempal_gc_stale_state() {
    [ -d "$MEMPAL_STATE_DIR" ] || return 0

    local marker="$MEMPAL_STATE_DIR/antigravity_last_sweep"
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
        -name 'antigravity_save_count_*' -mtime +"$ttl" \
        -exec rm -f {} + 2>/dev/null
    find "$MEMPAL_STATE_DIR" -maxdepth 1 -type f \
        -name 'antigravity_pending_*' -mtime +"$ttl" \
        -exec rm -f {} + 2>/dev/null
    find "$MEMPAL_STATE_DIR" -maxdepth 1 -type d \
        -name 'antigravity_woke_*' -mtime +"$ttl" \
        -exec rm -rf {} + 2>/dev/null

    return 0
}

# ── Fail-open emitters ────────────────────────────────────────────────
#
# Every code path in both hooks must terminate by calling exactly one
# of these emitters. Stdout is JSON. Exit status is always 0 — the hook
# never blocks the user's IDE on its own failure (constraint #2).
#
# CRITICAL: mempal_emit_stop_pass MUST NEVER emit
# {"decision":"continue"} — that would force the agent to keep running
# instead of letting the turn end. Antigravity treats any value other
# than "continue" (including `{}`) as "allow the stop". We enforce this
# by hard-coding the empty object output here.
mempal_emit_stop_pass() {
    printf '{}\n'
}

mempal_emit_wake_inject() {
    local message="$1"
    if [ -z "$message" ]; then
        printf '{}\n'
        return 0
    fi
    # Encode the message as JSON via Python so embedded quotes / newlines
    # / control chars don't corrupt the output.
    "$MEMPAL_PYTHON_BIN" -c "
import json, sys
msg = sys.argv[1]
print(json.dumps({'injectSteps': [{'ephemeralMessage': msg}]}))
" "$message" 2>/dev/null || printf '{}\n'
}
