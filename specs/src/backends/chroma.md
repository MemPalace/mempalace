# Behavior Spec: ChromaDB Storage Backend

Reference implementation of the MemPalace storage backend over an on-disk ChromaDB palace directory (RFC 001). A "palace" is a filesystem directory containing a `chroma.sqlite3` relational database plus one or more HNSW vector-segment subdirectories. This module adapts ChromaDB returns into typed results, protects against HNSW corruption before opening, and applies forward-compatibility migrations to the SQLite database (`mempalace/backends/chroma.py:L1-L1`).

## On-disk Contract

A palace directory contains `chroma.sqlite3` at its root; its presence is the definition of "this directory is a palace" (`mempalace/backends/chroma.py:L2116-L2117`, `mempalace/backends/chroma.py:L370-L372`). Vector segments are subdirectories whose names contain a `-` (hyphen) and do not start with `.`; each healthy segment directory holds `data_level0.bin`, `link_lists.bin`, and (after a flush) `index_metadata.pickle` (`mempalace/backends/chroma.py:L386-L396`, `mempalace/backends/chroma.py:L65-L66`, `mempalace/backends/chroma.py:L323-L323`). The default drawer collection name is `mempalace_drawers` (`mempalace/backends/chroma.py:L653-L653`).

Embedder identity is stored in a JSON sidecar file in the palace directory (filename `EMBEDDER_SIDECAR_FILENAME`), keyed by collection name, rather than in ChromaDB collection metadata (`mempalace/backends/chroma.py:L1751-L1772`).

Quarantined segment directories are renamed in place (never deleted) to `<segdir>.drift-<YYYYMMDD-HHMMSS>` for stale/payload-ratio corruption (`mempalace/backends/chroma.py:L422-L423`) or `<segdir>.corrupt-<YYYYMMDD-HHMMSS>` for invalid metadata (`mempalace/backends/chroma.py:L1027-L1028`). Migration completion is recorded by two marker files at the palace root: `.blob_seq_ids_migrated` and `.collection_type_fixed` (`mempalace/backends/chroma.py:L885-L886`).

## Metadata Filter Validation (where-clauses)

Supported operators are `$eq`, `$ne`, `$in`, `$nin`, `$and`, `$or`, `$contains` (required) plus `$gt`, `$gte`, `$lt`, `$lte` (optional) (`mempalace/backends/chroma.py:L40-L42`). Any key beginning with `$` that is not in this set MUST raise `UnsupportedFilterError`; silent dropping of unknown operators is forbidden. Validation walks the entire where-clause tree, descending into nested dicts and into list elements that are dicts (`mempalace/backends/chroma.py:L151-L169`). An empty/None where-clause is valid and validates trivially (`mempalace/backends/chroma.py:L156-L157`).

When filters are evaluated locally (during SQLite-path lexical search), boolean values are coerced to integers before comparison (`mempalace/backends/chroma.py:L222-L230`). `$eq`/`$ne` compare equality; `$in`/`$nin` test membership in the expected list (a missing list is treated as empty); `$contains` tests substring of the stringified expected value within the stringified actual value (`mempalace/backends/chroma.py:L231-L240`). The four range operators compare with standard ordering and return `False` rather than raising when the operand types are incomparable (`mempalace/backends/chroma.py:L241-L252`). `$and` requires all sub-clauses match; `$or` requires at least one; a bare scalar at a metadata key means exact equality; an unknown `$`-prefixed key raises `UnsupportedFilterError` (`mempalace/backends/chroma.py:L255-L278`).

## Lexical (BM25) Scoring

Tokenization lowercases text and extracts runs of two or more word characters (Unicode-aware) (`mempalace/backends/chroma.py:L43-L43`, `mempalace/backends/chroma.py:L172-L175`). BM25 uses `k1=1.5`, `b=0.75` defaults; returns all-zero scores when the query has no tokens, there are no documents, or all documents are empty (`mempalace/backends/chroma.py:L178-L193`). IDF uses the standard BM25 `log((N - df + 0.5)/(df + 0.5) + 1.0)` form, and per-document score sums `idf * freq*(k1+1) / (freq + k1*(1 - b + b*dl/avgdl))` over matching query terms (`mempalace/backends/chroma.py:L200-L218`).

