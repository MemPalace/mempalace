#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
PROJECT_DIR="${1:-$(pwd)}"
CONFIG_DIR="$HOME/.config/opencode"
CONFIG_FILE="$CONFIG_DIR/opencode.json"
BACKUP_FILE="$CONFIG_FILE.bak"

# Only enable ANSI output when writing to a real terminal.
if [ -t 1 ]; then
    COLOR_GREEN='\033[0;32m'
    COLOR_YELLOW='\033[1;33m'
    COLOR_RED='\033[0;31m'
    COLOR_RESET='\033[0m'
else
    COLOR_GREEN=''
    COLOR_YELLOW=''
    COLOR_RED=''
    COLOR_RESET=''
fi

usage() {
    cat <<EOF
Usage: ./$SCRIPT_NAME [project-dir]
  project-dir: Directory to initialize mempalace for (default: current directory)
EOF
}

info() {
    printf '%s\n' "$1"
}

success() {
    printf '%b\n' "${COLOR_GREEN}✓${COLOR_RESET} $1"
}

warn() {
    printf '%b\n' "${COLOR_YELLOW}⚠${COLOR_RESET} $1"
}

error() {
    printf '%b\n' "${COLOR_RED}✗${COLOR_RESET} $1" >&2
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

if [ ! -d "$PROJECT_DIR" ]; then
    error "Project directory does not exist: $PROJECT_DIR"
    exit 1
fi

detect_python() {
    local candidate
    for candidate in python3.12 python3.11 python3.10 python3.9; do
        if command -v "$candidate" >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    error "Python 3.9+ not found. Install Python 3.12, 3.11, 3.10, or 3.9 first."
    if command -v brew >/dev/null 2>&1; then
        warn "macOS hint: brew install python@3.12"
    else
        warn "Linux hint: install python3.12 (or 3.11/3.10/3.9) with your distro package manager."
    fi
    exit 1
}

detect_package_manager() {
    if command -v uv >/dev/null 2>&1; then
        printf '%s\n' "uv"
    elif command -v pipx >/dev/null 2>&1; then
        printf '%s\n' "pipx"
    else
        printf '%s\n' "pip"
    fi
}

resolve_uv_python() {
    local tool_dir tool_python
    tool_dir="$(uv tool dir)"
    tool_python="$tool_dir/mempalace/bin/python"
    if [ -x "$tool_python" ]; then
        printf '%s\n' "$tool_python"
        return 0
    fi
    return 1
}

resolve_pipx_python() {
    local pipx_home
    pipx_home="$(pipx environment --value PIPX_HOME 2>/dev/null || true)"
    if [ -n "$pipx_home" ] && [ -x "$pipx_home/venvs/mempalace/bin/python" ]; then
        printf '%s\n' "$pipx_home/venvs/mempalace/bin/python"
        return 0
    fi

    # Fall back to common pipx locations when environment metadata is unavailable.
    if [ -x "$HOME/.local/pipx/venvs/mempalace/bin/python" ]; then
        printf '%s\n' "$HOME/.local/pipx/venvs/mempalace/bin/python"
        return 0
    fi

    if [ -x "$HOME/.local/share/pipx/venvs/mempalace/bin/python" ]; then
        printf '%s\n' "$HOME/.local/share/pipx/venvs/mempalace/bin/python"
        return 0
    fi

    return 1
}

install_with_uv() {
    local detected_python="$1"
    local tool_python

    if tool_python="$(resolve_uv_python 2>/dev/null)"; then
        success "MemPalace already installed with uv; skipping install"
        RESOLVED_PYTHON="$tool_python"
        RUNNER_KIND="uv"
        return 0
    fi

    info "Installing MemPalace with uv using $detected_python"
    uv tool install --python "$detected_python" mempalace
    RESOLVED_PYTHON="$(resolve_uv_python)"
    RUNNER_KIND="uv"
    success "Installed MemPalace with uv"
}

install_with_pipx() {
    local detected_python="$1"
    local tool_python

    if pipx list 2>/dev/null | grep -q 'package mempalace'; then
        tool_python="$(resolve_pipx_python || true)"
        if [ -n "$tool_python" ]; then
            success "MemPalace already installed with pipx; skipping install"
            RESOLVED_PYTHON="$tool_python"
            RUNNER_KIND="pipx"
            return 0
        fi
    fi

    info "Installing MemPalace with pipx using $detected_python"
    pipx install --python "$detected_python" mempalace
    RESOLVED_PYTHON="$(resolve_pipx_python)"
    RUNNER_KIND="pipx"
    success "Installed MemPalace with pipx"
}

install_with_pip() {
    local detected_python="$1"

    if "$detected_python" -m pip show mempalace >/dev/null 2>&1; then
        success "MemPalace already installed for $detected_python; skipping install"
        RESOLVED_PYTHON="$detected_python"
        RUNNER_KIND="pip"
        return 0
    fi

    info "Installing MemPalace with pip using $detected_python --user"
    "$detected_python" -m pip install --user mempalace
    RESOLVED_PYTHON="$detected_python"
    RUNNER_KIND="pip"
    success "Installed MemPalace with pip"
}

run_mempalace() {
    "$RESOLVED_PYTHON" -m mempalace "$@"
}

update_opencode_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        warn "OpenCode config does not exist at $CONFIG_FILE; skipping MCP config update"
        return 0
    fi

    cp "$CONFIG_FILE" "$BACKUP_FILE"
    success "Created backup: $BACKUP_FILE"

    # Merge only the mempalace MCP entry so existing keys stay intact.
    "$DETECTED_PYTHON" - <<'PY' "$CONFIG_FILE" "$RESOLVED_PYTHON"
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
resolved_python = sys.argv[2]

with config_path.open() as handle:
    data = json.load(handle)

if not isinstance(data, dict):
    raise SystemExit("opencode.json must contain a JSON object")

mcp = data.get("mcp")
if mcp is None:
    mcp = {}
    data["mcp"] = mcp
elif not isinstance(mcp, dict):
    raise SystemExit("opencode.json field 'mcp' must be an object")

mcp["mempalace"] = {
    "type": "local",
    "command": [resolved_python, "-m", "mempalace.mcp_server"],
    "environment": {"PYTHONUNBUFFERED": "1"},
    "enabled": True,
    "timeout": 10000,
}

with config_path.open("w") as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")
PY

    success "Updated existing OpenCode MCP config"
}

