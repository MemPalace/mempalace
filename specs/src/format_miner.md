# Behavior Specification — `format_miner.py`

Third miner alongside the project-file miner and the conversation miner. It handles binary office-format documents (PDF, DOCX, PPTX, XLSX, RTF, EPUB), converting them to text at read time and filing the result into the palace as drawers. It never modifies source files on disk; conversion is in-memory only (mempalace/format_miner.py:L1-L17). It is intended to back the CLI invocation `mempalace mine <dir> --mode extract` (mempalace/format_miner.py:L9-L11).

## Public surface

The module's public exports are: `SUPPORTED_FORMATS`, `DEFAULT_MAX_FILE_SIZE`, `ExtractionStatus`, `decode_robust`, `is_icloud_dataless`, `extract_text`, `scan_formats`, `mine_formats` (mempalace/format_miner.py:L94-L103).

## Constants and contracts

`SUPPORTED_FORMATS` is the set of extensions handled, all lowercase with a leading dot: `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.rtf`, `.epub`. Matching is case-insensitive against the file suffix lowercased (mempalace/format_miner.py:L116-L127). `DEFAULT_MAX_FILE_SIZE` is 500 MB (`500 * 1024 * 1024` bytes), the same cap as the other miners; callers may override per-call (mempalace/format_miner.py:L130-L133). `DRAWER_UPSERT_BATCH_SIZE` is 1000 — drawers are upserted in batches of at most this size (mempalace/format_miner.py:L106-L107). `MIN_CHUNK_SIZE` is 50 chars (mempalace/format_miner.py:L109-L110).

Three filenames are always excluded as non-content even if their extension matches: `.DS_Store`, `Thumbs.db`, `desktop.ini` (mempalace/format_miner.py:L136-L141). A case-insensitive pattern matching any of `encrypt`, `decrypt`, `password`, `protected` in an exception message classifies the failure as encrypted (mempalace/format_miner.py:L144-L149).

## `ExtractionStatus`

An enumeration of outcomes. `OK` (`"ok"`) means text was extracted; every other case is a skip variant with a string value: `SKIP_TOO_LARGE` (`"skip:too_large"`), `SKIP_CLOUD_ONLY` (`"skip:cloud_only"`), `SKIP_EMPTY` (`"skip:empty"`), `SKIP_NO_MARKITDOWN` (`"skip:no_markitdown"`), `SKIP_NO_STRIPRTF` (`"skip:no_striprtf"`), `SKIP_ENCRYPTED` (`"skip:encrypted"`), `SKIP_PERMISSION` (`"skip:permission"`), `SKIP_BROKEN_SYMLINK` (`"skip:broken_symlink"`), `SKIP_UNRECOGNIZED` (`"skip:unrecognized"`), `SKIP_EXTRACTION_ERROR` (`"skip:extraction_error"`), `SKIP_MISSING_FORMAT_DEPS` (`"skip:missing_format_deps"`), `SKIP_NETWORK_TIMEOUT` (`"skip:network_timeout"`), `SKIP_UNREADABLE` (`"skip:unreadable"`) (mempalace/format_miner.py:L152-L173).

Four statuses are classified as transient/missing-dependency: `SKIP_NO_MARKITDOWN`, `SKIP_NO_STRIPRTF`, `SKIP_MISSING_FORMAT_DEPS`, `SKIP_NETWORK_TIMEOUT`. Files that hit these are NOT marked as already-mined, so a later mine retries them after the missing piece is installed or the network recovers (mempalace/format_miner.py:L176-L189, L538-L551).

## `decode_robust(raw: bytes) -> str`

Decodes bytes to text without raising. Empty input returns the empty string. It tries UTF-8 first; on failure tries CP1252; final fallback is UTF-8 with replacement so no byte is lost — invalid bytes become the replacement character (mempalace/format_miner.py:L197-L215).

## `is_icloud_dataless(path) -> bool`

Returns true when the path is an iCloud cloud-only placeholder. Two signals: (1) the suffix lowercased equals `.icloud`; (2) on macOS, the inode's `st_flags` (read via `lstat`) has the dataless bit `0x40000000` set. If `lstat` raises `OSError`, returns false. On non-macOS platforms the flag is absent or zero, so only the suffix check applies (mempalace/format_miner.py:L223-L245).

## `extract_text(path, max_file_size=DEFAULT_MAX_FILE_SIZE) -> (text|None, ExtractionStatus)`

Pure function: performs no I/O outside the single file at `path` and never modifies the source. Returns a tuple; `text` is `None` for every skip case and a non-empty string only for `OK` (mempalace/format_miner.py:L315-L326).

Accepts a string or path; `~`-prefixed paths are expanded (mempalace/format_miner.py:L327-L330). The checks run in this order, and the FIRST that matches determines the result:

