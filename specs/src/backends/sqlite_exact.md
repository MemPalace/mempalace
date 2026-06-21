# Behavior Specification: SQLite Exact-Vector Backend

Source: `mempalace/backends/sqlite_exact.py`

## Purpose & Overall Contract

This backend is a correctness-oriented, local-first vector store. It stores
embedding vectors as raw little-endian float32 blobs and answers similarity
queries by computing exact cosine distance over every matching row in a
collection — there is no approximate nearest-neighbor index
(`mempalace/backends/sqlite_exact.py:L1-L6`). It exposes a backend object
(`SQLiteExactBackend`) and a per-collection object (`SQLiteExactCollection`) as
its public surface (`mempalace/backends/sqlite_exact.py:L1047-L1047`).

## On-Disk Format

Each palace is backed by a single SQLite database file named
`sqlite_exact.sqlite3` located inside the palace directory
(`mempalace/backends/sqlite_exact.py:L41-L41`, `L823-L825`). A palace is
detected as belonging to this backend if and only if that file exists inside the
given path (`mempalace/backends/sqlite_exact.py:L1015-L1017`).

The schema, created on first connection, consists of these tables
(`mempalace/backends/sqlite_exact.py:L865-L894`):
- `meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)` — key/value settings.
- `collections(id INTEGER PK AUTOINCREMENT, name TEXT UNIQUE, dimension INTEGER, created_at TEXT)`.
- `documents(collection_id INTEGER, id TEXT, document TEXT, metadata_json TEXT, embedding BLOB, dim INTEGER, created_at TEXT, updated_at TEXT, PRIMARY KEY(collection_id, id), FK collection_id → collections(id) ON DELETE CASCADE)`.
- Index `idx_documents_collection` on `documents(collection_id)`.

The database is configured in WAL journal mode
(`mempalace/backends/sqlite_exact.py:L868-L868`). If the `collections` table
predates the `dimension` column, that column is added by migration
(`mempalace/backends/sqlite_exact.py:L895-L897`).

A full-text-search virtual table `docs_fts` (FTS5 over
`collection_id UNINDEXED, doc_id UNINDEXED, document`) is created when FTS5 is
available; the meta key `fts5_available` is then set to `"1"`. If FTS5 is not
available, no virtual table is created and `fts5_available` is set to `"0"`
(`mempalace/backends/sqlite_exact.py:L898-L920`). The presence/absence of FTS is
an observable behavior switch but does not change query correctness.

### Stored value encodings
- `metadata_json` is a JSON object serialized with sorted keys, no extra
  whitespace, and non-ASCII preserved; a null/empty metadata serializes as `{}`
  (`mempalace/backends/sqlite_exact.py:L52-L53`). On read, invalid or
  non-object JSON decodes to an empty object `{}`
  (`mempalace/backends/sqlite_exact.py:L56-L63`).
- `embedding` is the raw bytes of a 1-D float32 array; `dim` is the element
  count of that vector (`mempalace/backends/sqlite_exact.py:L66-L74`,
  `L381-L383`). An embedding must be a non-empty 1-D vector or a value error is
  raised (`mempalace/backends/sqlite_exact.py:L70-L74`).
- `created_at` / `updated_at` are UTC timestamps in ISO-8601 format
  (`mempalace/backends/sqlite_exact.py:L48-L49`).

## Backend: `SQLiteExactBackend`

Identity and declared capabilities: name `"sqlite_exact"`; capabilities include
requiring explicit embeddings, accepting/passing-through/returning embeddings,
metadata filters, lexical search, and local mode
(`mempalace/backends/sqlite_exact.py:L801-L813`). Supported maintenance kinds are
`"analyze"` and `"compact"` only; `"reindex"` is intentionally unsupported
because there is no ANN index (`mempalace/backends/sqlite_exact.py:L814-L816`).

### Connection management
- `_connect(palace_path, create)`: If the backend is closed, raises a
  backend-closed error (`mempalace/backends/sqlite_exact.py:L827-L829`). When
  `create` is false and the DB file is absent, raises palace-not-found
  (`mempalace/backends/sqlite_exact.py:L831-L832`). When `create` is true, the
  palace directory is created and its permissions are set to owner-only `0o700`
  (best-effort; permission errors are ignored)
  (`mempalace/backends/sqlite_exact.py:L833-L838`).
- Connections are cached per palace path; first-open work (connect + schema
  init) is serialized under a registry lock so two threads cannot each create a
  connection or run schema init concurrently on a fresh file; cache hits are a
  plain lookup of an open handle (`mempalace/backends/sqlite_exact.py:L839-L863`).

