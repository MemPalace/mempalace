# Deployment: shared pgvector palace across Mac + Dell

Runbook for the Orkid fleet deployment (2026-11-11). One palace, two node
classes: the Dell hosts the writable MCP services and postgres; Macs run the
CLI + hooks + per-harness stdio MCP servers. All nodes share one pgvector
table set via `pgvector_shared_namespace` — the mechanism documented in the
remote-server guide.

## Architecture

```
Mac (MacBook Pro)                        Dell (pop-os, 100.82.245.29)
───────────────                          ────────────────────────────
Claude Code / Codex / Cursor             mempalace-mcp.service   :8768/mcp (writable, bearer)
  hooks (Stop/PreCompact/SessionEnd/     mempalace-mcp-ro        :8769 (read-only)
   sessionStart)                           mempalace-mcp-proxy    :8766 (bearer inject)
  stdio MCP: mempalace-mcp ──┐             mempalace-daemon       :34125 (writer lease, local palace)
  CLI: mempalace             │             postgres :5432 (localhost only)
        │                    │                  ▲
        │  pgvector DSN      │   tailnet-only   │
        └──100.82.245.29:5433├──────────────────┘
           mempalace-pgproxy.socket (systemd-socket-proxyd)
Devin CLI sessions connect over HTTP via the :8766 proxy instead of stdio.
```

## Why shared_namespace (and not identical palace_path)

pgvector table names derive from `sha256(palace_path)`. The Dell palace lives
at `/home/orkidlabs/workspace/shared/mempalace/palace`. macOS makes `/home` an
**autofs mountpoint** — directories cannot be created beneath it even with
sudo (`EPERM`), so a Mac cannot reproduce that path string. `pgvector_shared_namespace`
replaces the path-derived prefix with `mempalace_<ns>_<digest>` on every node
that sets the same value, giving all nodes identical tables regardless of local
palace path.

Namespace in use: `orkid` → table prefix `mempalace_orkid_79a228e5d9ca`
(tables: `..._mempalace_drawers`, `..._mempalace_closets`).

## Component map

| Piece | Location | Notes |
|---|---|---|
| Shared namespace config | `~/.mempalace/config.json` (Mac), `/home/orkidlabs/.mempalace/config.json` (Dell) | `"pgvector_shared_namespace": "orkid"` |
| pgvector DSN (Mac) | same config | `postgresql://orkidlabs@100.82.245.29:5433/mempalace` (no password; see `.pgpass`) |
| pgvector DSN (Dell) | same config + `server.env` | `postgresql://orkidlabs:***@localhost:5432/mempalace` |
| Credentials (Mac) | `~/.pgpass` mode 600 | `100.82.245.29:5433:mempalace:orkidlabs:<password>` — never commit |
| Backend marker (Mac) | `~/.mempalace/palace/pgvector_backend.json` | records host/port/dbname/shared_namespace/table_prefix; must match DSN or the backend refuses ("Backend mismatch") |
| Backend marker (Dell) | `<palace>/pgvector_backend.json` | same fields, localhost target |
| Tailnet proxy | Dell `/etc/systemd/system/mempalace-pgproxy.{socket,service}` | binds `100.82.245.29:5433` only; `systemd-socket-proxyd 127.0.0.1:5432`; enabled at boot |
| Dell services | `mempalace-mcp`, `mempalace-mcp-ro`, `mempalace-mcp-proxy`, `mempalace-daemon` (+healthcheck/pg-backup/sync timers) | all on mempalace 3.12.0 |
| Peer-writer override | `mempalace-mcp.service.d/peer-writer.conf` | `MEMPALACE_MCP_ALLOW_PEER_WRITER=1` — required because the daemon holds the writer lease on the shared palace dir while pgvector itself coordinates writers |
| Mac package | editable install of this repo @ `feat/staging-pipeline-preprocess` (3.12.0) via homebrew python3.11 + `uv pip install 'psycopg[binary]>=3.1'` | `embedding_device: "cpu"` in config (Apple Silicon: ONNX models run CPU; see project docs) |

