# Spec: `mempalace/palace.py` — Shared Palace Operations

Consolidates collection access patterns used by both miners and the MCP server: backend resolution, embedder-identity enforcement, closet (index-layer) construction, mine locking, and "already mined" idempotency checks (mempalace/palace.py:L1-L5).

## Constants and Module State

`SKIP_DIRS` is a fixed set of directory names that traversal callers are expected to skip, including version control, dependency, cache, and build directories such as `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `env`, `dist`, `build`, `.next`, `coverage`, `.mempalace`, and language-specific cache/build dirs through `target` (mempalace/palace.py:L33-L57).

`NORMALIZE_VERSION` is the integer schema version for drawer normalization, currently `2`. When the normalization pipeline changes such that existing drawers should be rebuilt, this value is bumped; drawers whose stored `normalize_version` is missing or less than this value are treated as "not mined" so the next mine pass silently rebuilds them (mempalace/palace.py:L62-L70). Version 2 (2026-04) introduced noise-stripping for Claude Code JSONL; pre-v2 drawers stored system tags / hook chrome verbatim (mempalace/palace.py:L68-L70).

A default backend instance is constructed for the `chroma` backend at import time (mempalace/palace.py:L59). The environment variable name `MEMPALACE_BACKEND_EXPLICIT` is recognized as an explicit-backend override channel (mempalace/palace.py:L60, L311).

A process-wide set `_VALIDATED_IDENTITY` caches `(palace_path, collection_name, model_name)` tuples that have already had their embedder identity validated this process, so the identity check runs at most once per collection per run (mempalace/palace.py:L73-L76, L124-L126, L160).

## Embedder Identity Enforcement (RFC 001)

`_enforce_embedder_identity(collection, palace_path, collection_name, *, create)` checks (and for a brand-new empty collection, records) the embedder identity at open time so a model swap fails fast before any query silently returns degraded results (mempalace/palace.py:L79-L91).

It determines the "current" identity by first asking the collection for its effective embedder identity (used by server-side-embedding backends that embed with their own model); if that yields a named identity it is used, otherwise it falls back to the configured model name with dimension 0 (mempalace/palace.py:L103-L121). If no model name is available (nameless embedder or probe failure), enforcement is skipped (mempalace/palace.py:L116-L121).

If the `(palace_path, collection_name, model_name)` key is already in the validation cache, enforcement returns immediately (mempalace/palace.py:L124-L126).

It reads the stored identity and compares it against the current one. A deliberate identity-mismatch or dimension-mismatch error propagates to the caller (the user-facing contract); every other error is swallowed so bookkeeping never breaks memory operations (mempalace/palace.py:L128-L138). On unrecoverable read failures it logs at debug and returns without raising (mempalace/palace.py:L128-L138).

When no identity was previously recorded (`unknown` state with `stored is None`): if the collection count is `0` and `create` is true, the current identity is recorded; if the collection is non-empty, a warning is emitted instructing the user to run `mempalace palace set-embedder --model <name>` to record it (the populated-but-unrecorded legacy case must not be auto-labeled with the current model) (mempalace/palace.py:L140-L158). After processing, the key is added to the validation cache (mempalace/palace.py:L160).

## `get_collection`

`get_collection(palace_path, collection_name=None, create=True, backend=None, _skip_identity_check=False)` opens a palace collection through the backend layer (mempalace/palace.py:L163-L169).

If `collection_name` is `None`, the configured collection name is used (mempalace/palace.py:L176-L179). The backend is resolved for the palace (honoring the `explicit` override), and the collection is requested via a palace reference identifying the palace by path (mempalace/palace.py:L180-L195). If the backend's `get_collection` does not accept the `palace` keyword, a positional-path fallback call is made (mempalace/palace.py:L188-L195). If the backend declares the `requires_explicit_embeddings` capability, the returned collection is wrapped in an embedding-providing wrapper (mempalace/palace.py:L196-L197). Unless `_skip_identity_check` is set, embedder-identity enforcement runs before returning (mempalace/palace.py:L198-L200). `_skip_identity_check=True` exists so the `set-embedder` override path can open a palace whose recorded model differs from the current one (mempalace/palace.py:L170-L175).

## `set_palace_embedder_identity`

`set_palace_embedder_identity(palace_path, model=None, *, force=False, backend=None, collection_name=None)` records or force-overrides a collection's embedder identity and returns the tuple `(old, new)` of identities (mempalace/palace.py:L203-L218).

The target model is `model` if given, otherwise the configured `embedding_model`, normalized by stripping and lowercasing. If the resulting target is empty, a `ValueError` is raised stating no embedder model is available (mempalace/palace.py:L223-L230). If the target equals the configured model (normalized), the new identity is probed from the already-loaded embedder (including dimension); otherwise (explicit override of a non-configured model) only the name is recorded with dimension 0, never loading a foreign model just to probe a dimension (mempalace/palace.py:L231-L238).

The collection is opened with `create=True` and identity check skipped (mempalace/palace.py:L239-L245). The previously stored identity is read (best-effort, `None` on failure) (mempalace/palace.py:L246-L249). If a different model is already recorded and `force` is false, an identity-mismatch error is raised instructing the user to pass `--force` only if the vectors are compatible (mempalace/palace.py:L250-L254). Otherwise the new identity is written and the process-wide validation cache is cleared so a re-open re-checks against the newly recorded identity (mempalace/palace.py:L255-L259).

## `get_closets_collection`

`get_closets_collection(palace_path, create=True, backend=None)` returns the collection named `mempalace_closets`, which is the searchable index (closet) layer (mempalace/palace.py:L262-L273).

## Backend Resolution

`resolve_backend_name(palace_path, explicit=None)` resolves and validates the backend for a palace with this precedence: (1) explicit flag (CLI/MCP/direct argument or the `MEMPALACE_BACKEND_EXPLICIT` env var), (2) `backend` key in `~/.mempalace/config.json`, (3) `MEMPALACE_BACKEND` env var, (4) detected existing palace artifacts, (5) `chroma` default (mempalace/palace.py:L296-L318). The selected backend class is validated to exist (mempalace/palace.py:L319).

If the palace directory contains more than one backend's artifacts, a backend-mismatch error is raised listing them (mempalace/palace.py:L320-L325). If a single backend's artifacts are detected and differ from the selected backend, a backend-mismatch error is raised so write paths cannot silently mix storage formats in one palace directory (mempalace/palace.py:L326-L332). Otherwise the selected backend name is returned (mempalace/palace.py:L332).

`_config_backend_value(palace_path)` returns the config-file `backend` value (stripped, lowercased) only when the configured palace path resolves (after `~` expansion and absolute-path normalization) to the same path as the target palace; otherwise `None`, and any error yields `None` (mempalace/palace.py:L276-L288). `_env_backend_value()` returns the `MEMPALACE_BACKEND` env value stripped and lowercased, or `None` (mempalace/palace.py:L291-L293).

`get_backend_for_palace(palace_path, explicit=None)` returns the resolved backend instance (mempalace/palace.py:L335-L337).

`_backend_artifact_label(backend_name)` maps a backend name to its on-disk artifact label: `chroma` → `chroma.sqlite3`, `qdrant` → `qdrant_backend.json`, `pgvector` → `pgvector_backend.json`, `sqlite_exact` → `sqlite_exact.sqlite3`, anything else → `backend database` (mempalace/palace.py:L340-L349).

## `_open_collection_or_explain` (CLI/repair state messaging)

`_open_collection_or_explain(palace_path, *, collection_name=None, out=None, opener=None)` opens the palace collection or prints a state-specific, actionable message and returns `None`. The message sink defaults to the builtin print; a callable may be passed to route messages elsewhere (mempalace/palace.py:L352-L389).

The distinguished states are:
- State A — palace directory absent: emits a "No palace found" message and `init`/`mine` instructions, returns `None` (mempalace/palace.py:L391-L394).
- Backend mismatch during resolution: emits the mismatch message and returns `None` (mempalace/palace.py:L395-L400). Unknown backend name (e.g. a typo): emits an "Unknown backend selected" message instructing the user to set `--backend`/`MEMPALACE_BACKEND` to a registered backend, returns `None` (mempalace/palace.py:L401-L408).
- State B — directory present but no backend database artifact detected: short-circuits to a message naming the expected artifact label and `mempalace mine` instruction, returning `None` before touching the backend (so a read-only inspection does not lazily create the DB file) (mempalace/palace.py:L376-L379, L409-L416).
- State C — DB present but collection never bootstrapped (`init` ran, `mine` did not): on `CollectionNotInitializedError`, emits "initialized but empty (no drawers yet)" and `mine` instruction, returns `None` (mempalace/palace.py:L379-L381, L417-L427).
- `PalaceNotFoundError`: emits "No palace found" + init/mine instructions, returns `None` (mempalace/palace.py:L428-L431).
- Backend mismatch raised at open: emits mismatch message, returns `None` (mempalace/palace.py:L432-L435).
- `BackendClosedError`: re-raised (treated as a programmer/lifecycle error, not a recoverable palace state) (mempalace/palace.py:L436-L440).
- State E — any other unexpected error: emits an error message pointing the user at `mempalace repair-status --palace <path>`, returns `None` (mempalace/palace.py:L441-L444).
- State D — healthy: opens with `create=False` and the resolved backend and returns the collection (mempalace/palace.py:L381-L384, L417-L423).

## Closet (Index Layer) Construction

`CLOSET_CHAR_LIMIT` is 1500: a closet is filled until roughly 1500 chars, then a new one starts (mempalace/palace.py:L447). `CLOSET_EXTRACT_WINDOW` is 5000: only the first 5000 chars of source content are scanned for entities/topics (mempalace/palace.py:L448, L558).

`_ENTITY_STOPLIST` is a fixed set of capitalized words (articles, question words, conjunctions, role words like `User`/`Assistant`/`System`/`Tool`, weekday names, and month names) that are filtered out of entity extraction (mempalace/palace.py:L450-L507).

`_candidate_entity_words(text)` returns a list of entity-candidate words found using i18n-aware regular-expression patterns loaded from locale data for the configured entity languages, so non-Latin names (Cyrillic, accented Latin, etc.) are detected alongside ASCII. Compiled patterns are cached process-wide; uncompilable patterns are skipped (mempalace/palace.py:L513-L536).

### `build_closet_lines`

`build_closet_lines(source_file, drawer_ids, content, wing, room, drawer_metas=None)` returns a LIST of closet pointer lines; each line is one complete topic pointer and is never split across closets (mempalace/palace.py:L539-L553).

The drawer reference field is the first up to three `drawer_ids` joined by commas (mempalace/palace.py:L557). Entity/topic extraction scans only the first `CLOSET_EXTRACT_WINDOW` chars of content (mempalace/palace.py:L558).

Each emitted line has one of two pipe-delimited on-disk formats (mempalace/palace.py:L546-L552, L608-L611):
- Legacy 3-segment: `topic|entities|→drawer_ids`
- Tier 6a 4-segment: `topic|entities|YYYY-MM-DD:Lstart-Lend|→drawer_ids`

The 4-segment form is emitted when a date+line locator segment can be built from `drawer_metas`; otherwise every line uses the legacy 3-segment form (mempalace/palace.py:L559-L562, L605-L611).

Entities: a known-systems compound pre-pass detects multi-word product names ("Claude Code", "GitHub Copilot", …) atomically and masks them out of the working window so single-word extraction does not decompose them; their counts seed the frequency map (mempalace/palace.py:L564-L583). Single-word candidates are then counted, skipping any word in `_ENTITY_STOPLIST` and any word whose lowercase form is in the common-content-word ("COCA") filter (mempalace/palace.py:L572-L583). Entities are those terms with frequency ≥ 2, sorted by descending frequency, capped at 5, and joined with `;` into the entities field (empty string if none) (mempalace/palace.py:L584-L588).

Topics: action-verb phrases are extracted case-insensitively (verbs include built/fixed/wrote/added/pushed/tested/created/decided/migrated/reviewed/deployed/configured/removed/updated followed by 3–40 word/space chars), and Markdown section headers of 1–3 `#` levels (header text 5–60 chars) are appended; the combined list is deduplicated preserving order, each lowercased and stripped, capped at 12 (mempalace/palace.py:L590-L600).

