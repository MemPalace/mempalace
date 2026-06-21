# Behavior Spec: Qdrant Backend (derived from `tests/test_qdrant_backend.py`)

This spec describes the externally observable behavior of the Qdrant storage backend
as asserted by its conformance tests. It is implementation-language agnostic. All
claims cite the test file that pins the behavior; the tests are the ground-truth
contract a reimplementation must satisfy.

## Scope and test harness

The tests exercise a `QdrantBackend` against a fake in-memory REST client substituted
for the real `_QdrantRESTClient` symbol in the qdrant backend module
(`tests/test_qdrant_backend.py:L180-L190`). Each test resets the fake client's
recorded instances and clears the env vars `MEMPALACE_QDRANT_URL`,
`MEMPALACE_QDRANT_API_KEY`, `MEMPALACE_QDRANT_NAMESPACE`, and
`MEMPALACE_QDRANT_TIMEOUT` before running, meaning these env vars influence backend
configuration when present (`tests/test_qdrant_backend.py:L186-L189`).

A collection is obtained via `backend.get_collection(palace=PalaceRef(...),
collection_name=..., create=True)`, where `PalaceRef` carries `id`, `local_path`, and
optional `namespace` (`tests/test_qdrant_backend.py:L193-L196`).

### Fake client contract (the wire/storage shape the backend depends on)

The fake client implements the operations the backend calls, defining the expected
remote API surface: `collection_exists`, `get_collection_info` (returns vector `size`
and `distance: "Cosine"`), `create_collection(collection, dimension)`,
`create_payload_index(collection, field_name, field_schema)`, `upsert_points`,
`query_points`, `scroll_points`, `delete_points`, `count_points`, and
`delete_collection` (`tests/test_qdrant_backend.py:L77-L177`). Points are stored as
records with `id`, `vector`, and `payload`; payload keys may be dotted paths
(`tests/test_qdrant_backend.py:L20-L26`, `L120-L121`).

Server-side filter semantics the backend relies on: a filter has `must`, `must_not`,
`should` clauses; `must` requires all conditions, `must_not` requires none, `should`
requires at least one when present (`tests/test_qdrant_backend.py:L62-L74`). A
condition may be a nested filter, a `has_id` set membership, or a keyed condition
matching `match.value` (equality), `match.any` (set membership), `match.text_any`
(token-substring OR over lowercased text), or a numeric `range` with
`gt`/`gte`/`lt`/`lte` bounds (`tests/test_qdrant_backend.py:L29-L59`). Query results
are scored by cosine similarity and returned sorted descending, truncated to `limit`
(`tests/test_qdrant_backend.py:L123-L138`). Scroll returns a page plus a next-offset
cursor (`tests/test_qdrant_backend.py:L140-L161`).

## Registry

The backend registry must expose `"qdrant"` among `available_backends()`
(`tests/test_qdrant_backend.py:L199-L200`). The backend advertises
`"supports_namespace_isolation"` in its `capabilities`
(`tests/test_qdrant_backend.py:L466`).

## On-disk marker file contract

The backend persists a local marker file named `qdrant_backend.json` inside the
palace local path. The marker must NOT exist immediately after `get_collection(...)`
with `create=True` if no write has occurred yet
(`tests/test_qdrant_backend.py:L205`). It IS written upon the first successful write
of points (`tests/test_qdrant_backend.py:L222-L223`). After data exists,
`QdrantBackend.detect(path)` returns truthy (`tests/test_qdrant_backend.py:L222`,
`L510`).

The marker is written only on a successful first write: if the first upsert fails
(remote raises), the marker must not be created (`tests/test_qdrant_backend.py:L245-L257`).
Likewise, if a batch is rejected for duplicate ids before any write, the marker must
not be created (`tests/test_qdrant_backend.py:L345-L356`).

The marker participates in backend-mismatch detection. After writing and closing, if
a competing `chroma.sqlite3` file is present and the explicit backend env
(`MEMPALACE_BACKEND_EXPLICIT`) is set to `chroma`, `resolve_backend_name(path)` must
raise `BackendMismatchError` (`tests/test_qdrant_backend.py:L359-L369`).

The marker also records the remote target. If a marker exists for one remote URL and
a different `MEMPALACE_QDRANT_URL` is configured, opening the collection (even with
`create=False`) must raise `BackendMismatchError` whose message references the remote
target (`tests/test_qdrant_backend.py:L372-L380`).

## Pure-remote palace rejection

A palace with `local_path=None` (pure remote, no place to anchor the marker) must be
refused: `get_collection(...)` raises `BackendError` whose message references
`"local palace path"`. The backend must not silently open an unprotected remote
collection (`tests/test_qdrant_backend.py:L441-L448`).

## Add / upsert

`add` and `upsert` accept parallel arrays `ids`, `documents`, `metadatas`,
`embeddings` (`tests/test_qdrant_backend.py:L207-L220`, `L265-L276`). After adding 3
points, `count()` returns 3 (`tests/test_qdrant_backend.py:L224`).

Embedding dimension is fixed on first write. A later upsert with a differently sized
embedding vector raises `DimensionMismatchError`
(`tests/test_qdrant_backend.py:L337-L342`).

`add` rejects duplicate ids within the same batch, raising `ValueError` whose message
matches `"unique"` (`tests/test_qdrant_backend.py:L345-L354`).

## Query (vector search)

`query(query_embeddings, n_results, where=..., include=...)` returns a result whose
`ids` is a list-of-lists (one list per query embedding), with `documents`,
`embeddings`, and `distances` available when requested in `include`
(`tests/test_qdrant_backend.py:L226-L234`). Results are ordered by descending
similarity to the query embedding and filtered by the `where` clause. With a query of
`[1,0]` and filter `rank >= 2`, results are `["b","c"]` in that order, `b`'s document
is its stored text, and `b`'s returned embedding approximates `[0.9, 0.1]`
(`tests/test_qdrant_backend.py:L226-L234`).

