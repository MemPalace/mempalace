# Behavior Specification — `mempalace/repair.py`

Palace repair toolkit: scan/prune corrupt drawer IDs, rebuild the HNSW index, recover a palace by direct SQLite extraction, run read-only health checks, and un-poison `max_seq_id` bookmark rows. The module purpose is described in its header: when the vector index accumulates duplicate entries the index file can grow unbounded and crash; this module provides `status`, `scan`, `prune`, and `rebuild` operations (`mempalace/repair.py:L1-L30`).

## Constants and naming contracts

The default drawer collection name is `mempalace_drawers`, and a temporary collection name is derived as `mempalace_drawers__repair_tmp` (`mempalace/repair.py:L49-L50`). The closets (AAAK index layer) collection name is fixed at `mempalace_closets` and is intentionally not configurable, because closets reference drawer IDs by string and renaming would break cross-palace lookups (`mempalace/repair.py:L52-L57`).

The drawer collection name is resolved from user configuration's `collection_name`, falling back to `mempalace_drawers` when config is empty or unreadable (`mempalace/repair.py:L60-L74`). The set of collections rebuilt by `rebuild_from_sqlite` is the resolved drawer collection followed by the closets collection, in that order (drawers first as bulk data, closets second; ordering is informational, not load-bearing) (`mempalace/repair.py:L77-L85`). A back-compat alias `RECOVERABLE_COLLECTIONS` holds the static pair `(mempalace_drawers, mempalace_closets)` (`mempalace/repair.py:L88-L91`).

The palace path is resolved from config's `palace_path`, falling back to `~/.mempalace/palace` when config is unreadable (`mempalace/repair.py:L94-L102`).

`CHROMADB_DEFAULT_GET_LIMIT` is `10000` — the underlying store's default page cap that extraction can silently hit; an extracted count of exactly this value is treated as a suspicious signal (`mempalace/repair.py:L373-L380`).

`MAX_SEQ_ID_SANITY_THRESHOLD` is `2**53`; any `max_seq_id.seq_id` value above this is treated as a corruption artefact, since clean values are bounded well below this (`mempalace/repair.py:L1349-L1355`).

## `scan_palace(palace_path=None, only_wing=None, collection_name=None) -> (set, set)`

Scans the drawer collection for corrupt/unfetchable IDs. Resolves palace path and collection name from config when not given (`mempalace/repair.py:L256-L257`). Lists all IDs (optionally filtered to a single wing via metadata key `wing`) (`mempalace/repair.py:L263-L272`). If there are no IDs, returns two empty sets (`mempalace/repair.py:L274-L276`).

It probes IDs in batches of 100; for each batch it fetches by ID — IDs returned are "good", IDs requested but not returned are "bad". If a whole-batch fetch raises, it falls back to probing each ID individually, marking any that fail to return or that raise as bad (`mempalace/repair.py:L278-L302`). Progress lines with good/bad counts and an ETA are printed roughly every 50 batches (`mempalace/repair.py:L304-L312`).

Side effect / on-disk contract: it writes a file `corrupt_ids.txt` in the palace directory, one bad ID per line, sorted ascending, each line terminated by a newline (`mempalace/repair.py:L318-L322`). Returns the `(good_set, bad_set)` tuple (`mempalace/repair.py:L248-L323`).

## `prune_corrupt(palace_path=None, confirm=False, collection_name=None) -> None`

Deletes corrupt IDs listed in `corrupt_ids.txt`. If that file is absent, prints a message instructing to run scan first and returns without action (`mempalace/repair.py:L330-L334`). It reads non-blank stripped lines as the bad-ID list (`mempalace/repair.py:L336-L338`).

When `confirm` is false it is a dry run: it prints a notice and returns without deleting (`mempalace/repair.py:L340-L343`). When confirmed, it deletes in batches of 100, falling back to per-ID deletion if a batch delete raises; per-ID failures increment a failure counter rather than aborting (`mempalace/repair.py:L345-L365`). It reports collection size before and after and the deleted/failed counts (`mempalace/repair.py:L346-L370`).

## `rebuild_index(palace_path=None, confirm_truncation_ok=False, collection_name=None, progress=None)`

Rebuilds the HNSW index from scratch via a temporary collection. Default `progress` is a callable that decorates `Staged N/M` and `Re-filed N/M` lines with elapsed/rate/ETA (`mempalace/repair.py:L732-L758`). If the palace directory does not exist it prints a message and returns (`mempalace/repair.py:L762-L764`).

