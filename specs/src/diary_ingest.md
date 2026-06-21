# Behavior Specification: `diary_ingest`

Ingests daily-summary Markdown files into the palace: one searchable closet index plus per-entry verbatim drawers, with incremental (append-only) processing by default and full-rebuild on demand or on detected content change (mempalace/diary_ingest.py:L1-L17).

## Public Surface

### `ingest_diaries(diary_dir, palace_path, wing="diary", force=False)`

Ingests every `*.md` daily summary file found in `diary_dir` into the palace at `palace_path` (mempalace/diary_ingest.py:L102-L114).

Inputs:
- `diary_dir`: directory path (string or path); user-tilde-expanded and resolved to absolute before use (mempalace/diary_ingest.py:L115).
- `palace_path`: palace location passed through to backend collection access (mempalace/diary_ingest.py:L134-L135).
- `wing`: logical category name, default `"diary"`; used in drawer/closet IDs and metadata (mempalace/diary_ingest.py:L105, L153, L185-L192).
- `force`: when true, rebuilds every entry's closets and drawers from scratch and ignores prior state (mempalace/diary_ingest.py:L106, L126-L127, L159).

Output: a map `{"days_updated": <int>, "closets_created": <int>}` (mempalace/diary_ingest.py:L322-L325). The same shape with zeros is returned early when the diary directory does not exist or contains no `.md` files (mempalace/diary_ingest.py:L116-L123).

### CLI entry point (`python -m mempalace.diary_ingest`)

Command-line invocation parses arguments and calls `ingest_diaries` (mempalace/diary_ingest.py:L328-L338):
- `--dir` (required): daily summaries directory (mempalace/diary_ingest.py:L332).
- `--palace` (default `~/.mempalace/palace`, tilde-expanded) (mempalace/diary_ingest.py:L333).
- `--wing` (default `diary`) (mempalace/diary_ingest.py:L334).
- `--force` (boolean flag) (mempalace/diary_ingest.py:L335).
Usage examples in module docstring (mempalace/diary_ingest.py:L14-L16).

## File Discovery and Selection

Files are selected by glob `*.md` in `diary_dir` and processed in sorted (lexicographic) order (mempalace/diary_ingest.py:L120). Each file is read as UTF-8 with replacement on decode errors (mempalace/diary_ingest.py:L142).

A file is skipped if its trimmed text is shorter than 50 characters (mempalace/diary_ingest.py:L143-L144).

The date for a file is taken from the leading `YYYY-MM-DD` of its filename stem (regex `(\d{4}-\d{2}-\d{2})` anchored at start). A file whose stem does not begin with that pattern is skipped (mempalace/diary_ingest.py:L146-L149).

## Entry Splitting

Diary text is split into entries on lines beginning with `## ` (Markdown level-2 headers, matched by `^## .+` multiline) (mempalace/diary_ingest.py:L40, L56-L64). Each entry is a `(header, body)` pair: the header is the `## ...` line (trimmed); the body is all text up to the next header (trimmed), or empty for the last header with no following text (mempalace/diary_ingest.py:L56-L64). Text before the first `## ` header is not emitted as an entry.

The serialized text for an entry is `header + "\n" + body` when a body exists, otherwise just `header` (mempalace/diary_ingest.py:L241, L284).

## Incremental vs. Full Rebuild Decision

State is keyed per `(palace_path, diary_dir)` pair; each file's state is keyed within that by `"<wing>|<filename>"` (mempalace/diary_ingest.py:L153). Per file, prior state holds `content_hash`, `size`, and `entry_count` (mempalace/diary_ingest.py:L154-L156, L182).

The current content hash is SHA-256 of the UTF-8 file text (mempalace/diary_ingest.py:L158). Decision logic when `force` is false (mempalace/diary_ingest.py:L159-L167):
- If a prior `content_hash` exists and equals the current hash, the file is skipped (no change) (mempalace/diary_ingest.py:L160-L162).
- If no prior hash exists but prior `size` is nonzero and equals current size, the file is skipped (legacy size-based skip) but the state is backfilled with the current `content_hash` for future strict checks (mempalace/diary_ingest.py:L163-L167). Hash-based comparison is required because size-only comparison false-negatives on same-length edits (mempalace/diary_ingest.py:L151-L152).

A full rebuild occurs when `force` is true OR when a prior hash existed and differs from the current hash (in-place content change) (mempalace/diary_ingest.py:L172, L183). Otherwise processing is incremental and only entries appended past `prev_entry_count` are added to the closet index (mempalace/diary_ingest.py:L182, L279).

## Drawers (Verbatim Content)

Drawers are written to the main palace collection (mempalace/diary_ingest.py:L134). Each `## ` entry becomes one or more drawers (mempalace/diary_ingest.py:L240-L270). If an entry's serialized text length is within the configured `chunk_size` (mempalace/diary_ingest.py:L136), it becomes a single drawer; otherwise it is split into fixed-size character chunks of `chunk_size` each (mempalace/diary_ingest.py:L242-L270).

Drawer IDs use the `v2_` scheme: `drawer_diary_v2_<suffix>` where `<suffix>` is the first 24 hex chars of SHA-256 over `"<wing>|<date>|<entry_idx>|<entry_chunk_idx>"` (mempalace/diary_ingest.py:L80-L94). A legacy file-level ID scheme `drawer_diary_<suffix>` over `"<wing>|<date>"` existed pre-#1539 and is retained only for backwards-compatible cleanup (mempalace/diary_ingest.py:L67-L77).

