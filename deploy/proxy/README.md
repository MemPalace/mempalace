# MemPalace MCP Proxy — Streamable-HTTP Bridge (deploy/ example)

MemPalace's `--transport http` mode speaks plain JSON over HTTP
(`BaseHTTPRequestHandler`, `Connection: close`, no SSE). MCP clients that
expect the **streamable-HTTP** transport protocol (POST `/mcp` with
`Mcp-Session-Id`, GET `/mcp` with `text/event-stream`, DELETE `/mcp`)
cannot connect directly.

This package is a **deploy/ example**: a proxy that bridges the gap, plus
minimal operational tooling. It holds the upstream bearer token and
forwards requests — treat it as infrastructure glue, not part of the
MemPalace core.

## What's Included

| File | Purpose |
|------|---------|
| `mempalace_mcp_proxy.py` | Streamable-HTTP proxy with connection pooling, circuit breaker, retry |
| `mempalace-monitor.sh` | Proactive health monitor with desktop notifications |
| `com.mempalace.proxy.plist` | macOS launchd template (auto-start + KeepAlive) |
| `mempalace-proxy.service` | Linux systemd unit for the proxy |
| `proxy.env.example` | Environment file template for systemd |

## Why This Exists

The core server already ships Host/Origin/token checks and `/healthz` /
`/statusz` endpoints. What it lacks is the streamable-HTTP transport
itself. This example adds that transport plus a small ops layer:

- **Connection pooling** — one pooled upstream client instead of a new
  connection per request
- **Retry with retry-safety classification** — read-only calls may be
  retried; mutating `tools/call` operations get a single attempt
- **Circuit breaker** — upstream 5xx failures open the circuit and fail
  fast instead of hanging
- **`/health` and `/metrics`** — proxy-level health and Prometheus-style
  counters for monitoring systems

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

python mempalace_mcp_proxy.py
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

### 4. (Optional) Set up auto-start

**macOS:**
```bash
cp com.mempalace.proxy.plist ~/Library/LaunchAgents/
# Edit the plist to set your UPSTREAM_URL and python path
launchctl load ~/Library/LaunchAgents/com.mempalace.proxy.plist
```

**Linux (systemd):**
```bash
sudo cp mempalace_mcp_proxy.py /usr/local/bin/
sudo chmod +x /usr/local/bin/mempalace_mcp_proxy.py
sudo cp proxy.env.example /etc/mempalace/proxy.env
# Edit /etc/mempalace/proxy.env
sudo cp mempalace-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mempalace-proxy
```

To restart the upstream MemPalace server on failure, use the server's own
unit (`systemctl restart mempalace-server`) — it owns the port, the
writer lease, and the token configuration.

### 5. (Optional) Set up proactive monitoring

```bash
# Add to crontab (every 5 minutes)
*/5 * * * * /usr/local/bin/mempalace-monitor.sh
```

Set `PROXY_TOKEN` if the proxy has `INBOUND_TOKEN` configured — the
monitor's `/health` and `/mcp` checks carry it as a Bearer token.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/mcp` | JSON-RPC forwarded to upstream (with session management) |
| GET | `/mcp` | SSE keep-alive stream (for streaming clients) |
| DELETE | `/mcp` | Terminate a session |
| GET | `/health` | Health check — tests upstream with actual `tools/list` call |
| GET | `/metrics` | Prometheus-style metrics (counters, gauges) |

All endpoints enforce the inbound policy below — `/health` and `/metrics`
are not public surface (the health payload discloses the upstream URL).

## Inbound Security

The proxy attaches the upstream token to every forwarded request, so the
proxy itself must not be open to arbitrary callers.

| Variable | Default | Description |
|----------|---------|-------------|
| `ALLOWED_HOSTS` | `127.0.0.1[:PORT], localhost[:PORT], HOST` | Host header allowlist. Checked **independently of Origin** — a DNS-rebinding page cannot pair a foreign Host with a matching Origin. |
| `ALLOWED_ORIGINS` | (empty) | Origin allowlist for browser clients. An Origin equal to `http(s)://<Host>` is also accepted once Host passes. |
| `INBOUND_TOKEN` | (empty) | Bearer token required on all endpoints. Compared with `hmac.compare_digest`. |
| `PROXY_ALLOW_INSECURE_NO_TOKEN` | unset | Opt-out that permits a non-loopback bind with no token — only behind a trusted fronting layer. |

**Non-loopback binds require `INBOUND_TOKEN`.** With `HOST=0.0.0.0` and no
token (and no opt-out), the proxy refuses to start — the same rule the
MCP hub applies. The Host allowlist alone only stops browsers; a plain
`curl` can send any Host header it likes.

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

## Retry Safety

Retries apply only to requests proven read-only. A mutating `tools/call`
(add/update/delete/diary/…) gets exactly one attempt — if the upstream
commits and the response is lost, the proxy never replays the mutation.
Unknown or unparseable-as-safe shapes default to single-attempt.

## Circuit Breaker

- **3 consecutive upstream failures** (including JSON-bodied 5xx) →
  circuit opens, requests fail fast with 503
- **30 seconds** → half-open probe; success closes, failure reopens
- Upstream HTTP status is preserved in both JSON and SSE responses

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

Scrape with Prometheus or check manually (subject to the inbound policy):

```bash
curl -H "Host: 127.0.0.1:8766" http://127.0.0.1:8766/metrics
```

## Architecture

```
MCP Client (Claude/Devin/etc.)
    │
    │  streamable-HTTP (POST/GET/DELETE /mcp)
    │  Mcp-Session-Id, text/event-stream
    │  Host/Origin/INBOUND_TOKEN checks
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
           │  Connection: close, Bearer UPSTREAM_TOKEN
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

## Light MCP Compatibility

MemPalace has two MCP surfaces:

1. **Light MCP** (stdio only) — three consolidated tools
   (`palace_query`, `palace_exec`, `palace_coordinate`) for resource-
   constrained clients. This is the existing path:
   `client → light MCP over stdio → full HTTP hub`.

2. **Full MCP** (HTTP or stdio) — the complete tool catalog
   (`mempalace_search`, `mempalace_add_drawer`, `mempalace_kg_query`,
   etc.). This is what the proxy exposes over streamable-HTTP.

**This proxy forwards `tools/list` unchanged**, so clients connecting
through it receive the **full hub catalog**, not the three consolidated
light tools. The light MCP server currently only supports stdio and
cannot be used directly as this proxy's HTTP upstream.

If the intended feature is light MCP over HTTP, that requires
integration with the light dispatcher and an end-to-end test asserting
the three-tool catalog and correct dispatch. That is out of scope for
this PR.

## License

MIT (same as MemPalace)
