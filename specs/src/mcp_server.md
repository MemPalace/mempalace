# Behavior Specification: `mempalace/mcp_server.py`

MemPalace MCP (Model Context Protocol) server. Provides read/write/maintenance access to a
"palace" memory store and a SQLite knowledge graph over a JSON-RPC-over-stdio transport. It is
installed as the `mempalace-mcp` console script and registered via
`claude mcp add mempalace -- mempalace-mcp [--palace /path/to/palace]` (mempalace/mcp_server.py:L1-L21).

## Process startup, stdio protection, and environment

At module import time, before any heavy imports, the real stdout is captured and stdout is
redirected to stderr at both the Python level (`sys.stdout = sys.stderr`) and the file-descriptor
level (`os.dup2(2, 1)`, saving the original fd 1 via `os.dup(1)`). This protects the JSON-RPC
channel from C-level banners printed by transitive dependencies; environments without fd-level
stdio fall back to the Python-level redirect only (mempalace/mcp_server.py:L26-L43). The real
stdout is restored in `main()` before the protocol loop begins (mempalace/mcp_server.py:L3688-L3698, L3908).

Logging is initialized at import: a stderr stream handler is always installed; when the
`MEMPALACE_LOG_FILE` env var is set (non-empty after trimming) a file handler in append mode
(UTF-8) is added. If the log path cannot be opened, the server falls back to stderr-only and emits
a warning rather than failing to start. The root logger is reset with `force=True` so the env var's
contract holds even if logging was already configured (mempalace/mcp_server.py:L102-L165).

Command-line args are parsed permissively (unknown args ignored): `--palace PATH` and
`--backend NAME` (mempalace/mcp_server.py:L183-L198). If `--palace` is given, env var
`MEMPALACE_PALACE_PATH` is set to its absolute path (mempalace/mcp_server.py:L203-L204). If
`--backend` is given, the backend name is lowercased and validated (via `get_backend_class`, which
raises for unknown backends), then stored in `MEMPALACE_BACKEND_EXPLICIT` and `MEMPALACE_BACKEND`
(mempalace/mcp_server.py:L205-L211).

`main()` pops `PYTHONPATH` from the environment so spawned subprocesses inherit a clean env (this
side effect also strips it from the parent); reconfigures stdin/stdout to UTF-8 with
`errors="replace"`; runs the HNSW capacity pre-flight probe; optionally eager-warms the embedder;
starts the idle-exit watchdog; then enters the protocol loop (mempalace/mcp_server.py:L3893-L3930).

## JSON-RPC protocol loop

`main()` reads newline-delimited JSON from stdin one line at a time. Empty lines are skipped; EOF
(empty read) breaks the loop. Each line is parsed as JSON and dispatched to `handle_request`; if
the response is non-`None`, it is written to stdout as a single JSON line (no ASCII escaping) and
flushed. `KeyboardInterrupt` breaks the loop; any other exception is logged and the loop continues
(mempalace/mcp_server.py:L3931-L3947).

`handle_request(request)` (mempalace/mcp_server.py:L3507-L3685):

- Non-dict request → JSON-RPC error `-32600` "Invalid Request" with `id: null`
  (mempalace/mcp_server.py:L3509-L3514).
- Records the current monotonic time as the last-request time (drives the idle watchdog)
  (mempalace/mcp_server.py:L3515).
- `method == "initialize"`: negotiates a protocol version. If the client's `protocolVersion` is in
  the supported set it is echoed back, otherwise the first supported version is returned. Result
  contains `protocolVersion`, `capabilities: {tools: {}}`, and
  `serverInfo: {name: "mempalace", version: <__version__>}`. Supported versions, in order:
  `"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"` (mempalace/mcp_server.py:L3484-L3489, L3520-L3535).
- `method == "ping"`: returns `{result: {}}` (mempalace/mcp_server.py:L3536-L3537).
- `method` starting with `"notifications/"`: returns `None` (no response sent)
  (mempalace/mcp_server.py:L3538-L3540).
- `method == "tools/list"`: returns `result.tools` as a list of
  `{name, description, inputSchema}` for every entry in `TOOLS` (mempalace/mcp_server.py:L3541-L3551).
- `method == "tools/call"`: see dispatch rules below.
- Otherwise: if request has no `id` (a notification) return `None`; else JSON-RPC error `-32601`
  "Unknown method: <method>" (mempalace/mcp_server.py:L3678-L3685).

