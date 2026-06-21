# Behavior Spec: pgvector storage backend (derived from `tests/test_pgvector_backend.py`)

This spec describes the externally observable contract of the `pgvector` storage backend
as pinned by its test suite. The backend stores drawer records (id + document text +
metadata + embedding vector) in a Postgres `pgvector` table, one table per palace, with a
local on-disk marker file anchoring backend-identity protection.

The tests substitute an in-memory fake client for the real Postgres-backed client; the fake
reproduces the same Python-side filter and cosine-ranking semantics the real backend's
local-fallback path uses (tests/test_pgvector_backend.py:L31-L127). All behavior below is
therefore expressed in terms that any backend implementation must satisfy regardless of
language or driver.

## Registry and capabilities

- The backend registry exposes a backend named `"pgvector"` (tests/test_pgvector_backend.py:L146-L147).
- The backend advertises the capability `supports_namespace_isolation` (tests/test_pgvector_backend.py:L312-L315).

## Collection construction (`get_collection`)

- A collection is obtained from a backend instance given a palace reference (an id plus a
  local filesystem path) and a collection name, with `create=True` to provision the table
  (tests/test_pgvector_backend.py:L140-L143).
- `get_collection` accepts both a positional palace-path form (`get_collection(path, name, create=True)`)
  and a keyword `palace_path=` form (`get_collection(palace_path=path, collection_name=name, create=True)`);
  both forms produce working collections, and distinct paths produce distinct underlying tables
  (tests/test_pgvector_backend.py:L391-L401).
- Distinct palace references (distinct `PalaceRef.id`) map to distinct underlying tables even on
  the same backend instance and same DSN, which share a single client instance
  (tests/test_pgvector_backend.py:L299-L309).

### Local-path requirement (isolation anchor)

- A palace reference with no local path (`local_path=None`) is rejected: `get_collection` raises
  a `BackendError` whose message references `"local palace path"`. The local path is the only
  anchor for the marker file that provides mismatch protection, so a purely-remote palace is
  refused rather than opening an unprotected table (tests/test_pgvector_backend.py:L279-L286).

## On-disk marker file (`pgvector_backend.json`)

- The marker file lives at `<local_path>/pgvector_backend.json` (tests/test_pgvector_backend.py:L150-L152,L170).
- The marker is NOT written merely by creating a collection; it is written only on the first
  successful write of data (tests/test_pgvector_backend.py:L150-L171). Before any add, the file
  does not exist (tests/test_pgvector_backend.py:L152).
- After a successful first write, the marker file exists and `PgVectorBackend.detect(<local_path>)`
  returns truthy (tests/test_pgvector_backend.py:L169-L170).
- If the first write fails (the upsert raises), the marker file is NOT written; the original error
  propagates (tests/test_pgvector_backend.py:L198-L210).

### Marker participation in backend-mismatch resolution

- After data is written, `resolve_backend_name(<local_path>)` returns `"pgvector"`
  (tests/test_pgvector_backend.py:L253-L259).
- Requesting resolution with an explicit, different backend name (e.g. `explicit="qdrant"`) against
  a pgvector-marked palace raises `BackendMismatchError` (tests/test_pgvector_backend.py:L260-L261).
- Reopening a marked palace with a changed target DSN (`options={"dsn": ...}` different from the one
  recorded in the marker) raises `BackendMismatchError` (tests/test_pgvector_backend.py:L264-L276).
- If the marker file exists but is unreadable/corrupt (invalid JSON), reopening the collection raises
  `BackendMismatchError` (tests/test_pgvector_backend.py:L425-L433).

## Writing records (`add` / `upsert` / `update`)

- `add`/`upsert` take parallel arrays: `ids`, `documents`, `metadatas`, and `embeddings`. The
  embedding vectors must be supplied explicitly (tests/test_pgvector_backend.py:L154-L167).
- Explicit embeddings are mandatory: calling `add` without `embeddings` raises `ValueError` whose
  message references `"explicit embeddings"` (tests/test_pgvector_backend.py:L192-L195).
- `add` rejects duplicate ids within a single batch: an `ids` array containing the same id twice
  raises `ValueError` whose message references `"unique"` (tests/test_pgvector_backend.py:L220-L225).
- All embeddings in a collection must share one dimension. After a vector of dimension N is written,
  upserting a vector of a different dimension raises `DimensionMismatchError`
  (tests/test_pgvector_backend.py:L213-L217).
- After writes, `count()` returns the number of stored records (tests/test_pgvector_backend.py:L171).

### Update semantics

- `update(ids=[...], documents=[...], metadatas=[...])` replaces the document text and merges metadata:
  keys present in the update override existing keys, keys absent from the update are preserved
  (tests/test_pgvector_backend.py:L336-L348). Example: updating a record whose metadata is
  `{"wing": "x", "rank": 1}` with `{"rank": 9}` yields `{"wing": "x", "rank": 9}`
  (tests/test_pgvector_backend.py:L344-L348).