Quotes: double-quoted spans of length 15–150 are extracted (mempalace/palace.py:L602-L603).

One pointer line is emitted per topic, then one per the first up to three quotes (quote pointers wrap the quote in double quotes as the prefix) (mempalace/palace.py:L613-L617). If no lines were produced, exactly one fallback line is emitted whose prefix is `wing/room/<source-file-stem-truncated-to-40-chars>` (mempalace/palace.py:L619-L622).

### `_build_date_line_segment`

`_build_date_line_segment(drawer_metas)` produces a `YYYY-MM-DD:Lstart-Lend` locator string from only the first entry of the drawer-meta list, matching the `drawer_ids[:3]` truncation philosophy (pointers are approximate locators, not exhaustive indexes) (mempalace/palace.py:L627-L637, L663-L665).

Returns `None` (caller falls back to legacy 3-segment) when: the list is empty, the first meta is not a mapping, or `line_start`/`line_end` is missing (mempalace/palace.py:L638-L646). For the date part, it prefers a `content_date` (an already-ISO `YYYY-MM-DD` value) when present; otherwise it uses `filed_at` truncated at the first `T` (e.g. `2026-05-21T22:30:00.123456+00:00` → `2026-05-21`); if neither yields a non-empty date part, returns `None` (mempalace/palace.py:L648-L665). The output format is `<date>:L<line_start>-L<line_end>` (mempalace/palace.py:L665).

