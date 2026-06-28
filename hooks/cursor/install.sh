#!/bin/bash
# MEMPALACE CURSOR HOOK INSTALLER
#
# Optional helper. Copies the three Cursor hook scripts to a
# stable install location and merges entries into a Cursor
# `hooks.json` config file — without clobbering unrelated hooks
# already in that file.
#
# This is NEVER auto-invoked. Editor config is sacred; we do not
# modify a user's hooks.json without explicit consent. The user runs
# this script (or wires the hooks manually) as a documented opt-in.
#
# === USAGE ===
#
#   hooks/cursor/install.sh [options]
#
# Options:
#   --scope user|project   Target scope. Default: user.
#                          - user:    merges into ~/.cursor/hooks.json
#                          - project: merges into <target>/.cursor/hooks.json
#   --target <path>        Project root for --scope project (default: $PWD).
#                          Ignored for --scope user.
#   --install-dir <path>   Where to copy the hook scripts.
#                          Default: ~/.mempalace/hooks/cursor
#   --variant full|minimal Which hook set to wire.
#                          - full:    stop + preCompact + sessionStart
#                          - minimal: stop only
#                          Default: full.
#   --dry-run              Print the would-be JSON to stdout, do not write
#                          and do not copy scripts.
#   --uninstall            Remove MemPalace entries from the target
#                          hooks.json (preserves unrelated hooks).
#                          Does NOT delete the installed scripts.
#   -h, --help             Show this help and exit.
#
# === PORTABILITY ===
#
# Pure bash 3.2 + POSIX tools + python3 (which the hook scripts
# themselves already require). No `jq` dependency.
#
# Python helpers are materialised to temp files rather than piped via
# `$(... <<'PYEOF' ... PYEOF)` to dodge the bash 3.2.57 parser bug
# that trips on parens nested inside a heredoc body that lives inside
# a `$(...)` command substitution.

set -e

usage() {
    sed -n '2,38p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ── Defaults ──────────────────────────────────────────────────────
SCOPE="user"
TARGET=""
INSTALL_DIR="$HOME/.mempalace/hooks/cursor"
VARIANT="full"
DRY_RUN=0
UNINSTALL=0

# ── Parse args ────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --scope)
            shift
            SCOPE="${1:-}"
            ;;
        --target)
            shift
            TARGET="${1:-}"
            ;;
        --install-dir)
            shift
            INSTALL_DIR="${1:-}"
            ;;
        --variant)
            shift
            VARIANT="${1:-}"
            ;;
        --dry-run) DRY_RUN=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'install.sh: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 64
            ;;
    esac
    shift || true
done

case "$SCOPE" in
    user|project) ;;
    *)
        printf 'install.sh: --scope must be "user" or "project" (got "%s")\n' \
            "$SCOPE" >&2
        exit 64
        ;;
esac

case "$VARIANT" in
    full|minimal) ;;
    *)
        printf 'install.sh: --variant must be "full" or "minimal" (got "%s")\n' \
            "$VARIANT" >&2
        exit 64
        ;;
esac

# ── Resolve paths ─────────────────────────────────────────────────
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
SOURCE_DIR="$_script_dir"

