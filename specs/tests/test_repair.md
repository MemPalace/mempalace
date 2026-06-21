# Behavior Spec — `mempalace.repair` (derived from `tests/test_repair.py`)

This spec describes the observable behavior of the `repair` module as exercised by its test suite: scanning a palace for corrupt rows, pruning them, rebuilding the vector index, repairing poisoned `max_seq_id` bookmarks, extracting/rebuilding via the SQLite bypass, and vacuuming the SQLite store. A "palace" is a directory containing a `chroma.sqlite3` file plus a ChromaDB-style storage layout. The module talks to a pluggable storage backend (`ChromaBackend`) and to the raw `chroma.sqlite3` file directly.

The test file is purely a verification harness; the contracts below are the externally observable behaviors the implementation must satisfy.

## Configuration helpers

`_get_palace_path()` returns the palace directory as a string. It is sourced from configuration when available; otherwise it falls back to `<home>/.mempalace/palace` (tests/test_repair.py:L16-L29).

`_drawers_collection_name()` returns the configured drawers collection name; given a configured `collection_name` of `custom_drawers`, it returns `"custom_drawers"`. The default name used elsewhere when unconfigured is `mempalace_drawers` (tests/test_repair.py:L32-L39, L346-L349).

## ID pagination — `_paginate_ids(col, where=None)`

Returns a list of row IDs from a collection. A single page of `{"ids": [...]}` is returned verbatim and in order (tests/test_repair.py:L45-L49). An empty collection returns `[]` (tests/test_repair.py:L52-L56). When a `where` filter is supplied it is forwarded to the backend `get` along with `include=[]`, `limit=1000`, `offset=0` (tests/test_repair.py:L59-L63). If a backend `get` call raises (e.g. an offset bug), pagination falls back to a retry path; it continues collecting IDs and terminates when a page yields no new IDs (tests/test_repair.py:L66-L76).

## Drawer extraction — `_extract_drawers(col, total, batch_size)`

Returns a 3-tuple `(all_ids, all_docs, all_metas)`. Valid non-empty metadata dicts pass through unchanged and aligned to their IDs and documents (tests/test_repair.py:L82-L93).

Invalid metadata entries are sanitized to a sentinel dict `{"_repaired_empty_meta": True}`: a `None` entry becomes the sentinel (tests/test_repair.py:L96-L112); an empty dict `{}` entry also becomes the sentinel (tests/test_repair.py:L115-L129). This sanitization protects a later rebuild upsert from rejecting empty/None metadata.

Critical ordering invariant: the sanitizer preserves length and ordering — `all_ids`, `all_docs`, `all_metas` stay in lockstep so that index `i` always refers to the same row across all three lists (tests/test_repair.py:L132-L151). Pagination across multiple batches loses no rows and duplicates none; results are concatenated in batch order and sanitized identically (tests/test_repair.py:L154-L164).

## Scan — `scan_palace(palace_path, only_wing=None) -> (good, bad)`

Returns two sets of row IDs: `good` (readable) and `bad` (corrupt). An empty collection yields two empty sets (tests/test_repair.py:L179-L187). When all IDs read back successfully via a batch probe, all are placed in `good` and `bad` is empty (tests/test_repair.py:L191-L204).

Corruption detection works by probing: if a batch probe fails, the scan falls back to probing IDs individually; an ID that reads back goes to `good`, an ID whose individual read raises goes to `bad` (tests/test_repair.py:L208-L229). When `only_wing` is supplied, the first backend `get` is called with `where={"wing": <only_wing>}` (tests/test_repair.py:L233-L245).

## Prune — `prune_corrupt(palace_path, confirm=False)`

Reads a file named `corrupt_ids.txt` in the palace directory (newline-separated IDs). If that file is absent, it prints a message and returns without error and without touching the backend (tests/test_repair.py:L252-L254). In dry-run mode (`confirm=False`) it makes no backend calls (tests/test_repair.py:L257-L263).

When confirmed (`confirm=True`), it deletes the listed IDs from the collection via a batch delete (tests/test_repair.py:L266-L276). If the batch delete raises, it falls back to deleting each ID individually; e.g. for two bad IDs the total delete-call count is 3 (one failed batch plus two individual deletes) (tests/test_repair.py:L279-L291).

## Index rebuild — `rebuild_index(palace_path, confirm_truncation_ok=False)`

### No-op / guard cases

If the palace path does not exist, no backend is instantiated (tests/test_repair.py:L298-L301). If the collection count is 0 (empty palace), the live collection is not deleted (tests/test_repair.py:L306-L312). If reading the collection raises, no `delete_collection` occurs (tests/test_repair.py:L416-L422).