### `tools/call` dispatch (mempalace/mcp_server.py:L3552-L3676)

- Missing/invalid `params` or missing `name` → error `-32602` "Invalid params: 'name' is required
  for tools/call" (mempalace/mcp_server.py:L3553-L3561).
- Unknown tool name → error `-32601` "Unknown tool: <name>" (mempalace/mcp_server.py:L3564-L3569).
- Argument whitelisting: unless the handler accepts `**kwargs`, any argument key not declared in the
  tool's schema `properties` (excluding the internal `wait_for_previous`) causes error `-32602`
  "Unknown parameter(s) '...' for tool <name>". Accepted args are then filtered to declared
  properties only — this blocks callers spoofing internal params such as `added_by`/`source_file`
  (mempalace/mcp_server.py:L3570-L3606).
- Type coercion: for each arg, if the schema declares `"integer"` and the value is not an int it is
  coerced via int(); if `"number"` and not int/float, coerced via float(). A failed coercion →
  error `-32602` "Invalid value for parameter '<key>'" (mempalace/mcp_server.py:L3607-L3623).
- The internal `wait_for_previous` key is dropped before dispatch (mempalace/mcp_server.py:L3624).
- For `mempalace_diary_write`, a `content` argument is accepted as an alias for `entry`; the alias
  fills `entry` only when `entry` was absent or null ("entry wins") (mempalace/mcp_server.py:L3625-L3635).
- The handler is invoked with the filtered kwargs. On success the return value (a dict) is
  serialized with `json.dumps(result, indent=2, ensure_ascii=False)` and wrapped as
  `result.content = [{type: "text", text: <json string>}]` (mempalace/mcp_server.py:L3636-L3646).
- A `TypeError` whose message matches Python's "missing required arguments" form AND whose function
  qualname matches the handler's qualname → error `-32602` "Missing required parameter(s) '...' for
  tool <name>". Other `TypeError`s and all other exceptions → error `-32000` "Internal tool error"
  (mempalace/mcp_server.py:L3647-L3676).

`_internal_tool_error` builds the `-32000` error; when an exception is supplied it attaches
`error.data = {error_class, message}` (mempalace/mcp_server.py:L3492-L3504).

## Idle auto-exit watchdog

If `MEMPALACE_MCP_IDLE_HOURS` is set, its float value (in hours, clamped to >= 0) is the idle
timeout in seconds; if unset, the default is 8 hours; an unparseable value yields 0. A value of 0
disables the watchdog (mempalace/mcp_server.py:L229-L238, L3870-L3874). When enabled, a daemon
thread wakes every `min(60s, timeout/4)` and, once the time since the last handled request reaches
the timeout, logs and terminates the process immediately via `os._exit(0)`
(mempalace/mcp_server.py:L3862-L3890).

## Eager warmup

Controlled by `MEMPALACE_EAGER_WARMUP`. Truthy values `1/true/yes/on` (case-insensitive) enable it;
falsy `0/false/no/off`/empty/whitespace disable it silently; any other value logs a warning and
stays off (mempalace/mcp_server.py:L3701-L3702, L3787-L3796). When enabled and a backend DB exists
on disk, the embedder + HNSW segment are pre-loaded by opening the collection and running a probe
query with sentinel text `"__mempalace_warmup_probe__"` and `n_results=1`. When no palace exists, it
logs "nothing to warm" and makes no on-disk side effect (does NOT scaffold a palace). All failure
modes are fail-soft (logged, return) (mempalace/mcp_server.py:L3709, L3727-L3859).

## Backend / palace / cache management

The configured palace path comes from `MempalaceConfig()` (mempalace/mcp_server.py:L213). The active
backend name is resolved from the explicit env override and the palace path; default is `"chroma"`
(mempalace/mcp_server.py:L804-L818). Chroma client and collection handles are cached at module level.

`_get_client()` (Chroma only) reconnects when `chroma.sqlite3`'s inode changes (detects full
rebuild/repair/nuke) or its mtime changes by more than 0.01s (detects in-place external writes);
inode 0 (FAT/exFAT) disables the inode check as a safe fallback. If the DB file disappears while a
collection is cached, caches are invalidated. On any rebuild it re-runs the HNSW capacity probe
before opening chromadb (mempalace/mcp_server.py:L476-L543).

