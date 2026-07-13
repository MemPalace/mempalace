# Remote / Team Server

Run MemPalace as a **central memory service** that a whole team connects to:
one host stores the palace, does the embedding (optionally on a GPU), and
serves MCP over HTTP. Every teammate's AI reads and writes the same shared
memory instead of a palace on each laptop.

This is built from three pieces that already ship in MemPalace:

- the **HTTP transport** for the MCP server (`mempalace-mcp --transport http`),
- a **networked storage backend** ([Milvus / Zilliz Cloud](https://milvus.io/),
  [Qdrant](https://qdrant.tech/), or [Postgres + pgvector](/guide/configuration)),
- optional **GPU embedding** on the server.

::: warning This is a deliberate step away from single-machine local-first
By default MemPalace keeps everything on your own machine. A central server is
still **your** infrastructure — no telemetry, nothing phones home — but your
verbatim memory now lives on a server you operate and travels over your
network. If you choose a managed backend such as Zilliz Cloud, that backend
also receives the vectors and text by design. Run every self-hosted component
(Milvus, Qdrant, Postgres, the MCP host) on hardware you control, put it on a
private network or VPN, and treat the bearer token and TLS setup below as
mandatory, not optional. Embeddings are still produced locally on the server by
MemPalace.
:::

## Architecture

```
  Teammate A ─┐
  Teammate B ─┤  MCP over HTTP        ┌─ mempalace-mcp --transport http
  Teammate C ─┴──(bearer token, TLS)─▶│   (one host: embedding + GPU)
                                      └─────────────┬───────────────
                                                    │ vectors + verbatim text
                                                    ▼
                                              Milvus / Qdrant / pgvector
                                              (central storage)
```

## 1. Central storage

Pick a networked backend so all clients share one palace. **Milvus** can point
at a self-hosted Milvus server or Zilliz Cloud. Milvus Lite is still local to
one palace directory, so use a server URI for team mode.

Install the optional Milvus driver on the server host:

```bash
pip install mempalace[milvus]
```

Point MemPalace at the shared Milvus endpoint:

```bash
export MEMPALACE_BACKEND=milvus
export MEMPALACE_MILVUS_URI=https://your-cluster.api.region.zillizcloud.com
export MEMPALACE_MILVUS_TOKEN=your-token
```

Prefer Qdrant? It needs no extra Python package — MemPalace talks to its REST
API directly.

Run Qdrant (Docker shown; use a managed/self-hosted instance you control):

```bash
docker run -d --name qdrant -p 6333:6333 \
  -v "$HOME/qdrant_storage:/qdrant/storage" \
  qdrant/qdrant
```

Point MemPalace at it on the server host:

```bash
export MEMPALACE_BACKEND=qdrant
export MEMPALACE_QDRANT_URL=http://localhost:6333
export MEMPALACE_QDRANT_API_KEY=your-qdrant-api-key   # if your Qdrant requires one
```

| Variable | Default | Purpose |
|---|---|---|
| `MEMPALACE_BACKEND` | `chroma` | Set to `milvus`, `qdrant`, or `pgvector` to select the backend |
| `MEMPALACE_MILVUS_URI` | per-palace Milvus Lite | Milvus server / Zilliz Cloud URI |
| `MEMPALACE_MILVUS_TOKEN` | _(none)_ | Token for Milvus server / Zilliz Cloud |
| `MEMPALACE_MILVUS_DB_NAME` | _(none)_ | Optional Milvus database name |
| `MEMPALACE_MILVUS_NAMESPACE` | _(none)_ | Optional Milvus collection namespace prefix |
| `MEMPALACE_MILVUS_CONSISTENCY_LEVEL` | `Strong` | Milvus consistency level |
| `MEMPALACE_QDRANT_URL` | `http://localhost:6333` | Qdrant REST endpoint |
| `MEMPALACE_QDRANT_API_KEY` | _(none)_ | Sent as the `api-key` header when set |
| `MEMPALACE_QDRANT_NAMESPACE` | _(none)_ | Optional collection namespace prefix |
| `MEMPALACE_QDRANT_TIMEOUT` | backend default | REST request timeout (seconds) |

The backend can also be set with `--backend milvus` (or `qdrant` /
`pgvector`) on any `mempalace` / `mempalace-mcp` command, or with
`"backend": "milvus"` in `config.json`.

Prefer Postgres? Install `pip install mempalace[pgvector]`, point
`MEMPALACE_BACKEND=pgvector` at a database with the `vector` extension, and
the rest of this guide applies unchanged.

## 2. GPU embedding (optional)

Embedding is the heaviest step; running it on the server's GPU keeps recall
fast for everyone. Install one acceleration extra and select the device:

```bash
pip install mempalace[gpu]            # NVIDIA CUDA (onnxruntime-gpu)
export MEMPALACE_EMBEDDING_DEVICE=cuda
```

Other targets: `mempalace[dml]` + `MEMPALACE_EMBEDDING_DEVICE=dml` (DirectML,
Windows AMD/Intel/NVIDIA), `mempalace[coreml]` + `=coreml` (Apple Neural
Engine), or `=auto` to pick the best available provider. CPU is the default
and needs no extra.

## 3. Serve MCP over HTTP

One command — `mempalace serve` — runs the server with secure defaults. On a
network-exposed (`0.0.0.0`) bind it **auto-generates a strong bearer token**
(stored `0600` under `~/.mempalace/server/`, printed once), prints a
ready-to-paste client config, and runs in the foreground so Docker/systemd own
the lifecycle.

```bash
mempalace serve --host 0.0.0.0 --port 8765 --backend milvus
```

Output includes the token and the exact client command. Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address (`0.0.0.0` to accept remote clients) |
| `--port` | `8765` | Listen port |
| `--backend` | config/env | Storage backend (e.g. `qdrant`) |
| `--tls-cert` / `--tls-key` | _(none)_ | PEM cert + key to terminate **TLS natively** (server speaks `https`) |
| `--read-only` | off | Expose recall only — the mutating tools are hidden and refused |
| `--token` | auto | Use a specific bearer token instead of the generated one |
| `--allow-insecure` | off | Permit a non-loopback bind with no token (only behind a trusted proxy) |

The token always travels via the environment, never the command line, so it
can't leak through `ps`. Binding to a non-loopback host with no token and no
`--allow-insecure` refuses to start. The server also guards against
DNS-rebinding with a `Host` allowlist and an `Origin` loopback check, and
serializes concurrent writes — so multiple teammates can write to the shared
palace at once over HTTP.

::: tip TLS
Pass `--tls-cert`/`--tls-key` to terminate TLS in the server itself
(`https://…`). Otherwise the server is plaintext and you should front it with a
TLS-terminating reverse proxy (nginx/Caddy/Traefik) — never expose plaintext
`/mcp` beyond a trusted private network.
:::

The underlying server is `mempalace-mcp --transport http` (the same flags exist
there if you'd rather wire the token/TLS yourself); `mempalace serve` is the
turnkey wrapper over it.

## 4. Connect a client

Point each teammate's MCP client at the server's `/mcp` endpoint with the
shared token. For Claude Code:

```bash
claude mcp add --transport http mempalace https://memory.example.com/mcp \
  --header "Authorization: Bearer $MEMPALACE_MCP_HTTP_TOKEN"
```

Other MCP clients use the same two ingredients — the `…/mcp` URL and an
`Authorization: Bearer <token>` header. Verify connectivity from any host:

```bash
curl https://memory.example.com/healthz        # -> ok
```

Once connected, all of MemPalace's [MCP tools](/guide/mcp-integration) operate
against the shared palace — searches and saved memories are visible to the
whole team.

## Operating notes

- **Mining** still happens via the CLI (`mempalace mine …`) on the server host
  against the same backend, so the central palace stays populated.
- **One writer-lease per process**: a single `mempalace-mcp --transport http`
  process safely handles concurrent reads and writes. Don't point two server
  processes at the same backend collection.
- **Health checks**: `GET /healthz` returns `200 ok` without a token, so it
  works as a load-balancer/Kubernetes liveness probe.
- **Backups** are now your storage backend's responsibility (Milvus / Zilliz
  Cloud backups, Qdrant snapshots, or Postgres backups) rather than a single
  laptop's palace directory.

## One-command deployments

The repo ships ready-to-edit deployment files under
[`deploy/`](https://github.com/MemPalace/mempalace/tree/main/deploy):

**Docker Compose (server + Qdrant):**

```bash
cp deploy/server.env.example deploy/.env      # set MEMPALACE_MCP_HTTP_TOKEN
docker compose -f deploy/docker-compose.server.yml --env-file deploy/.env up -d
```

This brings up a Qdrant container and a MemPalace server running
`serve --host 0.0.0.0 --backend qdrant`, with a `/healthz` healthcheck and
persistent volumes. Embeddings stay local to the MemPalace container.

**systemd:**

`deploy/mempalace-server.service` is a hardened unit template
(`NoNewPrivileges`, `ProtectSystem=strict`, dedicated user) that runs
`mempalace serve` with its config from `/etc/mempalace/server.env`. Install
steps are in the file's header comment.

## See also

- [MCP Integration](/guide/mcp-integration) — the tools clients get once connected
- [Configuration](/guide/configuration) — config file, identity, environment variables
- [Local Models](/guide/local-models) — keeping embedding and any LLM assist local