Ordering of preflight guards (each can abort the whole operation):
1. SQLite integrity preflight (`sqlite_integrity_errors`); on any error it prints the abort guidance and returns without touching the store (`mempalace/repair.py:L771-L780`).
2. Poisoned-`max_seq_id` preflight (`maybe_repair_poisoned_max_seq_id_before_rebuild` with `assume_yes=True`); if that repair ran it returns immediately rather than continuing into the destructive rebuild (`mempalace/repair.py:L782-L787`).
3. Opens the collection and counts; on read error it prints recovery guidance and returns (`mempalace/repair.py:L789-L796`). If count is zero it prints "Nothing to repair" and returns (`mempalace/repair.py:L800-L802`).

It extracts all drawers in batches of 5000 (`mempalace/repair.py:L805-L808`), then runs `check_extraction_safety` (the truncation guard); if it raises `TruncationDetected` the message is printed and the function returns without deleting anything (`mempalace/repair.py:L814-L823`).

Backup contract: it backs up only `chroma.sqlite3` (not the bloated index files) by copying to `chroma.sqlite3.backup` when the source exists (`mempalace/repair.py:L825-L831`). It then rebuilds via the temp-collection path (cosine space) (`mempalace/repair.py:L833-L845`).

Failure / rollback behavior: on `RebuildCollectionError`, if the live collection was already replaced and a backup exists, it closes store handles, deletes the live collection, and restores `chroma.sqlite3` from the backup, reporting success or that manual restore is required; if the live collection was replaced with no backup it advises re-mining; if the live collection was never replaced it leaves the original palace untouched. The error is re-raised in all cases (`mempalace/repair.py:L846-L863`). On success it runs post-rebuild cleanup and prints a completion banner with the rebuilt count (`mempalace/repair.py:L865-L869`).

## `_rebuild_collection_via_temp(...) -> int`

Builds into a temporary collection named `<collection>__repair_tmp`, deleting any prior temp collection first (`mempalace/repair.py:L201-L209`). It upserts all rows in `batch_size` chunks into the temp collection, emitting `Staged N/M` progress, then verifies the temp count equals the expected count (raising on mismatch) (`mempalace/repair.py:L210-L218`).

It then deletes the live collection, sets `live_replaced=True`, recreates the live collection, upserts all rows again with `Re-filed N/M` progress, and verifies the count again (`mempalace/repair.py:L220-L233`). On success it deletes the temp collection (best-effort) and returns the rebuilt count (`mempalace/repair.py:L235-L239`). On any exception it deletes the temp collection (best-effort) and raises `RebuildCollectionError` carrying the `live_replaced` flag so the caller knows whether the destructive swap had begun (`mempalace/repair.py:L182-L187`, `mempalace/repair.py:L240-L245`).

## `_extract_drawers(col, total, batch_size) -> (ids, docs, metas)`

Pulls all rows by paging with `limit`/`offset` until `total` rows are read or a page returns no IDs (`mempalace/repair.py:L134-L143`). Metadata sanitation contract: any metadata entry that is not a non-empty dict (i.e. `None` or empty) is replaced with the sentinel `{"_repaired_empty_meta": True}` so the upsert validation does not reject the row mid-rebuild (`mempalace/repair.py:L145-L157`).

## `check_extraction_safety(palace_path, extracted, confirm_truncation_ok=False, collection_name=None) -> None`

Cross-checks extracted count against the SQLite ground truth. Returns immediately (no-op) when `confirm_truncation_ok` is set (`mempalace/repair.py:L421-L422`).

Strong signal: if `sqlite_drawer_count` reports more drawers than were extracted, it raises `TruncationDetected` with a printable abort message stating the loss count and percentage and recovery options (`mempalace/repair.py:L424-L444`). Weak signal: if the extracted count equals exactly `10000` AND the SQLite count could not be read (`None`), it raises `TruncationDetected` refusing to overwrite without `--confirm-truncation-ok` (`mempalace/repair.py:L446-L458`). The `TruncationDetected` exception carries `message`, `sqlite_count`, and `extracted` (`mempalace/repair.py:L383-L396`).

## `sqlite_drawer_count(palace_path, collection_name=None) -> int | None`

Counts rows in `chroma.sqlite3` for the drawer collection by joining `embeddings → segments → collections` filtered by collection name, opening the DB read-only (`mempalace/repair.py:L461-L494`). Returns the count, or `None` when `chroma.sqlite3` is absent or any schema/lock error occurs; callers treat `None` as "unknown" (`mempalace/repair.py:L474-L499`).

## `sqlite_integrity_errors(palace_path) -> list[str]`