`_get_collection(create=False)` returns the backend collection with caching and one automatic
retry. Backend-mismatch / unknown-backend errors return `None` and stash a structured
`_collection_open_error` with `{error, details, hint}`; generic open failures clear caches and retry
once before returning `None`. For Chroma, a missing `chroma.sqlite3` when `create=False` triggers a
cache reset and a "Chroma database missing" error. Fresh Chroma collections are created with
metadata `{"hnsw:space": "cosine", "hnsw:num_threads": 1, ...bloat guard}`; HNSW thread pinning is
re-applied to existing collections each open (mempalace/mcp_server.py:L546-L783).

`_no_palace()` returns `{error: "No palace found", hint: "Run: mempalace init <dir> && mempalace
mine <dir>"}` (mempalace/mcp_server.py:L786-L790). `_collection_error_or_no_palace()` returns the
stashed open error (with `backend` attached) or the no-palace error
(mempalace/mcp_server.py:L793-L801).

### Vector-disabled fallback (HNSW capacity divergence)

`_refresh_vector_disabled_flag()` runs the HNSW capacity probe (pure SQLite/metadata read, never
touches HNSW binaries, never raises). If sqlite vs HNSW counts diverge enough to risk a segfault on
segment load, `_vector_disabled` is set and vector-shaped tools route to a BM25-only SQLite fallback;
otherwise it is cleared. The probe runs at startup, on every client rebuild, and inside
search/check_duplicate before any chromadb access (mempalace/mcp_server.py:L409-L461, L3923).

### Transient index errors and cache reset

`_is_transient_index_error(result)` detects post-bulk-write HNSW flush errors (error strings
containing "error finding id", "internal error", "stale-index", or "stale index")
(mempalace/mcp_server.py:L338-L354). `_force_chroma_cache_reset()` drops the MCP-local client cache,
the shared backend per-palace cache, and Chroma's shared system cache (mempalace/mcp_server.py:L357-L406).

### Knowledge-graph cache

`KnowledgeGraph` instances are cached per canonicalized SQLite path
(`realpath` + `normcase` so symlink/case aliases collapse to one handle); creation is
double-checked under a lock (mempalace/mcp_server.py:L247-L284). The KG path is
`<palace>/knowledge_graph.sqlite3` when `--palace` was given, else the module default
(mempalace/mcp_server.py:L241-L244). `_call_kg(op)` runs `op(kg)` with one retry: on
`sqlite3.ProgrammingError` (closed DB, e.g. concurrent reconnect) it evicts the stale entry and
retries once, giving up after one retry (mempalace/mcp_server.py:L287-L326).

## Write-ahead log

Every write tool logs a JSONL entry via `_wal_log(op, payload)` before execution, providing an audit
trail for review/rollback. The implementation lives in a side-effect-free module so other code can
audit without triggering this module's stdio redirection (mempalace/mcp_server.py:L464-L473). WAL
records are emitted for: `add_drawer`, `delete_drawer`, `update_drawer`, `kg_add`, `kg_invalidate`,
`diary_write`, and (via `sync_palace`) sync (mempalace/mcp_server.py:L1851-L1861, L1970-L1978,
L2343-L2354, L2453-L2465, L2502-L2510, L2573-L2581, L2214-L2220).

## Drawer chunking and storage contract

Content larger than the configured `chunk_size` is split into bounded per-chunk drawers stored in a
single batched upsert (all-or-nothing). Each chunk's metadata carries `parent_drawer_id` (the
logical handle) and `chunk_index`; physical chunk ids have the form `<drawer_id>_chunk_<NNNNNN>`
(six-digit zero-padded index). The returned `drawer_id` on the chunked path is the LOGICAL group
handle — no physical row exists under that id, so `get`/`delete` by the logical id report
"not found"; callers iterate `chunk_ids` or query by `parent_drawer_id`
(mempalace/mcp_server.py:L1787-L1816, L1819-L1833, L1923-L1952). Chunks are reassembled in order by
`(chunk_index, chunk_id)` and concatenated with no separator (mempalace/mcp_server.py:L1623-L1668,
L1727-L1784). Metadata field `source_file` is reduced to its basename in responses
(mempalace/mcp_server.py:L1590-L1594).

## Read tools

