#!/usr/bin/env bash
#
# mempalace-watchdog.sh — Auto-restart watchdog for MemPalace MCP server.
#
# Checks every WATCHDOG_INTERVAL seconds that the MemPalace process is
# alive AND responsive (answers a tools/list JSON-RPC call). If not,
# restarts it with rate limiting.
#
# Usage:
#   ./mempalace-watchdog.sh
#
# Environment variables:
#   MEMPALACE_HOST       Host to check (default: 127.0.0.1)
#   MEMPALACE_PORT       Port to check (default: 8765)
#   MEMPALACE_TOKEN      Bearer token if auth is enabled (optional)
#   WATCHDOG_INTERVAL    Check interval in seconds (default: 30)
#   MAX_RESTARTS_PER_HOUR  Self-explanatory (default: 5)
#   RESTART_COOLDOWN     Minimum seconds between restarts (default: 60)
#   MEMPALACE_START_CMD  Command to start the server (default: "mempalace serve --host 0.0.0.0 --port 8765")
#   LOG_FILE             Where to write logs (default: /tmp/mempalace_watchdog.log)

set -uo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────

MEMPALACE_HOST="${MEMPALACE_HOST:-127.0.0.1}"
MEMPALACE_PORT="${MEMPALACE_PORT:-8765}"
MEMPALACE_TOKEN="${MEMPALACE_TOKEN:-}"
WATCHDOG_INTERVAL="${WATCHDOG_INTERVAL:-30}"
MAX_RESTARTS_PER_HOUR="${MAX_RESTARTS_PER_HOUR:-5}"
RESTART_COOLDOWN="${RESTART_COOLDOWN:-60}"
MEMPALACE_START_CMD="${MEMPALACE_START_CMD:-mempalace serve --host 0.0.0.0 --port ${MEMPALACE_PORT}}"
LOG_FILE="${LOG_FILE:-/tmp/mempalace_watchdog.log}"

# ── State ──────────────────────────────────────────────────────────────────────

restart_timestamps=()
last_restart=0

# ── Functions ──────────────────────────────────────────────────────────────────

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

is_process_alive() {
    pgrep -f "mempalace.*--port ${MEMPALACE_PORT}" > /dev/null 2>&1
}

is_server_responsive() {
    local url="http://${MEMPALACE_HOST}:${MEMPALACE_PORT}/mcp"
    local auth_header=""
    if [[ -n "$MEMPALACE_TOKEN" ]]; then
        auth_header="-H \"Authorization: Bearer ${MEMPALACE_TOKEN}\""
    fi
    local response
    response=$(eval curl -s --max-time 10 "$url" \
        -X POST \
        -H '"Content-Type: application/json"' \
        $auth_header \
        -d "'{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}'" 2>/dev/null)

    if [[ -z "$response" ]]; then
        return 1
    fi

    # Check for tools array in response
    echo "$response" | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin)
    tools = r.get('result', {}).get('tools', [])
    sys.exit(0 if len(tools) > 0 else 1)
except:
    sys.exit(1)
" 2>/dev/null
}

count_recent_restarts() {
    local now
    now=$(date +%s)
    local cutoff=$((now - 3600))
    local count=0
    for ts in "${restart_timestamps[@]}"; do
        if [[ $ts -gt $cutoff ]]; then
            count=$((count + 1))
        fi
    done
    echo "$count"
}

prune_old_restarts() {
    local now
    now=$(date +%s)
    local cutoff=$((now - 3600))
    local new_arr=()
    for ts in "${restart_timestamps[@]}"; do
        if [[ $ts -gt $cutoff ]]; then
            new_arr+=("$ts")
        fi
    done
    restart_timestamps=("${new_arr[@]}")
}

restart_server() {
    local now
    now=$(date +%s)

    # Rate limiting
    prune_old_restarts
    local recent
    recent=$(count_recent_restarts)
    if [[ $recent -ge $MAX_RESTARTS_PER_HOUR ]]; then
        log "ERROR: Rate limit reached ($recent restarts in last hour). Not restarting."
        return 1
    fi

    if [[ $((now - last_restart)) -lt $RESTART_COOLDOWN ]]; then
        local wait
        wait=$((RESTART_COOLDOWN - (now - last_restart)))
        log "Cooldown: waiting ${wait}s before restart allowed"
        sleep "$wait"
    fi

    log "Restarting MemPalace MCP server (restart #$((recent + 1)) this hour)..."

    # Kill existing process if any
    pkill -f "mempalace.*--port ${MEMPALACE_PORT}" 2>/dev/null
    sleep 2

    # Force kill if still alive
    if is_process_alive; then
        log "Process did not exit on SIGTERM, sending SIGKILL"
        pkill -9 -f "mempalace.*--port ${MEMPALACE_PORT}" 2>/dev/null
        sleep 1
    fi

    # Start new process
    eval "nohup ${MEMPALACE_START_CMD} > /tmp/mempalace_mcp.log 2>&1 &"
    local new_pid=$!
    last_restart=$(date +%s)
    restart_timestamps+=("$last_restart")

    log "Started new mempalace process (PID: $new_pid)"

    # Wait for it to become responsive
    local waited=0
    local max_wait=20
    while [[ $waited -lt $max_wait ]]; do
        sleep 1
        waited=$((waited + 1))
        if is_server_responsive; then
            log "Health check OK (${waited}s)"
            log "Server is back up and responsive"
            return 0
        fi
    done

    log "WARNING: Server started but not responsive after ${max_wait}s"
    return 1
}

# ── Main Loop ──────────────────────────────────────────────────────────────────

log "MemPalace watchdog started (interval=${WATCHDOG_INTERVAL}s, port=${MEMPALACE_PORT})"

while true; do
    if ! is_process_alive; then
        log "ALERT: mempalace-mcp process not found"
        restart_server
    elif ! is_server_responsive; then
        log "ALERT: mempalace-mcp not responding to health check"
        restart_server
    else
        log "Health check OK (0s)"
    fi
    sleep "$WATCHDOG_INTERVAL"
done