### SQLite integrity preflight (must run before opening the backend)

Before any backend collection is opened, a SQLite integrity preflight runs. If `sqlite_integrity_errors` reports problems, `rebuild_index` aborts: it prints a message containing `"SQLite-layer corruption detected before repair rebuild"`, references `"PRAGMA quick_check"` and `"delete_collection"`, echoes the reported error lines, and performs no `delete_collection`, no `create_collection`, and no backup copy (tests/test_repair.py:L1346-L1387). This preflight runs against a genuinely corrupt `chroma.sqlite3` (mangled middle pages) and must complete cleanly without the backend ever being opened — opening a corrupt store would otherwise crash past normal exception handling (tests/test_repair.py:L1390-L1458).

### Truncation safety guard

Before rebuilding, the extracted row count is checked against the SQLite-reported drawer count. If SQLite reports many more drawers than were extracted (e.g. SQLite 67,580 vs extracted 10,000), the rebuild aborts: no `delete_collection`, no `create_collection`, no backup (tests/test_repair.py:L667-L691). Passing `confirm_truncation_ok=True` overrides the guard and lets the rebuild proceed (3 delete_collection calls, 2 create_collection calls, upserts on both temp and new collections) (tests/test_repair.py:L696-L723).

### Poisoned-bookmark preflight

If poisoned `max_seq_id` rows are detected, `rebuild_index` short-circuits into a non-destructive `max_seq_id` repair and does NOT proceed into the legacy collection read/count/rebuild path. It prints `"Detected poisoned max_seq_id rows"` and `"non-destructive max_seq_id repair"`, and the backend's `get_collection` is never called for the legacy rebuild (instantiating the backend only to close cached clients is permitted) (tests/test_repair.py:L1505-L1524).

### Successful rebuild (temp-collection swap protocol)

On success the rebuild uses a temp-then-swap protocol against a collection named `<name>__repair_tmp`:

- Only the `chroma.sqlite3` file is backed up via a single file copy; the whole directory is not copied (tests/test_repair.py:L342-L343, L956).
- `create_collection` is called twice, in order: first `<name>__repair_tmp`, then `<name>` (tests/test_repair.py:L346-L349).
- `delete_collection` is called three times, in order: `<name>__repair_tmp`, `<name>`, `<name>__repair_tmp` (delete leftover temp, delete live, delete temp after build) (tests/test_repair.py:L350-L354).
- Rows are written with `upsert`, never `add`, on both the temp and new collections (tests/test_repair.py:L356-L359).

The default collection name comes from configuration: with a configured `custom_drawers`, `get_collection`, the SQLite count, and all create/delete calls target `custom_drawers` and `custom_drawers__repair_tmp` (closets are not part of this), and the SQLite count is queried as `(palace, "custom_drawers")` (tests/test_repair.py:L505-L538).

Deleting the leftover temp collection at start tolerates a "does not exist" error: if the first `delete_collection` raises a not-found `ValueError`, the rebuild continues and still produces the standard three-call delete sequence (tests/test_repair.py:L364-L403).

### Backend close + VACUUM ordering

After a successful rebuild, the backend handles are closed and then the FTS5 vacuum runs — strictly in that order ("close" before "vacuum"). `_vacuum_and_rebuild_fts5` is called once, with the palace path as its first positional argument and a `progress` keyword argument present (tests/test_repair.py:L1954-L1997).

### Failure / rollback behavior

`_delete_collection_if_exists(backend, palace, name)` re-raises an unexpected `ValueError` (one not matching a "does not exist" pattern) rather than swallowing it (tests/test_repair.py:L406-L411).

If the temp/staging collection ends with a count that does not match expectations (e.g. temp count 1 when 2 rows were extracted), the rebuild raises `RebuildCollectionError` with `live_replaced == False`, the live collection is left untouched, exactly one sqlite backup copy is made, and `delete_collection` is called only on the temp name (twice: pre-clean and post-failure cleanup) (tests/test_repair.py:L728-L754).

If the live upsert fails after the live collection has already been deleted, the error is `RebuildCollectionError` with `live_replaced == True`. Two sqlite copies occur (backup, then restore). The active backend's `delete_collection` call sequence is `tmp, live, tmp, live`, and `close_palace(palace)` is called once on the active backend (a helper backend used for restore is not closed) (tests/test_repair.py:L759-L798). If, during this restore path, a live `delete_collection` is missing (raises a not-found error), the rebuild still treats `live_replaced` as True and still performs the restore copy, with the same `tmp, live, tmp, live` delete sequence (tests/test_repair.py:L803-L843).