### `get_collection` (argument normalization)
Accepts either a keyword `palace=` (a palace reference object) plus
`collection_name` and optional `create`, or a positional palace path followed by
collection name and optional create, or a keyword `palace_path=`
(`mempalace/backends/sqlite_exact.py:L949-L980`). A `palace=` argument must be a
palace-reference instance, else a type error
(`mempalace/backends/sqlite_exact.py:L952-L954`). Unexpected extra arguments
raise type errors (`mempalace/backends/sqlite_exact.py:L958-L959`, `L970-L971`,
`L977-L978`). With no recognized form, raises a type error
(`mempalace/backends/sqlite_exact.py:L980-L980`).

When the palace reference has no local path, raises palace-not-found
(`mempalace/backends/sqlite_exact.py:L928-L930`). When not creating and the
palace directory does not exist, raises palace-not-found
(`mempalace/backends/sqlite_exact.py:L931-L932`). If the named collection row is
absent: when `create` is false, raises collection-not-initialized; when true, a
new `collections` row is inserted with the current timestamp
(`mempalace/backends/sqlite_exact.py:L934-L947`).

`create_collection` and `get_or_create_collection` both delegate to
`get_collection` with `create=True`
(`mempalace/backends/sqlite_exact.py:L1019-L1023`).

### `delete_collection(palace_path, collection_name)`
Connects without creating. If the collection does not exist, raises
collection-not-initialized
(`mempalace/backends/sqlite_exact.py:L1025-L1033`). Otherwise deletes all
`documents` for that collection, deletes its `docs_fts` rows (ignoring the error
if the FTS table is absent), then deletes the `collections` row, and commits
(`mempalace/backends/sqlite_exact.py:L1034-L1044`).

### `close_palace(palace)` and `close()`
`close_palace` removes the cached handle for the palace's local path (if any),
marks it closed, and closes its connection; a missing path or handle is a no-op
(`mempalace/backends/sqlite_exact.py:L982-L991`). `close()` marks the backend
closed under the registry lock, then closes all cached handles; after this no
new connection can be registered
(`mempalace/backends/sqlite_exact.py:L993-L1006`).

### `health(palace)`
Returns unhealthy if the backend is closed; unhealthy with message
"sqlite_exact database not found" if a palace with a local path is given but its
DB file is absent; otherwise healthy
(`mempalace/backends/sqlite_exact.py:L1008-L1013`).

## Collection: `SQLiteExactCollection`

A collection becomes unusable after either it or its underlying handle is closed;
operations on a closed collection raise a backend-closed error
(`mempalace/backends/sqlite_exact.py:L262-L264`). Each data operation runs inside
a cursor scope that takes the handle lock, commits on success, and rolls back and
re-raises on any exception (`mempalace/backends/sqlite_exact.py:L266-L279`).

### Dimension invariant
A collection has at most one embedding dimension. When writing embeddings: if a
single batch contains more than one distinct vector length, a
dimension-mismatch error is raised
(`mempalace/backends/sqlite_exact.py:L299-L307`). The first observed dimension is
recorded on the `collections` row; subsequent writes with a different dimension
raise a dimension-mismatch error
(`mempalace/backends/sqlite_exact.py:L308-L319`).

### `add(documents, ids, metadatas=None, embeddings=None)`
Validates batch lengths: `documents`, and (if provided) `metadatas` and
`embeddings`, must each have the same length as `ids`, else a value error
(`mempalace/backends/sqlite_exact.py:L233-L246`, `L367-L373`). Embeddings are
required; omitting them raises a value error
(`mempalace/backends/sqlite_exact.py:L374-L375`). Missing metadata defaults to
empty objects (`mempalace/backends/sqlite_exact.py:L376-L376`). Each row is
inserted with the same `created_at`/`updated_at` timestamp and its FTS entry is
(re)written; inserting an id that already exists in the collection is an error
because of the primary-key constraint
(`mempalace/backends/sqlite_exact.py:L377-L403`).

### `upsert(documents, ids, metadatas=None, embeddings=None)`
Same validation and embedding requirement as `add`
(`mempalace/backends/sqlite_exact.py:L405-L414`). On a primary-key conflict the
existing row's document, metadata, embedding, dimension, and `updated_at` are
overwritten (insert-or-replace semantics) and the FTS entry is rewritten
(`mempalace/backends/sqlite_exact.py:L415-L447`).