### Metadata filter (`where`) operators

`where` supports equality (`{"key": value}`) and comparison operators including
`{"$gte": n}` (`tests/test_qdrant_backend.py:L229-L232`). Compound/complex filters
include `$or` with nested conditions and substring operators such as
`{"$contains": ...}` on metadata values, plus document-text filters via
`where_document={"$contains": ...}` (`tests/test_qdrant_backend.py:L308-L315`).

### Exact local fallback for complex filters

When a query uses filters the remote cannot express exactly (here, `$or` combined with
a `$contains` metadata operator plus a `where_document` `$contains`), the backend must
NOT delegate filtering to the remote query endpoint: it falls back to exact local
evaluation. The test asserts the correct single result `["a"]` AND that the fake
client recorded zero `query_points` calls (`tests/test_qdrant_backend.py:L290-L316`).

## Lexical search

`lexical_search(query, n_results, where=...)` returns an object with `.hits`, each hit
exposing an `id`. Hits are ranked by lexical relevance to the query terms. For query
`"rareterm backend"` over the sample data, ranked hits are `["b","a"]`
(`tests/test_qdrant_backend.py:L236-L237`).

The lexical index is created server-side: the first created payload index targets
field `"document"` with schema `"text"`
(`tests/test_qdrant_backend.py:L238`).

Lexical filtering uses a `text_any` token condition pushed to the server. When no
document matches the query token, the search returns an empty hit list WITHOUT a full
scan: exactly one scroll call occurs, and that scroll's filter contains a `text_any`
condition (`tests/test_qdrant_backend.py:L319-L334`).

## Get

`get(ids=..., include=...)` returns records preserving the exact order and
multiplicity of the requested `ids`, including duplicates. Requesting
`["two","one","two"]` yields ids `["two","one","two"]` with documents in the same
positional order (`tests/test_qdrant_backend.py:L278-L280`). `get()` with no ids
returns all current ids of the collection (`tests/test_qdrant_backend.py:L286-L287`,
`L395-L396`). Requesting a deleted id yields an empty result
(`tests/test_qdrant_backend.py:L522-L523`).

## Update

`update(ids=..., metadatas=...)` merges the supplied metadata into existing metadata
rather than replacing it. Updating `{"room":"updated"}` on a record that had
`{"wing":"a"}` yields `{"wing":"a","room":"updated"}`
(`tests/test_qdrant_backend.py:L282-L283`).

## Delete

`delete` accepts either `ids=[...]` or `where={...}`. `delete(where={"wing":"b"})`
removes only matching records, leaving non-matching records intact
(`tests/test_qdrant_backend.py:L285-L287`). `delete(ids=[...])` removes by id
(`tests/test_qdrant_backend.py:L522-L523`).

## Multiple collections and isolation

A single backend/palace can host multiple named collections (e.g. `"drawers"` and
`"closets"`); writes to one collection do not appear in the other, even for the same
id value (`tests/test_qdrant_backend.py:L260-L287`).

Distinct palaces sharing the same `namespace` value but differing `PalaceRef.id`/
`local_path` must remain isolated: the same id `"same"` written to each returns each
palace's own document, and the two collections map to distinct remote collection names
(`col_a._remote_collection != col_b._remote_collection`)
(`tests/test_qdrant_backend.py:L383-L397`).

Cross-palace isolation (per `PalaceRef.id`) and cross-namespace isolation (per
`PalaceRef.namespace`) both satisfy the shared partition-isolation conformance: a
record stored under one partition is invisible when queried under another
(`tests/test_qdrant_backend.py:L451-L483`). The isolation mechanism is that the
namespace (and palace identity) partitions the remote collection name
(`tests/test_qdrant_backend.py:L480-L481`).

## Lifecycle, health, and error reporting

`backend.close_palace(path)` and `backend.close()` shut down access. After
`close_palace`, calling `col.count()` raises an exception
(`tests/test_qdrant_backend.py:L240-L242`).

If the remote collection is deleted out from under the backend after the marker
exists, the collection is unhealthy: `col.health().ok` is `False`, and `col.count()`
raises `CollectionNotInitializedError`
(`tests/test_qdrant_backend.py:L400-L408`).

When the search layer cannot open a collection because the backend itself fails (raises
`BackendError`), the search result distinguishes this from a missing palace: it returns
`{"error": "Backend error", "details": <message containing the backend error text>}`
(`tests/test_qdrant_backend.py:L411-L422`).

## Embedding wrapper integration

When the configured backend is qdrant (`MEMPALACE_BACKEND_EXPLICIT=qdrant`,
`MEMPALACE_BACKEND=qdrant`), the palace `get_collection` wrapper auto-embeds documents:
`add(documents=[...], ids=[...], metadatas=[...])` without explicit embeddings causes
texts to be embedded, and a subsequent `query(query_texts=[...])` returns the matching
id (`tests/test_qdrant_backend.py:L425-L438`).

## Live REST round-trip (opt-in)

A live integration test runs only when `MEMPALACE_QDRANT_LIVE_URL` is set; otherwise it
is skipped (`tests/test_qdrant_backend.py:L486-L489`). When enabled it uses a unique
namespace, passes `options={"url":..., "api_key":...}` to `get_collection`, and exercises
upsert, `detect`, filtered vector query (`where={"wing":"live"}` -> `["live-a"]`),
lexical search (`"rareterm"` -> top hit `live-a`), delete-by-id, and cleanup by deleting
the remote collection (`tests/test_qdrant_backend.py:L486-L529`).