If restoring the backup itself fails (e.g. a locked sqlite during the restore copy), the original error is preserved: output contains the restore error text (`"locked sqlite"`) and `"Manual restore required"`, and the raised `RebuildCollectionError` still carries the original failure message (`"live upsert failed"`) (tests/test_repair.py:L848-L882).

`_rebuild_collection_via_temp(backend, palace, ids, docs, metas, batch_size, progress)` keeps the original error when post-failure cleanup also fails: it raises `RebuildCollectionError` carrying the original build error (`"live build failed"`) with `live_replaced == True`, and the `delete_collection` sequence is `tmp, live, tmp` (tests/test_repair.py:L886-L918).

A temp-cleanup failure AFTER an otherwise successful rebuild is ignored (not propagated): the rebuild completes normally, one backup copy is made, and the delete sequence is the standard `tmp, live, tmp` (tests/test_repair.py:L923-L961).

## Truncation safety primitives

`check_extraction_safety(palace_path, extracted_count, collection_name=None, confirm_truncation_ok=False)`:

- Passes silently when the SQLite count equals the extracted count (tests/test_repair.py:L428-L431).
- Uses `sqlite_drawer_count(palace, collection_name)` with the supplied collection name (tests/test_repair.py:L434-L437), or the configured drawers collection name when none is supplied (tests/test_repair.py:L440-L446).
- Passes when the SQLite count is unreadable (`None`) but the extracted count is well under the cap (tests/test_repair.py:L449-L452).
- Raises `TruncationDetected` when SQLite reports more than was extracted. The exception exposes `sqlite_count`, `extracted`, and a `message` containing the SQLite count, extracted count, and the loss (difference) — all formatted with thousands separators (e.g. `"67,580"`, `"10,000"`, `"57,580"`) (tests/test_repair.py:L455-L467).
- Raises `TruncationDetected` when the SQLite count is unreadable AND the extracted count equals the default get cap `CHROMADB_DEFAULT_GET_LIMIT` (which equals 10,000 by the formatted message); the exception's `sqlite_count` is `None` and `extracted` equals the cap (tests/test_repair.py:L470-L480).
- `confirm_truncation_ok=True` short-circuits all checks and passes even when SQLite reports far more rows (tests/test_repair.py:L483-L487).

`sqlite_drawer_count(palace_path)` returns `None` (never crashes) when `chroma.sqlite3` is absent (tests/test_repair.py:L490-L492) or when the file exists but is not a valid SQLite database (tests/test_repair.py:L495-L500).

## Status — `status(palace_path) -> dict`

Returns a structured status dict and must work on corrupted/uninitialized palaces without opening a vector-store client.

- No `chroma.sqlite3` present: returns `{"status": "uninitialized", ...}` with a `message` containing `"no chroma.sqlite3"`, and prints text containing `"has no chroma.sqlite3 yet"` (tests/test_repair.py:L541-L551).
- `chroma.sqlite3` present but zero drawer rows: returns `{"status": "empty", ...}` with `message` containing `"no drawers yet"`, and prints text containing `"initialized but empty"` (tests/test_repair.py:L554-L567).
- Design invariant: on an initialized-but-empty palace, `status` must NOT open a backend client. Opening one would materialize HNSW segment/state files on disk. Verified by snapshotting the directory before and after — the file listing must be unchanged (tests/test_repair.py:L570-L592).
- When `sqlite_drawer_count` returns `None` (schema drift / locked file) it must NOT short-circuit on `"empty"`; instead it falls through to a capacity check (`hnsw_capacity_status`). The healthy/fall-through return shape has `drawers` and `closets` keys and no top-level `"empty"` status (tests/test_repair.py:L595-L628).
- The capacity probe is performed against the configured drawers collection first, then the fixed `mempalace_closets` collection: for a non-empty palace the two `hnsw_capacity_status` calls receive `(palace, <configured-drawers>)` then `(palace, "mempalace_closets")` (tests/test_repair.py:L631-L662).

## Poisoned `max_seq_id` repair

ChromaDB stores a per-segment `max_seq_id` bookmark. A known corruption ("poison") writes absurdly large values (an 8-byte big-endian misread of a `\x11\x11` + 6 ASCII-digit value, e.g. `1229822654365970487`). The detector and repair operate directly on the `chroma.sqlite3` file (tests/test_repair.py:L967-L1065).