# Resolve --install-dir to an absolute path before it gets baked into
# hooks.json. Cursor invokes hook commands from its own working
# directory (typically the project root), so a relative command path
# would silently fail to launch the hook. gh-PR review caught this.
case "$INSTALL_DIR" in
    /*) ;;
    *) INSTALL_DIR="$PWD/$INSTALL_DIR" ;;
esac

# Resolve the Python interpreter the same way the hooks themselves do
# so a user with a non-default Python is consistent across install +
# runtime.
if [ -n "${MEMPAL_PYTHON:-}" ] && [ -x "$MEMPAL_PYTHON" ]; then
    PYTHON_BIN="$MEMPAL_PYTHON"
else
    PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
    printf 'install.sh: python3 not found on PATH; cannot proceed.\n' >&2
    printf 'Set $MEMPAL_PYTHON to an interpreter path or install python3.\n' >&2
    exit 1
fi

# Determine the target hooks.json path.
case "$SCOPE" in
    user)
        if [ -n "$TARGET" ]; then
            printf 'install.sh: --target is only meaningful with --scope project; ignoring.\n' >&2
        fi
        TARGET_DIR="$HOME/.cursor"
        ;;
    project)
        TARGET_DIR="${TARGET:-$PWD}/.cursor"
        ;;
esac
TARGET_FILE="$TARGET_DIR/hooks.json"

# Determine which commands the merge / uninstall logic should
# install or remove. Paths point at the install location, NOT the
# source repo — once the user runs install.sh they can move / delete
# the cloned repo without breaking the wiring.
SAVE_CMD="$INSTALL_DIR/mempal_save_hook_cursor.sh"
PRECOMPACT_CMD="$INSTALL_DIR/mempal_precompact_hook_cursor.sh"
WAKE_CMD="$INSTALL_DIR/mempal_wake_hook_cursor.sh"

# ── Step 1: copy scripts (skipped on --dry-run / --uninstall) ─────
if [ "$UNINSTALL" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$INSTALL_DIR/lib"
    cp "$SOURCE_DIR/lib/common.sh" "$INSTALL_DIR/lib/common.sh"
    cp "$SOURCE_DIR/mempal_save_hook_cursor.sh"       "$INSTALL_DIR/"
    cp "$SOURCE_DIR/mempal_precompact_hook_cursor.sh" "$INSTALL_DIR/"
    cp "$SOURCE_DIR/mempal_wake_hook_cursor.sh"       "$INSTALL_DIR/"
    chmod +x "$INSTALL_DIR/mempal_save_hook_cursor.sh" \
             "$INSTALL_DIR/mempal_precompact_hook_cursor.sh" \
             "$INSTALL_DIR/mempal_wake_hook_cursor.sh"
    printf 'install.sh: copied scripts to %s\n' "$INSTALL_DIR" >&2
fi

# ── Short-circuit: uninstall with no existing file is a no-op ─────
#
# Without this, the merge step would happily write an empty
# {"version": 1, "hooks": {}} to a brand new file that the user
# never asked us to create — surprising behaviour that the test
# suite explicitly guards against.
if [ "$UNINSTALL" -eq 1 ] && [ ! -f "$TARGET_FILE" ]; then
    printf 'install.sh: nothing to uninstall (%s does not exist)\n' \
        "$TARGET_FILE" >&2
    exit 0
fi

# ── Step 2: merge / unmerge hooks.json via python3 ────────────────
#
# Materialise the merge logic to a temp .py file (see bash 3.2
# rationale at the top of this script), then invoke it. The Python
# script is responsible for:
#   * tolerating a missing or empty hooks.json (starts from {})
#   * preserving unrelated hook entries on install
#   * preserving unrelated hook entries on uninstall
#   * recognising MemPalace entries by basename in the `command` field
#   * idempotent install (re-running does not duplicate entries)

# mktemp portability: pass an explicit absolute template so we sidestep
# the BSD vs GNU difference in `-t` semantics (BSD treats it as a
# prefix; GNU treats it as a template). Honour TMPDIR if set, fall
# back to /tmp. gh-PR review caught the previous `-t` form as
# non-portable.
MERGE_PY="$(mktemp "${TMPDIR:-/tmp}/mempal-install-merge.XXXXXX")"
trap 'rm -f "$MERGE_PY"' EXIT

cat > "$MERGE_PY" <<'PYEOF'
"""hooks.json merge helper for hooks/cursor/install.sh.

Argv:
    sys.argv[1]: path to hooks.json (may not exist)
    sys.argv[2]: variant ("full" or "minimal")
    sys.argv[3]: uninstall flag ("1" or "0")
    sys.argv[4]: save_cmd absolute path
    sys.argv[5]: precompact_cmd absolute path
    sys.argv[6]: wake_cmd absolute path

Output: prints the merged JSON to stdout. Exits 2 on a malformed
existing config (refuses to overwrite a broken file).
"""
import json
import os
import sys

target_file = sys.argv[1]
variant = sys.argv[2]
uninstall = sys.argv[3] == "1"
save_cmd = sys.argv[4]
precompact_cmd = sys.argv[5]
wake_cmd = sys.argv[6]

# Recognise our entries by basename. The three filenames below are
# the unique product-of-our-naming convention; any entry whose command
# ends in one of them is treated as a MemPalace entry on
# install (so we replace rather than duplicate it) and on uninstall
# (so we remove it without touching unrelated entries). Matching on
# basename rather than a full-path substring lets users pick any
# --install-dir without breaking uninstall.
MEMPAL_BASENAMES = (
    "mempal_save_hook_cursor.sh",
    "mempal_precompact_hook_cursor.sh",
    "mempal_wake_hook_cursor.sh",
)

if os.path.exists(target_file):
    with open(target_file, "r", encoding="utf-8") as fh:
        try:
            cfg = json.load(fh)
        except Exception as exc:
            sys.stderr.write(
                "install.sh: existing %s is not valid JSON: %s\n"
                "Refusing to overwrite. Fix the file and retry.\n"
                % (target_file, exc)
            )
            sys.exit(2)
else:
    cfg = {}

if not isinstance(cfg, dict):
    sys.stderr.write(
        "install.sh: %s top level must be a JSON object; got %s\n"
        % (target_file, type(cfg).__name__)
    )
    sys.exit(2)

cfg.setdefault("version", 1)
cfg.setdefault("hooks", {})
if not isinstance(cfg["hooks"], dict):
    sys.stderr.write(
        "install.sh: %s 'hooks' must be a JSON object\n" % target_file
    )
    sys.exit(2)


def is_mempal_entry(entry):
    if not isinstance(entry, dict):
        return False
    cmd = entry.get("command", "")
    if not isinstance(cmd, str):
        return False
    # Match on basename so a customised --install-dir (e.g. /opt/...,
    # ~/.local/share/..., or anything with or without a leading dot)
    # still round-trips through uninstall.
    base = os.path.basename(cmd)
    return base in MEMPAL_BASENAMES


def filter_mempal(entries):
    if not isinstance(entries, list):
        return entries
    return [e for e in entries if not is_mempal_entry(e)]


def upsert(event, entry):
    existing = cfg["hooks"].get(event, [])
    if not isinstance(existing, list):
        sys.stderr.write(
            "install.sh: %s hooks[%s] must be a list\n" % (target_file, event)
        )
        sys.exit(2)
    cleaned = [e for e in existing if not is_mempal_entry(e)]
    cleaned.append(entry)
    cfg["hooks"][event] = cleaned


if uninstall:
    for event in list(cfg["hooks"].keys()):
        cfg["hooks"][event] = filter_mempal(cfg["hooks"][event])
        if not cfg["hooks"][event]:
            del cfg["hooks"][event]
else:
    upsert("stop", {"command": save_cmd, "loop_limit": 1})
    if variant == "full":
        upsert("preCompact", {"command": precompact_cmd})
        upsert("sessionStart", {"command": wake_cmd})

# Stable key order for the events MemPalace touches, then preserve
# any unrelated event names in their original order so future Cursor
# events we don't know about yet still round-trip.
known_order = [
    "sessionStart",
    "stop",
    "preCompact",
    "sessionEnd",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "subagentStart",
    "subagentStop",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "beforeReadFile",
    "afterFileEdit",
    "beforeSubmitPrompt",
    "afterAgentResponse",
    "afterAgentThought",
    "beforeTabFileRead",
    "afterTabFileEdit",
    "workspaceOpen",
]

ordered_hooks = {}
for event in known_order:
    if event in cfg["hooks"]:
        ordered_hooks[event] = cfg["hooks"][event]
for event, entries in cfg["hooks"].items():
    if event not in ordered_hooks:
        ordered_hooks[event] = entries
cfg["hooks"] = ordered_hooks

print(json.dumps(cfg, indent=2, sort_keys=False))
PYEOF

NEW_JSON="$("$PYTHON_BIN" "$MERGE_PY" \
    "$TARGET_FILE" "$VARIANT" "$UNINSTALL" \
    "$SAVE_CMD" "$PRECOMPACT_CMD" "$WAKE_CMD")"

# ── Step 3: emit, write, or remove ────────────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'install.sh: --dry-run; would write to %s\n' "$TARGET_FILE" >&2
    printf '%s\n' "$NEW_JSON"
    exit 0
fi

mkdir -p "$TARGET_DIR"

# If --uninstall left an empty hooks object AND no other top-level
# keys beyond version, remove the file entirely so the user's
# `.cursor/` directory does not accumulate orphan configs.
if [ "$UNINSTALL" -eq 1 ]; then
    # Inline the emptiness check via `python -c '...'` rather than a
    # temp .py file. The body is short enough that a tmpfile is pure
    # overhead, and removing the tmpfile eliminates a small leak
    # window if the script is interrupted between mktemp and rm -f.
    # gh-PR review suggested this simplification.
    NON_EMPTY="$(printf '%s' "$NEW_JSON" | "$PYTHON_BIN" -c '
import json, sys
cfg = json.load(sys.stdin)
hooks = cfg.get("hooks", {})
extras = [k for k in cfg.keys() if k not in ("version", "hooks")]
print("1" if (hooks or extras) else "0")
')"
    if [ "$NON_EMPTY" = "0" ] && [ -f "$TARGET_FILE" ]; then
        rm -f "$TARGET_FILE"
        printf 'install.sh: removed empty %s\n' "$TARGET_FILE" >&2
        exit 0
    fi
fi

TMP_FILE="${TARGET_FILE}.tmp.$$"
printf '%s\n' "$NEW_JSON" > "$TMP_FILE"
mv "$TMP_FILE" "$TARGET_FILE"

if [ "$UNINSTALL" -eq 1 ]; then
    printf 'install.sh: removed MemPalace entries from %s\n' "$TARGET_FILE" >&2
else
    printf 'install.sh: wrote %s\n' "$TARGET_FILE" >&2
    printf 'install.sh: restart Cursor (or wait for it to reload hooks.json)\n' >&2
fi
