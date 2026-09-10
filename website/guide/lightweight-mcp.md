# Lightweight MCP Integration & Palace Query Language (PQL)

The **MemPalace Lightweight MCP Server** reduces the MCP tool surface from **45 separate tools down to 3 high-density tools** (`palace_query`, `palace_exec`, `palace_coordinate`). This saves **>80–90% of schema context tokens** on every AI interaction while retaining 100% of underlying features, guarantees, and security checks.

---

## Quick Setup

### Connection Command
```bash
claude mcp add mempalace-light -- mempalace-light-mcp
codex mcp add mempalace-light -- mempalace-light-mcp
```

Side-by-side with the existing 45-tool server: keep `mempalace` pointing at `mempalace-mcp`, and register the 3-tool server as `mempalace-light`.

### With Custom Palace Path
```bash
claude mcp add mempalace-light -- mempalace-light-mcp --palace /path/to/palace
```

---

## The 3-Tool Triad

| Tool | Purpose | Accepts | Example Operations |
|---|---|---|---|
| `palace_query` | Unified Read & Recall | PQL query string or structured dict | Search, taxonomy, wings, rooms, drawer lookup, KG queries, timelines, graph traversal, tunnels, hallways, agent diary reading, status. |
| `palace_exec` | Unified Mutation & Ingestion | PQL command string or structured dict | Filing drawers, batch checkpoints, KG fact lifecycle (add/invalidate/supersede), tunnels, hallways, mining, sync, agent diary write, reconnect. |
| `palace_coordinate` | Multi-Agent Collaboration | Coordination DSL or structured dict | Task delegation (`task.request`), logstream events, live event waiting, acknowledgments, patches (`patch.ready`), artifacts, mesh estate snapshot. |

---

## Palace Query Language (PQL) Reference

Both string DSL format (for fast natural-language generation) and structured JSON dict format are supported.

### 1. `palace_query`

#### Search & Recall
- `FIND <query> [IN <wing>[/<room>]] [LIMIT <n>] [SINCE <date>] [BEFORE <date>] [MAX_DIST <f>] [STRATEGY <vector|union>] [SOURCE <path>]`
- `SEARCH <query> ...`
- Example: `FIND "jwt auth tokens" IN backend/auth LIMIT 5 SINCE 2026-01-01`
- Example: `FIND "database migrations" IN wing_core`

#### Taxonomy & Structure
- `TAXONOMY [IN <wing>]` — Full wing → room → drawer count tree.
- `WINGS` — List all wings with drawer counts.
- `ROOMS [IN <wing>]` — List rooms in a wing.
- `DRAWER <drawer_id>` — Fetch a single drawer by ID with metadata.
- `DRAWERS [IN <wing>[/<room>]] [LIMIT <n>] [OFFSET <n>] [SINCE <date>] [BEFORE <date>]`
- `CHECK DUP <content> [THRESHOLD <float>]` — Semantic duplicate check.
- `AAAK SPEC` — AAAK dialect specification.

#### Knowledge Graph
- `KG <entity> [AS OF <date>] [DIRECTION <outgoing|incoming|both>]`
- `KG TIMELINE [<entity>]`
- `KG STATS`
- Example: `KG Arthur AS OF 2026-04-01 DIRECTION outgoing`

#### Graph Navigation & Tunnels
- `TRAVERSE <room> [HOPS <n>]` — Graph walk from a room.
- `TUNNELS [BETWEEN <wing_a> AND <wing_b>] [IN <wing>]`
- `FOLLOW <wing>/<room>`
- `HALLWAYS [IN <wing>]`
- `GRAPH STATS`

#### Agent Diary & System
- `DIARY <agent_name> [LAST <n>] [IN <wing>]`
- `STATUS` — Palace overview, version, integrity, and protocol.
- `FILED` — Timestamp and count of last save.
- `SETTINGS` — Hook settings.

---

### 2. `palace_exec`

#### Drawers & Memories
- `ADD [IN <wing>/<room>] <verbatim_content> [SOURCE <file>] [ADDED_BY <agent>]`
- `UPDATE <drawer_id> [CONTENT <new_content>] [WING <wing>] [ROOM <room>]`
- `DELETE DRAWER <drawer_id>`
- `DELETE SOURCE <source_path> [COMMIT]` (defaults to dry run unless COMMIT is passed)
- `MINE <directory> [MODE projects|convos|extract] [WING <wing>] [LIMIT <n>] [DRY_RUN]`
- `SYNC [PROJECT <dir>] [WING <wing>] [APPLY]`
- `CHECKPOINT <json_payload>` — Batch items + diary write in a single call.

#### Knowledge Graph Lifecycle
- `KG ADD <subject> -> <predicate> -> <object> [FROM <date>] [TO <date>] [CLOSET <id>] [DRAWER <id>]`
- `KG INVALIDATE <subject> -> <predicate> -> <object> [ENDED <date>]`
- `KG SUPERSEDE <subject> -> <predicate>: <old_val> => <new_val> [AT <date>]`
- Example: `KG ADD Max -> loves -> chess FROM 2026-01-01`
- Example: `KG SUPERSEDE Max -> grade: 6 => 7 AT 2026-09-01`

#### Graph & Tunnels
- `TUNNEL CREATE <source_wing>/<source_room> -> <target_wing>/<target_room> [LABEL <text>]`
- `TUNNEL DELETE <tunnel_id>`
- `HALLWAY DELETE <hallway_id>`

#### Agent Diary & System
- `DIARY WRITE <agent_name> [TOPIC <topic>] [WING <wing>] <aaak_content>`
- `RECONNECT` — Flush caches and reconnect storage backend.
- `SETTINGS [SILENT_SAVE true|false] [DESKTOP_TOAST true|false]`

---

### 3. `palace_coordinate` (Multi-Agent Flow)

#### Tasks & Events
- `TASK CREATE project:<proj> from:<agent> to:<agent> goal:<text> branch:<br> base:<sha> done:<text>`
- `EVENT APPEND type:<type> stream:<stream> room:<room> from:<agent> [to:<agent>] [topic:<topic>] [correlation:<id>] [status:<status>] [body:<text>]`
- `EVENT LIST [stream:<stream>] [room:<room>] [topic:<topic>] [to:<agent>] [from:<agent>] [correlation:<id>] [since_id:<id>] [limit:<n>] [order:asc|desc]`
- `EVENT WAIT [stream:<stream>] [correlation:<id>] [since_id:<id>] [timeout_ms:<ms>]`
- `EVENT ACK event_id:<id> from:<agent> [status:<status>] [body:<text>]`

#### Artifacts & Patches
- `ARTIFACT PUT kind:<patch|file|log|json|note> created_by:<agent> content:<text>`
- `ARTIFACT GET <artifact_id>`
- `PATCH SUBMIT stream:<stream> from:<agent> [to:<agent>] [topic:<topic>] [correlation:<id>] [branch:<br>] [base:<sha>] diff:<diff_text>`
- `MESH PEERS` — Network replica state and version vectors.
