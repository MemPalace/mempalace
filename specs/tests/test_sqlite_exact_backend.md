# Spec: `tests/test_sqlite_exact_backend.py`

Behavior specification for the test suite that pins the observable contract of the
`sqlite_exact` storage backend and its integration with palace/search resolution.
Each section describes a behavior the backend (the system under test) MUST satisfy
for these tests to pass; citations point at the test that asserts the behavior.

## Test fixtures / construction helpers

A backend instance is constructed with no arguments, and a collection is obtained
by calling `get_collection` with a palace reference whose `id` and `local_path`
are both the palace directory path, a `collection_name`, and a `create` flag; the
helper returns both the backend and the collection handle (tests/test_sqlite_exact_backend.py:L20-L23).

## Backend registry exposure

The string identifier `"sqlite_exact"` MUST appear in the set returned by the
backend registry's `available_backends()` enumeration (tests/test_sqlite_exact_backend.py:L42-L43).

## Missing-collection error names the collection, not the palace

Requesting a non-existent collection with `create=False` MUST raise
`CollectionNotInitializedError`, and the error message MUST contain the requested
collection name while NOT containing the palace path
(tests/test_sqlite_exact_backend.py:L26-L34). The same contract applies to
`delete_collection(palace_path, collection_name)` for a missing collection: it
raises `CollectionNotInitializedError` whose message contains the collection name
but not the palace path (tests/test_sqlite_exact_backend.py:L36-L39).

## Add, vector query, metadata filters, update, and persistence

`add` accepts parallel lists `ids`, `documents`, `metadatas`, and `embeddings`;
metadata values may be strings, integers, or comma-joined tag strings
(tests/test_sqlite_exact_backend.py:L48-L61).

`query(query_embeddings, n_results)` returns a result whose `ids[0]` is the list
of matching ids ordered by ascending distance to the query vector; for query
`[1.0, 0.0]` against vectors `a=[1,0]`, `b=[0,1]`, `c=[0.2,0.8]` the order is
`["a","c","b"]` and the closest distance is `0.0`
(tests/test_sqlite_exact_backend.py:L63-L65).

`get(where=..., include=[...])` applies a structured metadata filter and returns
only matching rows. The filter language supports `$and` (list of sub-clauses),
exact-match equality, numeric comparison `$gte`, and substring membership
`$contains` over comma-joined tag fields; the conjunction of
`wing == "alpha"`, `chunk_index >= 1`, and `tags contains "sqlite"` selects only
id `"b"`, and `include` controls which of `documents`, `metadatas`, `embeddings`
are populated (tests/test_sqlite_exact_backend.py:L67-L79).

`update(ids, metadatas)` mutates metadata of existing rows in place; after setting
`room` to `"lab"` a subsequent `get` reflects the new value
(tests/test_sqlite_exact_backend.py:L81-L82).

Data is durable: after `close_palace(palace_path)` and reopening the collection
with `create=False`, `count()` returns the original row count (3) and previously
stored documents are retrievable unchanged
(tests/test_sqlite_exact_backend.py:L84-L91).

## Atomic batch writes

If any item within a single `add` batch fails (e.g. duplicate ids `["dup","dup"]`
within the same batch), the entire batch MUST be rolled back; the operation raises
an exception and the collection `count()` remains `0`
(tests/test_sqlite_exact_backend.py:L94-L105).

## Collection embedding-dimension enforcement

A collection fixes its embedding dimension on first insert. After adding a
2-dimensional vector, supplying a 3-dimensional vector to any of `add`, `upsert`,
`update`, or `query` MUST raise `DimensionMismatchError`
(tests/test_sqlite_exact_backend.py:L108-L121). The failed operations leave the
collection unchanged: `count()` stays `1` and the existing document is intact
(tests/test_sqlite_exact_backend.py:L123-L124).

## `get` preserves requested id order and duplicates

When `get(ids=[...])` is called with an explicit id list, the returned `ids` and
`documents` MUST appear in exactly the requested order, including repeated ids:
requesting `["b","a","b"]` returns ids `["b","a","b"]` and documents
`["doc b","doc a","doc b"]` (tests/test_sqlite_exact_backend.py:L127-L139).

## Upsert replacement, delete-by-filter, multi-collection isolation

Distinct named collections within the same palace are isolated: the same id
`"same"` can hold different documents in `drawers` versus `closets`
(tests/test_sqlite_exact_backend.py:L142-L163).

`upsert` of an existing id replaces its document and metadata rather than creating
a duplicate; after re-upserting `"same"` the count stays `1` and the document is
the replacement value (tests/test_sqlite_exact_backend.py:L147-L162).

`delete(where=...)` removes rows matching a metadata filter (here `version $in
[2,3]`), affecting only the target collection: `drawers` count drops to `0` while
`closets` count remains `1` (tests/test_sqlite_exact_backend.py:L165-L167).

## Lexical search with metadata filter and fallback

`lexical_search(query, n_results, where=...)` returns a result with a `.hits`
list, each hit exposing an `id`. It performs term-based ranking and applies the
metadata `where` filter; for query `"rareterm sqlite"` restricted to `wing == "w"`
only id `"b"` is returned (tests/test_sqlite_exact_backend.py:L170-L188).

