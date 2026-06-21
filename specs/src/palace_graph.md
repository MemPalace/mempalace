# Behavior Specification: `palace_graph.py`

Graph traversal layer for MemPalace. Builds a navigable graph from the palace structure where nodes are rooms (named ideas), edges are shared rooms across wings ("tunnels"), and edge types are halls ("corridors"). The graph is derived from storage-backend metadata; no external graph database is used (mempalace/palace_graph.py:L1-L16). It also manages explicit, topic, and entity tunnels persisted to an on-disk JSON file.

## Wing Name Normalization

A wing name is normalized for consistent lookup by: rejecting non-string inputs (returning a null/absent value), stripping surrounding whitespace, returning the null/absent value for an empty string, otherwise applying the shared wing-name normalization (which replaces hyphens and spaces with underscores) (mempalace/palace_graph.py:L40-L57). Non-string inputs return the null/absent value rather than raising, so a single malformed record in `tunnels.json` cannot break read-path filters that iterate the whole file (mempalace/palace_graph.py:L48-L57).

## Graph Cache

A module-level cache holds the most recently built `nodes` and `edges` plus a build timestamp, guarded by a lock for thread safety, with a time-to-live of 60 seconds (mempalace/palace_graph.py:L60-L66). `invalidate_graph_cache()` clears the cached nodes, edges, and timestamp so the next build recomputes from scratch; it is invoked by write paths (mempalace/palace_graph.py:L69-L75). Only non-empty graphs are cached, so newly populated palaces are picked up immediately rather than serving a stale empty result (mempalace/palace_graph.py:L178-L184).

## `build_graph(col=None, config=None)`

Builds the palace graph from storage metadata. Returns the cached `(nodes, edges)` pair when the cache is populated and fresher than the TTL (mempalace/palace_graph.py:L105-L111). A warm cache hit intentionally ignores the `col` and `config` arguments; a caller switching to a different collection must call `invalidate_graph_cache()` first (mempalace/palace_graph.py:L96-L99, L107-L108).

When no fresh cache exists and no collection is supplied, the collection is resolved from configuration without creating it; if the collection is unavailable, the function returns an empty nodes mapping and empty edges list (mempalace/palace_graph.py:L113-L116). The collection is resolved using `palace_path` and `collection_name` from config with `create=False`, returning a null/absent collection on any failure (mempalace/palace_graph.py:L78-L88).

All drawers are scanned in batches of up to 1000 records, requesting metadata only, advancing the offset by the number of returned ids; the loop ends when the offset reaches the total count or a batch returns no ids (mempalace/palace_graph.py:L118-L148). For each metadata record: a null/absent metadata entry is skipped silently rather than crashing the build (mempalace/palace_graph.py:L124-L134). From each record, `room`, `wing`, `hall`, and `date` fields are read (defaulting to empty string when absent) (mempalace/palace_graph.py:L135-L138). A record contributes to `room_data` only when `room` is non-empty, `room` is not the literal `"general"`, and `wing` is non-empty (mempalace/palace_graph.py:L139). For qualifying records: the wing is added to that room's wing set; a non-empty hall is added to the room's hall set; a non-empty date is added to the room's date set; and the room's count is incremented by one (mempalace/palace_graph.py:L139-L145).

### Edges Output

An edge is emitted for every room spanning two or more distinct wings. For such a room, wings are sorted, and for every unordered pair of wings `(wing_a, wing_b)` with `wing_a` before `wing_b`, one edge is emitted per hall associated with that room. Each edge is an object with fields `room`, `wing_a`, `wing_b`, `hall`, and `count` (the room's drawer count) (mempalace/palace_graph.py:L150-L166). A room in fewer than two wings produces no edges (mempalace/palace_graph.py:L153-L154).

### Nodes Output

`nodes` is a mapping from room name to an object with: `wings` (sorted list of distinct wing names), `halls` (sorted list of distinct hall names), `count` (integer drawer count), and `dates` (sorted list of distinct dates, truncated to the last 5 elements, or empty list when no dates) (mempalace/palace_graph.py:L168-L176).

## `traverse(start_room, col=None, config=None, max_hops=2)`

Walks the graph from a starting room, finding rooms connected through shared wings, returning a list of path objects (mempalace/palace_graph.py:L189-L195). If the start room is absent from the graph nodes, returns an object with an `error` message of the form `Room '<start_room>' not found` and a `suggestions` list of fuzzy-matched room names (mempalace/palace_graph.py:L198-L203).

The first result entry is the start room itself with `room`, `wings`, `halls`, `count`, and `hop` of 0 (mempalace/palace_graph.py:L204-L214). Traversal is breadth-first using a FIFO frontier seeded with the start room at depth 0 (mempalace/palace_graph.py:L216-L219). A node at or beyond `max_hops` is not expanded further (mempalace/palace_graph.py:L220-L221). For the current room, every not-yet-visited room sharing at least one wing is added to the results with `room`, `wings`, `halls`, `count`, `hop` (current depth + 1), and `connected_via` (sorted list of the shared wing names); each such room is marked visited and enqueued only if its hop is still below `max_hops` (mempalace/palace_graph.py:L223-L244). Results are sorted by ascending hop distance then by descending count, and capped at 50 entries (mempalace/palace_graph.py:L246-L248).

## `find_tunnels(wing_a=None, wing_b=None, col=None, config=None)`

Finds rooms that connect two wings (passive tunnels — the same named idea appearing in multiple wings); with no wing filters, returns all multi-wing rooms (mempalace/palace_graph.py:L251-L256). Both wing filters are normalized before comparison (mempalace/palace_graph.py:L258-L259). A room qualifies only if it spans two or more wings, and (when a normalized filter is provided) that filter wing must appear among the room's wings (mempalace/palace_graph.py:L262-L270). Each result object has `room`, `wings`, `halls`, `count`, and `recent` (the room's most recent date, or empty string when none) (mempalace/palace_graph.py:L272-L280). If no tunnels are found while a wing filter was provided, a warning is logged naming the raw and normalized filter values (mempalace/palace_graph.py:L282-L289). Results are sorted by descending count and capped at 50 (mempalace/palace_graph.py:L291-L292).