## Harness wiring (Mac, user scope)

| Harness | Hooks config | MCP config |
|---|---|---|
| Claude Code | `~/.claude/settings.json` → Stop/SessionEnd/PreCompact → `hooks/mempal_{save,session_end,precompact}_hook.sh` | `~/.claude.json` `mcpServers.mempalace` → `mempalace-mcp` |
| Codex | `~/.codex/hooks.json` → Stop/PreCompact (same scripts) | `~/.codex/config.toml` `[mcp_servers.mempalace]` |
| Cursor | `~/.cursor/hooks.json` → sessionStart/stop/preCompact → `~/.mempalace/hooks/cursor/*.sh` (installed copies, `loop_limit: 1` on stop) | `~/.cursor/mcp.json` `mcpServers.mempalace` |

Hook wrappers resolve the interpreter via `$MEMPAL_PYTHON` → `mempalace` on
PATH → `python3`/`python` that can `import mempalace`. They exit 0 with `{}`
on any failure so harnesses never break.

## Agent naming convention

`agent_name` is a **stable agent identity**, one diary wing per agent forever —
project/context goes in `topic`/`wing` fields, never in the name. Format:
`<host>-<harness>` (docs examples: `mac-codex`, `windows-codex`). Fleet roster:

`mac-devin`, `mac-claude`, `mac-codex`, `mac-cursor`, `mini-devin`, `dell-*`.

## Known divergences (by design / accepted)

- **Knowledge graph** (`knowledge_graph.sqlite3`) and **logstream**
  (`logstream.sqlite3`) live inside each palace *directory* — they are per-host
  SQLite, NOT shared by pgvector. Mac CLI `kg`/`logstream` commands touch the
  Mac-local files. Shared KG/logstream access is via the Dell-hosted MCP tools
  (`mempalace_kg_*`, `mempalace_event_*`, `mempalace_task_create`).
- Diary/drawer *content* is fully shared (pgvector); only these two local index
  layers diverge.

## Verification

```bash
# Mac CLI sees the shared palace (expect ~249K drawers, same as Dell MCP status)
mempalace status | head -5

# Write path: mine a file, then read it back
mkdir -p /tmp/mempal-e2e && echo "marker" > /tmp/mempal-e2e/T.md
mempalace mine /tmp/mempal-e2e --wing <test-wing>
mempalace search "marker" --wing <test-wing>

# Stdio MCP handshake (expect serverInfo 3.12.0 + 47 tools)
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | mempalace-mcp

# Hook smoke (expect {} exit 0 under SAVE_INTERVAL; real save at 15 exchanges)
echo '{"session_id":"t","transcript_path":"/tmp/fake.jsonl","cwd":"'$HOME'","hook_event_name":"Stop","stop_hook_active":false}' \
  | hooks/mempal_save_hook.sh

# Dell services
ssh dell 'systemctl is-active mempalace-mcp mempalace-mcp-ro mempalace-mcp-proxy mempalace-daemon mempalace-pgproxy.socket'
ssh dell 'ss -tln | grep -E "8768|8769|5433"'
```

## Rollback

1. Mac: remove `pgvector_shared_namespace`, `palace_path`, `collection_name`,
   `pgvector_dsn` (and `backend`) from `~/.mempalace/config.json`; delete
   `~/.mempalace/palace/pgvector_backend.json`; delete `~/.pgpass`.
2. Dell: `sudo systemctl disable --now mempalace-pgproxy.socket mempalace-pgproxy.service`,
   remove the two unit files, `daemon-reload`.
3. If tables were renamed: `ALTER TABLE mempalace_orkid_79a228e5d9ca_mempalace_drawers
   RENAME TO mempalace_ee12ce0ee8152577_mempalace_drawers` (and closets), restore
   `shared_namespace: null` + old `table_prefix` in the Dell marker, remove
   `pgvector_shared_namespace` from Dell config, restart mempalace services.