Runs `PRAGMA quick_check` on `chroma.sqlite3` (read-only). Returns an empty list when the file is absent (`mempalace/repair.py:L515-L517`). If the check command itself errors, returns a single-element list describing the failure (`mempalace/repair.py:L519-L523`). Otherwise returns the list of result messages excluding any equal to `ok` (case-insensitive) (`mempalace/repair.py:L525-L533`). `print_sqlite_integrity_abort` prints a multi-line abort message that previews up to the first five errors and lists offline recovery steps (`mempalace/repair.py:L536-L564`).

## `extract_via_sqlite(palace_path, collection_name) -> iterator of (id, document, metadata)`

Yields `(embedding_id, document, metadata_dict)` for every row in the named collection's METADATA segment, reading `chroma.sqlite3` directly and never opening the vector-store client or index — the recovery path for index corruption where rows remain intact on disk (`mempalace/repair.py:L985-L1012`). Yields nothing when the palace or `chroma.sqlite3` is missing, or when the collection's METADATA segment is not found (`mempalace/repair.py:L1013-L1029`).

Metadata resolution contract: each metadata row's value is taken from the first non-NULL column in the order `string_value`, `int_value`, `float_value`, `bool_value` (the bool value is coerced to boolean); rows where all four are NULL are dropped (`mempalace/repair.py:L1044-L1053`). The key `chroma:document` is removed from the metadata dict and returned as the document string (empty string when absent). Output preserves first-seen order of embedding IDs (`mempalace/repair.py:L1031-L1058`).

## `rebuild_from_sqlite(source_palace, dest_palace, *, archive_existing_dest=False, batch_size=1000) -> dict[str, int]`

Rebuilds a palace by streaming drawers from the source's `chroma.sqlite3` and upserting into a fresh destination palace, bypassing the vector-store read path entirely (`mempalace/repair.py:L1063-L1082`). Documents are re-embedded at upsert time using the configured embedding function; original vectors are not preserved (`mempalace/repair.py:L1084-L1089`). Paths are expanded and absolutized; `in_place` is true when source and dest resolve to the same path (`mempalace/repair.py:L1140-L1145`).

Validation (returns the empty dict `{}` to signal refusal, which callers must treat as an error / non-zero exit):
- In-place without `archive_existing_dest`: refuses (`mempalace/repair.py:L1158-L1166`).
- Source has no `chroma.sqlite3`: refuses (`mempalace/repair.py:L1167-L1173`).
- Non-in-place where dest already exists: refuses (`mempalace/repair.py:L1174-L1181`).

In-place mode renames the dest to `<dest>.pre-rebuild-<YYYYMMDD-HHMMSS>`, reads from the archive, and clears the store's process-wide system cache (best-effort with warning on failure) (`mempalace/repair.py:L1183-L1206`). It creates the dest directory and rebuilds each recoverable collection in order via `_rebuild_one_collection`, recording per-collection counts (`mempalace/repair.py:L1208-L1237`).

Return contract: a successful rebuild returns a dict with one key per recoverable collection (values may be `0` for legitimately empty collections); `{}` is reserved exclusively for validation refusals (`mempalace/repair.py:L1101-L1114`, `mempalace/repair.py:L1239-L1243`). The store handle is always released in a finally block on every exit path (`mempalace/repair.py:L1210-L1245`).

Process-wide side-effect warning: in-place mode clears the shared system cache, invalidating any live store clients in the same process for any palace; it must not be run inside a long-running process (`mempalace/repair.py:L1116-L1130`).

### `_rebuild_one_collection(...) -> int`

Creates the destination collection (inside the try block so an "already exists" failure is reported as a structured error) and streams rows from `extract_via_sqlite`, flushing in `batch_size` chunks (`mempalace/repair.py:L934-L956`). Each metadata that is `None`/empty is coerced to `{"_repaired_empty_meta": True}` (`mempalace/repair.py:L943-L953`). On any upsert failure it raises `RebuildPartialError` carrying `partial_counts`, `failed_collection`, `dest_palace`, and `archive_path`, leaving the partial dest in place for inspection (`mempalace/repair.py:L872-L897`, `mempalace/repair.py:L957-L982`).

## `status(palace_path=None, collection_name=None) -> dict`

Read-only health check comparing SQLite vs HNSW element counts; it never opens the vector-store client — it reads `chroma.sqlite3` and the index metadata directly (`mempalace/repair.py:L1248-L1264`). Return shapes:
- No palace directory: `{"status": "unknown", "message": "no palace at path"}` (`mempalace/repair.py:L1272-L1274`).
- Palace dir exists but no `chroma.sqlite3`: `{"status": "uninitialized", ...}` (`mempalace/repair.py:L1276-L1279`).
- Drawer SQLite count is exactly 0: `{"status": "empty", ...}` (`mempalace/repair.py:L1286-L1289`).
- Otherwise: `{"drawers": <info>, "closets": <info>}`, where each info carries `sqlite_count`, `hnsw_count`, `divergence`, `diverged`, `status`, `message` (`mempalace/repair.py:L1291-L1314`).

