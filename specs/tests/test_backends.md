# Behavior Spec: Storage Backend Contract (`tests/test_backends.py`)

This spec captures the externally observable contract of the pluggable storage backend
layer — the backend registry, the ChromaDB backend, the typed result objects, the
collection interface, and the on-disk repair/quarantine routines for HNSW vector
segments. It is derived from the test suite, which encodes the required behavior. All
claims cite the test that asserts them.

## 1. Typed result objects

### QueryResult
A `QueryResult` carries vector-query results with these attributes: `ids`, `documents`,
`metadatas`, `distances`, and `embeddings` (tests/test_backends.py:L80-L91). When built
from a backend response, each of `ids`/`documents`/`metadatas`/`distances` is a list of
lists (outer dimension = one entry per query); `embeddings` is `None` unless requested
(tests/test_backends.py:L86-L91).

`QueryResult.empty(num_queries=N)` produces a result whose `ids`, `documents`, and
`distances` are each a list of N empty lists, preserving the outer query dimension, with
`embeddings` of `None` (tests/test_backends.py:L106-L111).

### GetResult
A `GetResult` carries point-lookup results with attributes `ids`, `documents`, and
`metadatas`, each a flat list (not nested) (tests/test_backends.py:L94-L103).

### Dict-compatibility shim
Both typed results support transitional dict-style access: subscripting (`result["ids"]`),
`.get(key)`, `.get(key, default)` returning the default for absent keys, and `in`
membership tests. Known keys are present; unknown keys are absent and yield the supplied
default (tests/test_backends.py:L114-L121).

## 2. Collection query interface

A collection's `query` accepts `query_texts` or `query_embeddings` plus optional `where`
filter and `include` list, and returns a `QueryResult` (tests/test_backends.py:L80-L91).

Input validation for `query`:
- Calling with neither `query_texts` nor `query_embeddings` raises a value error
  (tests/test_backends.py:L284-L288).
- Supplying both `query_texts` and `query_embeddings` raises a value error
  (tests/test_backends.py:L291-L295).
- An empty input list (e.g. `query_texts=[]`) raises a value error
  (tests/test_backends.py:L298-L302).

When the underlying response is empty, the outer per-query dimension is reconstructed:
for two query texts, `ids`/`documents`/`distances` each become `[[], []]`
(tests/test_backends.py:L124-L134). If `embeddings` is in the `include` list, an empty
response yields `embeddings == [[], []]`; if not requested, `embeddings` is `None`
(tests/test_backends.py:L305-L316).

A `where` filter containing an unknown/unsupported operator (e.g. `$regex`) raises an
unsupported-filter error (tests/test_backends.py:L136-L141).

## 3. Collection write interface

The collection delegates `add`, `upsert`, `update`, `delete`, and `count` to the
underlying store. A sequence of `add`, `upsert`, `delete`, `count` is forwarded in order
as exactly those operations, and `count()` returns the store's count
(tests/test_backends.py:L144-L154).

The default `update` implementation rejects mismatched argument lengths: if `documents`
length differs from `ids` length it raises a value error mentioning "documents length";
if `metadatas` length differs from `ids` length it raises a value error mentioning
"metadatas length" (tests/test_backends.py:L426-L436). It does not silently misalign.

## 4. Backend registry

`available_backends()` returns the set of registered backend names and always includes
`"chroma"` (tests/test_backends.py:L157-L159). `get_backend("chroma")` returns a
ChromaBackend instance (tests/test_backends.py:L160). `get_backend(<unknown>)` raises a
key error (tests/test_backends.py:L163-L165).

### Backend resolution priority
`resolve_backend_for_palace` chooses a backend name with strict precedence: an explicit
argument beats everything; a config value beats env and default; an env value beats
default; with nothing supplied it returns the default `"chroma"`
(tests/test_backends.py:L168-L178).

### Palace references
A `PalaceRef` carries `id` and `local_path` and may be passed as the `palace` keyword to
`get_collection` (tests/test_backends.py:L439-L448). A plain path string is also accepted
in place of a `PalaceRef` (tests/test_backends.py:L451-L459).

## 5. ChromaBackend collection lifecycle

### detect
`ChromaBackend.detect(path)` returns true when `path` contains a `chroma.sqlite3` file,
and false for a directory that does not (tests/test_backends.py:L181-L184).

### get_collection create semantics
- `create=False` against a missing palace directory raises `PalaceNotFoundError`, and the
  raised value is NOT a `CollectionNotInitializedError` (the directory is genuinely
  absent), and the directory is not created (tests/test_backends.py:L451-L461,
  L1419-L1431).
