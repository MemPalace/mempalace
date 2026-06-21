# Behavior Spec: Backend Maintenance Hooks (RFC 001)

Source: `tests/test_maintenance_hooks.py`

This file is a test suite asserting the observable contract of backend
maintenance operations. Maintenance is observable rather than fire-and-forget:
a maintenance run returns a structured result and must serialize concurrent
runs of the same kind (tests/test_maintenance_hooks.py:L1-L7).

## Public Surface Under Test

The contract is exercised through these symbols imported from the storage
backend base layer: `BaseCollection`, `MaintenanceResult`, `PalaceRef`, and
`UnsupportedMaintenanceKindError` (tests/test_maintenance_hooks.py:L11-L16).

## MaintenanceResult value object

`MaintenanceResult` carries three observable fields: `kind` (a string naming the
maintenance kind), `status` (a string state), and `stats` (a map of metrics)
(tests/test_maintenance_hooks.py:L24-L26). The `stats` field defaults to an
empty map when not supplied at construction
(tests/test_maintenance_hooks.py:L27).

## BaseCollection default behavior

A collection that does not declare any maintenance support reports an empty
maintenance state (an empty map) (tests/test_maintenance_hooks.py:L40-L41). Any
attempt to run a maintenance kind on such a default collection raises
`UnsupportedMaintenanceKindError` (tests/test_maintenance_hooks.py:L42-L43).

A `BaseCollection` implementation exposes the required data operations `add`,
`upsert`, `query`, `get`, `delete`, and `count` (tests/test_maintenance_hooks.py:L31-L38).

## Declared maintenance kinds per backend

Each backend type declares a fixed set of supported maintenance kinds as a class
attribute `maintenance_kinds` (a set of strings)
(tests/test_maintenance_hooks.py:L46-L56):

- SQLite-exact backend supports exactly `{"analyze", "compact"}`
  (tests/test_maintenance_hooks.py:L52).
- pgvector backend supports exactly `{"analyze", "reindex"}`
  (tests/test_maintenance_hooks.py:L53).
- Qdrant backend declares an empty set because it self-optimizes
  (tests/test_maintenance_hooks.py:L54-L55).
- Chroma backend declares an empty set because its maintenance is handled by a
  separate repair CLI (tests/test_maintenance_hooks.py:L54-L56).

## SQLite-exact backend maintenance (real backend)

A SQLite-exact collection is obtained from the backend by referencing a palace
(`PalaceRef` with `id` and `local_path`), a collection name
`"mempalace_drawers"`, and a create flag; rows are added with parallel arrays of
`documents`, `ids`, `metadatas`, and `embeddings` (a 4-dimensional float vector
per row) (tests/test_maintenance_hooks.py:L64-L79).

### Maintenance state

The maintenance state reports `row_count` equal to the number of stored rows
(tests/test_maintenance_hooks.py:L82-L85). It reports `vector_index` as null/absent
because the SQLite-exact backend performs an exact scan with no approximate
nearest-neighbor index (tests/test_maintenance_hooks.py:L86). The state also
includes `page_count` and `freelist_pages` keys
(tests/test_maintenance_hooks.py:L87).

### analyze

Running maintenance kind `"analyze"` returns a result whose `kind` is
`"analyze"` and whose `status` is `"ran"`
(tests/test_maintenance_hooks.py:L90-L93).

### compact

Running maintenance kind `"compact"` returns a result whose `kind` is
`"compact"` and whose `status` is `"ran"`, and whose `stats` map includes a
`pages_reclaimed` key (tests/test_maintenance_hooks.py:L96-L101). The test
deletes 20 of 30 rows before compacting, establishing that compaction reports
reclaimable pages (tests/test_maintenance_hooks.py:L97-L101).

### reindex omitted

The SQLite-exact backend has no ANN index, so `"reindex"` is omitted rather than
treated as a no-op: requesting it raises `UnsupportedMaintenanceKindError`
(tests/test_maintenance_hooks.py:L104-L108).

### unknown kind