### Closet writing

`purge_file_closets(closets_col, source_file)` deletes every closet whose metadata `source_file` matches; called before re-writing closet lines on a re-mine so stale topics do not survive. Deletion failures are logged at debug and swallowed (mempalace/palace.py:L668-L678).

`upsert_closet_lines(closets_col, closet_id_base, lines, metadata)` packs the lines greedily into closets without splitting any line, and returns the number of closets written (mempalace/palace.py:L681-L690). Closets are deterministically numbered `<closet_id_base>_01`, `<closet_id_base>_02`, … with a zero-padded 2-digit suffix; each upsert fully overwrites the prior content at that ID (mempalace/palace.py:L691-L703). A new closet is started whenever adding the next line (plus a newline) would exceed `CLOSET_CHAR_LIMIT` and the current closet is non-empty; lines within a closet are joined by newlines (mempalace/palace.py:L705-L718). Because IDs are deterministic and reused, callers must `purge_file_closets` first when re-mining so stale higher-numbered closets from a larger prior run do not leak (mempalace/palace.py:L685-L690).

## Per-File Mine Lock

`mine_lock(source_file)` is a context manager providing a cross-platform file lock that prevents two agents from mining the same source file simultaneously (which would otherwise create duplicate drawers when delete+insert cycles interleave) (mempalace/palace.py:L721-L742).