## Collection Adapter: ChromaCollection

A `ChromaCollection` wraps an underlying ChromaDB collection and an optional `palace_path`. When `palace_path` is set, every write method (`add`, `upsert`, `update`, `delete`) acquires `mine_palace_lock(palace_path)` for the duration of the underlying call, serializing writers to prevent multi-threaded HNSW corruption; re-entrant acquisition from inside the mine pipeline is short-circuited by a per-thread guard. When `palace_path` is `None`, write-locking is a no-op (`mempalace/backends/chroma.py:L1201-L1238`).

### Write sanitization (observable contract on every write)

Before reaching ChromaDB, the metadatas list is sanitized: any entry that is `None` or an empty dict is replaced with the sentinel `{"_repaired_empty_meta": True}`, so the write succeeds and coerced drawers remain locatable via `where={"_repaired_empty_meta": True}` (`mempalace/backends/chroma.py:L1244-L1261`). Documents are sanitized by stripping lone UTF-16 surrogates (U+D800–U+DFFF) from every string; a bare string is treated as a single document, not split per-character (`mempalace/backends/chroma.py:L1263-L1290`). Both sanitizers pass `None` through unchanged (`mempalace/backends/chroma.py:L1256-L1257`, `mempalace/backends/chroma.py:L1280-L1281`).

### add / upsert / update

`add` and `upsert` take keyword-only `documents`, `ids`, optional `metadatas`, optional `embeddings`. Documents and metadatas are sanitized; `metadatas`/`embeddings` are passed only when not `None`; the underlying call runs inside the write lock (`mempalace/backends/chroma.py:L1292-L1316`). `update` takes keyword-only `ids` plus optional `documents`/`metadatas`/`embeddings`, and MUST raise `ValueError` if all three are `None`; only provided fields are forwarded, documents are surrogate-sanitized (`mempalace/backends/chroma.py:L1318-L1336`).

### query

Keyword-only inputs: exactly one of `query_texts` or `query_embeddings` MUST be provided (providing both or neither raises `ValueError`), and the chosen input MUST be a non-empty list (else `ValueError`) (`mempalace/backends/chroma.py:L1342-L1359`). `where` and `where_document` are validated. The `include` spec defaults to requesting distances; the backend translates the resolved spec into the ChromaDB include list of `documents`/`metadatas`/`distances`/`embeddings` (`mempalace/backends/chroma.py:L1361-L1383`). When the raw result has no ids, an empty `QueryResult` is returned with the correct per-query arity and embeddings shape (`mempalace/backends/chroma.py:L1387-L1398`). Otherwise the result returns parallel lists of `ids`, `documents`, `metadatas`, `distances` (each `None` inner list normalized to empty), and `embeddings` only when requested (`mempalace/backends/chroma.py:L1400-L1418`).

### get

Keyword-only `ids`/`where`/`where_document`/`limit`/`offset`/`include`. `where`/`where_document` are validated; include defaults to NOT requesting distances (`mempalace/backends/chroma.py:L1420-L1442`). Only provided parameters are forwarded (`mempalace/backends/chroma.py:L1443-L1452`). Ordering invariant: returned `documents` and `metadatas` lists are padded (with `""` and `{}` respectively) up to the length of `ids` so downstream positional zipping is always safe (`mempalace/backends/chroma.py:L1455-L1471`).

### delete / count

`delete` takes optional `ids` and `where` (validated), forwards only provided params, holds the write lock (`mempalace/backends/chroma.py:L1473-L1481`). `count` returns the underlying collection count (`mempalace/backends/chroma.py:L1483-L1484`).

### lexical_search

Returns BM25 lexical candidates. The fast path reads `chroma.sqlite3` directly (`_lexical_search_via_sqlite`); if that returns a non-None result it is used. Otherwise (e.g. a directly constructed collection with no palace path) it falls back to scanning all drawers in batches of 1000 through the client `get`, scoring with BM25, keeping only positive scores, sorting descending, and truncating to `n_results` (`mempalace/backends/chroma.py:L1486-L1537`).

