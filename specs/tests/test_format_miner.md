# Behavior Spec: `format_miner` (derived from `tests/test_format_miner.py`)

This spec describes the observable behavior of the `format_miner` module as constrained
by its test suite. It defines the public surface, status codes, extraction dispatch
rules, encoding fallbacks, directory scanning, and the `mine_formats` orchestrator.
All claims cite the test file that asserts the contract.

## Public Surface

The module exports the following names: `DEFAULT_MAX_FILE_SIZE`, `SUPPORTED_FORMATS`,
`ExtractionStatus`, `decode_robust`, `extract_text`, `is_icloud_dataless`,
`scan_formats` (tests/test_format_miner.py:L20-L28). It also exposes orchestrator and
adapter internals `mine_formats`, `_extract_via_markitdown`, `_extract_via_striprtf`,
`_register_file`, `chunk_text`, `detect_room`, `load_config`, `MempalaceConfig`,
`get_collection`, `mine_lock`, `file_already_mined`, `_compute_topic_tunnels_for_wing`,
which are referenced/patched as module-level bindings (tests/test_format_miner.py:L588-L613,
L671-L673, L1024, L1067, L1091, L1158, L1221, L1326, L1370, L1507, L1529).

## `SUPPORTED_FORMATS`

A set/collection of file extensions that MUST include `.pdf`, `.rtf`, `.docx`, `.xlsx`,
`.pptx`, `.epub` (tests/test_format_miner.py:L53-L59). Every entry MUST be lowercase and
MUST begin with a `.` (tests/test_format_miner.py:L62-L65).

## `DEFAULT_MAX_FILE_SIZE`

The default maximum file size is exactly `500 * 1024 * 1024` bytes (524288000)
(tests/test_format_miner.py:L90-L91).

## `ExtractionStatus`

An enumeration whose member names MUST include all of: `OK`, `SKIP_TOO_LARGE`,
`SKIP_CLOUD_ONLY`, `SKIP_EMPTY`, `SKIP_NO_MARKITDOWN`, `SKIP_NO_STRIPRTF`,
`SKIP_ENCRYPTED`, `SKIP_PERMISSION`, `SKIP_BROKEN_SYMLINK`, `SKIP_UNRECOGNIZED`,
`SKIP_EXTRACTION_ERROR`, `SKIP_MISSING_FORMAT_DEPS`, `SKIP_NETWORK_TIMEOUT`,
`SKIP_UNREADABLE` (tests/test_format_miner.py:L68-L87).

## `extract_text(path, max_file_size=DEFAULT_MAX_FILE_SIZE)`

Returns a 2-tuple `(text, status)` where `text` is a string on success or `None` on any
skip, and `status` is an `ExtractionStatus` member. The function accepts either a path
object or a string path without coercion (tests/test_format_miner.py:L273-L288). Extension
matching is case-insensitive; e.g. `doc.PDF` is treated as a PDF
(tests/test_format_miner.py:L291-L297).

### Dispatch and routing

- An unrecognized extension (not in `SUPPORTED_FORMATS`) returns `(None,
  SKIP_UNRECOGNIZED)` (tests/test_format_miner.py:L100-L105).
- `.rtf` files are routed to `_extract_via_striprtf` and MUST NEVER invoke
  `_extract_via_markitdown` (tests/test_format_miner.py:L367-L403). This case-insensitively
  applies to `.RTF` as well (tests/test_format_miner.py:L471-L482).
- Non-RTF supported formats (e.g. `.pdf`, `.docx`, `.xlsx`) are routed to
  `_extract_via_markitdown` and MUST NOT invoke `_extract_via_striprtf`
  (tests/test_format_miner.py:L406-L418, L349-L364, L485-L494).
- On successful extraction the returned status is `OK` and `text` equals the adapter's
  returned string verbatim (tests/test_format_miner.py:L349-L355, L375-L378).

### Pre-extraction checks (ordering)

- An empty file (zero bytes) returns `(None, SKIP_EMPTY)`
  (tests/test_format_miner.py:L113-L118).
- A file whose size exceeds `max_file_size` returns `(None, SKIP_TOO_LARGE)`; this check
  happens before invoking any extractor (tests/test_format_miner.py:L126-L131). When the
  cap is generous enough, size alone does not trigger the skip and extraction proceeds
  (tests/test_format_miner.py:L134-L143).
- An iCloud cloud-only/dataless file returns `(None, SKIP_CLOUD_ONLY)` and MUST NOT call
  `_extract_via_markitdown` (tests/test_format_miner.py:L228-L237).

### Error mapping from the MarkItDown adapter

- `ImportError` from `_extract_via_markitdown` → `(None, SKIP_NO_MARKITDOWN)`
  (tests/test_format_miner.py:L151-L160).
