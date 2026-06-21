# pgvector Backend — Behavior Specification

Source: `mempalace/backends/pgvector.py`

A Postgres + pgvector storage backend implementing the `BaseBackend` / `BaseCollection`
storage contract. It is opt-in and external: it only runs when the user explicitly selects
`pgvector` via config, environment, or CLI/MCP flag; embeddings are produced locally and only
the resulting vectors are written to Postgres (mempalace/backends/pgvector.py:L1-L23). The live
client requires an optional Postgres driver dependency, imported lazily so the module imports
even without it (mempalace/backends/pgvector.py:L19-L23, L508-L515).

## Module Constants and Contracts

- Default connection string is `postgresql://localhost:5432/mempalace` (mempalace/backends/pgvector.py:L63).
- The per-palace on-disk marker file is named `pgvector_backend.json` (mempalace/backends/pgvector.py:L64).
- Postgres identifiers are clamped to a 63-byte limit (mempalace/backends/pgvector.py:L65).
- Lexical tokenization matches runs of 2 or more word characters, case-insensitive/Unicode (mempalace/backends/pgvector.py:L66, L120-L123).
- Supported metadata-filter operators: `$eq`, `$ne`, `$in`, `$nin`, `$and`, `$or`, `$contains`, `$gt`, `$gte`, `$lt`, `$lte`. Any operator key (starting with `$`) outside this set raises an unsupported-filter error (mempalace/backends/pgvector.py:L70-L72, L162-L177).
- Only `$eq`, `$ne`, `$in`, `$nin`, `$and` are pushed down to SQL; all others are evaluated locally (mempalace/backends/pgvector.py:L73, L260-L289).

## Backend: `PgVectorBackend`

- Backend name is `pgvector` (mempalace/backends/pgvector.py:L1198).
- Advertised capabilities: requires explicit embeddings, supports embeddings in/passthrough/out, metadata filters, lexical search, namespace isolation, server-side indexes, and server mode (mempalace/backends/pgvector.py:L1199-L1211).
- Supported maintenance kinds are exactly `analyze` and `reindex`; `compact` is intentionally omitted (mempalace/backends/pgvector.py:L1212-L1216).
- `detect(path)` returns true when `pgvector_backend.json` exists in the given directory (mempalace/backends/pgvector.py:L1486-L1488).

### Configuration resolution

Connection string is resolved in priority order: options `dsn`, options `url`, env
`MEMPALACE_PGVECTOR_DSN`, config attribute `pgvector_dsn`, then the default; a blank result
falls back to the default (mempalace/backends/pgvector.py:L481-L496). Namespace is resolved from
options `namespace`, env `MEMPALACE_PGVECTOR_NAMESPACE`, then config attribute
`pgvector_namespace`; blank becomes none (mempalace/backends/pgvector.py:L488-L496).

### Table naming and isolation (RFC 001)

Isolation is one table per namespace + palace + collection (mempalace/backends/pgvector.py:L14-L17).
The table prefix is `mempalace`, optionally followed by a slugged namespace, followed by the
first 16 hex chars of a SHA-256 of the palace id, joined by underscores
(mempalace/backends/pgvector.py:L1227-L1240). The final table name appends a slugged collection
name and is clamped to the 63-byte identifier limit (hashing the overflow)
(mempalace/backends/pgvector.py:L1242-L1250, L381-L386). When the palace carries its own
namespace it overrides the config namespace for naming (mempalace/backends/pgvector.py:L1245-L1248).
Slugs replace non-`[A-Za-z0-9_]` runs with `_`; slugs longer than 48 chars are truncated to 35
and suffixed with a 12-char SHA-256 digest (mempalace/backends/pgvector.py:L372-L378).

### Opening collections: `get_collection` / `create_collection` / `get_or_create_collection`

Accepts either keyword `palace=` (a palace reference) with `collection_name`, `create`,
`options`, or positional/`palace_path=` forms that build a palace reference from a path; invalid
or extra arguments raise a type error (mempalace/backends/pgvector.py:L1403-L1446). `create_collection`
and `get_or_create_collection` both call open with create enabled (mempalace/backends/pgvector.py:L1490-L1494).

Marker protection rules when opening (mempalace/backends/pgvector.py:L1365-L1401):
- A pgvector palace MUST have a local path; opening with no local path raises a backend error
  because mismatch protection cannot be anchored (mempalace/backends/pgvector.py:L1377-L1387).
- If the marker file exists, the marker target is validated against current configuration
  (mempalace/backends/pgvector.py:L1371-L1374).
- If the marker file is absent and create is false, a palace-not-found error is raised
  (mempalace/backends/pgvector.py:L1375-L1376).
- If create is false and the backing table does not exist, a collection-not-initialized error is
  raised (mempalace/backends/pgvector.py:L1389-L1390).