- `create=False` against an existing palace dir + DB where the collection was never
  created raises `CollectionNotInitializedError`, which is a subclass of both
  `PalaceNotFoundError` and `FileNotFoundError` for backward compatibility
  (tests/test_backends.py:L1434-L1453).
- `create=True` creates the palace directory and the collection, returning a collection
  object; the created collection is readable by a fresh client
  (tests/test_backends.py:L464-L477).
- `create=True` is idempotent: calling it twice on the same name does not crash and
  returns a collection (tests/test_backends.py:L605-L618).
- Reopening with `create=True` preserves existing collection metadata rather than
  overwriting it (tests/test_backends.py:L621-L628).

### Collection metadata on creation
A newly created collection has `hnsw:space == "cosine"` (tests/test_backends.py:L480-L491),
and HNSW bloat-guard thresholds `hnsw:batch_size == 2` and `hnsw:sync_threshold == 2`
persisted on its metadata (tests/test_backends.py:L494-L514). The same guard applies via
the legacy `create_collection()` path (tests/test_backends.py:L516-L525). These low
thresholds force the vector index to persist its on-disk metadata after any write of two
or more records (tests/test_backends.py:L528-L546).

### HNSW thread pinning retrofit
`_pin_hnsw_threads(collection)` sets the collection's `hnsw.num_threads` to 1; a legacy
collection created without it gets it applied (tests/test_backends.py:L1373-L1387). The
retrofit never raises even if the underlying modify operation fails
(tests/test_backends.py:L1390-L1397). `get_collection(create=False)` applies this retrofit
when opening an existing palace, so the reopened collection has `num_threads == 1`
(tests/test_backends.py:L1400-L1416).

## 6. Lexical (full-text) search

`collection.lexical_search(query, n_results, where=...)` returns an object with a `.hits`
list; each hit has `.id` and `.metadata`. The search uses the on-disk SQLite full-text
index and must NOT scan the full collection (the underlying `count()`/`get()` must not be
called as a scan) (tests/test_backends.py:L187-L249). A `where` filter restricts hits to
matching metadata (tests/test_backends.py:L247-L249).

Hit ids are the public/external drawer ids (the `embedding_id`), not internal rowids
(tests/test_backends.py:L250-L252). Returned ids must round-trip: every id from
`lexical_search` must resolve through `get(ids=...)`, and the fetched ids equal the
requested ids (tests/test_backends.py:L255-L280).

## 7. Client caching and SQLite lock release

`close_palace(ref)` releases the backend's underlying SQLite file lock so that, after the
palace directory is removed and recreated, a fresh collection on the same path can be
written without a read-only/db-moved error; the rebuilt collection's count reflects only
new writes (tests/test_backends.py:L318-L334).

`close()` releases the SQLite lock for every cached client, so each previously-opened
palace path can be removed and reopened by a fresh backend in the same process
(tests/test_backends.py:L337-L361).

### Cache freshness invalidation
The backend tracks per-palace freshness as `(inode, mtime)` of the `chroma.sqlite3` file.
- If the DB file is removed (palace rebuild mid-flight), the next `_client()` call drops
  the stale cached client, returns a different client, and updates freshness to the
  post-rebuild stat (tests/test_backends.py:L364-L397).
- If the DB did not exist at cache time (freshness `(0, 0.0)`) and later appears, the next
  `_client()` call detects the `0 → nonzero` transition and rebuilds, returning a different
  client with updated freshness (tests/test_backends.py:L400-L423).

## 8. Cold-start quarantine gate

Repair/quarantine of HNSW segments runs once per palace per process, gated by a
`_quarantined_paths` set.

- `make_client(palace_path)` invokes `quarantine_stale_hnsw` on the first call for a
  palace only; subsequent calls skip it (tests/test_backends.py:L1182-L1213).
- The invalid-metadata quarantine (`quarantine_invalid_hnsw_metadata`) is likewise gated
  to the first `make_client` call (tests/test_backends.py:L1216-L1241).
- The gate is keyed by palace path: two distinct palaces each get exactly one quarantine
  attempt (tests/test_backends.py:L1244-L1270).
- The instance `_client()` path runs the same quarantine on first open, quarantining a
  corrupt segment (tests/test_backends.py:L1276-L1300), and fires at most once per palace
  for repeated `_client()` calls (tests/test_backends.py:L1303-L1331).
- The gate re-arms when the DB file's mtime changes between `_client()` calls, so the
  quarantine checks run again after external in-place writes
  (tests/test_backends.py:L1334-L1367, L1698-L1746).
- The gate also re-arms on inode change (full palace replacement)
  (tests/test_backends.py:L1749-L1795).