- An exception whose message indicates an encrypted/undecrypted file (e.g. "File has not
  been decrypted") → `(None, SKIP_ENCRYPTED)` (tests/test_format_miner.py:L168-L181).
- `PermissionError` → `(None, SKIP_PERMISSION)` (tests/test_format_miner.py:L189-L198).
- `TimeoutError` → `(None, SKIP_NETWORK_TIMEOUT)` (tests/test_format_miner.py:L332-L341).
- A generic non-specific exception (e.g. `RuntimeError`) → `(None,
  SKIP_EXTRACTION_ERROR)` (tests/test_format_miner.py:L305-L314).
- The adapter returning `None` → `(None, SKIP_EXTRACTION_ERROR)`
  (tests/test_format_miner.py:L317-L324).
- An exception whose type name is `MissingDependencyException` → `(None,
  SKIP_MISSING_FORMAT_DEPS)`. The catch is by exception type NAME, so the real
  dependency need not be importable for it to fire (tests/test_format_miner.py:L1197-L1230).

### Error mapping from the striprtf adapter

- `ImportError` from `_extract_via_striprtf` → `(None, SKIP_NO_STRIPRTF)`
  (tests/test_format_miner.py:L421-L431).
- A generic exception (e.g. `RuntimeError`) → `(None, SKIP_EXTRACTION_ERROR)`
  (tests/test_format_miner.py:L434-L444).
- Returning `None` → `(None, SKIP_EXTRACTION_ERROR)` (tests/test_format_miner.py:L447-L454).
- Returning an empty string → `(None, SKIP_EXTRACTION_ERROR)`; an RTF that strips to zero
  characters is treated as failure rather than filing an empty drawer
  (tests/test_format_miner.py:L457-L468).

### Filesystem edge cases

- A broken symlink (link whose target does not exist) returns `(None,
  SKIP_BROKEN_SYMLINK)` (tests/test_format_miner.py:L206-L213).
- A non-existent regular file that is NOT a symlink returns `(None, SKIP_UNREADABLE)`.
  `SKIP_BROKEN_SYMLINK` may only be returned when the path is itself a symlink
  (tests/test_format_miner.py:L1276-L1297).

## `is_icloud_dataless(path)`

Returns `True` for a macOS iCloud placeholder file, including paths with a literal
`.icloud` placeholder extension (e.g. `doc.pdf.icloud`)
(tests/test_format_miner.py:L221-L225).

## `decode_robust(raw_bytes)`

Decodes raw bytes to a string and MUST NEVER raise. The fallback order is UTF-8, then
CP1252, then UTF-8 with replacement (tests/test_format_miner.py:L241-L242).

- Clean UTF-8 bytes decode to the correct Unicode string (e.g. `b"hello \xe2\x9c\xa8
  world"` → `"hello ✨ world"`) (tests/test_format_miner.py:L245-L246).
- Bytes invalid as UTF-8 but valid CP1252 (e.g. CP1252 smart quotes `\x91`/`\x92`) decode
  via the CP1252 path WITHOUT producing the U+FFFD replacement character
  (tests/test_format_miner.py:L249-L254, L552-L559).
- Bytes invalid in both UTF-8 and CP1252 (e.g. `\xff\xfe\xfd\xfc...`) still return a
  string with the recoverable text preserved (tests/test_format_miner.py:L257-L261).
- Empty input returns the empty string (tests/test_format_miner.py:L264-L265).

## `scan_formats(directory)`

Walks a directory tree and returns a collection of path objects for supported files.

- Returns only files whose extension is in `SUPPORTED_FORMATS`; unsupported files (e.g.
  `.txt`) are excluded (tests/test_format_miner.py:L502-L511).
- Recurses into subdirectories (tests/test_format_miner.py:L514-L519).
- Skips hidden/build directories (same SKIP_DIRS set as the project miner — e.g. `.git`,
  `.venv`, `__pycache__`); files inside them are not returned
  (tests/test_format_miner.py:L522-L530).
- Skips `.DS_Store` entries (tests/test_format_miner.py:L533-L538).
- Skips symlinks: a symlink whose target is a supported file is NOT returned, while the
  real target file is (tests/test_format_miner.py:L909-L917).
- Returns an empty list (no error) when the directory does not exist
  (tests/test_format_miner.py:L541-L544).

## Adapter contracts (`_extract_via_striprtf`, `_extract_via_markitdown`)

- `_extract_via_striprtf` converts real RTF bytes to plain text; the output contains the
  document text and MUST NOT contain raw RTF control codes such as `\rtf1`
  (tests/test_format_miner.py:L580-L599).
- `_extract_via_markitdown` runs without raising on a minimal valid input; when it returns
  a value it is a string (it may also return `None`)
  (tests/test_format_miner.py:L602-L637).

## `mine_formats(format_dir, palace_path, wing=None, limit=None, dry_run=False)`

Orchestrator that discovers supported files, extracts and chunks text, and upserts drawers
into a collection. Collection access (`get_collection`), locking (`mine_lock`), and
idempotency (`file_already_mined`) are external dependencies
(tests/test_format_miner.py:L648-L681).

### Discovery and idempotency

- Uses `scan_formats` to discover files (tests/test_format_miner.py:L684-L696).
- When `file_already_mined` returns `True` for a file, that file is not extracted
  (tests/test_format_miner.py:L699-L713).
- `file_already_mined` is always called with `check_mtime=True` so updated documents get
  re-mined (tests/test_format_miner.py:L920-L940).
- `file_already_mined` is always called with `extract_mode="format"` so format-mode
  idempotency is scoped to its own drawer set, separate from convo/project miners. Both
  the pre-lock check and the post-lock recheck pass this kwarg
  (tests/test_format_miner.py:L1300-L1350).

### Limit semantics

- `limit=N` restricts processing to the first N files; only N files are extracted
  (tests/test_format_miner.py:L782-L801).
- The limit counts only new work — already-mined files that are skipped do not consume the
  limit budget (tests/test_format_miner.py:L804-L831).

### Wing and room assignment

- When `wing=None`, the wing defaults to the basename of `format_dir`
  (tests/test_format_miner.py:L834-L853).
- An explicit `wing=` argument overrides the directory-name default
  (tests/test_format_miner.py:L856-L876).
- `mine_formats` loads configuration via `load_config` to obtain the rooms list
  (called exactly once) (tests/test_format_miner.py:L1050-L1070).
- For each file, `detect_room(filepath, content, rooms, project_path)` is called (4
  positional args) to determine the room; the room is not hardcoded
  (tests/test_format_miner.py:L1073-L1098).
- The `room` field of each drawer's metadata reflects `detect_room`'s return value
  (tests/test_format_miner.py:L1101-L1135).

### Drawer metadata contract

Each non-sentinel content drawer upserted carries metadata that MUST include:

- `ingest_mode` equal to `"extract"`, distinguishing these drawers from project/convo
  drawers (tests/test_format_miner.py:L879-L896).
- `wing` set to the resolved wing name (tests/test_format_miner.py:L850-L853, L873-L876).
- `room` set to the detected room (tests/test_format_miner.py:L1124-L1135).
- `source_mtime` present and numeric (integer or float), enabling
  `file_already_mined(check_mtime=True)` to detect updates
  (tests/test_format_miner.py:L943-L968).
- `hall` present and a string (tests/test_format_miner.py:L971-L994).

### Sentinel behavior on skips

- When extraction returns a SKIP status, no content drawers are upserted; any upsert that
  fires must carry `is_sentinel=True`. The sentinel exists so the file is not re-extracted
  on every re-mine (tests/test_format_miner.py:L716-L743).
- Sentinels MUST NOT be written for transient missing-dependency statuses
  (`SKIP_NO_MARKITDOWN`, `SKIP_NO_STRIPRTF`, `SKIP_MISSING_FORMAT_DEPS`), so that
  installing the missing dependency later triggers a re-mine. (`_register_file` is the
  sentinel-writing hook and is not called for these statuses)
  (tests/test_format_miner.py:L1353-L1448).

### Successful extraction

- When extraction returns `OK` with text, `mine_formats` chunks the text and upserts at
  least one drawer (tests/test_format_miner.py:L746-L761).
- Chunk parameters `chunk_size`, `chunk_overlap`, and `min_chunk_size` from the user's
  `MempalaceConfig` are threaded through to `chunk_text`
  (tests/test_format_miner.py:L1489-L1553).

### Dry run

- With `dry_run=True`, `get_collection` is never called and no drawers are upserted
  (tests/test_format_miner.py:L764-L779).

### Cross-wing topic tunnels

- After the per-file loop, `_compute_topic_tunnels_for_wing(wing)` is called exactly once
  with the resolved wing (tests/test_format_miner.py:L1138-L1161).
- If `_compute_topic_tunnels_for_wing` raises, `mine_formats` MUST NOT propagate the
  exception — the mine completes (tests/test_format_miner.py:L1164-L1189).

### Resilience

- A per-file error MUST NOT crash the whole mine; the loop continues to the next file. For
  example, if `chunk_text` raises on the first file but succeeds on the second, the second
  file still produces a content upsert (tests/test_format_miner.py:L997-L1040).
- An unexpected error outside the per-file try/except (e.g. discovery/enumeration raising)
  is caught at an outer level: `mine_formats` does not raise, prints a partial-progress
  summary, and cleans up its PID file (tests/test_format_miner.py:L1451-L1486).

## Packaging contract (`pyproject.toml`)

The `mempalace[extract]` optional-dependency group MUST include the MarkItDown per-format
sub-extras `pdf`, `docx`, `pptx`, `xlsx` (each appearing inside some `markitdown[...]`
bracketed group). `.rtf` is covered by a separate `striprtf` dependency and `.epub` ships
in base MarkItDown, so neither needs a sub-extra here
(tests/test_format_miner.py:L1233-L1266).

## Platform note

Symlink-creation tests skip cleanly (rather than fail) on platforms/users lacking symlink
privileges; this is a test-harness accommodation, not a product contract
(tests/test_format_miner.py:L31-L45).