- Each opened collection is tracked per palace id for later close (mempalace/backends/pgvector.py:L1399-L1401).

### Marker file format and validation (on-disk contract)

The marker is JSON written with 2-space indent (mempalace/backends/pgvector.py:L1327-L1328) at
`<palace_local_path>/pgvector_backend.json` (mempalace/backends/pgvector.py:L1227-L1229). On write,
the palace directory is created and chmod `0700`, the marker file chmod `0600` (best-effort)
(mempalace/backends/pgvector.py:L1311-L1332). The marker object contains: `backend` = `pgvector`,
`schema_version` = 1, `created_at` = current UTC ISO timestamp, `palace_id`, and a `pgvector`
target object (mempalace/backends/pgvector.py:L1319-L1325). The target object contains sanitized
DSN fields `host`, `port` (default 5432), `dbname`, plus `namespace`, `palace_hash` (16 hex of
SHA-256 of palace id), and `table_prefix` (mempalace/backends/pgvector.py:L1252-L1272, L1231-L1233).

Validation reads the marker; an unreadable or malformed marker file raises a backend-mismatch
error (mempalace/backends/pgvector.py:L1277-L1288). A marker whose `backend` is not `pgvector`, or
whose `pgvector` target is missing/not an object, raises a mismatch error
(mempalace/backends/pgvector.py:L1290-L1299). Any target field that differs from the expected
current configuration raises a mismatch error listing the mismatched keys
(mempalace/backends/pgvector.py:L1300-L1309).

### Embedder identity sidecar

Embedder identity is stored in a separate sidecar file (not the marker), so it can be recorded on
a brand-new palace before the marker exists; sidecar path is
`<palace_local_path>/<EMBEDDER_SIDECAR_FILENAME>` or none when there is no local path
(mempalace/backends/pgvector.py:L1334-L1349). Reads/writes delegate to the shared sidecar
read/write helpers keyed by collection name (mempalace/backends/pgvector.py:L1345-L1349, L812-L818).

### Client lifecycle, close, health

One client instance is cached per configuration; obtaining a client after the backend is closed
raises a backend-closed error (mempalace/backends/pgvector.py:L1352-L1363). `close_palace` closes
and removes all collections tracked for that palace (mempalace/backends/pgvector.py:L1448-L1453).
`close` closes all collections and clients and marks the backend closed
(mempalace/backends/pgvector.py:L1455-L1469). `health(palace)`: returns unhealthy if closed; pings
the database (unhealthy on failure); if a palace with a local path is given but its marker file is
absent, returns unhealthy; otherwise healthy (mempalace/backends/pgvector.py:L1471-L1484).
`delete_collection` drops the backing table for the named collection
(mempalace/backends/pgvector.py:L1496-L1500).

## Database Client Behavior (`_PgVectorClient`)

The driver is imported lazily on first connect; if absent, a backend error instructs installing
the optional extra (mempalace/backends/pgvector.py:L508-L515). A connection failure raises a
backend error wrapping the driver error (mempalace/backends/pgvector.py:L528-L532). All SQL
executes under a re-entrant lock so a single shared connection is serialized across threads; on
any query failure the transaction is rolled back (best-effort) and the error is normalized to a
backend error; on success the transaction is committed (mempalace/backends/pgvector.py:L534-L552).
A closed client refuses to reconnect (mempalace/backends/pgvector.py:L523-L525, L746-L758).

Table schema created by `create_table` (mempalace/backends/pgvector.py:L596-L606): columns are
`id text PRIMARY KEY`, `document text NOT NULL DEFAULT ''`, `metadata jsonb NOT NULL DEFAULT
'{}'`, `embedding vector(<dimension>)`, and `updated_at timestamptz`. The vector extension is
ensured first via create-extension-if-not-exists, with failures swallowed (the later table create
fails loudly if the vector type is truly absent) (mempalace/backends/pgvector.py:L557-L564, L596-L597).
`table_dimension` reads the declared vector dimension via the canonical type formatting, returning
none when unavailable (mempalace/backends/pgvector.py:L574-L594).

### Write path NUL and surrogate stripping (ingest robustness contract)

Postgres cannot store NUL (0x00) in text or jsonb. On upsert, every row's id, document, and
serialized metadata is stripped of NUL bytes (recursively for metadata dict keys/values/lists)
and of lone Unicode surrogates before binding, so a single stray byte in a transcript cannot abort
a whole mine run (mempalace/backends/pgvector.py:L84-L117, L608-L641). Stripping order matters:
NUL is stripped before JSON serialization and lone surrogates after serialization
(mempalace/backends/pgvector.py:L619-L636). Non-string scalars pass through unchanged; stripping is
not injective, so two ids/keys differing only by a stripped byte collapse (last wins), which does
not occur in practice (mempalace/backends/pgvector.py:L96-L107). ChromaDB and the SQLite backend
store these bytes verbatim; stripping happens only in this backend
(mempalace/backends/pgvector.py:L92-L94).