`_detect_poisoned_max_seq_ids(db_path)` returns a list of `(segment_id, seq_id)` pairs where `seq_id` exceeds `MAX_SEQ_ID_SANITY_THRESHOLD`. Clean rows (e.g. `seq_id = 1234`) are excluded; all returned values are above the threshold (tests/test_repair.py:L1068-L1091).

`repair_max_seq_id(palace_path, dry_run=False, from_sidecar=None, segment=None, assume_yes=False)` returns a dict including keys `after` (proposed clean values per segment), `segment_repaired` (list of repaired segment IDs), `dry_run` (bool), and `backup` (path or `None`).

- Heuristic source-of-truth: each poisoned segment is restored to its collection's maximum `seq_id` from the `embeddings` table. Both segments of a collection (VECTOR and METADATA) receive that collection's max; drawers segments get the drawers collection max, closets segments get the closets collection max (tests/test_repair.py:L1094-L1104).
- The heuristic must decode BLOB-typed `embeddings.seq_id` values as 8-byte big-endian unsigned integers (chromadb 1.5.x native format), not crash; the computed max reflects the decoded blob value (tests/test_repair.py:L1173-L1196).
- Sidecar path: when `from_sidecar=<path>` points at a SQLite file containing a `max_seq_id` table, those exact values are restored (preferred over the heuristic). `segment_repaired` is populated and the live `max_seq_id` rows match the sidecar's values exactly (tests/test_repair.py:L1107-L1134).
- Dry run: `dry_run=True` returns `dry_run == True` and `segment_repaired == []`, mutates nothing on disk (before/after `max_seq_id` rows identical), and writes no backup file (no `chroma.sqlite3.max-seq-id-backup-*`) (tests/test_repair.py:L1137-L1154).
- Segment filter: `segment=<id>` repairs only that segment; `segment_repaired` lists only it, and the other poisoned segments remain above the sanity threshold (tests/test_repair.py:L1157-L1170).
- No-poison no-op: a palace with only clean bookmarks returns `segment_repaired == []`, `backup is None`, and leaves rows unchanged (tests/test_repair.py:L1199-L1225).

### Backups and retention

When a repair mutates data, a backup file is written into the palace directory named `chroma.sqlite3.max-seq-id-backup-<timestamp>`; `result["backup"]` is that path and the file exists. The backup preserves the pre-repair (poisoned) `max_seq_id` values (tests/test_repair.py:L1228-L1240).

Retention is bounded by the `MEMPALACE_MAX_BACKUPS` environment variable. After a repair, only the N newest `max-seq-id-backup-*` files are kept (e.g. with `MEMPALACE_MAX_BACKUPS=2`, 4 stale + 1 fresh leaves 2 survivors, and the freshly created backup is among them) (tests/test_repair.py:L1243-L1270). Setting `MEMPALACE_MAX_BACKUPS=0` disables pruning — every backup is kept (3 stale + 1 fresh = 4 remain) (tests/test_repair.py:L1273-L1291).

### Verification rollback

After updating bookmarks, a post-update detection runs. If poison is still detected, the repair raises `MaxSeqIdVerificationError` and leaves a backup file on disk so the caller can roll back (tests/test_repair.py:L1294-L1317).

### Queue preservation preflight

`maybe_repair_poisoned_max_seq_id_before_rebuild(palace_path, assume_yes=False)` returns `None` when there is nothing to do, otherwise a dict with a populated `segment_repaired`. When it repairs poisoned bookmarks it must NOT discard queued writes: existing `embeddings_queue` rows survive (e.g. 20 queued rows remain), while each poisoned segment's `max_seq_id` is reset to its collection max (tests/test_repair.py:L1461-L1502).

## SQLite integrity — `sqlite_integrity_errors(palace_path) -> list[str]`

Returns `[]` for a healthy SQLite database (tests/test_repair.py:L1320-L1329). For an unreadable/invalid SQLite file it returns a non-empty list whose first element contains `"quick_check failed"` (tests/test_repair.py:L1332-L1341).

## SQLite-bypass extraction — `extract_via_sqlite(palace_path, collection_name)`

Yields `(embedding_id, document, metadata)` tuples read directly from `chroma.sqlite3`, bypassing the vector store.

