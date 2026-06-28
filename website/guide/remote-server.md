# Remote / Team Server

Run MemPalace as a **central memory service** that a whole team connects to:
one host stores the palace, does the embedding (optionally on a GPU), and
serves MCP over HTTP. Every teammate's AI reads and writes the same shared
memory instead of a palace on each laptop.

This is built from three pieces that already ship in MemPalace:

- the **HTTP transport** for the MCP server (`mempalace-mcp --transport http`),
- a **networked storage backend** ([Qdrant](https://qdrant.tech/) or
  [Postgres + pgvector](/guide/configuration)),
- optional **GPU embedding** on the server.

::: warning This is a deliberate step away from single-machine local-first
By default MemPalace keeps everything on your own machine. A central server is
still **your** infrastructure — no third-party API, no telemetry, nothing
phones home — but your verbatim memory now lives on a server you operate and
travels over your network. Run every component (Qdrant, the MCP host) on
hardware you control, put it on a private network or VPN, and treat the
bearer token and TLS setup below as mandatory, not optional. Embeddings are
still produced locally on the server by MemPalace; only your own storage
backend ever receives the vectors and text.
:::

## Architecture

```
  Teammate A ─┐
  Teammate B ─┤  MCP over HTTP        ┌─ mempalace-mcp --transport http
  Teammate C ─┴──(bearer token, TLS)─▶│   (one host: embedding + GPU)
                                      └─────────────┬───────────────
                                                    │ vectors + verbatim text
                                                    ▼
                                              Qdrant / pgvector
                                              (central storage)
```

## 1. Central storage

Pick a networked backend so all clients share one palace. **Qdrant** needs no
extra Python package — MemPalace talks to its REST API directly.

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
| `MEMPALACE_BACKEND` | `chroma` | Set to `qdrant` (or `pgvector`) to select the backend |
| `MEMPALACE_QDRANT_URL` | `http://localhost:6333` | Qdrant REST endpoint |
| `MEMPALACE_QDRANT_API_KEY` | _(none)_ | Sent as the `api-key` header when set |
| `MEMPALACE_QDRANT_NAMESPACE` | _(none)_ | Optional collection namespace prefix |
| `MEMPALACE_QDRANT_TIMEOUT` | backend default | REST request timeout (seconds) |

The backend can also be set with `--backend qdrant` on any `mempalace` /
`mempalace-mcp` command, or with `"backend": "qdrant"` in `config.json`.

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

The MCP server speaks JSON-RPC over `POST /mcp` and exposes an unauthenticated
`GET /healthz` liveness probe for orchestrators. Binding to a **non-loopback**
host requires a bearer token — MemPalace refuses to start otherwise.

```bash
export MEMPALACE_MCP_HTTP_TOKEN="$(openssl rand -hex 32)"

mempalace-mcp --transport http --host 0.0.0.0 --port 8765 --backend qdrant
```

| Flag / variable | Default | Purpose |
|---|---|---|
| `--transport http` | `stdio` | Serve over HTTP instead of stdio |
| `--host` | `127.0.0.1` | Bind address (`0.0.0.0` to accept remote clients) |
| `--port` | `8765` | Listen port |
| `MEMPALACE_MCP_HTTP_TOKEN` | _(none)_ | **Required** for non-loopback binds; clients send `Authorization: Bearer <token>` |

The server protects against DNS-rebinding with a `Host` allowlist and an
`Origin` loopback check, and serializes concurrent writes — so multiple
teammates can write to the shared palace at once over HTTP.

::: danger Put TLS in front of it
The HTTP server is plaintext. For anything beyond a trusted private network,
run it behind a reverse proxy (nginx/Caddy/Traefik) terminating TLS, and keep
the bearer token secret. Only set
`MEMPALACE_MCP_HTTP_ALLOW_INSECURE_NO_TOKEN=1` when a trusted fronting layer
already enforces access control — never on a directly-exposed port.
:::

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
- **Backups** are now your storage backend's responsibility (Qdrant snapshots
  / Postgres backups) rather than a single laptop's palace directory.

## See also

- [MCP Integration](/guide/mcp-integration) — the tools clients get once connected
- [Configuration](/guide/configuration) — config file, identity, environment variables
- [Local Models](/guide/local-models) — keeping embedding and any LLM assist local
