#!/bin/bash
# MEMPALACE ANTIGRAVITY INSTALLER
#
# Idempotent installer for the Antigravity plugin. Copies
# .antigravity-plugin/* and hooks/antigravity/{lib,*.sh} into the
# install directory (default ~/.gemini/config/plugins/mempalace/),
# renders hooks.json.tmpl into hooks.json with absolute paths, and
# leaves the result in a state Antigravity will discover on next
# launch.
#
# === Usage ===
#
#   bash hooks/antigravity/install.sh                    # install with defaults
#   bash hooks/antigravity/install.sh --dry-run          # show what would happen
#   bash hooks/antigravity/install.sh --uninstall        # remove plugin
#   bash hooks/antigravity/install.sh --install-dir <p>  # custom install dir
#   bash hooks/antigravity/install.sh --log-level debug  # noisier output
#
# === Idempotency ===
#
# Re-running the installer produces a byte-identical install dir.
# Files are only written when their content differs from what is
# already on disk (cmp gate). The user's ~/.gemini/config/plugins/
# directory is never touched outside the mempalace/ subdirectory.
#
# === Uninstall safety ===
#
# Uninstall removes the install dir entirely IFF it is the
# mempalace/ plugin directory. We match by basename of the install
# dir, never by substring search, so a user who has a sibling plugin
# at ~/.gemini/config/plugins/mempalace-foo/ is unaffected.
#
# === set -e ===
#
# Installer can use `set -e` (constraint #2 only forbids it in the
# hook scripts themselves). On any error we exit non-zero so a CI run
# fails loudly.

set -e
set -u

# ── Repo root resolution ─────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
PLUGIN_SRC="$REPO_ROOT/.antigravity-plugin"
HOOKS_SRC="$REPO_ROOT/hooks/antigravity"

# ── Defaults ─────────────────────────────────────────────────────────
INSTALL_DIR_DEFAULT="$HOME/.gemini/config/plugins/mempalace"
INSTALL_DIR=""
DRY_RUN=0
UNINSTALL=0
LOG_LEVEL="info"

# ── Args ─────────────────────────────────────────────────────────────
print_usage() {
    cat <<'USAGE'
Usage: install.sh [--install-dir DIR] [--dry-run] [--uninstall] [--log-level LEVEL]

Options:
  --install-dir DIR   Plugin install directory.
                      Default: ~/.gemini/config/plugins/mempalace
  --dry-run           Show what would happen without writing anything.
  --uninstall         Remove the installed plugin.
  --log-level LEVEL   debug | info | warn | error. Default: info.
  -h, --help          Show this help.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --install-dir)
            INSTALL_DIR="${2:-}"
            shift 2
            ;;
        --install-dir=*)
            INSTALL_DIR="${1#*=}"
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        --log-level)
            LOG_LEVEL="${2:-info}"
            shift 2
            ;;
        --log-level=*)
            LOG_LEVEL="${1#*=}"
            shift
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            print_usage >&2
            exit 2
            ;;
    esac
done

if [ -z "$INSTALL_DIR" ]; then
    INSTALL_DIR="$INSTALL_DIR_DEFAULT"
fi