The SQLite lexical path returns `None` when there is no palace path (signaling fallback), and `[]` when the DB file is missing or the collection name is unknown (`mempalace/backends/chroma.py:L1548-L1563`). Query tokens are filtered to length >= 3; if none remain a recency fallback is used (`mempalace/backends/chroma.py:L1565-L1566`). With tokens, candidates come from the `embedding_fulltext_search` FTS table matched with the tokens joined by ` OR `, scoped to the collection; when a metadata `where` filter is present the candidate set is NOT capped before filtering (to avoid hiding valid scoped hits behind common-term noise), otherwise it is limited to `max(max_candidates=500, n_results)` (`mempalace/backends/chroma.py:L1580-L1610`). Recency fallback selects candidates ordered by `created_at DESC`, falling back to `id DESC` if `created_at` ordering fails (`mempalace/backends/chroma.py:L1612-L1644`). Metadata is loaded in chunks of 900 ids from `embedding_metadata`, with the special key `chroma:document` populating the document text and all other keys populating metadata (`mempalace/backends/chroma.py:L1646-L1695`). Candidates are kept in FTS/recency order, dropped if they fail the local `where` match, scored by BM25, filtered to positive scores, sorted descending, and truncated to `n_results` (`mempalace/backends/chroma.py:L1697-L1720`). Returned `LexicalHit.id` is the public `embedding_id` (user-facing drawer id), distinct from the internal rowid used for joining (`mempalace/backends/chroma.py:L1568-L1572`, `mempalace/backends/chroma.py:L1709-L1714`).

### distance_metric / metadata

`metadata` returns the underlying collection metadata or `{}` if absent (`mempalace/backends/chroma.py:L1722-L1732`). `distance_metric` reads `hnsw:space` lowercased; returns it when it is one of `cosine`/`l2`/`ip`, otherwise returns `"l2"` (the ChromaDB HNSW default when cosine was never set) (`mempalace/backends/chroma.py:L1734-L1749`).

### Embedder identity

`get_stored_embedder_identity` reads the sidecar keyed by collection name; `set_embedder_identity` writes it. The sidecar path is `None` when there is no palace path (`mempalace/backends/chroma.py:L1763-L1772`).

## HNSW Corruption Detection (pre-open safety)

### Payload sanity heuristics

`link_lists.bin / data_level0.bin` size ratio is the corruption signal. The ratio is `None` (not meaningful) when either file is missing or `data_level0.bin` is empty; it is treated as `inf` (suspicious) when present but unstattable (`mempalace/backends/chroma.py:L56-L80`). A non-trivial payload (data file larger than the 1024-byte floor) MUST have a non-empty `link_lists.bin` to be usable; below that floor an empty/absent link file is acceptable (`mempalace/backends/chroma.py:L83-L104`, `mempalace/backends/chroma.py:L144-L148`). Payload "appears sane" iff link lists are usable AND the ratio is `None` or `<= 10.0` (`mempalace/backends/chroma.py:L53-L53`, `mempalace/backends/chroma.py:L107-L113`).

### Segment health sniff-test

A segment is judged healthy without ever deserializing the pickle. When `index_metadata.pickle` is absent, the segment is healthy iff `link_lists.bin` is empty/absent (sub-threshold, never persisted — vs. an interrupted persist that wrote link data without metadata) (`mempalace/backends/chroma.py:L323-L333`). When the pickle is present, the payload must appear sane, the pickle must be at least 16 bytes, and its first two bytes must be `0x80 ??` with a final byte of `0x2e` (pickle protocol >= 2 start marker and terminator) (`mempalace/backends/chroma.py:L335-L348`). This is deliberately a byte-sniff only; deserialization is never performed here because it can execute arbitrary code (`mempalace/backends/chroma.py:L311-L321`).

### quarantine_stale_hnsw