### `update(ids, documents=None, metadatas=None, embeddings=None)`
Requires at least one of documents/metadatas/embeddings, else a value error
(`mempalace/backends/sqlite_exact.py:L449-L451`). Each provided list must match
the `ids` length (`mempalace/backends/sqlite_exact.py:L452-L459`). Ids not
present in the collection are silently skipped
(`mempalace/backends/sqlite_exact.py:L463-L473`). For present ids: document is
replaced if provided; metadata is *merged* on top of existing metadata (existing
keys preserved unless overwritten) if provided; embedding/dimension are replaced
if provided, otherwise kept; `updated_at` is set to the current time
(`mempalace/backends/sqlite_exact.py:L474-L496`). The FTS entry is rewritten for
each updated row (`mempalace/backends/sqlite_exact.py:L497-L497`).

### Row scan and filtering (`_rows`)
A scan reads all documents for the collection ordered by `rowid` (insertion
order), decodes metadata, and yields rows whose metadata matches `where` and
whose document matches `where_document`
(`mempalace/backends/sqlite_exact.py:L499-L527`). The returned document is the
empty string when null (`mempalace/backends/sqlite_exact.py:L517-L524`).

### Metadata filter semantics (`where`)
Filters are validated up front: any operator key (starting with `$`) outside the
supported set raises an unsupported-filter error
(`mempalace/backends/sqlite_exact.py:L135-L150`). Supported operators are
`$eq`, `$ne`, `$in`, `$nin`, `$and`, `$or`, `$contains`, `$gt`, `$gte`, `$lt`,
`$lte` (`mempalace/backends/sqlite_exact.py:L43-L45`).

Matching rules (`mempalace/backends/sqlite_exact.py:L185-L208`): an empty/absent
filter matches everything; `$and` requires all sub-clauses to match; `$or`
requires at least one; a bare field maps to that metadata key. A scalar value
means equality; a dict value applies each operator against the actual metadata
value. Booleans are coerced to integers before comparison
(`mempalace/backends/sqlite_exact.py:L152-L160`). Operator semantics
(`mempalace/backends/sqlite_exact.py:L161-L182`): `$eq`/`$ne` equality; `$in`/
`$nin` membership against a list (treating a null operand as empty); `$contains`
true when the string form of the operand is a substring of the string form of
the actual value; `$gt`/`$gte`/`$lt`/`$lte` numeric/orderable comparisons that
return false when the operands are not comparable.

### Document filter semantics (`where_document`)
Supports only `$contains` (substring of the document), `$and`, and `$or`; any
other key raises an unsupported-filter error
(`mempalace/backends/sqlite_exact.py:L211-L230`). An empty/absent document filter
matches everything (`mempalace/backends/sqlite_exact.py:L211-L213`).

### `query(...)` — vector similarity
`query_texts` is not supported and raises a value error directing callers to the
wrapper that embeds text (`mempalace/backends/sqlite_exact.py:L539-L542`).
`query_embeddings` is required and must be a non-empty list
(`mempalace/backends/sqlite_exact.py:L543-L546`).

The include-spec resolves which fields to return; distances are included by
default (`mempalace/backends/sqlite_exact.py:L548-L548`). The candidate rows are
those passing `where`/`where_document`
(`mempalace/backends/sqlite_exact.py:L555-L559`). For each query vector: if the
collection has a recorded dimension and the query length differs, a
dimension-mismatch error is raised
(`mempalace/backends/sqlite_exact.py:L561-L567`).

Scoring: for each candidate whose stored vector length equals the query length,
cosine similarity is computed; when either norm is zero the similarity is 0; the
returned distance is `1.0 - clamp(cosine, -1.0, 1.0)`. Candidates whose vector is
missing or whose length differs are skipped
(`mempalace/backends/sqlite_exact.py:L568-L576`). Results are sorted by ascending
distance (nearest first) and truncated to `n_results`
(`mempalace/backends/sqlite_exact.py:L577-L578`).

Output is a per-query list-of-lists structure with ids always populated, and
documents/metadatas/distances/embeddings populated only when requested by the
include-spec; embeddings are returned as float lists
(`mempalace/backends/sqlite_exact.py:L580-L593`).

### `get(...)` — direct retrieval
Resolves include-spec (distances off by default)
(`mempalace/backends/sqlite_exact.py:L605-L605`). Scans rows under `where`/
`where_document` (`mempalace/backends/sqlite_exact.py:L606-L607`). If `ids` is
given, the result is restricted to those ids that exist, returned *in the order
of the requested ids* (`mempalace/backends/sqlite_exact.py:L608-L610`). Then
`offset` and `limit` are applied in that order
(`mempalace/backends/sqlite_exact.py:L611-L614`). Returns ids always, and
documents/metadatas/embeddings only when requested
(`mempalace/backends/sqlite_exact.py:L615-L622`).

### `delete(ids=None, where=None)`
If `ids` is not given, the ids are derived from the rows matching `where`
(`mempalace/backends/sqlite_exact.py:L624-L629`). Each id's document row is
deleted, and its FTS row is deleted when FTS is available
(`mempalace/backends/sqlite_exact.py:L630-L639`).