1. Broken symlink: if the path is a symlink whose target does not exist → `SKIP_BROKEN_SYMLINK` (mempalace/format_miner.py:L332-L336).
2. iCloud cloud-only (checked before `stat()` to avoid triggering a materialization fetch): if `is_icloud_dataless` → `SKIP_CLOUD_ONLY` (mempalace/format_miner.py:L338-L342).
3. `stat()` errors: `PermissionError` → `SKIP_PERMISSION`; `FileNotFoundError` → `SKIP_BROKEN_SYMLINK` if it is a symlink else `SKIP_UNREADABLE`; any other `OSError` → `SKIP_UNREADABLE` (mempalace/format_miner.py:L347-L366).
4. Empty file (size 0) → `SKIP_EMPTY` (mempalace/format_miner.py:L368-L371).
5. Size strictly greater than `max_file_size` → `SKIP_TOO_LARGE` (mempalace/format_miner.py:L373-L378).
6. Suffix (lowercased) not in `SUPPORTED_FORMATS` → `SKIP_UNRECOGNIZED` (mempalace/format_miner.py:L380-L384).

Format routing: `.rtf` is converted via striprtf; all other supported formats via MarkItDown (mempalace/format_miner.py:L386-L394). Exceptions from conversion are classified, in this order:

- Missing transformer package (`ImportError`): `.rtf` → `SKIP_NO_STRIPRTF`, otherwise → `SKIP_NO_MARKITDOWN` (mempalace/format_miner.py:L395-L407).
- `TimeoutError` → `SKIP_NETWORK_TIMEOUT` (mempalace/format_miner.py:L408-L411).
- `PermissionError` → `SKIP_PERMISSION` (mempalace/format_miner.py:L412-L415).
- Any other exception: if its type name is `MissingDependencyException` → `SKIP_MISSING_FORMAT_DEPS`; else if the exception message matches the encrypted pattern → `SKIP_ENCRYPTED`; else → `SKIP_EXTRACTION_ERROR` (mempalace/format_miner.py:L416-L439).

If the transformer returns empty/falsy text (a parseable but content-empty document) → `SKIP_EXTRACTION_ERROR` (mempalace/format_miner.py:L441-L447). Otherwise returns the extracted text with `OK` (mempalace/format_miner.py:L449).

The MarkItDown path reads `text_content`, falling back to `markdown`, from the conversion result; non-string or missing results yield `None` (which the caller treats as extraction error) (mempalace/format_miner.py:L253-L280). The striprtf path reads the file's raw bytes, decodes via `decode_robust`, strips RTF, and returns `None` for non-string or empty-string results (mempalace/format_miner.py:L283-L307).

## `scan_formats(directory) -> list[Path]`

Walks the directory recursively and returns supported files in deterministic ascending path-sorted order, so a re-mine processes files in the same order each time (mempalace/format_miner.py:L457-L469, L497-L498). The directory input is expanded and resolved; a non-existent root returns an empty list (mempalace/format_miner.py:L471-L476).

It prunes directories named in the shared `SKIP_DIRS` set, skips filenames in the always-excluded set (`.DS_Store`, `Thumbs.db`, `desktop.ini`), skips symlinks (to prevent circular links and double-processing), and skips files whose lowercased suffix is not in `SUPPORTED_FORMATS` (mempalace/format_miner.py:L479-L495).

## `mine_formats(format_dir, palace_path, wing=None, agent="mempalace", limit=0, dry_run=False) -> None`

Orchestrator. Walks `format_dir`, extracts each supported file, chunks the result with the shared chunker, and files chunks as drawers under a wing. Source files are never modified (mempalace/format_miner.py:L693-L708).

Inputs: `format_dir` (directory walked recursively, with the same skip rules as `scan_formats`); `palace_path` (destination palace); `wing` defaults to the normalized basename of the resolved directory if not given; `agent` recorded in each drawer's `added_by`; `limit` caps the number of files mined when > 0; `dry_run` walks/extracts/chunks but opens no collection and upserts nothing (mempalace/format_miner.py:L709-L728, L755-L757).

Config: a palace-wide config is loaded once; its `chunk_size`, `chunk_overlap`, and `min_chunk_size` are threaded into the chunker (mempalace/format_miner.py:L753, L859-L865). A project config is loaded for room categories; on any failure or absence it falls back to a single room named `documents` (mempalace/format_miner.py:L759-L783).

Side effects — console output: prints a header block with wing, source path, file count (with a `(limit: N new)` suffix when `limit > 0`), palace path, and a `DRY RUN` notice when applicable (mempalace/format_miner.py:L805-L815). Per file it prints a one-line status: `+ [i/N] name +K` on success, `- [i/N] name STATUS` for a skip/non-OK status, `- [i/N] name EMPTY_AFTER_CHUNK` when chunking yields nothing, `! [i/N] name ERROR: Type` on per-file exception, or a `[DRY RUN] name → K drawers` line under dry-run (mempalace/format_miner.py:L853, L869, L879, L902, L916-L917).