The lock path is `~/.mempalace/locks/<first-16-hex-of-sha256(source_file)>.lock`, and the locks directory is created if absent (mempalace/palace.py:L744-L747). The lock is acquired blocking (mempalace/palace.py:L817-L838); on release the file is unlocked, closed, and a best-effort cleanup-unlink is attempted, all errors logged at debug and swallowed (mempalace/palace.py:L730-L741).

Locking uses Windows byte-range locking (`msvcrt`) on Windows and POSIX advisory `flock` elsewhere (mempalace/palace.py:L758-L796). Because POSIX advisory locks attach to the opened inode rather than the pathname, after acquiring the code verifies (via device+inode comparison of the path versus the open handle) that the held handle is still the inode reachable by the path; if not, it releases the stale handle and retries on the current pathname (mempalace/palace.py:L799-L838). Cleanup-unlink re-acquires the file non-blocking and only unlinks if it wins and the handle is still current, preserving the flock rendezvous so a waiter blocked on an old inode wakes, detects staleness, and retries on the current path; on Windows it releases/closes before attempting removal (mempalace/palace.py:L841-L904).

## Mine Validation and Errors

`MineAlreadyRunning` is an error raised when another `mempalace mine` already holds the per-palace lock (mempalace/palace.py:L907-L908).