### `mempalace_status` → `tool_status`
No inputs. Returns `{total_drawers, wings, rooms, protocol, aaak_dialect, backend}` where `wings` and
`rooms` are name→count maps. `protocol` is the fixed wake-up protocol text and `aaak_dialect` is the
fixed AAAK spec text (mempalace/mcp_server.py:L1136-L1199, L1206-L1232). If vector search is
disabled, status is read purely from SQLite, including `vector_disabled: true`,
`vector_disabled_reason`, and optional `hnsw_capacity` counts (mempalace/mcp_server.py:L904-L974,
L1144-L1145). Otherwise a SQLite fast-path tally is used when available; falling back to a paginated
metadata scan over the collection. Metadata-fetch failures add `error` and `partial: true` to the
otherwise-complete result. Drawers missing wing/room are counted under `"unknown"`
(mempalace/mcp_server.py:L1147-L1199, L977-L1015).

### `mempalace_list_wings` → `tool_list_wings`
No inputs. Returns `{wings: {name: count}}`, SQLite fast-path or full metadata scan; partial-error
semantics as above (mempalace/mcp_server.py:L1235-L1258).

### `mempalace_list_rooms` → `tool_list_rooms(wing=None)`
Optional `wing` filter (sanitized; empty/whitespace treated as absent). Returns
`{wing: <wing or "all">, rooms: {name: count}}` (mempalace/mcp_server.py:L1261-L1292). Invalid wing
name → `{error: <msg>}` (mempalace/mcp_server.py:L1263-L1265).

### `mempalace_get_taxonomy` → `tool_get_taxonomy`
No inputs. Returns `{taxonomy: {wing: {room: count}}}` (mempalace/mcp_server.py:L1295-L1318).

### `mempalace_get_aaak_spec` → `tool_get_aaak_spec`
No inputs. Returns `{aaak_spec: <AAAK spec text>}` (mempalace/mcp_server.py:L1448-L1450).

### `mempalace_search` → `tool_search`
Inputs: `query` (required, max 250 chars enforced in schema), `limit` (default 5), `wing`, `room`,
`max_distance` (default 1.5), `min_similarity` (deprecated alias), `context`. `limit` is clamped to
`[1, 100]` (`_MAX_RESULTS`) (mempalace/mcp_server.py:L1321-L1330, L874). Invalid wing/room →
`{error}`. `min_similarity`, if given, is converted to a distance threshold as `1.0 -
min_similarity` (mempalace/mcp_server.py:L1336-L1339). The query is run through a prompt-contamination
sanitizer before embedding; when sanitization occurred the result carries `query_sanitized: true`
and a `sanitizer` sub-object `{method, original_length, clean_length, clean_query}`
(mempalace/mcp_server.py:L1340-L1341, L1379-L1387). Search delegates to `search_memories` with
`vector_disabled` passed through; on a transient index error caches are reset, the call sleeps ~2s
and retries once, marking `index_recovered: true` if the retry succeeds
(mempalace/mcp_server.py:L1346-L1375). When vector is disabled, `vector_disabled`/`vector_disabled_reason`
are added (mempalace/mcp_server.py:L1376-L1378). `context`, if provided, only sets
`context_received: true` (it is not used for embedding) (mempalace/mcp_server.py:L1388-L1390).

### `mempalace_check_duplicate` → `tool_check_duplicate(content, threshold=0.9)`
Returns `{is_duplicate, matches}` where each match is
`{id, wing, room, similarity, content}` (content truncated to 200 chars + "..."). Only matches with
similarity >= `threshold` are returned (mempalace/mcp_server.py:L1393-L1445). When vector search is
disabled it returns `{is_duplicate: false, matches: [], vector_disabled: true, vector_disabled_reason,
hint}` rather than a false negative (mempalace/mcp_server.py:L1394-L1408). Errors →
`{error: "Duplicate check failed"}` (mempalace/mcp_server.py:L1443-L1445).

### `mempalace_get_drawer` → `tool_get_drawer(drawer_id)`
Returns the logical drawer payload `{drawer_id, content, wing, room, metadata}`; chunked drawers also
include `chunks` and `chunk_ids`. Missing → `{error: "Drawer not found: <id>"}`
(mempalace/mcp_server.py:L2240-L2252, L1671-L1688).