Requesting an unrecognized kind such as `"bogus"` raises
`UnsupportedMaintenanceKindError` (tests/test_maintenance_hooks.py:L111-L114).

## pgvector backend maintenance (advisory-lock reindex flow)

The pgvector flow is tested against a fake client implementing the client
contract surface: `table_exists(table)`, `count_rows(table)`,
`has_vector_index(table)`, `try_advisory_lock(classid, objid)`,
`advisory_unlock(classid, objid)`, `create_hnsw_index(table)`, and
`analyze_table(table)` (tests/test_maintenance_hooks.py:L122-L153). The fake
advisory lock is a single boolean: `try_advisory_lock` returns false if already
locked, otherwise sets locked true and returns true; `advisory_unlock` clears it
(tests/test_maintenance_hooks.py:L138-L145).

A pgvector collection is constructed with a backend, the client, a config (DSN
and optional namespace), a `PalaceRef`, a collection name `"mempalace_drawers"`,
and a physical table name (tests/test_maintenance_hooks.py:L159-L169).

### reindex builds index under lock

When no vector index exists, running `"reindex"` returns `status == "ran"` with
`stats["vector_index"] == "hnsw"`, having created exactly one index, and the
advisory lock is released afterward (released in a finally path), so the lock is
not held on return (tests/test_maintenance_hooks.py:L172-L178).

### reindex no-op when index exists

When a vector index already exists, running `"reindex"` returns
`status == "noop"` and never attempts to build an index (create count remains 0)
(tests/test_maintenance_hooks.py:L181-L186).

### reindex already-running when lock held

When the advisory lock is already held by another session, running `"reindex"`
returns `status == "already_running"` and does not trigger a build (create count
remains 0) (tests/test_maintenance_hooks.py:L189-L195). This is the
serialization guarantee for concurrent same-kind runs.

### analyze

Running `"analyze"` returns `status == "ran"` and invokes the client's table
analyze operation exactly once (tests/test_maintenance_hooks.py:L198-L202).

### unknown / unsupported kind

Running `"compact"` against pgvector raises
`UnsupportedMaintenanceKindError` because pgvector omits compaction (relying on
autovacuum) (tests/test_maintenance_hooks.py:L205-L208).

### maintenance state reports index

When a vector index exists, the maintenance state reports `row_count` from the
client row count, `vector_index == "hnsw"`, and `index_build_complete == true`
(tests/test_maintenance_hooks.py:L211-L215).

### maintenance no-op when table missing

If the underlying table does not yet exist (collection opened with create but
never written), maintenance must no-op rather than letting a raw "relation does
not exist" error escape: `"reindex"` returns `status == "noop"`, `"analyze"`
returns `status == "noop"`, and the maintenance state reports `row_count == 0`
(tests/test_maintenance_hooks.py:L218-L226).

## pgvector identifier and advisory-key invariants

### HNSW index name

The HNSW index name derived from a table name must never equal the table name
itself (to avoid collisions in the system catalog), and its UTF-8 byte length
must be at most 63 bytes, for table names of any length including short, medium,
exactly-63-character, and 200-character inputs
(tests/test_maintenance_hooks.py:L229-L237). A naive 63-byte truncation would
return a 63-character table name verbatim and collide; the overflow is hashed
instead (tests/test_maintenance_hooks.py:L230-L232).

### Advisory lock object id

The advisory lock object id derived from a table name must fit within a signed
32-bit integer range (at least -2^31 and strictly less than 2^31) and must be
stable across repeated computation for the same table, for table names of
varying length (tests/test_maintenance_hooks.py:L240-L246). The maintenance lock
class id constant is likewise within the signed 32-bit integer range
(tests/test_maintenance_hooks.py:L247).

## EmbeddingCollection delegation

A collection wrapper (`EmbeddingCollection`) wrapping an inner collection
delegates maintenance to the inner collection: its reported maintenance state
equals the inner collection's state, and running a maintenance kind returns the
inner collection's result (e.g. `status == "ran"`)
(tests/test_maintenance_hooks.py:L255-L275).