## `graph_stats(col=None, config=None)`

Returns summary statistics about the palace graph as an object with: `total_rooms` (count of nodes), `tunnel_rooms` (number of rooms in two or more wings), `total_edges` (length of the edge list), `rooms_per_wing` (mapping of wing name to room count, ordered most-common first), and `top_tunnels` (up to 10 multi-wing rooms sorted by descending wing count, each with `room`, `wings`, `count`) (mempalace/palace_graph.py:L295-L315).

## Fuzzy Matching (internal, used by `traverse`)

A query is matched against room names case-insensitively: a room containing the lowercased query as a substring scores 1.0; otherwise a room containing any hyphen-split word of the query scores 0.5; non-matching rooms are excluded. Matches are sorted by descending score and the top 5 (by default) room names are returned (mempalace/palace_graph.py:L318-L329).

## Explicit Tunnels — On-Disk Storage Contract

Explicit tunnels are agent/user-created cross-wing links, stored as a JSON file derived from the configured `palace_path` (via `config.tunnel_file`) so they survive palace rebuilds, separate from the rebuildable storage backend (mempalace/palace_graph.py:L332-L347). A pre-3.3.6 legacy location is the hardcoded path `~/.mempalace/tunnels.json`, retained only for one-time orphan detection (mempalace/palace_graph.py:L350-L352).

### Loading

Loading reads the configured tunnel file and returns its parsed contents only if that content is a list; non-list content yields an empty list (mempalace/palace_graph.py:L373-L384). A missing or corrupt/unreadable file yields an empty list (rather than raising), and corruption logs a warning naming the file (mempalace/palace_graph.py:L355-L384). If the configured file is missing but a differently-pathed legacy file exists, a warning naming both paths is logged; no automatic migration or merge occurs (mempalace/palace_graph.py:L386-L394).

### Saving (atomic + permissions)

Saving is atomic: the data is JSON-serialized (2-space indent) to a temporary file `<tunnel_file>.tmp`, flushed and fsync'd (fsync failures tolerated), then renamed into place, so a crash mid-write cannot leave a partial file that wipes existing tunnels (mempalace/palace_graph.py:L397-L430). The parent directory is created if absent and restricted to permissions `0o700`; the resulting file is restricted to `0o600`; permission-change failures on unsupported platforms are tolerated (mempalace/palace_graph.py:L405-L434).

### Tunnel Record Shape and Canonical ID

A tunnel endpoint key is `"<wing>/<room>"` (mempalace/palace_graph.py:L437-L438). The canonical tunnel ID is symmetric: the two endpoint keys are sorted, joined with a `↔` separator, hashed with SHA-256, and truncated to the first 16 hex characters, so `(A,B)` and `(B,A)` resolve to the same ID and deduplicate into one record (mempalace/palace_graph.py:L441-L454).

## `create_tunnel(source_wing, source_room, target_wing, target_room, label="", source_drawer_id=None, target_drawer_id=None, kind="explicit")`

Creates a symmetric (undirected) tunnel between two palace locations; `(A,B)` and `(B,A)` resolve to the same canonical ID, and a second call with the same endpoints updates the existing record rather than duplicating (mempalace/palace_graph.py:L488-L508).

