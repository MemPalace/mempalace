# API Reference

Comprehensive parameter-level documentation for all public Python APIs.

## `mempalace.searcher`

### `search(query, palace_path, wing=None, room=None, n_results=5)`

CLI-oriented search that prints results to stdout.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `str` | — | Search query text |
| `palace_path` | `str` | — | Path to ChromaDB palace directory |
| `wing` | `str` | `None` | Filter by wing name |
| `room` | `str` | `None` | Filter by room name |
| `n_results` | `int` | `5` | Maximum number of results |

**Raises:** `SearchError` if palace not found or query fails.

---

### `search_memories(query, palace_path, wing=None, room=None, n_results=5) → dict`

Programmatic search returning a dict. Used by the MCP server.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `str` | — | Search query text |
| `palace_path` | `str` | — | Path to ChromaDB palace directory |
| `wing` | `str` | `None` | Filter by wing name |
| `room` | `str` | `None` | Filter by room name |
| `n_results` | `int` | `5` | Maximum number of results |

**Returns:**
```python
{
    "query": str,
    "filters": {"wing": str | None, "room": str | None},
    "results": [
        {
            "text": str,           # verbatim drawer content
            "wing": str,           # wing name
            "room": str,           # room name
            "source_file": str,    # original file basename
            "similarity": float,   # 0.0 to 1.0
        }
    ]
}
```

On error: `{"error": str, "hint": str}`

---

## `mempalace.layers`

### `class Layer0(identity_path=None)`

Identity layer (~50 tokens). Reads from `~/.mempalace/identity.txt`.

| Method | Returns | Description |
|--------|---------|-------------|
| `render()` | `str` | Identity text or default message |
| `token_estimate()` | `int` | Approximate token count (`len(text) // 4`) |

---

### `class Layer1(palace_path=None, wing=None)`

Essential story layer (~500–800 tokens). Auto-generated from top drawers.

| Attribute | Type | Description |
|-----------|------|-------------|
| `MAX_DRAWERS` | `int` | Max moments in wake-up (15) |
| `MAX_CHARS` | `int` | Hard cap on L1 text (3200) |

| Method | Returns | Description |
|--------|---------|-------------|
| `generate()` | `str` | Compact L1 text grouped by room |

---

### `class Layer2(palace_path=None)`

On-demand retrieval layer (~200–500 tokens per call).

| Method | Parameters | Returns |
|--------|-----------|---------|
| `retrieve(wing, room, n_results=10)` | Wing/room filters | Formatted drawer text |

---

### `class Layer3(palace_path=None)`

Deep semantic search layer (unlimited depth).

| Method | Parameters | Returns |
|--------|-----------|---------|
| `search(query, wing=None, room=None, n_results=5)` | Query + optional filters | Formatted result text |
| `search_raw(query, wing=None, room=None, n_results=5)` | Query + optional filters | List of result dicts |

---

### `class MemoryStack(palace_path=None, identity_path=None)`

Unified 4-layer interface.

| Method | Parameters | Returns | Description |
|--------|-----------|---------|-------------|
| `wake_up(wing=None)` | Optional wing | `str` | L0 + L1 context (~600–900 tokens) |
| `recall(wing, room, n_results=10)` | Filters | `str` | L2 on-demand retrieval |
| `search(query, wing, room, n_results=5)` | Query + filters | `str` | L3 deep search |
| `status()` | — | `dict` | All layer status info |

---

## `mempalace.knowledge_graph`

### `class KnowledgeGraph(db_path=None)`

Default path: `~/.mempalace/knowledge_graph.sqlite3`

#### Write Methods

| Method | Parameters | Returns | Description |
|--------|-----------|---------|-------------|
| `add_entity(name, entity_type='unknown', properties=None)` | Name, type, props dict | `str` (entity ID) | Add or update entity node |
| `add_triple(subject, predicate, obj, valid_from, valid_to, confidence, source_closet, source_file)` | See below | `str` (triple ID) | Add relationship triple |
| `invalidate(subject, predicate, obj, ended=None)` | Entity names, end date | — | Mark relationship as ended |