- Records not named in the update are left unchanged (tests/test_pgvector_backend.py:L349-L350).
- `update` with only `ids` and no document or metadata raises `ValueError` whose message references
  `"at least one"` (tests/test_pgvector_backend.py:L351-L352).

## Vector query (`query`)

- `query(query_embeddings=[vec], n_results=K, where=..., include=[...])` returns nearest records
  ranked by ascending vector distance (cosine). Results are nested per query: `result.ids[0]` is the
  list of ids for the first query embedding, `result.ids[0][0]` is the nearest
  (tests/test_pgvector_backend.py:L174-L182).
- `include` may request `documents`, `metadatas`, `distances`, and `embeddings`; when `embeddings`
  is included, `result.embeddings[0][0]` returns the stored vector for the top hit
  (tests/test_pgvector_backend.py:L177-L182).
- The query embedding dimension must match the collection's known dimension; a mismatched query
  vector raises `DimensionMismatchError` (tests/test_pgvector_backend.py:L384-L388).

## Filtering: pushdown vs local fallback

The `where` filter governs which records a query/get/delete sees. Two execution paths exist but are
observably equivalent in result set:

- Simple equality (`{"wing": "project"}`) and `$in` are pushed down (executed server-side); they
  return all matching records (tests/test_pgvector_backend.py:L173-L181).
- Complex filters — `$or`, `$contains`, and comparison operators (`$gte`, etc.) — route through the
  local exact-match path and must still return the correct rows
  (tests/test_pgvector_backend.py:L228-L250):
  - `{"$or": [{"wing": "x"}, {"wing": "z"}]}` returns records matching either branch
    (tests/test_pgvector_backend.py:L243-L244).
  - `{"tags": {"$contains": "sqlite"}}` returns records whose `tags` value contains the substring
    (tests/test_pgvector_backend.py:L246-L247).
  - A vector query with `where={"rank": {"$gte": 2}}` returns only records meeting the comparison
    (tests/test_pgvector_backend.py:L249-L250).

## Read (`get`) with paging

- `get(ids=[...], include=[...])` returns the named records' documents and metadata
  (tests/test_pgvector_backend.py:L345-L346).
- `get(where=..., limit=L, offset=O, include=[...])` supports paging: `limit` caps the number of
  returned ids and `offset` skips earlier matches; included `embeddings` come back with the stored
  vector dimension (tests/test_pgvector_backend.py:L355-L365).
- `get(ids=[...])` for an absent/deleted id returns an empty id list (tests/test_pgvector_backend.py:L491-L492).

## Lexical search (`lexical_search`)

- `lexical_search(query=..., n_results=K, where=...)` returns `.hits`, a list ordered by lexical
  relevance, each hit carrying an `id` (tests/test_pgvector_backend.py:L184-L185). For query
  `"rareterm backend"` over documents where one contains `rareterm` and another only `backend`, the
  more relevant document ranks first (`["b", "a"]`) (tests/test_pgvector_backend.py:L184-L185).

## Delete (`delete`)

- `delete(where={"wing": "y"})` (pushdown equality) removes matching records
  (tests/test_pgvector_backend.py:L376-L378).
- `delete(where={"$or": [...]})` (local-fallback path) removes records matching the complex filter
  (tests/test_pgvector_backend.py:L379-L381).
- `delete(ids=[...])` removes the named records (tests/test_pgvector_backend.py:L491-L492).

## Health, lifecycle, and closed-state errors

- A healthy initialized collection reports `health().ok == True`, and the backend reports
  `backend.health(palace).ok == True` (tests/test_pgvector_backend.py:L404-L410).
- After the underlying table is deleted but the marker remains, the collection is considered
  not-initialized: `health().ok` is `False` and `count()` raises `CollectionNotInitializedError`
  (tests/test_pgvector_backend.py:L289-L296).
- `backend.delete_collection(<path>, <name>)` drops the collection; afterward `health().ok` is `False`
  (tests/test_pgvector_backend.py:L404-L412).
- `backend.close_palace(<path>)` closes the palace; subsequent operations on its collection raise
  (tests/test_pgvector_backend.py:L187-L189).
- `backend.close()` marks the backend terminally closed; subsequent `get_collection` raises
  `BackendError` (tests/test_pgvector_backend.py:L415-L422).

## Cross-palace and namespace isolation

- Two palaces (distinct ids) on the same backend map to distinct tables, and a record written to one
  is invisible to the other (partition isolation) (tests/test_pgvector_backend.py:L299-L309).
- Namespace isolation: palace references carrying distinct `namespace` values map to distinct tables
  whose names embed the (sanitized) namespace (e.g. namespace `"tenant-a"` produces a table containing
  `tenant_a`). A record under one namespace is invisible under the other
  (tests/test_pgvector_backend.py:L312-L333).

## DSN/namespace configuration from environment

- The connection DSN and namespace may be supplied via environment variables
  `MEMPALACE_PGVECTOR_DSN` and `MEMPALACE_PGVECTOR_NAMESPACE`. Config resolution reads
  `dsn` from `MEMPALACE_PGVECTOR_DSN` and `namespace` from `MEMPALACE_PGVECTOR_NAMESPACE`
  (tests/test_pgvector_backend.py:L436-L443).