### Preflight ordering
On `_client()` open, the repair/quarantine routines run in this exact order before the
persistent client is constructed: `_fix_missing_collection_type`, `_fix_blob_seq_ids`,
`quarantine_invalid_hnsw_metadata`, `quarantine_stale_hnsw`
(tests/test_backends.py:L1659-L1695).

## 9. BLOB seq_id repair (`_fix_blob_seq_ids`)

Operates on the palace's `chroma.sqlite3`. Repairs legacy databases that stored
`embeddings.seq_id` as an 8-byte big-endian BLOB by converting it to the equivalent
integer (e.g. BLOB-of-42 becomes integer 42 with sqlite type `integer`)
(tests/test_backends.py:L631-L645). It is a no-op when seq_ids are already integers
(tests/test_backends.py:L648-L660) and a no-op (no error) when the palace has no
`chroma.sqlite3` (tests/test_backends.py:L663-L665).

Safety carve-outs:
- The `max_seq_id` table is owned by the current store version and must never be
  interpreted/converted; its BLOB value is left unchanged as a `blob`
  (tests/test_backends.py:L668-L688).
- A BLOB with the sysdb-10 prefix `\x11\x11` in `embeddings.seq_id` is skipped (left as a
  `blob`), not converted (tests/test_backends.py:L691-L705).
- Mixed rows: genuine big-endian u64 BLOBs convert to integers while sysdb-10-prefixed
  BLOBs are left as blobs, preserving row order
  (tests/test_backends.py:L708-L726).

Migration marker (`_BLOB_FIX_MARKER`, an on-disk file in the palace):
- Written after a successful BLOB→INTEGER conversion (tests/test_backends.py:L729-L744).
- Written even when the migration was a no-op (already integer), so future calls
  short-circuit (tests/test_backends.py:L747-L768).
- When the marker is present, the function does NOT open the SQLite file at all
  (tests/test_backends.py:L798-L817). Opening Python's sqlite against a live store WAL DB
  can corrupt the next client, so the marker is load-bearing.
- After the pre-open probe the SQLite connection is closed exactly once
  (tests/test_backends.py:L771-L795).

## 10. Collection type repair (`_fix_missing_collection_type`)

Operates on the palace's `chroma.sqlite3`, on the `collections.config_json_str` column.
For each collection whose config JSON is an object lacking `_type`, it sets
`_type = "CollectionConfigurationInternal"` (tests/test_backends.py:L823-L841). Includes
empty objects `{}` and NULL configs (NULL is treated as an empty object and gets the type
added) (tests/test_backends.py:L933-L957).

Configs that already contain `_type` are left byte-for-byte unchanged
(tests/test_backends.py:L844-L862). Non-dict JSON (arrays `[]`, the literal `null`) is
skipped without error (tests/test_backends.py:L960-L978). A malformed-JSON row is skipped
and does not prevent fixing other valid rows (tests/test_backends.py:L981-L997).

Migration marker (`_COLLECTION_TYPE_MARKER`):
- Not written when the palace has no `chroma.sqlite3`; the call is a no-op
  (tests/test_backends.py:L865-L870).
- Written after a successful migration (tests/test_backends.py:L873-L891), and also when
  all collections already had `_type` (no-op case) (tests/test_backends.py:L910-L930).
- When the marker exists, sqlite is not opened (tests/test_backends.py:L894-L907).

## 11. Stale HNSW segment quarantine (`quarantine_stale_hnsw`)

Scans a palace for HNSW segment subdirectories and renames (quarantines) those that are
both stale-by-mtime AND fail an integrity check. Returns a list of the new (renamed)
paths; quarantined directories are renamed with a `.drift-<timestamp>` suffix and the
original directory no longer exists; the renamed directory retains its `data_level0.bin`
(tests/test_backends.py:L1029-L1045).

Mtime gate: a segment is a candidate only if its HNSW data file mtime is older than the
`chroma.sqlite3` mtime by at least `stale_seconds`. A segment with recent mtime relative
to the DB is left alone — the mtime gate short-circuits before the integrity gate
(tests/test_backends.py:L1144-L1151).

Integrity gate (`_segment_appears_healthy`), applied to stale candidates:
- A complete metadata file (begins with PROTO bytes `\x80\x04`, ends with STOP byte
  `\x2e`, with sufficient size) marks the segment healthy; a stale-but-healthy segment is
  the async-flush steady state and is NOT quarantined
  (tests/test_backends.py:L1007-L1008, L1048-L1064).
- A malformed/all-zero metadata file marks the segment corrupt and it is quarantined
  (tests/test_backends.py:L1029-L1045).
