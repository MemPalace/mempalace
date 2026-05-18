#!/bin/bash
# MEMPALACE PRE-COMPACT HOOK — Emergency save before compaction
#
# Claude Code "PreCompact" hook. Fires RIGHT BEFORE the conversation
# gets compressed to free up context window space.
#
# This is the safety net. When compaction happens, the AI loses detailed
# context about what was discussed. This hook forces one final save of
# EVERYTHING before that happens.
#
# Unlike the save hook (which gates on a message-count threshold and on
# MEMPAL_VERBOSE), this runs the mine synchronously on every PreCompact
# event — compaction is always worth saving before. The hook itself
# returns ``{}`` so it does not emit a ``decision: block`` to Claude
# Code; the "always run" semantics live in the mine call, not in the
# Stop-hook block protocol.
#
# === INSTALL ===
# Add to .claude/settings.local.json:
#
#   "hooks": {
#     "PreCompact": [{
#       "hooks": [{
#         "type": "command",
#         "command": "/absolute/path/to/mempal_precompact_hook.sh",
#         "timeout": 30
#       }]
#     }]
#   }
#
# For Codex CLI, add to .codex/hooks.json:
#
#   "PreCompact": [{
#     "type": "command",
#     "command": "/absolute/path/to/mempal_precompact_hook.sh",
#     "timeout": 30
#   }]
#
# === HOW IT WORKS ===
#
# Claude Code sends JSON on stdin with:
#   session_id      — unique session identifier
#   transcript_path — path to the JSONL transcript file
#
# The hook runs the transcript mine synchronously (the foreground
# ``mempalace mine`` call below blocks until it returns), then prints
# ``{}`` to stdout so Claude Code proceeds with the compaction. We do
# not emit a ``decision: block`` to the hook protocol — the
# "always save before compaction" guarantee is provided by the
# synchronous mine, not by the Stop-hook block contract.
#
# === MEMPALACE CLI ===
# The hook ALWAYS mines the active conversation transcript synchronously
# before compaction (via `mempalace mine <transcript-dir> --mode convos`).
# MEMPAL_DIR is an *additional*, optional target for project files — it
# does not replace the conversation mine.

STATE_DIR="$HOME/.mempalace/hook_state"
mkdir -p "$STATE_DIR"

# Optional: project directory (code / notes / docs) to also mine before
# compaction. Mined with `--mode projects`. The conversation transcript
# is always mined regardless — this is purely additive.
# Example: MEMPAL_DIR="$HOME/projects/my_app"
MEMPAL_DIR=""

# Resolve the Python interpreter. Same contract as mempal_save_hook.sh:
# MEMPAL_PYTHON (explicit override) → $(command -v python3) → bare python3.
MEMPAL_PYTHON_BIN="${MEMPAL_PYTHON:-}"
if [ -z "$MEMPAL_PYTHON_BIN" ] || [ ! -x "$MEMPAL_PYTHON_BIN" ]; then
    MEMPAL_PYTHON_BIN="$(command -v python3 2>/dev/null || echo python3)"
fi

# Read JSON input from stdin
INPUT=$(cat)

# Parse session_id and transcript_path in one call. Sanitize both, then
# read sanitized values from one-per-line stdout into shell variables
# (avoids ``eval`` on generated code, #1231 review). Uses ``sed -n 'Np'``
# rather than the bash 4-only ``mapfile`` so the script also runs on
# macOS /bin/bash 3.2.57 (Apple GPLv3 freeze, 2006), where ``mapfile``
# silently caused every parsed value to fall back to its default (#1440).
#
# The leading ``__MEMPAL_PARSE_OK__`` sentinel lets the defense-in-depth
# guard below distinguish "Python parsed cleanly" from "Python crashed
# and printed nothing". Same parsing contract as mempal_save_hook.sh.
# Python stderr is captured to last_python_err.log so the guard below can
# distinguish "bad user input" from "broken interpreter / future regression
# in this inline script". Same diagnostic contract as mempal_save_hook.sh.
#
# ``umask 077`` inside the command-substitution subshell makes the
# ``2>$STATE_DIR/last_python_err.log`` redirect create the file at mode
# 0600 atomically, closing the TOCTOU window between creation at
# umask-default and the ``chmod 600`` below. ``printf '%s'`` replaces
# ``echo`` so payloads beginning with ``-n``/``-e``/``-E`` or containing
# backslashes are not mangled by echo flag parsing.
_mempal_parsed=$(
    umask 077
    printf '%s' "$INPUT" | "$MEMPAL_PYTHON_BIN" -c "
import sys, json, re
data = json.load(sys.stdin)
sid = data.get('session_id', '')
tp = data.get('transcript_path', '')
safe = lambda s: re.sub(r'[^a-zA-Z0-9_/.\-~]', '', str(s))
print('__MEMPAL_PARSE_OK__')
print(safe(sid))
print(safe(tp))
" 2>"$STATE_DIR/last_python_err.log"
)
# Drop the empty file on success; chmod 600 on failure to mirror
# last_input.log's privacy contract.
if [ -s "$STATE_DIR/last_python_err.log" ]; then
    chmod 600 "$STATE_DIR/last_python_err.log" 2>/dev/null