The backend MUST have an internal lexical-index availability check
(`_fts_available(cursor)`). When that check reports the full-text index is
unavailable, `lexical_search` MUST still produce correct ranked results via a
fallback path, returning `"b"` as the top hit
(tests/test_sqlite_exact_backend.py:L190-L192).

When the lexical-index window returns more candidates than `n_results` and many
share the query term, the metadata `where` filter MUST be applied to select the
correct row even if it is not within the first lexical window: with 12 `wing=old`
rows and one `wing=target` row all containing `"needle"`, a `n_results=1` search
filtered to `wing == "target"` returns the `target` id
(tests/test_sqlite_exact_backend.py:L195-L207).

## Implicit-sibling logical filters

A `where` clause may combine an explicit logical operator with sibling key/value
predicates at the same level; all such predicates are conjoined. `where={"$and":
[{"wing": "w"}], "room": "right"}` selects only the row with `room == "right"`,
returning id `"b"` (tests/test_sqlite_exact_backend.py:L210-L224).

## `close_palace` invalidates open collection handles

After `close_palace(palace)`, any previously obtained collection handle for that
palace MUST report unhealthy: `health().ok` is `False`, and further operations
such as `count()` raise an exception
(tests/test_sqlite_exact_backend.py:L227-L236).

## Palace wrapper auto-embeds documents for `sqlite_exact`

When the backend is selected via environment variable
`MEMPALACE_BACKEND_EXPLICIT=sqlite_exact`, a collection obtained through the
palace-level `get_collection(palace_path, create=True)` automatically embeds
document text on `add` (documents are added without explicit embeddings) and on
`query(query_texts=...)`. With a stub embedder, querying by text returns the
matching id as `[["a"]]` (tests/test_sqlite_exact_backend.py:L239-L254).

## Backend-mismatch protection

If a palace directory already contains a `chroma.sqlite3` artifact but the
explicit backend selection is `sqlite_exact`, opening the palace via
`get_collection(..., create=True)` MUST raise `BackendMismatchError`
(tests/test_sqlite_exact_backend.py:L257-L264).

If a palace directory contains BOTH `chroma.sqlite3` and `sqlite_exact.sqlite3`
artifacts, backend resolution (`resolve_backend_name(palace_path)`) MUST raise
`BackendMismatchError` even when the explicit selection is `chroma` — mixed
artifacts are always rejected (tests/test_sqlite_exact_backend.py:L267-L275).

## Exact ranking uses cosine distance

`query` ranks by cosine distance. For query `[1.0, 0.0]` against vectors
`same=[1,0]`, `half=[0.5, sqrt(0.75)]` (a 60-degree vector), and
`orthogonal=[0,1]`, the result order is `["same","half","orthogonal"]` with
distances approximately `[0.0, 0.5, 1.0]`
(tests/test_sqlite_exact_backend.py:L278-L290).

## Search union uses the backend's lexical search

With `sqlite_exact` selected, `searcher.search_memories(query, palace_path,
n_results, candidate_strategy="union")` routes lexical matching through the
backend. For query `"rareterm"` the top result is the document containing
`"rareterm"`, reported with `source_file` basename `"rare.md"` and
`matched_via == "bm25_backend"` (tests/test_sqlite_exact_backend.py:L293-L337).

## Search reports unsupported lexical capability

When the active collection's `lexical_search` raises
`UnsupportedCapabilityError`, a union-strategy `search_memories` MUST NOT fail;
instead the returned result includes
`unsupported_capability == "supports_lexical_search"`
(tests/test_sqlite_exact_backend.py:L340-L369).

## Search vector-disabled fallback

When `search_memories(..., vector_disabled=True)` is called with the
`sqlite_exact` backend selected, the result reports
`unsupported_capability == "chroma_hnsw_fallback"` and `backend == "sqlite_exact"`
(the vector-disabled fallback is Chroma-only)
(tests/test_sqlite_exact_backend.py:L372-L380).

## Concurrent first-open shares a single connection

Two threads concurrently first-opening the same palace MUST share one handle and
exactly one underlying sqlite connection: creation is serialized so only one
connection is created, no errors occur, and both returned collections share the
same internal `_handle` object
(tests/test_sqlite_exact_backend.py:L383-L431). After `backend.close()`, the
single created connection is closed — executing a statement on it raises a
programming error (tests/test_sqlite_exact_backend.py:L433-L435).

## Shared backend types

The test relies on these named types from the backend package: `BackendMismatchError`,
`CollectionNotInitializedError`, `DimensionMismatchError`, `PalaceRef`,
`QueryResult`, `UnsupportedCapabilityError`, and the `available_backends` enumerator
(tests/test_sqlite_exact_backend.py:L8-L17). `QueryResult` is constructible with
keyword fields `ids`, `documents`, `metadatas`, and `distances`, each a list-of-lists
keyed by query index (tests/test_sqlite_exact_backend.py:L344-L350). `PalaceRef`
is constructible with `id` and `local_path` keyword fields
(tests/test_sqlite_exact_backend.py:L22-L22).