verify_server() {
    local response
    response="$(printf '%s\n' '{"jsonrpc":"2.0","method":"tools/list","id":1}' | "$RESOLVED_PYTHON" -m mempalace.mcp_server 2>/dev/null || true)"
    if [ -z "$response" ]; then
        error "MemPalace MCP server did not respond to tools/list"
        exit 1
    fi
    success "Verified MemPalace MCP server startup"
}

DETECTED_PYTHON="$(detect_python)"
PACKAGE_MANAGER="$(detect_package_manager)"
RESOLVED_PYTHON=""
RUNNER_KIND=""

info "Using project directory: $PROJECT_DIR"
info "Detected Python: $DETECTED_PYTHON"
info "Preferred installer: $PACKAGE_MANAGER"

case "$PACKAGE_MANAGER" in
    uv)
        install_with_uv "$DETECTED_PYTHON"
        ;;
    pipx)
        install_with_pipx "$DETECTED_PYTHON"
        ;;
    pip)
        install_with_pip "$DETECTED_PYTHON"
        ;;
esac

if [ -f "$PROJECT_DIR/mempalace.yaml" ]; then
    success "mempalace.yaml already exists; skipping init"
else
    info "Initializing MemPalace for $PROJECT_DIR"
    if run_mempalace init --help 2>&1 | grep -q -- '--yes'; then
        run_mempalace init "$PROJECT_DIR" --yes
    else
        printf 'y\n' | run_mempalace init "$PROJECT_DIR"
    fi
    success "Initialized MemPalace project"
fi

update_opencode_config
verify_server

printf '\n'
success "OpenCode + MemPalace setup complete"
info "Next steps:"
info "  1. Start OpenCode and confirm the mempalace MCP is enabled"
info "  2. Run: mempalace mine \"$PROJECT_DIR\""
