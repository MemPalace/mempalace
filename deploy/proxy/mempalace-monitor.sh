#!/usr/bin/env bash
#
# mempalace-monitor.sh — Proactive health monitor for MemPalace proxy + server.
#
# Runs periodically (recommended: every 5 minutes via cron). Checks:
#   1. Proxy /health endpoint responds with upstream_ok=true
#   2. MCP tools/list returns tools (functional check, not just port open)
#   3. Upstream server reachable (if different host from proxy)
#
# If checks fail N times consecutively, sends a desktop notification
# (macOS osascript / Linux notify-send) and attempts remediation.
#
# Usage:
#   ./mempalace-monitor.sh
#
# Environment variables:
#   PROXY_URL       Proxy base URL (default: http://127.0.0.1:8766)
#   UPSTREAM_HOST   Upstream host for SSH check (optional, set to skip SSH)
#   UPSTREAM_SSH    SSH target for upstream check (optional, e.g. user@host)
#   SSH_KEY         SSH key path (optional)
#   MIN_TOOLS       Minimum expected tool count (default: 10)
#   MAX_FAILURES    Consecutive failures before alert (default: 3)
#   LOG_FILE        Monitor log path (default: /tmp/mempalace_monitor.log)

set -uo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────

PROXY_URL="${PROXY_URL:-http://127.0.0.1:8766}"
UPSTREAM_HOST="${UPSTREAM_HOST:-}"
UPSTREAM_SSH="${UPSTREAM_SSH:-}"
SSH_KEY="${SSH_KEY:-}"
MIN_TOOLS="${MIN_TOOLS:-10}"
MAX_FAILURES="${MAX_FAILURES:-3}"
LOG_FILE="${LOG_FILE:-/tmp/mempalace_monitor.log}"
FAIL_FILE="/tmp/mempalace_monitor_failures"

# ── Functions ──────────────────────────────────────────────────────────────────

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

notify() {
    if command -v osascript &> /dev/null; then
        osascript -e "display notification \"$1\" with title \"MemPalace Monitor\" sound name \"Basso\"" 2>/dev/null || true
    elif command -v notify-send &> /dev/null; then
        notify-send "MemPalace Monitor" "$1" 2>/dev/null || true
    fi
}

check_proxy_health() {
    local response
    response=$(curl -s --max-time 10 "${PROXY_URL}/health" 2>/dev/null)
    if [[ -z "$response" ]]; then
        log "FAIL: Proxy /health no response"
        return 1
    fi

    local upstream_ok
    upstream_ok=$(echo "$response" | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin)
    print('true' if r.get('upstream_ok') else 'false')
except:
    print('false')
" 2>/dev/null)

    if [[ "$upstream_ok" == "true" ]]; then
        local circuit sessions
        circuit=$(echo "$response" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r.get('circuit_state','?'))" 2>/dev/null)
        sessions=$(echo "$response" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r.get('active_sessions',0))" 2>/dev/null)
        log "OK: Proxy healthy (circuit=$circuit, sessions=$sessions)"
        return 0
    else
        log "FAIL: Proxy upstream_ok=false"
        return 1
    fi
}

check_mcp_tools() {
    local count
    count=$(curl -s --max-time 15 "${PROXY_URL}/mcp" \
        -X POST \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' 2>/dev/null | \
        python3 -c "import sys,json; r=json.load(sys.stdin); print(len(r.get('result',{}).get('tools',[])))" 2>/dev/null)

    if [[ "$count" -ge "$MIN_TOOLS" ]] 2>/dev/null; then
        log "OK: MCP tools/list returned $count tools"
        return 0
    else
        log "FAIL: MCP tools/list returned $count tools (expected >= $MIN_TOOLS)"
        return 1
    fi
}

check_upstream_ssh() {
    if [[ -z "$UPSTREAM_SSH" ]]; then
        return 0  # Skip if not configured
    fi

    local ssh_opts="-o BatchMode=yes -o ConnectTimeout=5"
    if [[ -n "$SSH_KEY" ]]; then
        ssh_opts="$ssh_opts -o IdentitiesOnly=yes -i $SSH_KEY"
    fi

    if ssh $ssh_opts "$UPSTREAM_SSH" "echo ok" > /dev/null 2>&1; then
        log "OK: Upstream SSH reachable"
        return 0
    else
        log "FAIL: Upstream SSH unreachable"
        return 1
    fi
}

remediate() {
    log "Attempting remediation..."
    # Try reloading via launchd (macOS) or systemd (Linux)
    if command -v launchctl &> /dev/null; then
        local plist_label="${LAUNCHD_LABEL:-com.mempalace.proxy}"
        local plist_path="${HOME}/Library/LaunchAgents/${plist_label}.plist"
        launchctl unload "$plist_path" 2>/dev/null
        sleep 2
        launchctl load "$plist_path" 2>/dev/null
        sleep 3
        log "Remediation: relaunched proxy via launchd ($plist_label)"
    elif command -v systemctl &> /dev/null; then
        local service="${SYSTEMD_SERVICE:-mempalace-proxy}"
        sudo systemctl restart "$service" 2>/dev/null
        sleep 3
        log "Remediation: restarted proxy via systemd ($service)"
    else
        log "Remediation: no process manager found (launchctl/systemctl), cannot auto-restart"
    fi
}

# ── Main ───────────────────────────────────────────────────────────────────────

failures=0
[[ -f "$FAIL_FILE" ]] && failures=$(cat "$FAIL_FILE")

all_ok=true

check_proxy_health || all_ok=false
check_mcp_tools || all_ok=false
check_upstream_ssh || all_ok=false

if $all_ok; then
    echo 0 > "$FAIL_FILE"
    # Trim log if it gets too large
    if [[ -f "$LOG_FILE" ]]; then
        lines=$(wc -l < "$LOG_FILE")
        if [[ $lines -gt 500 ]]; then
            tail -200 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
        fi
    fi
else
    failures=$((failures + 1))
    echo "$failures" > "$FAIL_FILE"

    if [[ $failures -ge $MAX_FAILURES ]]; then
        log "CRITICAL: $failures consecutive failures. Sending notification."
        notify "MemPalace has been unhealthy for $failures checks. Check $LOG_FILE"
        remediate
        echo 0 > "$FAIL_FILE"
    fi
fi