Upsert is an INSERT … ON CONFLICT(id) DO UPDATE that overwrites document, metadata, embedding, and
updated_at; empty input is a no-op; missing `updated_at` defaults to the current UTC ISO timestamp
(mempalace/backends/pgvector.py:L608-L641, L76-L77).

### Vector serialization contract

Vectors are sent to Postgres as the pgvector text literal `[v1,v2,...]` (avoids needing a
client-side vector adapter) (mempalace/backends/pgvector.py:L348-L355). On read, a vector value is
parsed from list/tuple or from the bracketed comma-separated text form into a list of floats; empty
yields an empty list, none yields none (mempalace/backends/pgvector.py:L358-L369).

### Query, scroll, delete, count (SQL paths)

`query_rows`: selects `id, document, metadata` (plus `embedding` if requested) and `embedding <=>
%s::vector AS distance`, applies the pushdown WHERE predicate, orders by ascending distance, and
limits; bound-parameter order is distance vector, WHERE params, then limit
(mempalace/backends/pgvector.py:L643-L669). `scroll_rows` selects all matching rows without ordering
or limit (mempalace/backends/pgvector.py:L671-L689). `delete_rows` deletes by id list (`id =
ANY(...)`) when ids given, otherwise by the WHERE predicate (mempalace/backends/pgvector.py:L691-L704).
`count_rows` returns the row count (mempalace/backends/pgvector.py:L706-L708). `drop_table` drops the
table if it exists (mempalace/backends/pgvector.py:L710-L711).

Row decoding normalizes id to string, null document to empty string, metadata to a dict (parsing
JSON text if needed), embedding to a parsed vector (when requested), and distance to a float when
requested (mempalace/backends/pgvector.py:L760-L778).

### SQL filter translation (pushdown)

Pushdown filters translate to JSONB containment predicates using the `@>` operator
(mempalace/backends/pgvector.py:L417-L444): `$eq` → contains; `$ne` → NOT contains; `$in` → OR of
contains (or FALSE when empty); `$nin` → NOT (OR of contains); a bare field value → contains;
`$and` recurses; an empty filter yields the literal `TRUE`
(mempalace/backends/pgvector.py:L447-L464). Metadata is serialized for containment with compact,
sorted-key, non-ASCII-preserving JSON (mempalace/backends/pgvector.py:L80-L81).

### Maintenance (RFC 001)

`has_vector_index` checks for an HNSW index on the table (mempalace/backends/pgvector.py:L716-L723).
HNSW index name is `<table>_hnsw_idx`, clamped/hashed to fit the identifier limit to avoid colliding
with the table name (mempalace/backends/pgvector.py:L406-L414, L732-L741). The index is built with
`USING hnsw (embedding vector_cosine_ops)` (mempalace/backends/pgvector.py:L732-L741). `analyze_table`
runs ANALYZE (mempalace/backends/pgvector.py:L743-L744). Index builds are serialized across writers
using a session advisory lock with a fixed class id (the constant `0x4D454D50`, ASCII "MEMP") and a
table-derived signed-int4 object id (mempalace/backends/pgvector.py:L393-L403, L725-L730).

## Collection Behavior (`PgVectorCollection`)

### Table materialization and dimension enforcement

`_ensure_table` rejects non-positive dimension; creates the table on first write at the given
dimension; if the table already exists with a different declared dimension, raises a
dimension-mismatch error; caches the known dimension (mempalace/backends/pgvector.py:L820-L842).
A query with a vector whose size differs from the table's known dimension raises a
dimension-mismatch error (mempalace/backends/pgvector.py:L1026-L1032). A write batch mixing
embedding dimensions raises a dimension-mismatch error (mempalace/backends/pgvector.py:L315-L326).
Each embedding must be a non-empty 1-D vector (mempalace/backends/pgvector.py:L308-L312).

### Closed/uninitialized guards

Operations on a closed collection or closed backend raise a backend-closed error
(mempalace/backends/pgvector.py:L802-L804). When the backing table is absent: if the marker exists,
read operations (`_scroll`, `query`, `count`, `delete`, `maintenance`) raise a
collection-not-initialized error; if no marker exists they return empty/no-op
(mempalace/backends/pgvector.py:L844-L850, L1010-L1017, L1086-L1108, L1170-L1171).

### `add`

Validates equal-length batch arrays; embeddings are mandatory; ids must be unique within the batch;
if any id already exists in the collection, raises a value error; otherwise delegates to upsert
(mempalace/backends/pgvector.py:L292-L306, L873-L884).