- Round-trips all rows: N upserted rows are returned as N tuples with documents and metadata intact and matched by ID (tests/test_repair.py:L1559-L1588). The rows are read from the `METADATA`-scope segment (documents/metadata live under `METADATA`; HNSW lives under `VECTOR`); the extraction must read only `METADATA`-scope segments (tests/test_repair.py:L1590-L1617).
- Preserves metadata value types: integers, floats, and booleans round-trip as their original types (e.g. `chunk_index` stays an int `7`, `score` stays float `0.42`, `is_active` stays boolean `True`), not coerced to strings (tests/test_repair.py:L1620-L1650).
- Filters strictly by collection name: querying an unknown collection yields an empty result even when other collections exist (no leakage across collections) (tests/test_repair.py:L1653-L1662).
- Missing palace (no `chroma.sqlite3`): yields nothing, raises nothing (tests/test_repair.py:L1665-L1670).

## SQLite-bypass rebuild — `rebuild_from_sqlite(source, dest, archive_existing_dest=False)`

Reads rows from `source` via the SQLite bypass and writes them into a fresh palace at `dest`. Returns a dict mapping collection name to rebuilt row count.

- End-to-end round-trip: rebuilding 40 drawers + 1 closet yields `{"mempalace_drawers": 40, "mempalace_closets": 1}`; the destination palace reports matching counts, metadata `where` filters return the correct subsets, and specific document bodies and metadata round-trip exactly (tests/test_repair.py:L1673-L1723).
- Refuses an existing different destination: when `source != dest` and `dest` already exists, it returns `{}` and leaves `dest` untouched (a pre-existing marker file and the absence of `chroma.sqlite3` are preserved) (tests/test_repair.py:L1726-L1741).
- In-place rebuild (`source == dest`) requires opt-in. With `archive_existing_dest=True` it moves the original aside to `<dest>.pre-rebuild-<timestamp>`, reads from that archive, and rebuilds into the original location. Exactly one archive directory is created, the archive still contains `chroma.sqlite3` with the original row count, and the rebuilt palace has the same count (tests/test_repair.py:L1744-L1772).
- In-place without the archive flag aborts untouched: returns `{}`, leaves `chroma.sqlite3` unchanged in size, and creates no archive — protecting against deleting the only copy of the data (tests/test_repair.py:L1775-L1788).
- Source missing `chroma.sqlite3`: returns `{}` and leaves `dest` non-existent (tests/test_repair.py:L1791-L1801).
- In-place + archive must validate the source BEFORE archiving: a directory lacking `chroma.sqlite3` must NOT be renamed first; it returns `{}`, leaves the original dir and its marker file in place, and creates no archive (tests/test_repair.py:L1804-L1820).
- Mid-rebuild upsert failure raises `RebuildPartialError` exposing `failed_collection` (the collection that failed), `partial_counts` (e.g. `{"mempalace_drawers": 0}`), `archive_path` (a directory still containing `chroma.sqlite3`), and `dest_palace` (the absolute destination path) (tests/test_repair.py:L1823-L1855).
- Honors the configured drawers collection name: a palace whose drawers collection is custom-named has THAT collection rebuilt (counts keyed by the custom name), and the default `mempalace_drawers` name must not receive any rows in the destination. Closets stay fixed at `mempalace_closets` by design (tests/test_repair.py:L1858-L1908).

## FTS5 vacuum — `_vacuum_and_rebuild_fts5(palace_path)`

VACUUMs `chroma.sqlite3` and rebuilds the FTS5 full-text index when the `embedding_fulltext_search` virtual table is present; afterward `PRAGMA integrity_check` returns `ok` (tests/test_repair.py:L1914-L1929). It runs VACUUM without error when the FTS5 table is absent, leaving the DB integrity-clean (tests/test_repair.py:L1932-L1944). It silently skips (raises nothing) when `chroma.sqlite3` does not exist (tests/test_repair.py:L1947-L1949).

## Named error/exception types (observable contract)

The module exposes these named exceptions and sentinels used by callers: `TruncationDetected` (fields `sqlite_count`, `extracted`, `message`) (tests/test_repair.py:L460-L463); `CHROMADB_DEFAULT_GET_LIMIT` constant (tests/test_repair.py:L474-L478); `RebuildCollectionError` (field `live_replaced`) (tests/test_repair.py:L746-L749, L789, L836, L913); `ChromaNotFoundError` (tests/test_repair.py:L830); `MAX_SEQ_ID_SANITY_THRESHOLD` constant (tests/test_repair.py:L1090); `MaxSeqIdVerificationError` (tests/test_repair.py:L1312); `RebuildPartialError` (fields `failed_collection`, `partial_counts`, `archive_path`, `dest_palace`) (tests/test_repair.py:L1847-L1855).