All four wing/room arguments are required to be non-empty strings; each is stripped, and an empty or non-string value raises a validation error naming the field (mempalace/palace_graph.py:L457-L461, L538-L541). For `kind == "explicit"` only, room existence is verified against the storage backend: if the source or target room does not exist, a validation error is raised of the form `Source room '<room>' does not exist in wing '<wing>'` (or the target equivalent) (mempalace/palace_graph.py:L550-L557). Room existence is checked by querying for at least one drawer matching both wing and room; if the collection is unreachable or the query fails, existence is assumed true (creation is allowed) (mempalace/palace_graph.py:L464-L485). Endpoint wing slugs are compared and stored verbatim — `"my-wing"` and `"my_wing"` produce two distinct tunnels (mempalace/palace_graph.py:L502-L504, L531-L536).

The stored tunnel object contains: `id` (canonical ID), `source` (`{wing, room}`), `target` (`{wing, room}`), `label`, `kind`, and `created_at` (UTC ISO-8601 timestamp). `source` preserves the caller's argument order. Optional `source_drawer_id` / `target_drawer_id`, when provided, are stored as a `drawer_id` field inside the corresponding endpoint object (mempalace/palace_graph.py:L561-L572). The `kind` field is one of `"explicit"` (default), `"topic"`, or `"entity"`, preserved so readers can distinguish real-room traversals from synthetic links (mempalace/palace_graph.py:L518-L522).

The load-mutate-save cycle is serialized under a file lock keyed on the tunnel file path, so concurrent creators do not drop each other's writes (mempalace/palace_graph.py:L574-L577). If an existing tunnel with the same ID is found: the original `created_at` is preserved, an `updated_at` UTC ISO-8601 timestamp is set, and the L7 dynamics fields (`strength`, `stability`, `last_activated`, `access_count`) are carried over from the existing record before dynamics fields are (re)initialized with defaults for any still-missing fields; the existing record is replaced in place and the file saved, returning the updated record (mempalace/palace_graph.py:L579-L599). For a brand-new tunnel, dynamics fields are initialized from defaults, the record appended, and the file saved (mempalace/palace_graph.py:L600-L604).

A single configuration instance is created per call and reused across the file helpers to avoid redundant disk reads of the config file (mempalace/palace_graph.py:L543-L548).

## `list_tunnels(wing=None)`

Returns all explicit tunnels, optionally filtered by wing (mempalace/palace_graph.py:L607-L612). When a wing filter is given, both the filter and each stored tunnel's source and target wings are normalized before comparison, so legacy underscore data and hyphen data both match; a tunnel matches if either endpoint's normalized wing equals the normalized filter (mempalace/palace_graph.py:L613-L628). A `source`/`target` value that is explicitly null in a hand-edited file is treated as an empty object during the comparison (mempalace/palace_graph.py:L620-L627).

## `delete_tunnel(tunnel_id)`

Under the tunnel-file lock, loads tunnels, removes every tunnel whose `id` equals the given ID, saves, and returns `{"deleted": <tunnel_id>}` (mempalace/palace_graph.py:L631-L637).

## `follow_tunnels(wing, room, col=None, config=None)`

Given a location, finds all tunnels leading from or to it and optionally hydrates connected drawer content (mempalace/palace_graph.py:L640-L645). The query wing is normalized, falling back to the raw value when normalization yields the null/absent value (so an empty/whitespace query still has a comparison value) (mempalace/palace_graph.py:L646-L651). For each tunnel (with null source/target treated as empty objects): if the normalized source wing matches and the source room equals `room`, an `outgoing` connection is emitted pointing at the target; if instead the normalized target wing matches and the target room equals `room`, an `incoming` connection is emitted pointing at the source (mempalace/palace_graph.py:L655-L683). Each connection object has `direction` (`"outgoing"` or `"incoming"`), `connected_wing`, `connected_room`, `label` (default empty), `drawer_id` (may be null/absent), and `tunnel_id` (mempalace/palace_graph.py:L662-L683). When no connections are found, a warning naming the location is logged (mempalace/palace_graph.py:L685-L686).

When a collection is provided and there are connections with drawer IDs, the documents for those drawer IDs are fetched and each matching connection gains a `drawer_preview` field containing the first 300 characters of the connected drawer's document; any failure during hydration is swallowed (logged at debug) and leaves connections without previews (mempalace/palace_graph.py:L688-L702).

## Topic Tunnels

Topic tunnels auto-link wings sharing confirmed topic labels. The synthetic room identifier is `topic:<name>` (prefix constant `topic:`), namespacing topic tunnels away from literal folder-derived rooms; these are stored via `create_tunnel` with `kind="topic"` so they share storage and dedup with explicit tunnels (mempalace/palace_graph.py:L705-L723, L731-L738). A topic name is normalized for overlap detection by stringifying, stripping, and lowercasing (mempalace/palace_graph.py:L726-L728).

