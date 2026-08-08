# MCP Tools Reference

Detailed parameter schemas for all 44 MCP tools.

## Palace — Read Tools

### `mempalace_status`

Palace overview: total drawers, wing and room counts, AAAK spec, and memory protocol.

**Parameters:** None

**Returns:** `{ total_drawers, wings, rooms, protocol, aaak_dialect }`

---

### `mempalace_list_wings`

List all wings with drawer counts.

**Parameters:** None

**Returns:** `{ wings: { "wing_name": count } }`

---

### `mempalace_list_rooms`

List rooms within a wing (or all rooms if no wing given).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `wing` | string | No | Wing to list rooms for |

**Returns:** `{ wing, rooms: { "room_name": count } }`

---

### `mempalace_get_taxonomy`

Full wing → room → drawer count tree.

**Parameters:** None

**Returns:** `{ taxonomy: { "wing": { "room": count } } }`

---

### `mempalace_search`

Semantic search. Returns verbatim drawer content with similarity scores.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | **Yes** | What to search for |
| `limit` | integer | No | Max results (default: 5) |
| `wing` | string | No | Filter by wing |
| `room` | string | No | Filter by room |

**Returns:** `{ query, filters, results: [{ text, wing, room, source_file, similarity }] }`

---

### `mempalace_check_duplicate`

Check if content already exists in the palace before filing.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `content` | string | **Yes** | Content to check |
| `threshold` | number | No | Similarity threshold 0–1 (default: 0.85–0.87) |

**Returns:** `{ is_duplicate, matches: [{ id, wing, room, similarity, content }] }`

---

### `mempalace_get_aaak_spec`

Returns the AAAK dialect specification.

**Parameters:** None

**Returns:** `{ aaak_spec: "..." }`

---

## Palace — Write Tools

### `mempalace_add_drawer`

File verbatim content into the palace. Identical content (same deterministic drawer ID) is silently skipped. For similarity-based duplicate detection before filing, use `mempalace_check_duplicate`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `wing` | string | **Yes** | Wing (project name) |
| `room` | string | **Yes** | Room (aspect: backend, decisions, etc.) |
| `content` | string | **Yes** | Verbatim content to store |
| `source_file` | string | No | Where this came from |
| `added_by` | string | No | Who is filing (default: "mcp") |

**Returns:** `{ success, drawer_id, wing, room }`

---

### `mempalace_checkpoint`

Save a whole session in one call. Semantic-dedups each item, files the non-duplicates as drawers, then writes one diary entry. Use this instead of many separate `mempalace_check_duplicate` / `mempalace_add_drawer` / `mempalace_diary_write` calls — it renders as a single tool-call card in the host UI (and keeps the spinner up for the whole save). Reuses the same single-item handlers, so dedup, idempotency, and verbatim guarantees are identical.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `items` | array | **Yes** | Verbatim items to file. Each is `{ wing, room, content }` |
| `diary` | object | No | Diary entry written after filing: `{ agent_name, entry, topic?, wing? }` (`entry` is AAAK-format) |
| `dedup_threshold` | number | No | Similarity threshold 0–1 for the per-item dedup check (default 0.9) |
| `added_by` | string | No | Who is filing these drawers. An explicit value takes precedence; otherwise the diary `agent_name`, else `checkpoint` |

**Returns:** `{ added: [...], duplicates: [...], errors: [...], diary? }`

---

### `mempalace_delete_drawer`

Delete a drawer by ID. Irreversible.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `drawer_id` | string | **Yes** | ID of the drawer to delete |

**Returns:** `{ success, drawer_id }`

---

### `mempalace_mine`