**`add_triple` parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `subject` | `str` | — | Source entity name |
| `predicate` | `str` | — | Relationship type |
| `obj` | `str` | — | Target entity name |
| `valid_from` | `str` | `None` | Start date (YYYY-MM-DD) |
| `valid_to` | `str` | `None` | End date |
| `confidence` | `float` | `1.0` | Confidence score 0.0–1.0 |
| `source_closet` | `str` | `None` | Link to verbatim memory |
| `source_file` | `str` | `None` | Original source file |

#### Query Methods

| Method | Parameters | Returns |
|--------|-----------|---------|
| `query_entity(name, as_of=None, direction='outgoing')` | Entity name, date filter, direction | `list[dict]` |
| `query_relationship(predicate, as_of=None)` | Relationship type, date filter | `list[dict]` |
| `timeline(entity_name=None)` | Optional entity filter | `list[dict]` |
| `stats()` | — | `dict` with entities, triples, predicates |
| `seed_from_entity_facts(entity_facts)` | Dict of entity facts | — |

**`query_entity` direction values:** `"outgoing"` (entity→?), `"incoming"` (?→entity), `"both"`

---

## `mempalace.palace_graph`

### `build_graph(col=None, config=None) → (nodes, edges)`

Build the palace graph from ChromaDB metadata.

**Returns:**
- `nodes`: `dict` of `{room: {wings: list, halls: list, count: int, dates: list}}`
- `edges`: `list` of `{room, wing_a, wing_b, hall, count}`

---

### `traverse(start_room, col=None, config=None, max_hops=2) → list`

BFS graph traversal from a room across wings.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `start_room` | `str` | — | Room slug to start from |
| `max_hops` | `int` | `2` | Max connection depth |

**Returns:** `[{room, wings, halls, count, hop, connected_via}]` (max 50)

---

### `find_tunnels(wing_a=None, wing_b=None, col=None, config=None) → list`

Find rooms spanning multiple wings.

**Returns:** `[{room, wings, halls, count, recent}]` (max 50)

---

### `graph_stats(col=None, config=None) → dict`

**Returns:** `{total_rooms, tunnel_rooms, total_edges, rooms_per_wing, top_tunnels}`

---

## `mempalace.dialect`

### `class Dialect(entities=None, skip_names=None)`

| Parameter | Type | Description |
|-----------|------|-------------|
| `entities` | `dict[str, str]` | Full name → 3-letter code mapping |
| `skip_names` | `list[str]` | Names to skip (fictional characters, etc.) |

#### Class Methods

| Method | Parameters | Returns |
|--------|-----------|---------|
| `from_config(config_path)` | JSON file path | `Dialect` instance |

#### Instance Methods

| Method | Parameters | Returns | Description |
|--------|-----------|---------|-------------|
| `compress(text, metadata=None)` | Plain text + optional metadata dict | `str` | AAAK-formatted summary |
| `encode_entity(name)` | Entity name | `str \| None` | 3-letter entity code |
| `encode_emotions(emotions)` | List of emotion strings | `str` | Compact emotion codes |
| `compress_file(path, output=None)` | Zettel JSON path | `str` | Compress zettel file |
| `compress_all(dir, output=None)` | Zettel directory | `str` | Compress all zettels |
| `save_config(path)` | Output path | — | Save entity mappings |
| `compression_stats(original, compressed)` | Both texts | `dict` | Compression ratio stats |

#### Static Methods

| Method | Parameters | Returns |
|--------|-----------|---------|
| `count_tokens(text)` | Any text | `int` |

---

## `mempalace.config`

### `class MempalaceConfig()`

Reads from `~/.mempalace/config.json` and environment variables.

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `palace_path` | `str` | `~/.mempalace/palace` | ChromaDB storage path |
| `collection_name` | `str` | `mempalace_drawers` | ChromaDB collection name |

| Method | Description |
|--------|-------------|
| `init()` | Create config directory and default files |