else
    rm -f "$STATE_DIR/last_python_err.log"
fi
_MEMPAL_PARSE_MARKER=$(printf '%s\n' "$_mempal_parsed" | sed -n '1p')
SESSION_ID=$(printf '%s\n' "$_mempal_parsed" | sed -n '2p')
TRANSCRIPT_PATH=$(printf '%s\n' "$_mempal_parsed" | sed -n '3p')
SESSION_ID="${SESSION_ID:-unknown}"
TRANSCRIPT_PATH="${TRANSCRIPT_PATH:-}"

# Defense-in-depth: if INPUT was non-empty but Python never reached the
# print() calls (sentinel missing), parsing silently failed. Surface the
# raw payload so the next debugger does not lose a day to hook.log lines
# that say "Session unknown". Bounded to 4 KB and overwritten on each
# failure (not appended) to keep ~/.mempalace/hook_state/ from growing
# unbounded under a repeating misconfiguration. chmod 600 so the dump,
# which mirrors the Claude Code PreCompact payload (includes
# transcript_path revealing the user's home + project layout), is not
# world-readable.
if [ -n "$INPUT" ] && [ "$_MEMPAL_PARSE_MARKER" != "__MEMPAL_PARSE_OK__" ]; then
    echo "[$(date '+%H:%M:%S')] WARN: input parse failed (sentinel missing); see $STATE_DIR/last_input.log and $STATE_DIR/last_python_err.log" >> "$STATE_DIR/hook.log"
    # ``head -c 4096`` is a byte cap, locale-independent; ``${INPUT:0:4096}``
    # would count characters under UTF-8 and slip a multibyte payload past
    # the bound. ``set -o pipefail`` is not enabled in this script so the
    # natural SIGPIPE-on-printf from ``head`` closing stdin is absorbed.
    # ``umask 077`` in the subshell creates last_input.log at mode 0600
    # atomically — the ``chmod 600`` below stays as belt-and-suspenders.
    ( umask 077 && printf '%s' "$INPUT" | head -c 4096 > "$STATE_DIR/last_input.log" )
    chmod 600 "$STATE_DIR/last_input.log" 2>/dev/null
fi

# Expand ~ in path
TRANSCRIPT_PATH="${TRANSCRIPT_PATH/#\~/$HOME}"

# Validate that TRANSCRIPT_PATH looks like a transcript file. Mirrors
# mempalace.hooks_cli._validate_transcript_path so the shell hook
# rejects the same shapes the Python hook rejects (#1231 review).
is_valid_transcript_path() {
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

echo "[$(date '+%H:%M:%S')] PRE-COMPACT triggered for session $SESSION_ID" >> "$STATE_DIR/hook.log"

# Run ingest synchronously so memories land before compaction. Two
# independent targets — both run if both are set:
#   1. TRANSCRIPT_PATH (from Claude Code) → parent dir, --mode convos
#   2. MEMPAL_DIR → --mode projects
if is_valid_transcript_path "$TRANSCRIPT_PATH" && [ -f "$TRANSCRIPT_PATH" ]; then
    mempalace mine "$(dirname "$TRANSCRIPT_PATH")" --mode convos \
        >> "$STATE_DIR/hook.log" 2>&1
elif [ -n "$TRANSCRIPT_PATH" ]; then
    echo "[$(date '+%H:%M:%S')] Skipping invalid transcript path: $TRANSCRIPT_PATH" \
        >> "$STATE_DIR/hook.log"
fi
if [ -n "$MEMPAL_DIR" ] && [ -d "$MEMPAL_DIR" ]; then
    mempalace mine "$MEMPAL_DIR" --mode projects \
        >> "$STATE_DIR/hook.log" 2>&1
fi

# Silent: return empty JSON to not block. "decision": "allow" is invalid —
# only "block" or {} are recognized.
echo '{}'