Renames segment dirs unsafe to open. Returns `[]` and does nothing if `chroma.sqlite3` is absent or unstattable, or the palace dir is unlistable (`mempalace/backends/chroma.py:L351-L384`). It skips names without `-`, starting with `.`, or already containing `.drift-`, and skips non-directories or segments lacking `data_level0.bin` (`mempalace/backends/chroma.py:L386-L396`). A segment is quarantined when EITHER its payload ratio exceeds 10x (corruption, NOT gated by mtime — opening such a segment can SIGSEGV) OR the `chroma.sqlite3` mtime is at least `stale_seconds` (default 300s) newer than the segment's `data_level0.bin` mtime AND the segment fails the health sniff-test (`mempalace/backends/chroma.py:L398-L420`). Healthy-but-stale segments are left in place and logged as flush-lag (`mempalace/backends/chroma.py:L412-L420`). Quarantine renames `seg_dir` to `seg_dir.drift-<stamp>`; on rename failure it logs and continues. Returns the list of new target paths (`mempalace/backends/chroma.py:L422-L448`).

### quarantine_invalid_hnsw_metadata

Scans segment dirs and renames any whose `index_metadata.pickle` is unreadable or structurally invalid, so ChromaDB rebuilds cleanly instead of crashing in the native loader. Skips names without `-`, starting with `.`, or containing `.drift-`/`.corrupt-`, non-directories, and segments without a metadata pickle (`mempalace/backends/chroma.py:L943-L967`). The pickle is loaded through a whitelist-only unpickler (only `chromadb...PersistentData` is allowed; any other class raises) (`mempalace/backends/chroma.py:L505-L539`). Transient read errors (EOF/OS errors, or unpickling errors mentioning "truncated"/"ran out of input") are skipped without quarantine (`mempalace/backends/chroma.py:L972-L986`). A segment is quarantined when: the payload is an unrecognized type, `id_to_label` is present but not a dict, labels are present but dimensionality is missing/invalid AND not recoverable, or dimensionality is present but invalid (not a positive non-boolean integer) (`mempalace/backends/chroma.py:L889-L890`, `mempalace/backends/chroma.py:L991-L1022`). "Recoverable missing dimensionality" requires an integer `total_elements_added >= live label count`, a consistent bijective `label_to_id`/`id_to_label` pair of equal sizes, sane payload files, and data above the floor — this avoids wrongly quarantining post-deletion segments (`mempalace/backends/chroma.py:L905-L941`). Quarantine renames to `seg_dir.corrupt-<stamp>` and the list of targets is returned (`mempalace/backends/chroma.py:L1024-L1036`).

### HNSW capacity / divergence probe

`hnsw_capacity_status(palace_path, collection_name="mempalace_drawers")` compares the SQLite embedding count against the element count the HNSW pickle knows about, to detect the failure where HNSW capacity froze while SQLite kept growing (which would segfault on open). It NEVER raises; on any internal error it returns a status dict with `status="unknown"` (`mempalace/backends/chroma.py:L653-L737`). The returned dict has keys `segment_id`, `sqlite_count`, `hnsw_count`, `divergence`, `diverged`, `status` (one of `"ok"`/`"diverged"`/`"unknown"`), and `message` (`mempalace/backends/chroma.py:L662-L682`). If the VECTOR segment id or SQLite count is unreadable, status stays `unknown` (`mempalace/backends/chroma.py:L684-L693`). If the HNSW pickle has not been flushed (`hnsw_count` is `None`), vector search is left enabled and status stays `unknown` rather than disabling vectors on an inconclusive signal (`mempalace/backends/chroma.py:L703-L715`). Divergence is `sqlite_count - hnsw_count`; it is flagged `diverged` when it exceeds `max(divergence_floor, 10% of sqlite_count)`, where `divergence_floor = max(2000, 2 * sync_threshold)` (`mempalace/backends/chroma.py:L608-L609`, `mempalace/backends/chroma.py:L698-L728`). The configured `hnsw:sync_threshold` is read from `collection_metadata` (default 1000 when unreadable/absent) (`mempalace/backends/chroma.py:L612-L650`).

### Element/embedding counting (direct SQLite reads)

The element count is read by safely unpickling `index_metadata.pickle` (allowlist unpickler, hnswlib not required) and counting `id_to_label` entries; returns `None` when the file is absent or the unpickle fails (`mempalace/backends/chroma.py:L542-L583`). The SQLite embedding count joins `embeddings -> segments -> collections` filtered by collection name; returns `None` on missing DB or any SQLite error (`mempalace/backends/chroma.py:L740-L766`). The VECTOR segment id is read directly from `segments`/`collections` where `scope='VECTOR'`, returning `None` on missing DB or error (`mempalace/backends/chroma.py:L451-L477`). All direct reads open the DB through a read-only URI helper to avoid loading a segment that might segfault (`mempalace/backends/chroma.py:L461-L461`).

