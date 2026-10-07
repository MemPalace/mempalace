# MemPalace Graph

MemPalace Graph builds a local Neo4j visualization index over the existing MemPalace storage at `~/.mempalace`. It reads MemPalace-owned files in read-only mode, extracts lightweight metadata and relationships, and stores only that graph index in Neo4j.

By default, this app does not copy full MemPalace memory content into Neo4j or SQLite.
MemPalace files remain the source of truth.
Neo4j stores only metadata, source pointers, hashes, and graph relationships for visualization.
Full content is resolved from the original source file only when explicitly requested.

## Architecture

The source of truth is `~/.mempalace`. Neo4j is a lightweight graph index for visualization in Neo4j Browser or Bloom. The app SQLite database at `.sync/mempalace_sync.sqlite3` stores sync state only: source file hashes, record locators, memory IDs, offsets, and sync run status.

The app reads:

- `~/.mempalace/palace/config.json`
- `~/.mempalace/palace/knowledge_graph.sqlite3`
- `~/.mempalace/palace/chroma.sqlite3`
- `~/.mempalace/palace/wal/write_log.jsonl`
- fallback on this local layout: `~/.mempalace/wal/write_log.jsonl`

The app ignores Chroma/vector internals by default, including `*.bin`, `*.pickle`, and UUID-like index folders. It never writes to MemPalace-owned files and opens MemPalace SQLite with `mode=ro`.

## Setup

```bash
cp .env.example .env
docker compose up -d
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/inspect_mempalace.py
python scripts/sync_mempalace.py --once --create-schema
python scripts/check_no_duplication.py
python scripts/sync_mempalace.py --watch
```

## Environment

`.env.example` includes all supported settings. The most important values are:

- `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`
- `MEMPALACE_HOME=~/.mempalace`
- `MEMPALACE_PALACE_DIR=~/.mempalace/palace`
- `MEMPALACE_KNOWLEDGE_GRAPH_DB=~/.mempalace/palace/knowledge_graph.sqlite3`
- `MEMPALACE_CHROMA_DB=~/.mempalace/palace/chroma.sqlite3`
- `MEMPALACE_WRITE_LOG=~/.mempalace/palace/wal/write_log.jsonl`
- `MEMPALACE_SYNC_STATE_PATH=.sync/mempalace_sync.sqlite3`
- `MEMPALACE_STORE_CONTENT=false`
- `MEMPALACE_STORE_SNIPPET=true`

Path values expand `~`. CLI flags can override the environment.

## Commands

Inspect the real MemPalace schema without Neo4j:

```bash
python scripts/inspect_mempalace.py
python scripts/inspect_mempalace.py --mempalace-home ~/.mempalace
```

Run a one-shot sync:

```bash
python scripts/sync_mempalace.py --once --create-schema
```

Run a dry run without writing to Neo4j or sync state:

```bash
python scripts/sync_mempalace.py --once --dry-run
```

Run watch mode:

```bash
python scripts/sync_mempalace.py --watch
```

Resolve full content from the original MemPalace source for a synced memory:

```bash
python scripts/resolve_memory.py <memory_id>
python scripts/resolve_memory.py <memory_id> --json
python scripts/resolve_memory.py <memory_id> --open-source
```

Chroma-backed memories use source locators like `chroma:embedding:<drawer_id>`. Neo4j stores only the configured snippet; `resolve_memory.py` reads the full drawer text from `chroma.sqlite3` read-only.

Check the no-duplication rule:

```bash
python scripts/check_no_duplication.py
```

Example Cypher is in `scripts/example_queries.cypher`.

To list readable memory previews in Neo4j Browser:

```cypher
MATCH (m:Memory)-[:BELONGS_TO]->(d:Drawer)-[:IN_CLOSET]->(c:Closet)-[:IN_ROOM]->(r:Room)-[:IN_WING]->(w:Wing)
WHERE m.sync_deleted_at IS NULL
  AND m.source_record_locator STARTS WITH 'chroma:embedding:'
RETURN m.id AS memory_id,
       m.title AS title,
       w.name AS wing,
       r.name AS room,
       c.name AS hall,
       m.snippet AS preview,
       m.source_record_locator AS locator
ORDER BY wing, room, title
LIMIT 100;
```

## Neo4j Browser And Bloom

Open Neo4j Browser at:

http://localhost:7474

Connect using the credentials from `.env`. If Bloom is available, create a Perspective with:

- `Memory`
- `Wing`
- `Room`
- `Closet`
- `Drawer`
- `Person`
- `Topic`
- `Project`
- `Tag`
- `SourceFile`

Suggested Bloom styling:

- Memory caption: `title`
- Memory secondary text: `snippet`
- Memory size: `retrieval_count`
- Memory color: `importance`
- SourceFile caption: `path`
- Topic caption: `name`
- Person caption: `name`

Bloom shows metadata and snippets only by default. It does not show full memory content unless `MEMPALACE_STORE_CONTENT=true` was explicitly enabled before sync.

## Graph Model

Nodes:

- `Memory`, `Wing`, `Room`, `Closet`, `Drawer`
- `Person`, `Topic`, `Project`, `Tag`, `SourceFile`

Relationships:

- `(:Memory)-[:BELONGS_TO]->(:Drawer)`
- `(:Drawer)-[:IN_CLOSET]->(:Closet)`
- `(:Closet)-[:IN_ROOM]->(:Room)`
- `(:Room)-[:IN_WING]->(:Wing)`
- `(:Memory)-[:MENTIONS]->(:Person)`
- `(:Memory)-[:ABOUT]->(:Topic)`
- `(:Memory)-[:RELATED_TO_PROJECT]->(:Project)`
- `(:Memory)-[:TAGGED_AS]->(:Tag)`
- `(:Memory)-[:FROM_FILE]->(:SourceFile)`
- `(:Memory)-[:SIMILAR_TO]->(:Memory)`
- `(:Memory)-[:RELATED_TO]->(:Memory)` for unknown edge types

## Troubleshooting

Neo4j not running: run `docker compose up -d` and confirm ports `7474` and `7687` are available.

Wrong Neo4j password: update `.env`, then recreate the Neo4j volume if the database was initialized with a different password.

`~/.mempalace` not found: pass `--mempalace-home` or set `MEMPALACE_HOME`.

`knowledge_graph.sqlite3` not found: confirm `MEMPALACE_KNOWLEDGE_GRAPH_DB` points to the real file.

SQLite database locked: the app opens MemPalace SQLite read-only. Retry after active MemPalace writes settle.

Unknown MemPalace schema: run `python scripts/inspect_mempalace.py`. The app prints all tables and candidate mappings instead of mutating data.

No records imported: inspect candidate memory tables and check whether the source database has rows.

No Bloom access: use Neo4j Browser with the queries in `scripts/example_queries.cypher`.

No-duplication check failed: inspect the reported properties or sync-state columns, delete the derived Neo4j/sync-state data, keep MemPalace files untouched, and sync again with `MEMPALACE_STORE_CONTENT=false`.

## Tests

```bash
pytest
```

Unit tests use fixtures and do not require Neo4j.
