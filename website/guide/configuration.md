# Configuration

## Global Config

Located at `~/.mempalace/config.json`:

```json
{
  "palace_path": "/custom/path/to/palace",
  "collection_name": "mempalace_drawers",
  "people_map": {"Kai": "KAI", "Priya": "PRI"},
  "max_backups": 10
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `palace_path` | `~/.mempalace/palace` | Where the default local palace stores your drawers |
| `collection_name` | `mempalace_drawers` | Default backend collection name |
| `people_map` | `{}` | Entity name → AAAK code mappings |
| `max_backups` | `10` | How many timestamped palace backups to keep before the oldest are pruned. Applies to `mempalace migrate` (`<palace>.pre-migrate.*`) and `mempalace repair max-seq-id` (`chroma.sqlite3.max-seq-id-backup-*`), which each write a full copy every run. Set to `0` to keep every backup (e.g. when an external retention policy manages cleanup). |

## Storage backends

ChromaDB is the default and needs no configuration. MemPalace also ships a
pluggable backend contract, exercised across deliberately different substrates
(an embedded store, an exact-cosine local store, a REST store, and a SQL/JSONB
store) so the contract is never accidentally shaped around one vendor. Every
non-default backend is opt-in.

| Backend | Mode | Install | Namespaces | Lexical | Configure with |
| ------- | ---- | ------- | :--------: | :-----: | -------------- |
| `chroma` _(default)_ | Local (embedded) | bundled | – | ✓ | – |
| `sqlite_exact` | Local (exact) | bundled | – | ✓ | – |
| `milvus` | Local (Lite) · Server opt-in | `mempalace[milvus]` | ✓ | ✓ | `MEMPALACE_MILVUS_URI` |
| `qdrant` | Server (REST) | bundled | ✓ | ✓ | `MEMPALACE_QDRANT_URL` |
| `pgvector` | Server (Postgres) | `mempalace[pgvector]` | ✓ | ✓ | `MEMPALACE_PGVECTOR_DSN` |
<!-- New backends add one row here and one `### <Backend>` subsection (with its connection variables) below; keep README's compatibility table in sync. -->

Select a backend with `--backend <name>` on any `mempalace` / `mempalace-mcp`
command, `MEMPALACE_BACKEND=<name>` in the environment, or `"backend": "<name>"`
in `config.json`.

::: warning Verbatim data leaves your machine on opt-in
When a server-mode backend points anywhere other than your own local or trusted
self-hosted service, MemPalace sends and stores verbatim drawer text and
metadata there. That is an explicit, deliberate backend choice — never the
default.
:::

Server-mode backends isolate tenants by namespace and write a local marker file
(`<backend>_backend.json`) in the palace directory, guarding against silently
opening a palace against the wrong server.

### ChromaDB

The default. Local, embedded, no service to run. Drawers are stored at
[`palace_path`](#global-config); there are no connection settings to configure.

### SQLite exact

Local and built-in (no extra to install). Runs exact cosine over every row — no
ANN index — so it is the reference for exact-vector correctness checks and small
palaces. Select with `--backend sqlite_exact`; it has no connection settings.

### Milvus

A Milvus backend using `pymilvus`. Install the optional driver with
`pip install mempalace[milvus]`. When `MEMPALACE_MILVUS_URI` is unset,
MemPalace uses per-palace Milvus Lite at `<palace>/milvus.db`; set a server or
Zilliz Cloud URI to use a shared Milvus deployment.

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `MEMPALACE_MILVUS_URI` | per-palace Milvus Lite | Milvus server / Zilliz Cloud URI |
| `MEMPALACE_MILVUS_TOKEN` | _(none)_ | Token for Milvus server / Zilliz Cloud |
| `MEMPALACE_MILVUS_DB_NAME` | _(none)_ | Optional Milvus database name |
| `MEMPALACE_MILVUS_NAMESPACE` | _(none)_ | Collection namespace prefix (tenant isolation) |
| `MEMPALACE_MILVUS_CONSISTENCY_LEVEL` | `Strong` | Milvus consistency level (`Strong`, `Session`, `Bounded`, `Eventually`) |

### Qdrant

A networked REST backend. No driver to install — the client uses the Python
standard library — so you only need a [Qdrant](https://qdrant.tech/) instance
you control.

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `MEMPALACE_QDRANT_URL` | `http://localhost:6333` | Qdrant REST endpoint |
| `MEMPALACE_QDRANT_API_KEY` | _(none)_ | Sent as the `api-key` header when set |
| `MEMPALACE_QDRANT_NAMESPACE` | _(none)_ | Collection namespace prefix (tenant isolation) |
| `MEMPALACE_QDRANT_TIMEOUT` | `10.0` | REST request timeout, in seconds |

### Postgres + pgvector

A networked SQL/JSONB backend. Install the driver with
`pip install mempalace[pgvector]`; the server must have the `vector` extension
available.

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `MEMPALACE_PGVECTOR_DSN` | `postgresql://localhost:5432/mempalace` | Postgres connection string |
| `MEMPALACE_PGVECTOR_NAMESPACE` | _(none)_ | Schema namespace (tenant isolation) |

For an end-to-end deployment that puts a server-mode backend behind the MCP
server, see [Remote / Team Server](/guide/remote-server).

## Project Config

Generated by `mempalace init` in your project directory:

### `mempalace.yaml`

```yaml
wing: myproject
rooms:
  - backend
  - frontend
  - decisions
palace_path: ~/.mempalace/palace
```

### `entities.json`

```json
{
  "Kai": "KAI",
  "Priya": "PRI"
}
```

Wings are auto-detected during `mempalace init` from:
- Directory names → project wings
- Detected people in file content → person wings
- Explicit `--wing` flag on mine commands

## Identity

Located at `~/.mempalace/identity.txt`. Plain text. Becomes Layer 0 — loaded every session.

```text
I am Atlas, a personal AI assistant for Alice.
Traits: warm, direct, remembers everything.
People: Alice (creator), Bob (Alice's partner).
Project: A journaling app that helps people process emotions.
```

::: tip
Write your identity file in first person from the AI's perspective. This becomes the AI's self-concept on wake-up.
:::

## Palace Path Override

All commands accept `--palace <path>` to override the default location:

```bash
mempalace search "query" --palace /tmp/test-palace
mempalace mine ~/data/ --palace /tmp/test-palace
```

The MCP server also accepts `--palace`:

```bash
python -m mempalace.mcp_server --palace /custom/palace
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `MEMPALACE_PALACE_PATH` | Override palace path (same as `--palace`) |
| `MEMPAL_DIR` | Directory for auto-mining in hooks |
| `MEMPALACE_MAX_BACKUPS` | Override `max_backups` retention count (`0` disables pruning) |
| `MEMPALACE_BACKEND` | Select the storage backend (default `chroma`) — see [Storage backends](#storage-backends) for each backend's connection variables |