`_sqlite_wing_room_counts` tallies drawers per wing/room directly from SQLite without opening the collection (which would cold-load the vector index, costing ~60s on large palaces). It uses `busy_timeout = 3000` to wait out transient locks, returns `None` when the DB is missing, the collection is not bootstrapped, or any SQLite error occurs (signaling fallback to the client path), and otherwise returns `(total, {wing: {room: count}})`. Numeric wing/room values are coalesced from `string_value`/`int_value`/`float_value`, defaulting to `"?"`; the metadata join is explicitly scoped to `s.scope = 'METADATA'` to avoid double counting (`mempalace/backends/chroma.py:L769-L853`).

## SQLite Migrations (run before opening)

### _fix_blob_seq_ids

Repairs the ChromaDB 0.6.x->1.5.x bug where `seq_id` was stored as big-endian 8-byte BLOBs but 1.5.x expects INTEGER, which crashes the Rust compactor. Skipped entirely if the DB is missing or the `.blob_seq_ids_migrated` marker exists (`mempalace/backends/chroma.py:L1039-L1075`). Scoped to the `embeddings` table only (the `max_seq_id` table is left to ChromaDB). Rows whose BLOB starts with the sysdb-10 prefix `0x11 0x11` are skipped (logged), not converted; remaining BLOB rows are converted via big-endian decode to integers and committed (`mempalace/backends/chroma.py:L1076-L1099`). It MUST run before `PersistentClient` construction. After a successful run (whether or not rows changed) the marker file is touched so future opens skip the SQLite connection entirely (`mempalace/backends/chroma.py:L1062-L1107`).

### _fix_missing_collection_type

Adds a `_type` key to `collections.config_json_str` where absent, because chromadb 1.5.9+ raises `KeyError: '_type'` on open when the older versions wrote `{}`. Skipped if DB missing or `.collection_type_fixed` marker exists (`mempalace/backends/chroma.py:L1110-L1132`). For each collection row, missing/empty config is treated as `{}`; rows whose config is unparseable JSON or not a dict are skipped; otherwise `_type` is set to `"CollectionConfigurationInternal"` and updates are committed (`mempalace/backends/chroma.py:L1133-L1166`). Must run before `PersistentClient` construction; the marker is touched on completion (`mempalace/backends/chroma.py:L1124-L1170`).

## Backend: ChromaBackend

Name is `"chroma"`. Declared capabilities: embeddings-in, embeddings-passthrough, embeddings-out, metadata-filters, contains-fast, lexical-search, local-mode (`mempalace/backends/chroma.py:L1780-L1803`). `detect(path)` returns true iff `path/chroma.sqlite3` exists (`mempalace/backends/chroma.py:L2115-L2117`). `backend_version()` returns the installed chromadb version string (`mempalace/backends/chroma.py:L2011-L2014`).

### Client caching and freshness

The backend caches one `PersistentClient` per palace path plus the `(inode, mtime)` of `chroma.sqlite3` at cache time (`mempalace/backends/chroma.py:L1805-L1810`). `_client` rebuilds the client when: the cached DB file has disappeared (cache invalidated), the inode changed (both sides nonzero), the mtime changed by more than 0.01s, or a stat appeared where there was none (DB created after caching). FAT/exFAT inode-0 cases never fire inode comparisons but still honor mtime (`mempalace/backends/chroma.py:L1875-L1939`). On any detected disk change the per-process quarantine gate for that path is cleared so the HNSW pre-checks re-run; then `_prepare_palace_for_open` runs and a new client is constructed; freshness is re-stat'd afterward because the DB may be created lazily (`mempalace/backends/chroma.py:L1919-L1939`). Operating on a closed backend raises `BackendClosedError` (`mempalace/backends/chroma.py:L1892-L1895`).

### Pre-open safety pass