### `mempalace_list_drawers` → `tool_list_drawers(wing=None, room=None, limit=20, offset=0)`
`limit` clamped to `[1, 100]`, `offset` clamped to `>= 0`. Filters combine with `$and` when both
present. Returns `{drawers, total, count, offset, limit}`; each drawer is
`{drawer_id, wing, room, content_preview, metadata[, chunks, chunk_ids]}` with `content_preview`
truncated to 200 chars + "...". Drawers are collapsed by `parent_drawer_id` and sorted by
`drawer_id` (mempalace/mcp_server.py:L2255-L2296, L1727-L1784).

## Write tools

### `mempalace_add_drawer` → `tool_add_drawer(wing, room, content, source_file=None, added_by="mcp")`
Sanitizes wing/room (names), content; strips lone surrogates from `source_file`/`added_by`. Invalid
→ `{success: false, error}` (mempalace/mcp_server.py:L1819-L1843). Opens the collection with
`create=True`. The drawer id is derived from content+wing+room (content hash, idempotent). Metadata
written: `{wing, room, source_file, added_by, filed_at (ISO now), id_recipe}` plus `chunk_index`
(mempalace/mcp_server.py:L1845-L1871). Idempotency: probes whether the drawer id (and, for oversized
content, the last chunk id and the legacy single-row id) already exist; if so returns
`{success: true, reason: "already_exists", drawer_id}` without writing (mempalace/mcp_server.py:L1873-L1893).
Single-doc path returns `{success: true, drawer_id, wing, room, chunks: 1}`; chunked path returns
additionally `chunk_ids` and `chunks: N` (mempalace/mcp_server.py:L1895-L1952). After upsert, the
new id's readability is verified; if not readable a stale-index error is raised. Failures →
`{success: false, error}` (mempalace/mcp_server.py:L1902-L1907, L1937-L1942, L1953-L1954).

### `mempalace_delete_drawer` → `tool_delete_drawer(drawer_id)`
Deletes all physical rows of a logical drawer. Missing → `{success: false, error: "Drawer not
found: <id>"}`. Success → `{success: true, drawer_id, deleted_ids, chunks_deleted}`. Irreversible
(mempalace/mcp_server.py:L1957-L1992).

### `mempalace_update_drawer` → `tool_update_drawer(drawer_id, content=None, wing=None, room=None)`
With all three omitted returns `{success: true, drawer_id, noop: true}`. Missing drawer →
`{success: false, error: "Drawer not found"}`. Content is sanitized; wing/room are sanitized and
updated only when they differ case-insensitively from the existing value. If the new content exceeds
`chunk_size` or the drawer was already chunked, it is re-chunked (upsert new chunks, delete stale
ids) and returns `chunks`/`chunk_ids`; else a single-row update. Returns `{success: true, drawer_id,
wing, room[, chunks, chunk_ids]}` (mempalace/mcp_server.py:L2299-L2404).

### `mempalace_mine` → `tool_mine(source, mode="projects", wing=None, agent="mempalace", limit=0, dry_run=False, extract="exchange")`
Triggers in-process mining. `mode` ∈ {`projects`, `convos`, `extract`}; an invalid mode →
`{success: false, error}`. A missing/non-directory source (after `~` expansion) →
`{success: false, error}`. Miner stdout is captured at both Python and fd level so it cannot corrupt
the JSON-RPC channel (mempalace/mcp_server.py:L1995-L2044, L2047-L2101). Success →
`{success: true, mode, dry_run, output[, output_truncated]}` where `output` is the miner's
human-readable summary, capped at the trailing 4000 chars (with `output_truncated: true` when
trimmed) (mempalace/mcp_server.py:L2188-L2196). Structured failures: concurrent mine →
`error_class: "LockHeldByOtherProcess"`; integrity failure → `"MineValidationError"`; missing
`extract` extra → `"MissingDependency"`; value error → `"ValueError"`; early exit (SystemExit) →
`"Interrupted"`; other → the exception's class name (mempalace/mcp_server.py:L2137-L2187). Non-dry-run
invalidates the metadata cache (mempalace/mcp_server.py:L2197-L2199).

### `mempalace_sync` → `tool_sync(project_dir=None, wing=None, apply=False)`
Prunes drawers whose source files are gitignored, deleted, or moved. Default is a dry-run report;
`apply=true` commits deletions. Success → `{success: true, ...report}`. Concurrent mine →
`{success: false, error_class: "LockHeldByOtherProcess"}`; value error and generic failures →
`{success: false, error}`. Applying invalidates the metadata cache
(mempalace/mcp_server.py:L2202-L2237).

## Graph and tunnel tools