### `upsert`

Validates batch lengths; embeddings are mandatory; normalizes vectors and derives the dimension;
ensures the table; metadata defaults to empty dicts; builds rows with stringified id/document,
JSON-able metadata, vector, and current-UTC `updated_at`; upserts; then writes the marker file
(mempalace/backends/pgvector.py:L886-L906). Metadata that cannot be JSON-serialized becomes an
empty dict (mempalace/backends/pgvector.py:L329-L334).

### `update`

Requires at least one of documents/metadatas/embeddings; mismatched array lengths raise a value
error; fetches existing rows, skips ids that do not exist; for each surviving id, keeps prior
values where the field is omitted and merges metadata (provided keys override prior), then upserts
the changed rows (mempalace/backends/pgvector.py:L908-L940).

### `query`

`query_texts` is rejected (callers must pass embeddings via the palace wrapper); embeddings are
required and must be a non-empty list; filters are validated
(mempalace/backends/pgvector.py:L982-L1001). If the filter cannot be fully pushed down (any
`where_document`, `$or`, `$contains`, comparison operators, etc.), the query runs the local exact
path: scroll all rows with embeddings, apply local metadata and document filters, compute cosine
distance per query vector, sort ascending, and take the top-`n_results`
(mempalace/backends/pgvector.py:L1002-L1009, L942-L980, L260-L289). Otherwise it runs the SQL path
per query vector with pushdown WHERE (mempalace/backends/pgvector.py:L1018-L1056). The result is a
per-query nested structure of ids, and (when requested via include) documents, metadatas,
distances, and embeddings; distances default to being included
(mempalace/backends/pgvector.py:L945, L1018, L1050-L1056). On the SQL path a null distance is
reported as `1.0` (mempalace/backends/pgvector.py:L1043-L1047). Cosine distance is `1 - cosine
similarity` clamped to `[-1, 1]`, and is none when a stored vector is missing or has a different
size (mempalace/backends/pgvector.py:L337-L345).

### `get`

Fetches rows by ids and/or filters; pushable filters go to SQL while non-pushable ones (and id
membership, full where, and where_document) are re-applied locally; when ids are given the result
preserves the requested id order and drops missing ids; `offset` then `limit` slicing is applied;
returns ids and (per include) documents, metadatas, embeddings; distances are not included by
default for get (mempalace/backends/pgvector.py:L1058-L1084, L852-L871).

### `delete`

Validates filters; if the table is absent, behaves per the uninitialized guard
(mempalace/backends/pgvector.py:L1086-L1091). With only ids, deletes by id list; with only a fully
pushable filter, deletes by SQL predicate; otherwise resolves matching rows locally and deletes
them by id (mempalace/backends/pgvector.py:L1092-L1100).

### `count`

Returns the table row count, subject to the closed and uninitialized guards
(mempalace/backends/pgvector.py:L1102-L1108).

### `lexical_search`

Validates filters; scrolls rows applying pushable filters in SQL and the rest locally; computes
BM25 scores (k1 = 1.5, b = 0.75) over the documents; keeps only positive-score hits; sorts by
descending score; returns the top-`n_results` as lexical hits each carrying id, document, metadata,
and score (mempalace/backends/pgvector.py:L1110-L1127, L126-L159). BM25 returns all-zero scores
when the query has no terms, there are no documents, or all documents are empty
(mempalace/backends/pgvector.py:L127-L136).

### `health`, `maintenance_state`, `run_maintenance`

`health`: unhealthy if closed; unhealthy if the table is not found or probing raises; otherwise
healthy (mempalace/backends/pgvector.py:L1132-L1140). `maintenance_state` returns `row_count`,
`vector_index` (`hnsw` or none), and `index_build_complete`; any probe failure returns the empty
default `{row_count: 0, vector_index: null, index_build_complete: false}`
(mempalace/backends/pgvector.py:L1142-L1157). `run_maintenance(kind)`: rejects unsupported kinds;
returns a `noop` result with reason "no table" when the table is not yet materialized; `analyze`
runs ANALYZE and returns status `ran` (mempalace/backends/pgvector.py:L1159-L1174). `reindex` builds
the optional HNSW index: it is a `noop` if the index already exists; otherwise it acquires the
session advisory lock, returning status `already_running` if the lock is held by another session;
under the lock it re-checks for the index (noop if it appeared), builds the HNSW index returning
status `ran`, and always releases the lock in a finally block
(mempalace/backends/pgvector.py:L1176-L1194). Building the HNSW index makes search approximate; the
exact `<=>` scan is the 100%-recall default (mempalace/backends/pgvector.py:L1176-L1180, L1212-L1216).