`_prepare_palace_for_open` runs four ordered steps before any `PersistentClient` is created: (1) `_fix_missing_collection_type`, (2) `_fix_blob_seq_ids`, then gated by a per-process `_quarantined_paths` set, (3) `quarantine_invalid_hnsw_metadata`, (4) `quarantine_stale_hnsw`; after running steps 3-4 the path is added to the gate so they fire once per palace until a disk change re-arms them. The pass is idempotent (`mempalace/backends/chroma.py:L1945-L1995`). The gate is mutated without a lock; concurrent first-opens may both run the idempotent quarantine, producing at worst one redundant no-op rename (`mempalace/backends/chroma.py:L1954-L1961`).

### get_collection

Accepts both the new keyword form (`palace=PalaceRef, collection_name=..., create=False, options=None`) and legacy positional `(palace_path, collection_name, create)`; argument normalization rejects mixing styles and unexpected args with `TypeError` (`mempalace/backends/chroma.py:L2020-L2034`, `mempalace/backends/chroma.py:L2149-L2204`). The palace must resolve to a local path or `PalaceNotFoundError` is raised; with `create=False` a non-existent palace directory raises `PalaceNotFoundError` (`mempalace/backends/chroma.py:L2036-L2041`). With `create=True` the directory is created and chmod'd to `0o700` (best-effort) (`mempalace/backends/chroma.py:L2043-L2048`).

The embedding function is resolved and passed explicitly to both get and create paths because ChromaDB 1.x does not persist it; a reader omitting it would silently get the library default and mismatch the writer's vectors (`mempalace/backends/chroma.py:L1812-L1827`, `mempalace/backends/chroma.py:L2055-L2056`). HNSW space defaults to `"cosine"`, overridable via `options["hnsw_space"]` (`mempalace/backends/chroma.py:L2051-L2053`). On `create=True`, the collection is fetched if it exists, else created with metadata `{"hnsw:space": <space>, "hnsw:num_threads": 1, "hnsw:batch_size": 2, "hnsw:sync_threshold": 2}` (`mempalace/backends/chroma.py:L116-L142`, `mempalace/backends/chroma.py:L2058-L2070`). On `create=False`, a missing collection raises `CollectionNotInitializedError` (`mempalace/backends/chroma.py:L2076-L2080`). On either path, a `ValueError` that looks like an embedding-function-name mismatch is translated into a user-friendly explanation naming the current `MEMPALACE_EMBEDDING_MODEL` and the two recovery options (revert the model, or rebuild-index); otherwise it re-raises unchanged (`mempalace/backends/chroma.py:L1829-L1859`, `mempalace/backends/chroma.py:L2071-L2085`). Before returning, `hnsw:num_threads=1` is re-applied in memory on every open (legacy palaces built without it are retrofitted), and the result is wrapped in a `ChromaCollection` carrying the palace path (`mempalace/backends/chroma.py:L856-L883`, `mempalace/backends/chroma.py:L2086-L2087`).

The `hnsw:batch_size`/`hnsw:sync_threshold` of 2 is the empirical minimum for chromadb >= 1.5.4 (1 is rejected) that guarantees any mine of 2+ drawers triggers a natural persist, so `index_metadata.pickle` and `link_lists.bin` are written rather than left empty (`mempalace/backends/chroma.py:L116-L142`).

### Lifecycle

`close_palace(palace)` accepts a `PalaceRef` or path string, drops the cached client and freshness entry, and calls the client's close to release the rust-side SQLite file lock (without which the path is unreopenable/unremovable in-process) (`mempalace/backends/chroma.py:L2089-L2101`). `close()` closes every cached client, clears caches, and marks the backend closed (`mempalace/backends/chroma.py:L2103-L2108`). `health()` returns unhealthy when closed, else healthy (`mempalace/backends/chroma.py:L2110-L2113`).

### Legacy surface

`get_or_create_collection(palace_path, collection_name)` is a shim for `get_collection(..., create=True)` (`mempalace/backends/chroma.py:L2123-L2125`). `delete_collection(palace_path, collection_name)` deletes the named collection (`mempalace/backends/chroma.py:L2127-L2129`). `create_collection(palace_path, collection_name, hnsw_space="cosine")` always creates (never get-or-create) with the same HNSW metadata block (`mempalace/backends/chroma.py:L2131-L2146`).