- `mempalace_traverse` → `tool_traverse_graph(start_room, max_hops=2)`: `max_hops` clamped to
  `[1, 10]`; delegates to `traverse` (mempalace/mcp_server.py:L1453-L1459).
- `mempalace_find_tunnels` → `tool_find_tunnels(wing_a=None, wing_b=None)`: sanitizes optional wing
  filters; delegates to `find_tunnels` (mempalace/mcp_server.py:L1462-L1472).
- `mempalace_graph_stats` → `tool_graph_stats`: returns
  `{total_rooms, tunnel_rooms, total_edges, rooms_per_wing, top_tunnels}`. A SQLite fast-path
  reconstructs these without cold-loading HNSW: a node is a room with a non-empty wing and a usable
  room name (the catch-all `"general"` is excluded); edges are per-hall cross-wing crossings of
  multi-wing rooms; `top_tunnels` keeps multi-wing rooms among the top 10 by wing-count (room-name
  tiebreaker). Falls back to the client path for non-chroma backends
  (mempalace/mcp_server.py:L1018-L1133, L1475-L1486).
- `mempalace_create_tunnel` → `tool_create_tunnel(source_wing, source_room, target_wing,
  target_room, label="", source_drawer_id=None, target_drawer_id=None)`: sanitizes all four
  endpoints; `ValueError` (invalid names or failed room-existence checks) → `{error}`
  (mempalace/mcp_server.py:L1489-L1524).
- `mempalace_list_tunnels` → `tool_list_tunnels(wing=None)` (mempalace/mcp_server.py:L1527-L1533).
- `mempalace_delete_tunnel` → `tool_delete_tunnel(tunnel_id)`: missing/non-string → `{error:
  "tunnel_id is required"}` (mempalace/mcp_server.py:L1536-L1540).
- `mempalace_list_hallways` → `tool_list_hallways(wing=None)` (mempalace/mcp_server.py:L1543-L1549).
- `mempalace_delete_hallway` → `tool_delete_hallway(hallway_id)`: returns `{deleted: bool}`; missing
  id → `{error: "hallway_id is required"}` (mempalace/mcp_server.py:L1552-L1556).
- `mempalace_follow_tunnels` → `tool_follow_tunnels(wing, room)`: sanitizes both (required);
  delegates to `follow_tunnels` (mempalace/mcp_server.py:L1559-L1569).

## Knowledge-graph tools

- `mempalace_kg_query` → `tool_kg_query(entity, as_of=None, direction="both")`: `direction` must be
  `"outgoing"`, `"incoming"`, or `"both"` else `{error}`. `as_of` accepts `YYYY-MM-DD` or
  `YYYY-MM-DDTHH:MM:SSZ`. Returns `{entity, as_of, facts, count}`
  (mempalace/mcp_server.py:L2410-L2422).
- `mempalace_kg_add` → `tool_kg_add(subject, predicate, object, valid_from=None, valid_to=None,
  source_closet=None, source_file=None, source_drawer_id=None)`: sanitizes subject/object (KG
  values), predicate (name), and temporal fields. Returns `{success: true, triple_id, fact:
  "<subj> → <pred> → <obj>"}` (mempalace/mcp_server.py:L2425-L2479).
- `mempalace_kg_invalidate` → `tool_kg_invalidate(subject, predicate, object, ended=None)`: when
  `ended` is omitted it defaults to today's date (ISO) and the response reflects the resolved value.
  Returns `{success: true, fact, ended}` (mempalace/mcp_server.py:L2482-L2517).
- `mempalace_kg_timeline` → `tool_kg_timeline(entity=None)`: returns `{entity: <entity or "all">,
  timeline, count}` (mempalace/mcp_server.py:L2520-L2528).
- `mempalace_kg_stats` → `tool_kg_stats`: returns the graph's stats dict
  (mempalace/mcp_server.py:L2531-L2533).

## Agent diary tools