## Palace wrapper auto-embedding

- When the palace layer selects pgvector (via `MEMPALACE_BACKEND`/`MEMPALACE_BACKEND_EXPLICIT` set to
  `pgvector`), the wrapper auto-embeds document text so that `add(documents=..., ids=..., metadatas=...)`
  without explicit embeddings works, and a subsequent `query(query_texts=..., n_results=1)` returns the
  matching id (tests/test_pgvector_backend.py:L446-L459).

## Client connection concurrency and lifecycle

- First-connect race: two threads concurrently triggering the first connection must converge on a single
  shared connection — exactly one connection is created, both threads succeed without error, and the
  client's connection is that single connection (tests/test_pgvector_backend.py:L522-L600).
- `close()` is terminal for the client: it closes the underlying connection
  (tests/test_pgvector_backend.py:L601-L603,L651-L652), and any subsequent operation raises `BackendError`
  whose message references `"closed"` without creating a new connection
  (tests/test_pgvector_backend.py:L606-L656).

## Unstorable-byte sanitization on the write path

Postgres text/jsonb cannot store NUL (0x00) bytes and cannot encode lone UTF-16 surrogates; rather than
abort the ingest or drop the record, the write path sanitizes these bytes so the same inputs that other
backends accept remain ingestible (tests/test_pgvector_backend.py:L709-L718,L770-L779).

- NUL bytes (0x00) in id, document, and any metadata key/value are stripped before binding. After upsert,
  no NUL survives in the bound id, document, or serialized metadata JSON; surrounding content is otherwise
  preserved. Example: id `"draw\x00er"` -> `"drawer"`, document `"before\x00after"` -> `"beforeafter"`,
  metadata `{"go\x00od": "v\x00w", "nested": ["a\x00b", 7]}` -> `{"good": "vw", "nested": ["ab", 7]}`
  (tests/test_pgvector_backend.py:L709-L744).
- Lone UTF-16 surrogates in id, document, and metadata are replaced with U+FFFD (one surrogate -> one
  replacement char), not dropped. After upsert every bound field is UTF-8 encodable; surrounding content
  is preserved (tests/test_pgvector_backend.py:L770-L810).
- A single record carrying both a NUL and a lone surrogate comes out clean on every text-bound field:
  NUL dropped, surrogate -> U+FFFD, surrounding content preserved
  (tests/test_pgvector_backend.py:L813-L851).
- Each upsert binds exactly one row's params per input record; the bound tuple order is
  `(id, document, metadata_json, ...)` where `metadata_json` is the serialized metadata
  (tests/test_pgvector_backend.py:L733-L744).

### `_strip_nul` helper contract

- `_strip_nul` removes NUL from strings and recurses into list/tuple items and dict keys and values:
  - `"a\x00b"` -> `"ab"`; NUL-free strings unchanged; `""` -> `""`; `"\x00"` -> `""`
    (tests/test_pgvector_backend.py:L750-L753).
  - Nested dicts/lists are fully stripped on keys, values, and items
    (tests/test_pgvector_backend.py:L754-L756).
  - Tuples recurse and remain tuples (tests/test_pgvector_backend.py:L757-L759).
  - Two keys differing only by a NUL collapse to one; the last value wins
    (tests/test_pgvector_backend.py:L760-L762).
  - Non-string scalars pass through unchanged and keep their type (`7`, `3.5`, `True`, `None`)
    (tests/test_pgvector_backend.py:L763-L767).

### Surrogate stripping via shared config utility

- Surrogate replacement on the write path uses the shared `config.strip_lone_surrogates` over the id,
  document, and the serialized metadata JSON (no pgvector-local helper). Because metadata JSON serialization
  leaves a surrogate raw in the string (non-ASCII-escaped), a single pass over the serialized JSON cleans it:
  the surrogate disappears and the parsed JSON shows U+FFFD in its place
  (tests/test_pgvector_backend.py:L854-L866).

## Live Postgres round-trip (opt-in)

When `MEMPALACE_PGVECTOR_LIVE_URL` is set, an end-to-end contract holds against real Postgres
(tests/test_pgvector_backend.py:L462-L519): after upserting two records, `detect(<path>)` is truthy and
`count()==2` (tests/test_pgvector_backend.py:L482-L483); a filtered vector query returns the matching id
(tests/test_pgvector_backend.py:L485-L486); lexical search returns the expected hit
(tests/test_pgvector_backend.py:L488-L489); delete-by-id removes the record
(tests/test_pgvector_backend.py:L491-L492). Reopening the existing table in a fresh backend with
`create=False` and writing another same-dimension vector must NOT falsely raise
`DimensionMismatchError` — the stored column dimension must be read back correctly so reopen succeeds
(tests/test_pgvector_backend.py:L494-L513). Cleanup deletes the collection regardless of outcome
(tests/test_pgvector_backend.py:L514-L519).