### `compute_topic_tunnels(topics_by_wing, min_count=1, label_prefix="shared topic")`

Creates a tunnel for every (wing_a, wing_b, shared-topic) triple where the pair of wings shares at least `min_count` topics (mempalace/palace_graph.py:L741-L768). An empty or null `topics_by_wing` returns an empty list (mempalace/palace_graph.py:L769-L770). `min_count` is coerced to an integer and clamped to a minimum of 1 (mempalace/palace_graph.py:L767, L772). Per wing, a normalized-topic-to-first-seen-casing map is built; non-string/empty wing keys are skipped, non-list/tuple topic values are skipped, non-string and empty-normalized topic names are skipped, and the first observed casing is preserved; wings whose bucket ends up empty are skipped (mempalace/palace_graph.py:L774-L796). The wing key written into the working map is canonicalized via wing-name normalization, so mixed slug forms collapse into one canonical record (mempalace/palace_graph.py:L791-L796).

Wing pairs are iterated in sorted order over unordered pairs; a pair sharing fewer than `min_count` topics produces no tunnels at all (not even its single shared topic) (mempalace/palace_graph.py:L798-L806). Shared topics are processed in sorted order for deterministic output; for each, the display casing is taken from the first wing's bucket (falling back to the second), the room is `topic:<topic_name>`, and a tunnel is created with both endpoints using that room and a label `<label_prefix>: <topic_name>` (mempalace/palace_graph.py:L807-L821). Returns the list of created tunnel dicts (mempalace/palace_graph.py:L799-L822).

### `topic_tunnels_for_wing(wing, topics_by_wing, min_count=1, label_prefix="shared topic")`

Computes topic tunnels involving only one wing, used for incremental updates after a single wing finishes mining (mempalace/palace_graph.py:L825-L836). Returns an empty list if `topics_by_wing` is empty/null or `wing` is non-string/empty (mempalace/palace_graph.py:L837-L838). The wing argument is canonicalized via wing-name normalization; its topic list is looked up directly, falling back to scanning all entries for one whose normalized key matches; if no usable list is found the result is an empty list (mempalace/palace_graph.py:L840-L854). For each other wing (skipping non-string/empty keys, the wing itself by normalized comparison, and empty topic lists), a 2-wing slice is fed to `compute_topic_tunnels` and the results accumulated (mempalace/palace_graph.py:L855-L876).

## Entity Tunnels

### `entity_tunnels_for_wing(wing, hallways, label_prefix="shared entity")`

Computes entity tunnels involving a single wing. An entity tunnel bridges two wings when the same entity appears in within-wing hallway records of both; it shares storage, dedup, and listing with explicit/topic tunnels via `create_tunnel` (mempalace/palace_graph.py:L879-L900). Endpoints use the synthetic room id `entity:<name>` to avoid collisions with literal rooms; entity casing is preserved (mempalace/palace_graph.py:L894-L896).

Returns an empty list if `hallways` is empty/null or `wing` is non-string/empty (mempalace/palace_graph.py:L901-L902). The wing is normalized once (mempalace/palace_graph.py:L904). It builds a map of `entity -> {normalized_wing -> display_wing}` from hallway records: non-dict hallways are skipped, hallways with a non-string/empty `wing` are skipped, and both `entity_a` and `entity_b` positions count (the pair is unordered); non-string/empty entity values are skipped, and the first-seen display wing form is preserved (mempalace/palace_graph.py:L906-L923). If no entities are found, returns an empty list (mempalace/palace_graph.py:L925-L926).

Entities are processed in sorted order for deterministic output. An entity not present in this wing is skipped (so an entity living only in this wing produces zero tunnels) (mempalace/palace_graph.py:L928-L938). For each other wing (sorted, excluding this wing), a tunnel is created with room `entity:<entity>`, source as this wing's display name, target as the other wing's display name, label `<label_prefix>: <entity>`, and `kind="entity"`; results are accumulated and returned (mempalace/palace_graph.py:L939-L951).

## Side Effects Summary

- Reads storage-backend (collection) metadata and documents for graph building, room-existence checks, and drawer-preview hydration (mempalace/palace_graph.py:L118-L148, L464-L485, L688-L702).
- Reads and writes a JSON tunnels file derived from `palace_path`, with atomic temp-file-rename writes and `0o700`/`0o600` permission hardening (mempalace/palace_graph.py:L373-L434).
- Acquires a file-path-keyed lock around tunnel mutate cycles (`create_tunnel`, `delete_tunnel`) (mempalace/palace_graph.py:L574-L577, L631-L636).
- Emits warning/debug log records to the `mempalace_graph` logger for corrupt files, legacy-file detection, empty filter results, and hydration failures (mempalace/palace_graph.py:L37, L282-L289, L379-L394, L685-L686, L700).