Each drawer's metadata carries the shared base set: `date`, `wing`, `room="daily"`, `source_file` (absolute path of the diary file), `source_session="daily_diary"`, `filed_at` (UTC ISO-8601 timestamp), and `entities` only when entity extraction produced any (mempalace/diary_ingest.py:L176, L185-L194). Plus per-drawer: `chunk_index` (a global counter across the whole file, incrementing for every chunk regardless of entry boundary), `entry_index`, `entry_chunk_index`, and `entry_header_preview` (first 120 chars of the header) (mempalace/diary_ingest.py:L236-L270). The global `chunk_index` ordering lets sibling chunks be stitched back together by `(source_file, chunk_index)` downstream (mempalace/diary_ingest.py:L221-L235, L285-L288).

All drawers for a file are accumulated and written in a single batched upsert so the embedding pass commits every chunk or none; a partial write is explicitly avoided (mempalace/diary_ingest.py:L229-L277). The upsert is skipped when there are no drawer IDs (mempalace/diary_ingest.py:L272-L277).

On a full rebuild, all prior drawers for the file are purged first by deleting all records matching `source_file` before the fresh upsert; this single step removes legacy file-level drawers, stale v2 drawers from a run with more entries, and content shifted across entry boundaries (mempalace/diary_ingest.py:L196-L219). The purge failure is caught and logged at debug without aborting; if the subsequent upsert also fails, the state file is left unchanged so the next run retries (mempalace/diary_ingest.py:L204-L219).

## Closets (Searchable Index Layer)

Closets are written to the closets collection (mempalace/diary_ingest.py:L135). For each new entry (all entries on full rebuild, only entries after `prev_entry_count` otherwise), closet pointer lines are built referencing the canonical entry drawer (the `entry_chunk_index=0` drawer) (mempalace/diary_ingest.py:L279-L293). Closets pack topics up to a character limit (1500) and never split a topic line across closets (mempalace/diary_ingest.py:L5-L6, palace.py:L447, L681-L718).

Closet ID base is `closet_diary_<suffix>` where `<suffix>` is the first 24 hex chars of SHA-256 over `"<wing>|<date>"`; numbered closets are derived from this base (mempalace/diary_ingest.py:L97-L99, L296, L310). Closet metadata carries `date`, `wing`, `room="daily"`, `source_file`, `filed_at`, and `entities` when present (mempalace/diary_ingest.py:L297-L305).

On a full rebuild, leftover closets for the file are purged before re-writing so a shorter day leaves no orphan closets (mempalace/diary_ingest.py:L7-L8, L306-L309). The count of closets written is added to `closets_created` (mempalace/diary_ingest.py:L310-L311).

## State Persistence (On-Disk Contract)

State files live under `~/.mempalace/state/` (created if missing) and are never written inside the user's diary directory (mempalace/diary_ingest.py:L9-L10, L43-L53). The filename is `diary_ingest_<key>.json`, where `<key>` is the first 24 hex chars of SHA-256 over `"<palace_path>|<diary_dir>"`, giving each (palace, diary-dir) pair an independent state file (mempalace/diary_ingest.py:L50-L53).

The state file is JSON (indent 2). Each entry is keyed by `"<wing>|<filename>"` and holds an object with fields `size` (int), `content_hash` (hex string), `entry_count` (int), and `ingested_at` (UTC ISO-8601 string) (mempalace/diary_ingest.py:L313-L321). The state file is written once at the end of the run, after all files are processed (mempalace/diary_ingest.py:L321). When `force` is true or no state file exists, processing starts from empty state; a corrupt/unreadable state file is treated as empty (mempalace/diary_ingest.py:L126-L132).

## Concurrency and Ordering Guarantees

Per source file, the upsert and closet-rebuild are serialized under a `mine_lock` keyed by the absolute file path, so two concurrent ingests cannot interleave for the same file (mempalace/diary_ingest.py:L11, L178-L180, palace.py:L722-L732). The global `chunk_index` increases monotonically in file order across entries and chunks (mempalace/diary_ingest.py:L236-L270).

## Side Effects and Observable Output

- Filesystem reads: each `*.md` file under `diary_dir` (mempalace/diary_ingest.py:L120, L142).
- Filesystem writes: drawer and closet records into the palace collections; the per-pair JSON state file under `~/.mempalace/state/` (mempalace/diary_ingest.py:L272-L311, L321); lock files via `mine_lock` (mempalace/diary_ingest.py:L180).
- Stdout: `"Diary directory not found: <path>"` when the directory is missing (mempalace/diary_ingest.py:L117); `"No .md files in <path>"` when none found (mempalace/diary_ingest.py:L122); `"Diary: <N> days updated, <M> new closets"` printed only when at least one day was updated (mempalace/diary_ingest.py:L322-L323).
- Entities are extracted from each file's full text and stamped onto drawer and closet metadata for filterable search (mempalace/diary_ingest.py:L12, L175, L193-L194, L304-L305).

## Counters

`days_updated` increments once per file that passes the change check and is processed (mempalace/diary_ingest.py:L319). `closets_created` accumulates the number of closets written per file (mempalace/diary_ingest.py:L310-L311).