Mine a directory into the palace — the MCP equivalent of `mempalace mine`. Wraps the same in-process miners the CLI uses; runs synchronously and returns the miner's summary as `output`. The palace write lock is automatic — a concurrent mine returns a structured already-running error. Orphan cleanup is separate (see `mempalace_sync`).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source` | string | **Yes** | Directory to mine |
| `mode` | string | No | `projects` (code/docs, default), `convos` (chat transcripts), or `extract` (office docs; needs the `mempalace[extract]` extra) |
| `wing` | string | No | Target wing (default: source directory name) |
| `agent` | string | No | Recorded on every drawer (default: `mempalace`) |
| `limit` | integer | No | Max files to process (0 = all; default 0) |
| `dry_run` | boolean | No | Report what would be filed without writing (default false) |
| `extract` | string | No | Convos extraction strategy: `exchange` (default) or `general`; ignored by other modes |

**Returns:** `{ success, mode, dry_run, output }` on success (`output` is the miner's human-readable summary; `output_truncated: true` is added when a very large summary is tail-trimmed), or `{ success: false, error, error_class? }` on failure.

---

### `mempalace_delete_by_source`

Bulk-delete every drawer mined from one `source_file` (exact match). Use this to clean up benchmark or test data that was accidentally mined into a user wing — for example ShareGPT dumps or `results_mempal_*.jsonl` eval files drowning out real memories in semantic search. Matching is pushed down to the storage backend via a `where` filter, so it is not subject to the SQLite variable limit no matter how many drawers share the source. Returns a dry-run match count and a small sample by default; pass `dry_run=false` to commit. Irreversible.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source_file` | string | **Yes** | Exact `source_file` metadata value to remove (e.g. the full path that was mined) |
| `dry_run` | boolean | No | Preview the match count without deleting; default `true`. Pass `false` to actually delete |

**Returns (dry run):** `{ success, dry_run, source_file, match_count, sample, hint }`
**Returns (commit):** `{ success, dry_run, source_file, deleted }`

---

### `mempalace_sync`

Prune drawers whose source files are gitignored, deleted, or moved. Returns a dry-run report by default; pass `apply=true` to commit deletions.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_dir` | string | No | Project root to scope the sync (auto-detected from drawer metadata if omitted) |
| `wing` | string | No | Limit to one wing |
| `apply` | boolean | No | Actually delete drawers; default is dry-run preview |

**Returns:** `{ scanned, kept, gitignored, missing, no_source, out_of_scope, removed_drawers, removed_closets, dry_run, by_source }`

---

### `mempalace_get_drawer`

Fetch a single drawer by ID — returns full content and metadata.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `drawer_id` | string | **Yes** | ID of the drawer to fetch |

**Returns:** `{ drawer_id, content, wing, room, metadata }` where `metadata.source_file`, when present, is the basename only — the absolute path written by the miners is reduced before the dict is returned to MCP clients.

---

### `mempalace_list_drawers`

List drawers with pagination. Optional wing/room filter. Returns IDs, wings, rooms, and content previews.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `wing` | string | No | Filter by wing |
| `room` | string | No | Filter by room |
| `limit` | integer | No | Max results per page (default 20, max 100) |
| `offset` | integer | No | Offset for pagination (default 0) |

**Returns:** `{ drawers: [...], total, limit, offset }`

---

### `mempalace_update_drawer`

Update an existing drawer's content and/or metadata (wing, room). Fetches the existing drawer first; returns an error if not found.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `drawer_id` | string | **Yes** | ID of the drawer to update |
| `content` | string | No | New content (omit to keep existing) |
| `wing` | string | No | New wing (omit to keep existing) |
| `room` | string | No | New room (omit to keep existing) |

**Returns:** `{ success, drawer_id, updated_fields }`

---

## Knowledge Graph Tools

### `mempalace_kg_query`

Query entity relationships with time filtering.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity` | string | **Yes** | Entity to query (e.g. "Max", "MyProject") |
| `as_of` | string | No | Date filter — only facts valid at this date (YYYY-MM-DD) |
| `direction` | string | No | `outgoing`, `incoming`, or `both` (default: `both`) |

**Returns:** `{ entity, as_of, facts: [{ direction, subject, predicate, object, valid_from, valid_to, current }], count }`

---

### `mempalace_kg_add`

Add a fact to the knowledge graph.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `subject` | string | **Yes** | The entity doing/being something |
| `predicate` | string | **Yes** | Relationship type (e.g. "loves", "works_on") |
| `object` | string | **Yes** | The entity being connected to |
| `valid_from` | string | No | When this became true (YYYY-MM-DD) |
| `source_closet` | string | No | Closet ID where this fact appears |

**Returns:** `{ success, triple_id, fact }`

---

### `mempalace_kg_invalidate`

Mark a fact as no longer true.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `subject` | string | **Yes** | Entity |
| `predicate` | string | **Yes** | Relationship |
| `object` | string | **Yes** | Connected entity |
| `ended` | string | No | When it stopped being true (default: today) |

**Returns:** `{ success, fact, ended }`

---

### `mempalace_kg_supersede`

Atomically replace a fact with its successor at a single shared boundary. Use when a single-valued fact changes (model, employer, address) instead of a separate `mempalace_kg_invalidate` + `mempalace_kg_add` — a point-in-time query at the boundary then returns only the new value.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `subject` | string | **Yes** | Entity whose fact is changing |
| `predicate` | string | **Yes** | Relationship (e.g. `uses_model`, `works_at`) |
| `old_object` | string | **Yes** | Value being replaced |
| `new_object` | string | **Yes** | New value |
| `at` | string | No | Boundary instant (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ; default: now UTC) |

**Returns:** `{ success, triple_id, fact, superseded }`

---

### `mempalace_kg_timeline`

Chronological timeline of facts.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity` | string | No | Entity to get timeline for (omit for full timeline) |

**Returns:** `{ entity, timeline: [{ subject, predicate, object, valid_from, valid_to, current }], count }`

---

### `mempalace_kg_stats`

Knowledge graph overview.

**Parameters:** None

**Returns:** `{ entities, triples, current_facts, expired_facts, relationship_types }`

---

## Navigation Tools

### `mempalace_traverse`

Walk the palace graph from a room. Find connected ideas across wings.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `start_room` | string | **Yes** | Room to start from |
| `max_hops` | integer | No | How many connections to follow (default: 2) |

**Returns:** `[{ room, wings, halls, count, hop, connected_via }]`

---

### `mempalace_find_tunnels`

Find rooms that bridge two wings.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `wing_a` | string | No | First wing |
| `wing_b` | string | No | Second wing |

**Returns:** `[{ room, wings, halls, count, recent }]`

---

### `mempalace_graph_stats`

Palace graph overview: nodes, tunnels, edges, connectivity.

**Parameters:** None

**Returns:** `{ total_rooms, tunnel_rooms, total_edges, rooms_per_wing, top_tunnels }`

---

### `mempalace_create_tunnel`

Create a cross-wing tunnel linking two palace locations. Use when content in one project relates to another — e.g., an API design in `project_api` connects to a database schema in `project_database`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source_wing` | string | **Yes** | Wing of the source |
| `source_room` | string | **Yes** | Room in the source wing |
| `target_wing` | string | **Yes** | Wing of the target |
| `target_room` | string | **Yes** | Room in the target wing |
| `label` | string | No | Description of the connection |
| `source_drawer_id` | string | No | Specific source drawer ID |
| `target_drawer_id` | string | No | Specific target drawer ID |

**Returns:** `{ success, tunnel_id, source, target }`

---

### `mempalace_list_tunnels`

List all explicit cross-wing tunnels. Optionally filter by wing.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `wing` | string | No | Filter tunnels by wing (source or target) |

**Returns:** `{ tunnels: [...], count }`

---

### `mempalace_delete_tunnel`

Delete an explicit tunnel by its ID.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tunnel_id` | string | **Yes** | Tunnel ID to delete |

**Returns:** `{ success, tunnel_id }`

---

### `mempalace_list_hallways`

List within-wing hallway records (entity-to-entity co-occurrence links built at mine time). Optionally filter by wing.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `wing` | string | No | Filter hallways by wing |

**Returns:** `[ { id, wing, entity_a, entity_b, co_occurrence_count, rooms, ... }, ... ]`

---

### `mempalace_delete_hallway`

Delete a hallway record by its ID.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `hallway_id` | string | **Yes** | Hallway ID to delete |

**Returns:** `{ deleted: bool }`

---

### `mempalace_follow_tunnels`

Follow tunnels from a room to see what it connects to in other wings. Returns connected rooms with drawer previews.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `wing` | string | **Yes** | Wing to start from |
| `room` | string | **Yes** | Room to follow tunnels from |

**Returns:** `[{ wing, room, label, previews }]`

---

## Agent Diary Tools

### `mempalace_diary_write`

Write to your personal agent diary.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_name` | string | **Yes** | Your name — each agent gets its own wing |
| `entry` | string | **Yes** | Diary entry (in AAAK format recommended) |
| `topic` | string | No | Topic tag (default: "general") |

**Returns:** `{ success, entry_id, agent, topic, timestamp }`

---

### `mempalace_diary_read`

Read recent diary entries.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_name` | string | **Yes** | Your name |
| `last_n` | integer | No | Number of recent entries (default: 10) |

**Returns:** `{ agent, entries: [{ date, timestamp, topic, content }], total, showing }`

---

## System Tools

### `mempalace_hook_settings`

Get or set auto-save hook behaviour. `silent_save=true` saves directly without MCP-level clutter; `silent_save=false` uses the legacy blocking path. `desktop_toast=true` surfaces a desktop notification when a save completes. Call with no arguments to view the current settings.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `silent_save` | boolean | No | `true` = silent direct save, `false` = blocking MCP calls |
| `desktop_toast` | boolean | No | `true` = show desktop toast via `notify-send` |

**Returns:** `{ silent_save, desktop_toast }`

---

### `mempalace_memories_filed_away`

Check whether a recent palace checkpoint was saved. Returns message count and timestamp of the last save.

**Parameters:** None

**Returns:** `{ filed, message_count, timestamp }`

---

### `mempalace_reconnect`

Force a reconnect to the palace database. Use this after external scripts or CLI commands modified the palace directly, which can leave the in-memory HNSW index stale.

**Parameters:** None

**Returns:** `{ success, message, drawers, vector_disabled[, vector_disabled_reason] }` (on no-palace: `{ success: false, message, drawers, vector_disabled }`; on exception: `{ success: false, error }`)

---

## Agent Coordination Tools (Logstream)

Append-only coordination events and exact artifacts for multi-agent work — see the [Agent Logstream](/concepts/agent-logstream) concept page. Backed by `logstream.sqlite3` in the palace directory, independent of the vector index. In `--read-only` mode the mutating tools (`event_append`, `event_ack`, `artifact_put`, `patch_submit`) are hidden and refused.

### `mempalace_event_append`

Append an immutable coordination event.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | **Yes** | Event type, e.g. `task.request`, `task.reply`, `patch.ready` |
| `stream` | string | **Yes** | Logical stream, e.g. `project/myapp` or `shared_agent_brain` |
| `room` | string | **Yes** | Sub-channel: `delegation`, `patches`, `reviews`, `status` |
| `from_agent` | string | **Yes** | Writer agent identity |
| `to_agent` | string | No | Target agent, or `*` for broadcast |
| `correlation_id` | string | No | Task id tying request and reply events together |
| `branch` | string | No | Git branch, when relevant |
| `base_commit` | string | No | Git commit the work started from |
| `status` | string | No | `open`, `claimed`, `ready`, `applied`, `blocked`, `failed`, `superseded` |
| `body` | string | No | Verbatim content (max 256 KiB) |
| `metadata` | object | No | Extra structured fields, stored verbatim |
| `artifact_ids` | array | No | Ids of already-stored artifacts to reference |

**Returns:** `{ success, event }` — the stored event including server-generated `id`, `seq`, and `created_at`.

---

### `mempalace_event_list`

List events with structured filters, oldest first.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `stream` | string | No | Filter by stream |
| `room` | string | No | Filter by room |
| `type` | string | No | Filter by event type |
| `to_agent` | string | No | Filter by target; also matches `*` broadcasts |
| `from_agent` | string | No | Filter by writer |
| `correlation_id` | string | No | Filter by correlation id |
| `status` | string | No | Filter by status |
| `since_event_id` | string | No | Only events strictly after this id (precise cursor) |
| `since_created_at` | string | No | Only events at/after this time (inclusive) |
| `limit` | integer | No | Max events (default 50, cap 500) |

**Returns:** `{ events: [...], count }`

---

### `mempalace_event_wait`

Block until a matching event exists or the timeout expires (long-poll; max 5 minutes). Accepts the same filters as `event_list` plus:

For live-tail clients that can keep an HTTP connection open, use
`GET /logstream/stream` SSE instead; `event_wait` is the polling MCP surface.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `timeout_ms` | integer | No | Wait duration in ms (default 60000, clamped to 300000) |
| `limit` | integer | No | Max events to return on match (default 50) |

**Returns:** `{ timed_out, events: [...], count }` — timeout is a normal result, not an error.

---

### `mempalace_event_ack`

Acknowledge an event: appends a new `event.ack` routed back to the original writer, with `correlation_id` copied from the target (falling back to the target's id). Never mutates the target event.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `event_id` | string | **Yes** | Event to acknowledge |
| `from_agent` | string | **Yes** | Acknowledging agent identity |
| `status` | string | No | e.g. `applied`, `failed` |
| `body` | string | No | Verbatim ack notes |

**Returns:** `{ success, event }` — the new ack event.

---

### `mempalace_artifact_put`

Store exact artifact content for handoffs. UTF-8 text only, max 4 MiB.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `kind` | string | **Yes** | `patch`, `file`, `log`, `json`, `note` |
| `content` | string | **Yes** | Exact content |
| `created_by` | string | **Yes** | Writer agent identity |
| `metadata` | object | No | Extra fields, e.g. branch/base_commit |

**Returns:** `{ success, artifact: { id, kind, sha256, size_bytes, created_by, created_at } }`

---

### `mempalace_artifact_get`

Fetch an artifact by id — exact content plus `sha256` for verification.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `artifact_id` | string | **Yes** | Artifact id |

**Returns:** `{ artifact: { id, kind, sha256, size_bytes, content, created_by, created_at, metadata } }` (or `{ error }` if not found)

---

### `mempalace_patch_submit`

Convenience: store a patch artifact and append its `patch.ready` event in one call.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `content` | string | **Yes** | Unified diff content |
| `from_agent` | string | **Yes** | Submitting agent identity |
| `stream` | string | **Yes** | Logical stream |
| `room` | string | No | Sub-channel (default `patches`) |
| `to_agent` | string | No | Target agent or `*` |
| `correlation_id` | string | No | Task id tying the patch to its request |
| `branch` | string | No | Git branch |
| `base_commit` | string | No | Git commit the patch applies to |
| `body` | string | No | Verbatim notes |
| `metadata` | object | No | Extra structured fields |

**Returns:** `{ success, artifact, event }`

---

### `mempalace_mesh_peers`

Mesh estate snapshot — this hub's view of its logstream peers (see [Shared Brain](/guide/shared-brain#coordinating-across-machines)):
this replica's identity, version vector and self-derived node profile; each
configured peer's reachability, last sync outcome, remote version vector and
advertised profile; origins known only transitively; and `origin_profiles`
keyed by replica id. Exactly the `GET /sync/peers` payload, produced by the
same function — the committed compat surface for mesh dashboards. Bearer
tokens are never included.

**Parameters:** None

**Returns:** `{ self: { replica_id, name, version_vector, profile }, peers: [ { name, url, replica_id, reachable, last_success_at, last_error, remote_version_vector, profile } ], unnamed_origins, origin_profiles, sync_interval_s }`

A node `profile` is pure derivation, never configuration: `roles` (subset of
`replica` / `agents` / `compute`), `accelerator` (`{ provider, embedder }`
from the resolved onnxruntime provider — CUDA, DirectML, CoreML or CPU),
`drawers` (live store count), `hardware` (platform string), `advertised_at`.
Profiles propagate over the sync surfaces, so carriers relay them for
replicas they only know transitively.