Per-file processing order: read source mtime (best-effort; `None` on error) (mempalace/format_miner.py:L829-L832); unless dry-run, skip if `file_already_mined` with `check_mtime=True, extract_mode="format"` is true (counted as skipped) (mempalace/format_miner.py:L839-L843); call `extract_text` and tally the status name (mempalace/format_miner.py:L845-L846). On non-OK status or empty text, write a skip sentinel only if the status is not transient, and continue (mempalace/format_miner.py:L848-L854). Otherwise chunk the text; if no chunks, register an empty-file sentinel and continue (mempalace/format_miner.py:L859-L870). Route the drawer to a room via the shared room detector (mempalace/format_miner.py:L872-L876). Under dry-run, accumulate drawer counts without filing (mempalace/format_miner.py:L878-L884). Otherwise file the chunks under a lock; if the locked re-check reports the file already mined, count it skipped (mempalace/format_miner.py:L886-L898).

The `limit` cap stops the loop once `files_mined >= limit` (counting both dry-run and real mines) (mempalace/format_miner.py:L882-L883, L903-L904).

Robustness: each file is wrapped so one malformed file logs and is counted as errored without crashing the mine (mempalace/format_miner.py:L823-L825, L905-L918). `KeyboardInterrupt` prints an interruption notice and stops; partial progress is safe because drawer IDs are deterministic and re-mining upserts the same rows (mempalace/format_miner.py:L919-L922). Any outer-loop exception is caught, logged, and reported to stderr so the summary still prints (mempalace/format_miner.py:L923-L939).

On clean completion (no interruption) and not dry-run: cross-wing topic tunnels are computed for the wing; tunnel-computation failures never fail the mine (logged, reported to stderr) (mempalace/format_miner.py:L940-L960). Then an end-of-mine FTS5 integrity check runs, which raises a validation error if the palace's full-text index is malformed (mempalace/format_miner.py:L962-L968).

Cleanup (always, in `finally`): the mine PID file is cleared so hook-spawned mines do not leave a stale PID (mempalace/format_miner.py:L969-L981).

Summary (always printed last): a block reporting files seen (the last processed index), files extracted, files skipped, files errored, total drawers, and a per-status breakdown sorted by descending count (mempalace/format_miner.py:L509-L535, L983-L990).

## Drawer on-disk contract (sentinels)

Skip/empty sentinels write a single document `[empty]` with id `sentinel_<wing>_<first 24 hex chars of SHA-256 of source_file>`. The sentinel metadata contains: `wing`, `room` = `"documents"`, `source_file`, `chunk_index` = `-1`, `added_by` = agent, `filed_at` = current ISO timestamp, `ingest_mode` = `"extract"`, `extract_mode` = `"format"`, `normalize_version`, and `is_sentinel` = true. Sentinel write failures are swallowed (mempalace/format_miner.py:L554-L582).

## Drawer on-disk contract (content chunks)

`_file_chunks_locked` extracts a per-file content date once, then under a source-file lock re-checks `file_already_mined(check_mtime=True, extract_mode="format")`; if true it returns `(0, True)` (skipped) without writing (mempalace/format_miner.py:L617-L632). Otherwise it purges existing drawers for that `source_file` (delete-by-`source_file`; failure swallowed) and upserts fresh chunks in batches (mempalace/format_miner.py:L634-L690).

Each content drawer has a deterministic id derived from `(wing, room, source_file, chunk_index)` (mempalace/format_miner.py:L645). Its metadata always contains: `wing`, `room`, `source_file`, `chunk_index`, `added_by` = agent, `filed_at` (single ISO timestamp shared across the file's chunks), `ingest_mode` = `"extract"`, `extract_mode` = `"format"`, `normalize_version`, `hall` (content-keyword routing), and `id_recipe` (mempalace/format_miner.py:L639-L659). Conditionally added: `source_mtime` when known; `line_start`/`line_end` when present on the chunk; `content_date` when a file content date was found; `entities` when entity extraction returns any (mempalace/format_miner.py:L660-L675).

Before upserting each batch, an id/metadata collision assertion runs against the collection (mempalace/format_miner.py:L679). Upsert errors are re-raised unless the message contains `already exists` (case-insensitive), which is tolerated (mempalace/format_miner.py:L680-L689). The function returns `(drawers_added, skipped)` (mempalace/format_miner.py:L607, L690).

<promise>SPEC_WRITTEN path=specs/src/format_miner.md citations=58</promise>