### `mempalace_diary_write` → `tool_diary_write(agent_name, entry, topic="general", wing="")`
`agent_name` is sanitized then lowercased (case-insensitive identity). When `wing` is empty it
defaults to `wing_<agent_name with spaces→underscores>`; room is fixed `"diary"`. The entry id is
`diary_<wing>_<YYYYMMDD_HHMMSSffffff>_<sha256(entry)[:12]>`. Metadata written includes `hall:
"hall_diary"`, `type: "diary_entry"`, `agent`, `filed_at` (ISO), and `date` (`YYYY-MM-DD`). Single
entry uses `add` and returns `{success, entry_id, agent, topic, timestamp, chunks: 1}`; oversized
entries are split (single batched `add`, NOT upsert — timestamp-precise ids must not silently
overwrite) into `<entry_id>_chunk_<NNNNNN>` chunks each carrying `parent_entry_id`, returning
`chunk_ids`/`chunks: N`. The returned `entry_id` on the chunked path is the logical group handle
(mempalace/mcp_server.py:L2539-L2660).

### `mempalace_diary_read` → `tool_diary_read(agent_name, last_n=10, wing="")`
`agent_name` is lowercased for filtering; `last_n` clamped to `[1, 100]`. Always scopes to
`room="diary"` and the given agent; `wing` (when provided) further scopes, else reads across all
wings. Returns entries `{date, timestamp, topic, content}` sorted by timestamp descending and
truncated to `last_n`: `{agent, entries, total, showing}`. No entries →
`{agent, entries: [], message: "No diary entries yet."}`. Errors →
`{error: "Failed to read diary entries"}` (mempalace/mcp_server.py:L2663-L2731).

## Settings and hook tools

### `mempalace_hook_settings` → `tool_hook_settings(silent_save=None, desktop_toast=None)`
With no args, returns current `{silent_save, desktop_toast}`. When provided, each is persisted and
listed in `updated`. Returns `{success: true, settings: {silent_save, desktop_toast}[, updated]}`
(mempalace/mcp_server.py:L2734-L2775).

### `mempalace_memories_filed_away` → `tool_memories_filed_away`
Reads and consumes the checkpoint ack file at `~/.mempalace/hook_state/last_checkpoint` (JSON). If
absent → `{status: "quiet", message: "No recent journal entry", count: 0, timestamp: null}`. On
success it deletes the file and returns `{status: "ok", message: "✦ <N> messages tucked into
drawers", count: <N>, timestamp: <ts>}`. On parse/OS error it deletes the file and returns
`{status: "error", message: "✦ Journal entry filed in the palace", count: 0, timestamp: null}`
(mempalace/mcp_server.py:L2778-L2806).

### `mempalace_reconnect` → `tool_reconnect`
Forces a full cache drop: closes the shared backend palace and any previously-cached backend, closes
the MCP-local Chroma client, clears Chroma's shared system cache, resets all module caches and the
vector-disabled flag, discards the quarantine marker for the palace path, and closes+drains all
cached `KnowledgeGraph` handles. Then re-opens the collection. Returns `{success: true, message:
"Reconnected to palace", drawers: <count>, vector_disabled, vector_disabled_reason}` on success; if
the palace cannot be reopened it returns `{success: false, message, drawers: 0, vector_disabled[,
details, hint, error]}`; if reopened but some handles failed to close →
`{success: false, message: "...failed to fully reset cached handles", drawers, vector_disabled,
vector_disabled_reason, error}` (mempalace/mcp_server.py:L2812-L2937).

## Fixed protocol/dialect text contracts

`PALACE_PROTOCOL` is a fixed multi-step wake-up protocol string returned in status responses
(mempalace/mcp_server.py:L1206-L1213). `AAAK_SPEC` is a fixed dialect-specification string describing
entity codes, emotion markers, pipe-separated structure, ISO dates, count/importance markers, and
hall/wing/room conventions; it is embedded in status responses and returned by `get_aaak_spec`
(mempalace/mcp_server.py:L1215-L1232).

## Invariants summary

- JSON-RPC: notifications (no `id`, or `notifications/*` method) never receive a response
  (mempalace/mcp_server.py:L3538-L3540, L3678-L3680).
- All tool results are returned as a single text content block of pretty-printed UTF-8 JSON
  (mempalace/mcp_server.py:L3641-L3645).
- Chunk ids are deterministic `<parent>_chunk_<6-digit index>`; chunked logical ids never address a
  physical row directly (mempalace/mcp_server.py:L1809, L1928, L2639).
- Writes are WAL-logged before the underlying store mutation
  (mempalace/mcp_server.py:L1851-L1861, L1970-L1978, L2343-L2354, L2453-L2465, L2502-L2512,
  L2573-L2581).
- Idle watchdog terminates the process via `os._exit(0)` (no cleanup handlers run)
  (mempalace/mcp_server.py:L3887).