### `count()`
Returns the number of documents in the collection, or 0 if no count row exists
(`mempalace/backends/sqlite_exact.py:L641-L648`).

### `lexical_search(query, n_results=10, where=None)`
Validates `where` (`mempalace/backends/sqlite_exact.py:L650-L651`). When FTS is
available and the query yields usable tokens, FTS ranking is used; otherwise a
fallback BM25 scan runs in code
(`mempalace/backends/sqlite_exact.py:L652-L669`).

FTS path (`mempalace/backends/sqlite_exact.py:L671-L733`): query is tokenized
into words of length ≥ 2; if there are no tokens, the FTS path is skipped
(returns nothing, caller falls back). Tokens are joined with `OR`. Without a
`where` filter, the SQL is limited to `max(n_results*5, n_results)` candidates;
with a `where` filter no SQL limit is applied
(`mempalace/backends/sqlite_exact.py:L674-L693`). On any SQLite error during the
FTS query the method returns nothing so the caller falls back to the code-based
scan (`mempalace/backends/sqlite_exact.py:L694-L696`). Matching document/metadata
rows are fetched in id chunks of up to 900
(`mempalace/backends/sqlite_exact.py:L699-L714`). Hits preserve FTS rank order,
skip rows whose metadata fails `where`, score is the negated BM25 rank (higher =
better), and the list is truncated at `n_results`
(`mempalace/backends/sqlite_exact.py:L715-L733`).

Fallback BM25 (`mempalace/backends/sqlite_exact.py:L98-L132`, `L656-L669`): the
query and documents are lowercased and tokenized on word characters of length
≥ 2 (`mempalace/backends/sqlite_exact.py:L42-L42`, `L92-L95`). Standard BM25 with
parameters `k1=1.5`, `b=0.75` is applied across the candidate set; documents with
a score of 0 are dropped; results are sorted by descending score and truncated to
`n_results` (`mempalace/backends/sqlite_exact.py:L98-L132`, `L657-L669`).

### Embedder identity
`get_stored_embedder_identity()` returns null if the collection is uninitialized
or no model name is stored; otherwise returns the stored model name plus the
collection's recorded dimension (0 if none)
(`mempalace/backends/sqlite_exact.py:L328-L343`). It is stored under a
collection-scoped meta key `embedder_model:<collection_name>`
(`mempalace/backends/sqlite_exact.py:L325-L326`). `set_embedder_identity` is a
no-op when the identity or its model name is empty, otherwise upserts the model
name into `meta` (`mempalace/backends/sqlite_exact.py:L345-L353`).

### Collection health and maintenance
`close()` marks the collection closed (`mempalace/backends/sqlite_exact.py:L735-L736`).
`health()` is unhealthy if closed or the handle is closed, else healthy
(`mempalace/backends/sqlite_exact.py:L738-L741`).

`maintenance_state()` reports `row_count`, a `vector_index` that is always null
by design (exact cosine, no ANN), and best-effort `page_count` and
`freelist_pages` from SQLite pragmas
(`mempalace/backends/sqlite_exact.py:L743-L758`).

`run_maintenance(kind)` rejects any kind outside `{analyze, compact}` with an
unsupported-kind error (`mempalace/backends/sqlite_exact.py:L760-L766`).
`"analyze"` runs `ANALYZE` and reports status "ran"
(`mempalace/backends/sqlite_exact.py:L767-L771`). `"compact"` runs `VACUUM`
(temporarily switching the connection to autocommit since VACUUM cannot run in a
transaction), serialized by the handle lock in-process and by SQLite's write lock
across processes; it reports pages before/after and pages reclaimed
(`max(0, before - after)`) (`mempalace/backends/sqlite_exact.py:L773-L798`).

## Concurrency & Ordering Guarantees
- All per-collection mutations and reads serialize on the handle lock
  (`mempalace/backends/sqlite_exact.py:L266-L279`).
- Unfiltered row scans return rows in insertion (`rowid`) order
  (`mempalace/backends/sqlite_exact.py:L503-L511`).
- `query` returns nearest-first by ascending cosine distance
  (`mempalace/backends/sqlite_exact.py:L577-L578`).
- `get` with explicit ids returns rows in the caller's id order
  (`mempalace/backends/sqlite_exact.py:L608-L610`).

## Side Effects
- Filesystem: creates the palace directory (mode `0o700`) and the
  `sqlite_exact.sqlite3` file plus WAL/journal files; writes all data there
  (`mempalace/backends/sqlite_exact.py:L833-L838`, `L852-L863`, `L868-L868`).
- No network or external-service access anywhere in this module.
