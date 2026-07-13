# MemPalace MCP Proxy — Production Hardening Layer

MemPalace's `--transport http` mode speaks plain JSON over HTTP
(`BaseHTTPRequestHandler`, `Connection: close`, no SSE). MCP clients that
expect the **streamable-http** transport protocol (POST `/mcp` with
`Mcp-Session-Id`, GET `/mcp` with `text/event-stream`, DELETE `/mcp`)
cannot connect directly.

This package provides a production-grade proxy that bridges the gap, plus
the operational tooling to keep it running unattended.

## What's Included

| File | Purpose |
|------|---------|
| `mempalace-mcp-proxy.py` | Streamable-HTTP proxy with connection pooling, circuit breaker, retry |
| `mempalace-watchdog.sh` | Auto-restart watchdog (detects hangs, not just crashes) |
| `mempalace-monitor.sh` | Proactive health monitor with desktop notifications |
| `com.mempalace.proxy.plist` | macOS launchd template (auto-start + KeepAlive) |
| `mempalace-proxy.service` | Linux systemd unit for the proxy |
| `mempalace-watchdog.service` | Linux systemd unit for the watchdog |
| `proxy.env.example` | Environment file template for systemd |

## Why This Exists

The core MemPalace server is excellent at storage and retrieval but has
no operational layer for production deployments:

- **No connection pooling** — each request opens a new connection
- **No retry logic** — a single transient failure kills the request
- **No circuit breaker** — cascading failures with no protection
- **No health endpoint** — no way to distinguish "listening" from "working"
- **No auto-restart** — when the process hangs, it stays hung
- **No metrics** — no observability for monitoring systems

This package adds all of that without modifying the MemPalace core.

## Quick Start

### 1. Install dependencies

```bash
pip install aiohttp httpx
```

### 2. Start the proxy

```bash
# Point at your MemPalace HTTP server
export UPSTREAM_URL="http://127.0.0.1:8765/mcp"

# If your server has MEMPALACE_MCP_HTTP_TOKEN set, match it here:
# export UPSTREAM_TOKEN="your-secret-token"

python mempalace-mcp-proxy.py
```

The proxy now listens on `127.0.0.1:8766` and speaks streamable-HTTP.

### 3. Connect your MCP client

```bash
# Claude Code
claude mcp add --transport http mempalace http://127.0.0.1:8766/mcp

# Or in your MCP config:
# {
#   "mcpServers": {
#     "mempalace": {
#       "url": "http://127.0.0.1:8766/mcp",
#       "transport": "http"
#     }
#   }
# }
```

### 4. (Optional) Set up auto-start + watchdog

**macOS:**
```bash
cp com.mempalace.proxy.plist ~/Library/LaunchAgents/
# Edit the plist to set your UPSTREAM_URL and python path
launchctl load ~/Library/LaunchAgents/com.mempalace.proxy.plist
```

**Linux (systemd):**
```bash
sudo cp mempalace-mcp-proxy.py /usr/local/bin/
sudo cp mempalace-watchdog.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/mempalace-mcp-proxy.py /usr/local/bin/mempalace-watchdog.sh
sudo cp proxy.env.example /etc/mempalace/proxy.env
# Edit /etc/mempalace/proxy.env
sudo cp mempalace-proxy.service /etc/systemd/system/
sudo cp mempalace-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mempalace-proxy mempalace-watchdog
```

### 5. (Optional) Set up proactive monitoring

```bash
# Add to crontab (every 5 minutes)
*/5 * * * * /usr/local/bin/mempalace-monitor.sh
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/mcp` | JSON-RPC forwarded to upstream (with session management) |
| GET | `/mcp` | SSE keep-alive stream (for streaming clients) |
| DELETE | `/mcp` | Terminate a session |
| GET | `/health` | Health check — tests upstream with actual `tools/list` call |
| GET | `/metrics` | Prometheus-style metrics (counters, gauges) |

## Configuration

All configuration is via environment variables — no config files, no
hardcoded paths.

| Variable | Default | Description |
|----------|---------|-------------|
| `UPSTREAM_URL` | `http://127.0.0.1:8765/mcp` | MemPalace HTTP endpoint |
| `UPSTREAM_TOKEN` | (empty) | Bearer token for upstream auth |
| `HOST` | `127.0.0.1` | Proxy bind host |
| `PORT` | `8766` | Proxy bind port |
| `UPSTREAM_TIMEOUT` | `120` | Per-request timeout (seconds) |
| `MAX_RETRIES` | `2` | Max retry attempts for transient failures |
| `SESSION_TTL` | `1800` | Session expiry (seconds) |
| `LOG_LEVEL` | `INFO` | Log level (DEBUG/INFO/WARNING/ERROR) |

## Circuit Breaker

The proxy includes a circuit breaker to prevent cascading failures:

- **3 consecutive failures** → circuit opens (requests fail fast with 503)
- **30 seconds** → circuit transitions to half-open
- **Next request** → probe; success closes the circuit, failure reopens it

This means if your MemPalace server goes down, the proxy doesn't hang
for 120 seconds on every request — it fails immediately, letting your
MCP client handle the error gracefully.

## Watchdog

The watchdog (`mempalace-watchdog.sh`) complements systemd's
`Restart=on-failure` by also catching **hangs** — situations where the
process is alive but not responding to JSON-RPC. This happens when:

- The embedding model gets stuck loading
- The storage backend (ChromaDB/Qdrant) deadlocks
- The HTTP handler thread is blocked

The watchdog sends an actual `tools/list` JSON-RPC call and checks for
a valid response. If it fails, it kills and restarts the server.

Rate limiting: max 5 restarts per hour, 60s cooldown between restarts.

## Metrics

The `/metrics` endpoint exposes Prometheus-style counters:

```
mempalace_proxy_requests_total 42
mempalace_proxy_requests_success 40
mempalace_proxy_requests_failed 2
mempalace_proxy_mcp_errors 1
mempalace_proxy_connect_errors 3
mempalace_proxy_timeout_errors 0
mempalace_proxy_active_sessions 2
mempalace_proxy_uptime_seconds 3600.0
mempalace_proxy_circuit_state{state="closed"} 0
```

Scrape with Prometheus or check manually:

```bash
curl http://127.0.0.1:8766/metrics
```

## Architecture

```
MCP Client (Claude/Devin/etc.)
    │
    │  streamable-HTTP (POST/GET/DELETE /mcp)
    │  Mcp-Session-Id, text/event-stream
    ▼
┌──────────────────────┐
│   MCP Proxy (:8766)  │
│  ┌────────────────┐  │
│  │ Circuit Breaker│  │
│  │ Retry w/ backoff│ │
│  │ Session Mgmt   │  │
│  │ /health        │  │
│  │ /metrics       │  │
│  └───────┬────────┘  │
│          │           │
│  pooled httpx client │
└──────────┼───────────┘
           │
           │  plain JSON HTTP (POST /mcp)
           │  Connection: close
           ▼
┌──────────────────────┐
│  MemPalace (:8765)   │
│  BaseHTTPRequestHandler│
│  ChromaDB / Qdrant   │
└──────────────────────┘
```

## Privacy

The proxy does **not**:

- Send any data to external services
- Log request bodies (only method, status, elapsed time)
- Include any telemetry or analytics
- Store any user content

It is a transparent forwarding layer. All data stays between your MCP
client and your MemPalace server, consistent with MemPalace's
"local-first, zero external API" design principle.

## License

MIT (same as MemPalace)