# ── Absolutize the install dir ───────────────────────────────────────
#
# The cursor PR review caught that a relative --install-dir would get
# baked into hooks.json verbatim, leaving paths like
# `./plugins/.../mempal_save_hook_antigravity.sh` that Antigravity
# can't resolve at runtime. Absolutize before writing anything.
mempal_absolutize() {
    local p="$1"
    case "$p" in
        /*) printf '%s' "$p" ;;
        ~*) printf '%s' "${p/#\~/$HOME}" ;;
        *)
            # Resolve relative to the user's $PWD at invocation time.
            # We never `cd` in the main shell of this installer, so
            # $PWD is already the user's invocation directory — no
            # subshell cd dance needed.
            local base="${PWD}"
            printf '%s/%s' "$base" "$p"
            ;;
    esac
}
INSTALL_DIR="$(mempal_absolutize "$INSTALL_DIR")"
# Squash any `//` or `./` or `name/..` artefacts using Python's
# os.path.normpath; falls back to the raw value if Python is missing
# (which would be very unusual on macOS / Linux).
if command -v python3 >/dev/null 2>&1; then
    INSTALL_DIR="$(python3 -c 'import os,sys; print(os.path.normpath(sys.argv[1]))' "$INSTALL_DIR")"
fi

# ── Logging ──────────────────────────────────────────────────────────
log() {
    local lvl="$1"; shift
    local msg="$*"
    case "$lvl" in
        debug)
            [ "$LOG_LEVEL" = "debug" ] && echo "[install] DEBUG: $msg"
            return 0
            ;;
        info)
            case "$LOG_LEVEL" in
                debug|info) echo "[install] $msg" ;;
            esac
            ;;
        warn)
            case "$LOG_LEVEL" in
                debug|info|warn) echo "[install] WARN: $msg" >&2 ;;
            esac
            ;;
        error)
            echo "[install] ERROR: $msg" >&2
            ;;
    esac
}

# ── Action helpers (dry-run aware) ──────────────────────────────────
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[install] DRY-RUN: $*"
        return 0
    fi
    "$@"
}

# ── Render template ──────────────────────────────────────────────────
#
# Substitutes __PLUGIN_DIR__ in $src into $dst with $INSTALL_DIR.
# Emits the rendered file to a temp path first, then promotes it iff
# the content differs from what's already at $dst. The cmp gate is
# what makes the installer idempotent: a no-op re-run produces no
# disk writes (and the test suite asserts byte-equality).
render_template() {
    local src="$1"
    local dst="$2"
    if [ ! -f "$src" ]; then
        log error "template not found: $src"
        return 1
    fi
    local tmp
    tmp="$(mktemp "${TMPDIR:-/tmp}/mempal_agy_render.XXXXXX")"
    # Python over awk/sed — INSTALL_DIR may legitimately contain
    # characters (spaces, colons) that would require careful escaping
    # in a sed s/// replacement. Python read+replace handles all of
    # them uniformly.
    python3 -c "
import sys
src, dst, install_dir = sys.argv[1:4]
with open(src, 'r') as f:
    body = f.read()
body = body.replace('__PLUGIN_DIR__', install_dir)
with open(dst, 'w') as f:
    f.write(body)
" "$src" "$tmp" "$INSTALL_DIR"
    if [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then
        rm -f "$tmp"
        log debug "unchanged: $dst"
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[install] DRY-RUN: would render $src -> $dst"
        rm -f "$tmp"
        return 0
    fi
    mv "$tmp" "$dst"
    log info "wrote: $dst"
}

# ── copy_file: cmp-gated copy that preserves mode ────────────────────
copy_file() {
    local src="$1"
    local dst="$2"
    if [ ! -f "$src" ]; then
        log error "missing source file: $src"
        return 1
    fi
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        log debug "unchanged: $dst"
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[install] DRY-RUN: would copy $src -> $dst"
        return 0
    fi
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    log info "wrote: $dst"
}

# ── Uninstall path ───────────────────────────────────────────────────
#
# We DO NOT remove the install dir by string-substring match against
# the path. We require the install dir's basename to be exactly
# "mempalace" — that way an unrelated sibling like
# ~/.gemini/config/plugins/mempalace-foo/ is left alone, and a
# malformed --install-dir like ~ or / cannot wipe the user's home.
do_uninstall() {
    local base
    base="$(basename "$INSTALL_DIR")"
    if [ "$base" != "mempalace" ]; then
        log error "refusing to uninstall: install dir basename is '$base', expected 'mempalace'"
        log error "(safety guard: prevents accidental wipe of unrelated directories)"
        return 1
    fi
    if [ ! -d "$INSTALL_DIR" ]; then
        log info "nothing to uninstall: $INSTALL_DIR does not exist"
        return 0
    fi
    # Verify the dir LOOKS like our plugin before removing — a
    # plugin.json file with our marker is the proof.
    if [ ! -f "$INSTALL_DIR/plugin.json" ]; then
        log error "refusing to uninstall: $INSTALL_DIR has no plugin.json"
        return 1
    fi
    if ! grep -q '"name"[[:space:]]*:[[:space:]]*"mempalace"' "$INSTALL_DIR/plugin.json" 2>/dev/null; then
        log error "refusing to uninstall: $INSTALL_DIR/plugin.json is not a mempalace plugin"
        return 1
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[install] DRY-RUN: would rm -rf $INSTALL_DIR"
        return 0
    fi
    rm -rf "$INSTALL_DIR"
    log info "uninstalled: $INSTALL_DIR"
}

if [ "$UNINSTALL" -eq 1 ]; then
    do_uninstall
    exit 0
fi

# ── Pre-install sanity ───────────────────────────────────────────────
if [ ! -d "$PLUGIN_SRC" ]; then
    log error "missing source: $PLUGIN_SRC"
    exit 1
fi
if [ ! -d "$HOOKS_SRC" ]; then
    log error "missing source: $HOOKS_SRC"
    exit 1
fi

# Soft-check that mempalace-mcp is on PATH; warn but do not fail.
if ! command -v mempalace-mcp >/dev/null 2>&1; then
    log warn "mempalace-mcp is not on PATH; the MCP server will fail to start until it is."
    log warn "  fix: 'uv tool install mempalace' or 'pip install mempalace'"
fi

# Soft-check ~/.gemini exists; if missing, Antigravity isn't installed.
if [ ! -d "$HOME/.gemini" ]; then
    log warn "$HOME/.gemini not found — Antigravity is probably not installed yet."
    log warn "  the install will still proceed; Antigravity will pick up the plugin on first launch."
fi

log info "install dir: $INSTALL_DIR"

# ── Install: directories ─────────────────────────────────────────────
run mkdir -p "$INSTALL_DIR" \
    "$INSTALL_DIR/skills/mempalace" \
    "$INSTALL_DIR/skills/mempalace-recall" \
    "$INSTALL_DIR/rules" \
    "$INSTALL_DIR/hooks" \
    "$INSTALL_DIR/hooks/lib"

# ── Install: plugin metadata ─────────────────────────────────────────
copy_file "$PLUGIN_SRC/plugin.json"     "$INSTALL_DIR/plugin.json"
copy_file "$PLUGIN_SRC/mcp_config.json" "$INSTALL_DIR/mcp_config.json"
copy_file "$PLUGIN_SRC/README.md"       "$INSTALL_DIR/README.md"

# ── Install: skills (real files, no symlinks at the discovery path) ──
copy_file "$PLUGIN_SRC/skills/mempalace/SKILL.md" \
    "$INSTALL_DIR/skills/mempalace/SKILL.md"
copy_file "$PLUGIN_SRC/skills/mempalace-recall/SKILL.md" \
    "$INSTALL_DIR/skills/mempalace-recall/SKILL.md"

# ── Install: optional recall rule ────────────────────────────────────
#
# Antigravity discovers markdown rules under the plugin's rules/
# directory. This one is recall-only and intentionally lightweight —
# it complements the mempalace-recall skill. Shipping it as a plugin
# rule (not an always-on global rule) keeps it scoped to recall-
# relevant turns, honouring MemPalace's "memory should feel instant"
# budget.
copy_file "$PLUGIN_SRC/rules/mempalace-recall.md" \
    "$INSTALL_DIR/rules/mempalace-recall.md"

# ── Install: hooks ───────────────────────────────────────────────────
copy_file "$HOOKS_SRC/lib/common.sh"                    "$INSTALL_DIR/hooks/lib/common.sh"
copy_file "$HOOKS_SRC/mempal_save_hook_antigravity.sh"  "$INSTALL_DIR/hooks/mempal_save_hook_antigravity.sh"
copy_file "$HOOKS_SRC/mempal_wake_hook_antigravity.sh"  "$INSTALL_DIR/hooks/mempal_wake_hook_antigravity.sh"

# Ensure hook scripts are executable on the install side. cp preserves
# mode but a fresh git clone from a tarball might not — chmod is
# defensive, idempotent, and bash 3.2 safe.
if [ "$DRY_RUN" -ne 1 ]; then
    chmod 755 "$INSTALL_DIR/hooks/mempal_save_hook_antigravity.sh" 2>/dev/null || true
    chmod 755 "$INSTALL_DIR/hooks/mempal_wake_hook_antigravity.sh" 2>/dev/null || true
fi

# ── Install: render hooks.json from template ─────────────────────────
render_template "$PLUGIN_SRC/hooks.json.tmpl" "$INSTALL_DIR/hooks.json"

# ── Done ─────────────────────────────────────────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
    log info "DRY-RUN complete; no files written."
else
    log info "install complete: $INSTALL_DIR"
    log info "restart Antigravity to load the plugin."
fi