- A truncated (under-floor-size) metadata file (e.g. only `\x80\x04`) is quarantined
  (tests/test_backends.py:L1129-L1141).
- Missing metadata file with no link_lists and only trivial data (≤
  `_HNSW_MISSING_METADATA_DATA_FLOOR` bytes) means no persist was attempted; the segment
  is treated as fresh/healthy and left alone (tests/test_backends.py:L1066-L1101).
- Missing metadata file but with non-trivial data (> floor) AND a populated
  `link_lists.bin` indicates an interrupted persist; the segment is unhealthy and
  quarantined (tests/test_backends.py:L1083-L1091, L1104-L1126).

`stale_seconds=0.0` forces the integrity gate to run on every segment regardless of mtime
delta; on a freshly and correctly persisted segment it quarantines nothing
(tests/test_backends.py:L565-L568).

Edge cases:
- A missing palace path, or an existing dir with no `chroma.sqlite3`, returns `[]` without
  raising (tests/test_backends.py:L1154-L1159).
- Directories already carrying a `.drift-` suffix are never re-renamed
  (tests/test_backends.py:L1162-L1176).

### Single-record / sub-threshold persistence interaction
With the batch_size=2 guard, a single-record upsert persists no metadata pickle and no
link_lists; `_segment_appears_healthy` treats that combination as sub-threshold (never
persisted), not corruption, and `quarantine_stale_hnsw(stale_seconds=0.0)` returns `[]`
(tests/test_backends.py:L571-L602). A correctly persisted multi-record segment has its
`index_metadata.pickle` and a non-empty `link_lists.bin`, is reported healthy, and is not
quarantined (tests/test_backends.py:L528-L568).

## 12. Invalid HNSW metadata quarantine (`quarantine_invalid_hnsw_metadata`)

Reads each segment's `index_metadata.pickle` (via a safe restricted unpickler) and
quarantines segments whose metadata is structurally invalid. Quarantined directories are
renamed with a `.corrupt-<timestamp>` suffix and the original no longer exists; returns
the list of new paths (tests/test_backends.py:L1456-L1468).

Quarantined (renamed) when:
- `dimensionality` is None while `id_to_label` is non-empty and inconsistent — e.g.
  `{"dimensionality": None, "id_to_label": {"a": 1}}` with no consistency proof
  (tests/test_backends.py:L1456-L1468).
- `dimensionality` is None and `label_to_id` is inconsistent with `id_to_label` (mismatched
  inverse mapping) (tests/test_backends.py:L1526-L1550).
- `id_to_label` is present but not a dict (e.g. a list) (tests/test_backends.py:L1567-L1579).
- The pickle payload is not a metadata object/schema at all (e.g. a list)
  (tests/test_backends.py:L1582-L1594).
- The pickle contains an unsafe/dangerous payload; loading it must NOT execute the payload,
  and the segment is quarantined (tests/test_backends.py:L1597-L1618).

Kept (not quarantined) when:
- `dimensionality` is None but the metadata is internally consistent —
  `total_elements_added` equals live label count and `id_to_label`/`label_to_id` are
  proper inverses (tests/test_backends.py:L1471-L1494).
- A post-deletion shape where `total_elements_added` (monotonic) exceeds the live label
  count, with consistent inverse mappings — this dim-None shape is recoverable, not
  corruption (tests/test_backends.py:L1497-L1523).
- An uninitialized segment: `dimensionality` None with empty `id_to_label`
  (tests/test_backends.py:L1553-L1564).
- A transient read error (EOF mid-flush) or a truncated pickle (UnpicklingError) — these
  are skipped, returning `[]` and leaving the segment in place
  (tests/test_backends.py:L1621-L1656).

## 13. Embedding-function mismatch handling

`ChromaBackend._explain_ef_mismatch(err, palace_path)` recognizes the store's
embedding-function-conflict error (raised when the user changed the embedding model on an
existing palace) and returns a recovery message containing the palace path, the
`MEMPALACE_EMBEDDING_MODEL` env var name, and the `rebuild-index` command
(tests/test_backends.py:L1798-L1813). For unrelated value errors it returns `None`, so the
caller re-raises the original error unmodified (tests/test_backends.py:L1816-L1820).

End-to-end: opening an existing palace (`create=False`) with an incompatible embedding
function name raises a value error whose message mentions `rebuild-index`
(tests/test_backends.py:L1823-L1854).

## 14. Palace collection-name configuration

`palace.get_collection(palace_path, create=...)` forwards to the default backend's
`get_collection` using the configured collection name from configuration. It passes
`palace_path`, the configured `collection_name`, and `create` through unchanged
(tests/test_backends.py:L1857-L1877).