`MineValidationError` is an error raised at end of mine when an integrity check on the palace reports errors (mempalace/palace.py:L911-L912). Construction requires a non-empty palace path and at least one error string, raising `ValueError` otherwise (mempalace/palace.py:L914-L918). Its message states `FTS5/SQLite quick_check failed: <N> issue(s)`, and it exposes the `palace_path` and an immutable tuple `errors` snapshot (mempalace/palace.py:L919-L922).

`_validate_palace_fts5_after_mine(palace_path)` raises `MineValidationError` if a SQLite integrity check reports errors after a mine; it applies only to the `chroma` backend and is a no-op otherwise (mempalace/palace.py:L925-L933). Before the read-only integrity check it closes the live ChromaDB handles (passing the live default backend singleton so the writer's cached client is actually closed and the WAL is flushed before the re-open) (mempalace/palace.py:L934-L947).

## Per-Palace Mine Lock

`mine_palace_lock(palace_path)` is a context manager providing a non-blocking, per-palace lock around the full mine pipeline so that N concurrent `mempalace mine <dir>` invocations cannot corrupt the same palace's vector index (mempalace/palace.py:L1042-L1072).

The lock file is `~/.mempalace/locks/mine_palace_<first-16-hex-of-sha256(key)>.lock`, where `key` is the fully normalized palace path: `~`-expanded, `realpath`-resolved (symlinks and `..` collapsed), and case-folded (so case-insensitive filesystems do not let two paths differing only in case hash to different keys) (mempalace/palace.py:L1057-L1078). Locks against different palaces can proceed in parallel; only writes into the same palace are serialized (mempalace/palace.py:L1053-L1056).

The lock is re-entrant per thread: if the current thread already holds the lock for this palace, the context manager passes through without re-acquiring (so collection-level write methods that take the lock compose with the outer mine-pipeline lock without self-deadlock) (mempalace/palace.py:L1067-L1071, L1080-L1083). Re-entrancy tracking is per-thread and tagged with the process id so a forked child does not inherit re-entrant credit from its parent (the OS flock is not inherited semantically; the child must reacquire) (mempalace/palace.py:L950-L988).

Acquisition is non-blocking on byte 0 of the file: Windows uses `msvcrt` non-blocking lock; POSIX uses `flock` exclusive non-blocking. If the lock is already held, a `MineAlreadyRunning` error is raised whose message names the resolved palace path and the prior holder, telling the caller to wait or stop the holder (mempalace/palace.py:L1100-L1129).

Byte 0 of the lock file is reserved as the OS lock sentinel; holder identity is written from byte 1 onward so a contender can read the identity without colliding with the locked byte (mempalace/palace.py:L1002-L1006). On acquire, the holder writes its identity (`<pid> <first 3 argv joined by space>`) from byte 1 (best-effort; failures swallowed) (mempalace/palace.py:L1024-L1039, L1130-L1131). A waiting contender reads the prior holder's identity from byte 1 onward and renders it as `PID N (cmdline)`, `PID N`, or `another writer (identity not recorded)` when the body is empty/non-numeric (mempalace/palace.py:L991-L1021). On exit the byte-0 lock is released (matching region) and the file closed; the per-thread held marker is cleared in a `finally` even if the body raises (mempalace/palace.py:L1132-L1152).

`mine_global_lock` is a backward-compatible alias for `mine_palace_lock`; new code should use `mine_palace_lock(palace_path)` for per-palace scoping (mempalace/palace.py:L1155-L1158).

## "Already Mined" Idempotency

`_metadata_matches_extract_mode(meta, extract_mode)` returns true when `extract_mode` is `None`, or when the stored `extract_mode` equals it; additionally, when checking for `exchange` mode, a stored `extract_mode` of `None` (legacy drawer) counts as a match (mempalace/palace.py:L1161-L1165).

`file_already_mined(collection, source_file, check_mtime=False, extract_mode=None)` returns whether a file has already been filed in the palace at the current schema (mempalace/palace.py:L1168-L1190). It returns `False` (so the file gets re-mined) when no drawers exist for the source file, when the stored `normalize_version` is missing or older than `NORMALIZE_VERSION` (triggering a silent rebuild after a normalization upgrade), or — when `check_mtime=True` — when the file's current mtime differs from the stored `source_mtime` (mempalace/palace.py:L1174-L1189).

Because additive mining lets one source file have multiple drawer groups (one per mining pass) each with its own stored `source_mtime`/`normalize_version`, the function returns `True` if ANY stored group is current; it iterates the matching drawers in paginated batches of 1000 (offset-advanced by the returned id count) rather than relying on a `limit=1` shortcut whose ordering across matching rows is undefined (mempalace/palace.py:L1191-L1213). For each meta: when `extract_mode` is set, drawers not matching the mode are skipped (mempalace/palace.py:L1216-L1220); a missing `normalize_version` defaults to `1` and any version below `NORMALIZE_VERSION` is skipped as stale (mempalace/palace.py:L1222-L1224); when `check_mtime` is false the first surviving match returns `True` (mempalace/palace.py:L1226-L1227); when `check_mtime` is true, a meta with no `source_mtime` is skipped, and a match returns `True` only when the stored and current mtimes are within 0.001 seconds (mempalace/palace.py:L1228-L1232). After exhausting all pages without a current match, returns `False`; any exception yields `False` (mempalace/palace.py:L1233-L1238).

`bulk_check_mined(collection)` returns a dict mapping `source_file` → `source_mtime` (as a float) for every document carrying both fields, fetched in paginated batches of 1000 over the whole collection (since a WHERE-IN filter on thousands of paths is unsupported) so callers can compare locally instead of one query per file (mempalace/palace.py:L1241-L1264). On error it logs a warning and returns whatever was loaded so far (partial result) (mempalace/palace.py:L1265-L1267).

`prefetch_mined_set(collection, extract_mode=None)` returns the set of `source_file` paths whose stored drawers are at or above `NORMALIZE_VERSION`, fetched in a single paginated scan (batches of 1000) instead of one query per file, for callers that do `if path in result_set: skip` (mempalace/palace.py:L1270-L1286). It mirrors the `file_already_mined` `check_mtime=False` version gate: a missing `normalize_version` defaults to `1`; drawers are filtered by `extract_mode` using the same matching rule; a source is added only when its version is `≥ NORMALIZE_VERSION` (mempalace/palace.py:L1287-L1305). On error it logs a warning and returns the partial set (mempalace/palace.py:L1306-L1308).

## Externally Observable Contracts Summary

- Closet pointer line format (on-disk in the `mempalace_closets` collection): `topic|entities|→drawer_ids` (legacy) or `topic|entities|YYYY-MM-DD:Lstart-Lend|→drawer_ids` (Tier 6a); entities joined by `;`, up to 5; drawer ids joined by `,`, up to 3 (mempalace/palace.py:L546-L552, L557, L584-L588, L608-L611).
- Closet IDs: `<base>_NN` with zero-padded 2-digit sequence starting at 01; each ~1500 chars; lines never split (mempalace/palace.py:L691-L718).
- Lock files live under `~/.mempalace/locks/`: per-file `<sha256[:16]>.lock`, per-palace `mine_palace_<sha256[:16]>.lock`; byte 0 is the OS lock sentinel; holder identity stored from byte 1 as `<pid> <argv0..2>` (mempalace/palace.py:L744-L747, L1002-L1006, L1024-L1039, L1073-L1078).
- Backend artifact filenames: `chroma.sqlite3`, `qdrant_backend.json`, `pgvector_backend.json`, `sqlite_exact.sqlite3` (mempalace/palace.py:L340-L349).
- Environment variables consulted: `MEMPALACE_BACKEND_EXPLICIT`, `MEMPALACE_BACKEND` (mempalace/palace.py:L60, L292, L311).
