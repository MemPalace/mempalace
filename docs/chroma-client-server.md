# Chroma HTTP mode: client/server topology for multi-process palaces

Opt-in transport for the ChromaDB backend (#832, #1096): one standalone
`chroma run` server exclusively owns the palace directory, and every
MemPalace process (MCP servers, hooks, mining CLI) is a thin HTTP client.
Enable it with `MEMPALACE_CHROMA_MODE=http`.

## Why

Embedded `chromadb.PersistentClient` is not process-safe: when more than
one process writes the same palace — multiple MCP server instances,
stop-hook mines firing alongside an interactive session, a manual
`mempalace mine` — writers contest the sqlite file lock and can corrupt
the HNSW index (see the incident cluster tracked in #1963). Worse, a
blocked writer waits on the file lock with no timeout, so palace calls
can hang the MCP server indefinitely.

In HTTP mode, one server process owns the files and serializes writes;
concurrency is handled where ChromaDB supports it. Every client call is
health-probed and timeout-bounded, so failures surface as structured
errors instead of hangs.

Single-process installs need none of this: the default remains
`embedded`, with zero configuration and no external service.

## Topology

- **One server**: `chroma run --path <palace-path> --host 127.0.0.1
  --port 8801`, typically managed as a user service (examples below). It
  must be the **only** process that opens the files under the palace
  directory.
- **Thin clients**: every MemPalace process constructs its Chroma client
  through [`mempalace/palace_client.py`](../mempalace/palace_client.py) —
  the single construction point. In HTTP mode nothing outside that module
  may build a Chroma client; that rule is what prevents a second embedded
  writer from re-appearing.
- The client returned by `make_http_client()` is wrapped in a
  `TimeoutProxy` that also wraps every collection obtained from it, so the
  whole call graph is timeout-guarded without call-site changes.

## Config surface

| Variable | Default | Meaning |
| --- | --- | --- |
| `MEMPALACE_CHROMA_MODE` | `embedded` | Set `http` to enable client/server mode. |
| `CHROMA_HOST` | `127.0.0.1` | Server address the clients connect to. |
| `CHROMA_PORT` | `8801` | Server port. |
| `MEMPALACE_OP_TIMEOUT` | `15` (seconds) | Hard per-operation ceiling on every wire call. |

While the server is running, never open the same palace in embedded mode
(e.g. offline repair tooling): two writers on the same sqlite/HNSW files
is the corruption scenario HTTP mode exists to remove. Stop the server
first.

## Error contract

Palace failures in HTTP mode are **structured errors, never hangs**. If a
call blocks forever, that is a bug in this contract, not expected
behavior. The two shapes (both in `mempalace/palace_client.py`):

- `PalaceBackendUnreachableError` — the server did not answer. Raised by
  the construction-time health probe (`GET /api/v2/heartbeat`, 3 s
  timeout, error names `host:port`) **and** by in-flight calls whose
  transport dies mid-request (`httpx.TransportError` / builtin
  `ConnectionError` are translated into this same shape), so consumers see
  one error whether the server died before or during the call.
- `PalaceOperationTimeoutError` — a single operation exceeded the
  `MEMPALACE_OP_TIMEOUT` ceiling. The error names the operation (e.g.
  `collection[memories].query`) and the backend address.

The MCP server catches both and returns a structured tool error with a
recovery hint. Errors are not retried internally — the probe/timeout
already bounded the wait; a retry would just double it.

## Running the server as a user service

### macOS (launchd)

`~/Library/LaunchAgents/com.example.chroma-server.plist` (adjust the
`chroma` binary path — `which chroma` — and the palace path):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.chroma-server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/bin/chroma</string>
        <string>run</string>
        <string>--path</string>
        <string>/path/to/.mempalace/palace</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8801</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/chroma-server.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/chroma-server.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ANONYMIZED_TELEMETRY</key>
        <string>False</string>
    </dict>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.example.chroma-server.plist
launchctl print gui/$UID/com.example.chroma-server | grep state   # → running
# restart:
launchctl bootout gui/$UID/com.example.chroma-server
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.example.chroma-server.plist
```

### Linux (systemd user unit)

`~/.config/systemd/user/chroma-server.service`:

```ini
[Unit]
Description=ChromaDB server for MemPalace

[Service]
ExecStart=/path/to/bin/chroma run --path %h/.mempalace/palace --host 127.0.0.1 --port 8801
Restart=always
RestartSec=2
Environment=ANONYMIZED_TELEMETRY=False

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now chroma-server
systemctl --user status chroma-server
journalctl --user -u chroma-server -f   # logs
```

## Operations

**Heartbeat** (is the server up?):

```bash
curl http://127.0.0.1:8801/api/v2/heartbeat
# → {"nanosecond heartbeat": ...}
```

**Respawn behavior**: with `KeepAlive` (launchd) / `Restart=always`
(systemd), the service manager restarts the server within seconds if it
crashes. Clients need no action: the MCP server health-probes on
construction and returns structured errors until the backend is back,
then reconnects on the next call.

**Backup / restore**: back up with the server stopped (or accept a
crash-consistent copy at your own risk):

```bash
# stop the service (see above), then:
tar -czf mempalace-backup-$(date +%Y%m%d-%H%M).tar.gz -C ~/.mempalace palace
# restart the service
```

Restore is the reverse: stop, untar over the palace directory, start.

**Vacuum** (chromadb 1.5.x): stop all writers first, take a backup, then:

```bash
# stop the service, then:
chroma vacuum --path ~/.mempalace/palace
# restart the service
```

## Embedded-only mechanisms gated off in HTTP mode

Several embedded-era mechanisms are explicitly skipped in HTTP mode. Each
is correct for a process that owns the palace files and wrong for a thin
HTTP client:

1. **Pre-open repair pass** (`ChromaBackend.make_client`) — quarantining
   HNSW segments client-side against files a live server owns is a
   corruption risk; the server does its own segment management.
2. **Inode-keyed client cache** (`ChromaBackend._client`) — the cache is
   keyed on the server address instead; file identity is meaningless when
   this process never opens the files.
3. **mtime-based reconnect** (`mcp_server._get_client`) — the server bumps
   `chroma.sqlite3`'s mtime on every write, so the embedded freshness check
   would force a client rebuild on every call; the client is cached for the
   process lifetime instead.
4. **HNSW capacity probe** (`mcp_server._refresh_vector_disabled_flag`) —
   guards an embedded-only segfault (#1222) that cannot happen in a process
   that never loads HNSW segments; left on it would wrongly route search to
   the BM25 fallback.
5. **Collection `_write_lock` flock** (`ChromaCollection._write_lock`) —
   the `mine_palace_lock` flock guarded embedded ChromaDB's multi-threaded
   HNSW corruption (#974/#965); over HTTP the server serializes writes, and
   the non-blocking flock turned legitimate concurrent writers into hard
   `MineAlreadyRunning` errors.
6. **MCP peer-writer lease** (#1818) — the process-lifetime writer lease
   protects peers from each other's stale in-memory embedded state; in HTTP
   mode it would turn the multi-process topology this transport exists for
   into read-only peers (#1888). Whole mining passes still take
   `mine_palace_lock` for their own application-level atomicity.