It prints per-collection counts and a `DIVERGED` marker, and recommends running repair when either drawers or closets are diverged (`mempalace/repair.py:L1294-L1313`).

## `max_seq_id` un-poisoning

`_detect_poisoned_max_seq_ids(db_path, *, segment=None, threshold=2**53)` returns `[(segment_id, seq_id), ...]` for rows whose `seq_id` exceeds the threshold, optionally restricted to one segment (`mempalace/repair.py:L1358-L1380`). `_compute_heuristic_seq_id` returns `MAX(embeddings.seq_id)` over the collection owning a segment, decoding 8-byte big-endian BLOB values to integers and returning 0 when no rows (`mempalace/repair.py:L1383-L1415`). `_read_sidecar_seq_ids` loads `{segment_id: seq_id}` from a sidecar DB and rejects it (raising) when any seq_id is BLOB-typed; raises `FileNotFoundError` if the sidecar file is absent (`mempalace/repair.py:L1418-L1437`).

`maybe_repair_poisoned_max_seq_id_before_rebuild(palace_path, *, backup=True, dry_run=False, assume_yes=False) -> dict | None` returns `None` when there is no DB or no poisoned rows; otherwise it runs the non-destructive `repair_max_seq_id` instead of a destructive rebuild and returns its result dict (`mempalace/repair.py:L567-L614`).

`repair_max_seq_id(palace_path, *, segment=None, from_sidecar=None, threshold=2**53, backup=True, dry_run=False, assume_yes=False) -> dict` un-poisons bookmark rows (`mempalace/repair.py:L1440-L1460`). It returns a result dict with keys `palace_path`, `dry_run`, `aborted`, `segment_repaired`, `before`, `after`, `backup` (`mempalace/repair.py:L1466-L1474`). Abort/no-op paths set `aborted=True` with `reason` one of `palace-missing`, `db-missing`, `user-aborted`; when no poisoned rows are detected it returns the unmodified result (`mempalace/repair.py:L1485-L1500`, `mempalace/repair.py:L1539-L1542`).

New values come from the sidecar map (skipping segments absent from the sidecar) or from the heuristic; both `before` and `after` maps are populated per segment (`mempalace/repair.py:L1506-L1528`). Dry run returns without modifying rows (`mempalace/repair.py:L1530-L1532`). Otherwise it requires confirmation (honoring `assume_yes`) (`mempalace/repair.py:L1539-L1542`).

Backup contract: when `backup` is set it copies `chroma.sqlite3` to `chroma.sqlite3.max-seq-id-backup-<YYYYMMDD-HHMMSS>` in the palace dir, records the path in the result, and prunes old backups down to the configured `max_backups` retention (`mempalace/repair.py:L1544-L1563`). It closes store handles, then applies all updates in a single transaction (rollback and re-raise on error) (`mempalace/repair.py:L1565-L1577`). Post-update it re-detects poisoned rows and raises `MaxSeqIdVerificationError` if any remain, citing the backup path (`mempalace/repair.py:L1579-L1584`). On success it records `segment_repaired` and prints the restored count (`mempalace/repair.py:L1586-L1590`).

## Errors and helper contracts

`_verify_collection_count` raises `RuntimeError` with a labeled count-mismatch message when actual count differs from expected (`mempalace/repair.py:L160-L163`). `_delete_collection_if_exists` swallows "does not exist"/"not found" value errors and not-found errors so deleting a missing collection is a no-op, but re-raises other value errors (`mempalace/repair.py:L166-L179`). `_close_chroma_handles` closes the store handles for a palace and clears the process-wide system cache, all best-effort (`mempalace/repair.py:L1322-L1342`). `_post_rebuild_cleanup` releases store handles then VACUUMs `chroma.sqlite3` and rebuilds the FTS5 full-text index (`embedding_fulltext_search`) when present; failures here are non-fatal warnings (`mempalace/repair.py:L685-L729`).

## CLI entry point (standalone module execution)

When run as a program it accepts a positional `command` of `status`, `scan`, `prune`, or `rebuild`, plus `--palace`, `--wing`, and `--confirm` (`mempalace/repair.py:L1593-L1599`). The palace path is expanded from `--palace` when given (`mempalace/repair.py:L1601`). It dispatches: `status → status()`, `scan → scan_palace(only_wing=--wing)`, `prune → prune_corrupt(confirm=--confirm)`, `rebuild → rebuild_index()` (`mempalace/repair.py:L1603-L1610`).
