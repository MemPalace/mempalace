# Spec: `mempalace/miner.py` — Project File Miner

Files project files into the palace verbatim: reads `mempalace.yaml`, routes each file to a room, splits content into drawer-sized chunks, and stores them with metadata and a searchable closet index. No summarization (mempalace/miner.py:L1-L8).

## Constants & Admission Rails

### File-type admission
- `READABLE_EXTENSIONS` is the set of file suffixes that are mineable: `.txt .md .py .js .ts .jsx .tsx .json .jsonl .yaml .yml .html .css .java .go .rs .swift .kt .kts .rb .sh .csv .sql .toml .cs .csproj .sln .razor .cshtml` plus all PHP-family extensions (mempalace/miner.py:L78-L109).
- `PHP_EXTENSIONS` is a set of PHP/template suffixes: `.php .php3 .php4 .php5 .php7 .php8 .phtml .phps .phpt .inc .aw .fcgi .ctp .module .install .profile .theme .engine .twig .blade .tpl .latte .volt` (mempalace/miner.py:L50-L76).
- `SKIP_FILENAMES` are always skipped (unless force-included): `entities.json`, `mempalace.yaml`, `mempalace.yml`, `mempal.yaml`, `mempal.yml`, `.gitignore`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock` (mempalace/miner.py:L111-L121).

### Size & chunk caps
- `MAX_FILE_SIZE` = 500 MB; files larger than this are skipped during scan (mempalace/miner.py:L134, L1562-L1563).
- `DRAWER_UPSERT_BATCH_SIZE` = 1000: chunks are written in batches of at most this size (mempalace/miner.py:L133, L1421).
- `MAX_CHUNKS_PER_FILE` default = 50,000: a file producing more chunks than the effective cap is skipped (mempalace/miner.py:L148, L1363-L1375).
- `CHUNK_SIZE` / `CHUNK_OVERLAP` / `MIN_CHUNK_SIZE` are re-exported aliases of `config.DEFAULT_CHUNK_*` for backward compatibility (mempalace/miner.py:L127-L131).

### Chunk-cap resolution (`_resolve_max_chunks_per_file`)
- Precedence: explicit `override` argument > env `MEMPALACE_MAX_CHUNKS_PER_FILE` > `MAX_CHUNKS_PER_FILE` default (mempalace/miner.py:L158-L197).
- A value of `0` from any source disables the cap entirely (mempalace/miner.py:L159-L168, L1363).
- A negative `override` prints a WARNING to stderr and falls back to the default (mempalace/miner.py:L170-L177).
- A non-integer env value prints a WARNING to stderr and falls back to the default (mempalace/miner.py:L181-L189).
- A negative env value prints a WARNING to stderr and falls back to the default (mempalace/miner.py:L190-L196).

## Ignore Matching

### `GitignoreMatcher`
- Loads one directory's `.gitignore` and produces an ignore decision for paths relative to that directory's base (mempalace/miner.py:L205-L261).
- `from_dir` returns `None` if there is no `.gitignore` file, the file cannot be read, or it yields no usable rules (mempalace/miner.py:L212-L261).
- Parsing rules: blank lines skipped; `\#`/`\!` prefixes are literal-escaped (leading char stripped); `#` lines are comments and skipped; leading `!` marks a negated rule; leading `/` marks an anchored rule; trailing `/` marks a directory-only rule (mempalace/miner.py:L224-L256).
- `matches(path, is_dir)` returns `True`/`False` if any rule matched (last match wins), or `None` if no rule applies or the path is outside the base dir (mempalace/miner.py:L263-L279).
- Rule matching: directory-only rules apply to dir components (the path itself if a dir, else its parent components); anchored or multi-segment patterns match from the root with `**` recursion support; otherwise any path component matched by glob counts (mempalace/miner.py:L281-L318).

### Helpers
- `load_gitignore_matcher(dir, cache)` loads and memoizes a directory's matcher in `cache` (mempalace/miner.py:L321-L325).
- `is_gitignored(path, matchers, is_dir)` applies matchers in ancestor order; last non-`None` decision wins; default not-ignored (mempalace/miner.py:L328-L335).
- `should_skip_dir(dirname)` is `True` when the directory name is in `SKIP_DIRS` or ends with `.egg-info` (mempalace/miner.py:L338-L340).
- `normalize_include_paths(list)` strips whitespace and slashes from each entry and returns a set of project-relative POSIX strings (mempalace/miner.py:L343-L350).
- `is_exact_force_include(path, project, includes)` is `True` only when the path's project-relative POSIX form exactly equals a listed include (mempalace/miner.py:L353-L363).
- `is_force_included(path, project, includes)` is `True` when the relative path equals, is a descendant of, or is an ancestor of any listed include (mempalace/miner.py:L366-L387).

## Config Loading

`load_config(project_dir)` (mempalace/miner.py:L395-L434):
- Resolves the project dir (expanduser + resolve) and looks for `mempalace.yaml`, falling back to legacy `mempal.yaml` (mempalace/miner.py:L399-L405).
- If neither exists: prints a notice to stderr, derives the wing name by normalizing the project directory's basename, and returns a default config with one `general` room (`{"wing": <name>, "rooms": [{"name":"general","description":"All project files","keywords":["general"]}]}`) (mempalace/miner.py:L406-L432).
- Otherwise parses and returns the YAML document as a dict (mempalace/miner.py:L433-L434).

## File Routing (`detect_room`)

Routes a file to a room name with this priority order (mempalace/miner.py:L464-L503):
1. Any folder path component (excluding the filename) that matches a room name or one of its keywords (mempalace/miner.py:L477-L483).
2. The filename stem matches a room name (mempalace/miner.py:L485-L488).
3. Keyword scoring: for each room, count case-insensitive occurrences (in the first 2000 chars of content) of its keywords plus its name; the highest-scoring room with a positive score wins (mempalace/miner.py:L475, L490-L501).
4. Fallback room name `"general"` (mempalace/miner.py:L503).

Matching uses `_name_matches`, which is `True` on string equality or when one value is a separator-bounded token (split on `-`, `_`, `.`, `/`) of the other, preventing incidental substring collisions like `views` in `interviews` (mempalace/miner.py:L441-L461).

## Chunking (`chunk_text`)

Splits content into drawer chunks, returning a list of objects with shape `{"content": str, "chunk_index": int, "line_start": int, "line_end": int}` (mempalace/miner.py:L511-L605).
- Defaults for unspecified `chunk_size`/`chunk_overlap`/`min_chunk_size` come from the module-level constants (mempalace/miner.py:L531-L536).
- Validation (raises `ValueError`): `chunk_size` must be a positive int; `chunk_overlap` must be a non-negative int; `chunk_overlap` must be strictly less than `chunk_size`; `min_chunk_size` must be a non-negative int (mempalace/miner.py:L544-L557).
- Content is stripped; empty content returns `[]` (mempalace/miner.py:L559-L562).
- Chunks advance by `chunk_size`, then attempt to break at a paragraph boundary (`\n\n`) past the midpoint, else a line boundary (`\n`) past the midpoint (mempalace/miner.py:L568-L580).
- A candidate chunk is emitted only if its stripped length is `>= min_chunk_size`; emitted chunks get a sequential `chunk_index` starting at 0 (mempalace/miner.py:L581-L601).
- `line_start`/`line_end` are 1-indexed line numbers in the stripped source (count of `\n` before the start/end offset, plus 1); they are approximate locators (mempalace/miner.py:L583-L592).
- Next start = `end - chunk_overlap` when more content remains, else `end` (mempalace/miner.py:L603).

## Known-Entity Registry

On-disk file: `~/.mempalace/known_entities.json` (mempalace/miner.py:L613).

### Cache & loading
- `_refresh_known_entities_cache` reloads the JSON only when the file's mtime changed; on read failure (`OSError` reading mtime) the cache is reset to empty (mempalace/miner.py:L619-L667).
- The registry is a dict of category → list (of names) or category → dict (name→code). The special key `topics_by_wing` maps wing → list of topic names; its inner topic names are surfaced as known entities but its outer wing keys are NOT (mempalace/miner.py:L645-L667).
- `_load_known_entities()` returns a flat frozenset of all known entity names (mempalace/miner.py:L670-L676).
- `_load_known_entities_raw()` returns a shallow copy of the full category dict (mempalace/miner.py:L679-L688).

### Writing (`add_to_known_entities`)
- Unions `{category: [names]}` input into the registry file, creating the parent directory if needed (mempalace/miner.py:L723-L756).
- Categories stored as lists are unioned case-insensitively, preserving on-disk order of existing names; categories stored as `{name: code}` dicts get new names added as keys with `None` value (existing codes preserved); unknown/missing categories are seeded as a deduped list (mempalace/miner.py:L779-L816).
- When `wing` is provided and input has a `topics` list, those topics are recorded under `topics_by_wing[wing]`, case-insensitively deduped (first-seen casing kept), and *replaced* (not unioned) per wing; empty topics drop the wing entry; empty map drops the `topics_by_wing` key (mempalace/miner.py:L691-L721, L775-L819).
- The file is written as JSON with indent 2 and `ensure_ascii=False`, then chmod `0o600` (best-effort) (mempalace/miner.py:L821-L825).
- The in-process cache is invalidated after write so same-process callers see the change immediately (mempalace/miner.py:L827-L830).
- Returns the registry path as a string (mempalace/miner.py:L750-L832).
- `get_topics_by_wing()` returns the `topics_by_wing` map filtered to valid non-empty wing strings with non-empty string topic lists; returns `{}` when missing/malformed (mempalace/miner.py:L835-L854).

## Hall Detection (`detect_hall`)

Routes content to a hall by keyword scoring against `MempalaceConfig().hall_keywords` (cached after first call). Scans the first 3000 chars lowercased, counts keyword presence per hall, returns the highest-scoring hall or `"general"` when none score (mempalace/miner.py:L857-L881).

## Entity Metadata Extraction (`_extract_entities_for_metadata`)

Returns a `;`-joined string of entity names for drawer metadata (or `""` when none) (mempalace/miner.py:L884-L938):
- Known-registry names matched case-insensitively against the full content with word boundaries (mempalace/miner.py:L900-L909).
- Capitalized candidate words appearing `>= 2` times (and length `> 2`) within the first `_ENTITY_EXTRACT_WINDOW` = 5000 chars, after a known-systems compound pre-pass and after filtering the closet stoplist and COCA common-word filter (mempalace/miner.py:L611-L616, L911-L933).
- The result list is sorted and truncated to `_ENTITY_METADATA_LIMIT` = 25 entries *before* joining (so a name is never cut in half) (mempalace/miner.py:L616, L934-L938).

## Content-Date Extraction (`_extract_content_date`)

Returns an ISO `YYYY-MM-DD` string or `None`, first-match-wins across this hierarchy (mempalace/miner.py:L1202-L1229):
1. Filename stem: ISO regex first, then a strict full-date regex gate (`_VALID_DATE_RE`) before a non-fuzzy date parse; junk filenames return `None` (mempalace/miner.py:L1018-L1060).
2. YAML frontmatter `date`/`created`/`published` field (frontmatter delimited by leading `---` and a closing `\n---`); parsed values formatted as `%Y-%m-%d` (mempalace/miner.py:L1063-L1113).
3. Content body, first ~10 lines (frontmatter skipped): ISO regex, then slash-dates with locale auto-disambiguation, then a gated natural-language parse (mempalace/miner.py:L1116-L1187).
4. Filesystem mtime formatted as ISO date (mempalace/miner.py:L1190-L1199).
5. `None` — caller falls back to `filed_at` (mempalace/miner.py:L1228-L1229).

Date-parsing invariants:
- ISO regex matches `YYYY-MM-DD`, `YYYY/MM/DD`, `YYYY.MM.DD`; invalid calendar values return `None` (mempalace/miner.py:L965, L1005-L1015).
- `_VALID_DATE_RE` accepts only three complete shapes (numeric YYYY-MM-DD; month-name + day + year; day + month-name + year); partial dates are deliberately rejected; fuzzy parsing is never enabled (mempalace/miner.py:L968-L1002).
- Slash-date locale: if any first-number `> 12` appears in the head, locale is locked to DD/MM for the file; two-digit years follow the 70–99→19xx, 00–69→20xx convention (mempalace/miner.py:L1152-L1170).

## Drawer Metadata (`_build_drawer_metadata`)

Builds the metadata object for one drawer (mempalace/miner.py:L1232-L1281). Always-present keys: `wing`, `room`, `source_file`, `chunk_index`, `added_by` (the agent), `filed_at` (current local time, ISO), `normalize_version`, `id_recipe`, and `hall` (mempalace/miner.py:L1259-L1277). Conditional keys, added only when non-`None`/non-empty: `source_mtime`, `line_start`, `line_end`, `content_date`, `entities` (mempalace/miner.py:L1269-L1280).

`add_drawer(...)` is a backward-compatible single-drawer writer: it computes the deterministic drawer id, reads the source mtime (or `None` on `OSError`), builds metadata, and upserts one document into the collection; returns `True` (mempalace/miner.py:L1284-L1306).

## Processing One File (`process_file`)

Returns `(drawer_count, room_name, skip_reason)` (mempalace/miner.py:L1314-L1336):
- `skip_reason` is `None` on success and on every non-chunk-cap skip; it is `"chunk_cap"` when the per-file chunk cap aborted the file (mempalace/miner.py:L1330-L1336).
- Pre-lock skip if already mined (mtime-checked) when not dry-run → returns `(0, "general", None)` (mempalace/miner.py:L1339-L1342).
- File read uses UTF-8 with replacement on errors; on `OSError` returns `(0, "general", None)` (mempalace/miner.py:L1344-L1347).
- Content is stripped; if shorter than the effective `min_chunk_size`, returns `(0, "general", None)` (mempalace/miner.py:L1349-L1351).
- Room is detected, then content is chunked (mempalace/miner.py:L1353-L1360).
- If the effective cap > 0 and chunk count exceeds it: prints a `! [skip]` notice to stderr and returns `(0, room, "chunk_cap")` (mempalace/miner.py:L1362-L1375).
- Dry-run: prints a `[DRY RUN]` line and returns `(len(chunks), room, None)` without writing (mempalace/miner.py:L1377-L1379).
- Otherwise, under a per-file lock: re-checks already-mined (returns `(0, room, None)` if so); deletes stale drawers for this `source_file` (purge ignored on failure, debug-logged) before re-inserting (mempalace/miner.py:L1384-L1397).
- Extracts source mtime (once) and the content-date (once per file, shared across chunks) (mempalace/miner.py:L1404-L1412).
- Writes chunks in batches of `DRAWER_UPSERT_BATCH_SIZE`; each batch checks collisions via `assert_no_collisions` before upserting documents, ids, and metadata; accumulates all metadata (mempalace/miner.py:L1421-L1450).
- When a closets collection is provided and drawers were added: builds closet lines (passing drawer metadata to enable the Tier-6a 4-segment pointer `topic|entities|YYYY-MM-DD:Lstart-Lend|→ids`, falling back to a 3-segment form otherwise), purges prior closets for the file, and upserts the new closet lines with metadata (`wing`, `room`, `source_file`, `drawer_count`, `filed_at`, `normalize_version`, optional `entities`) (mempalace/miner.py:L1452-L1486).
- The closet id base is `closet_{wing}_{room}_{first-24-hex-of-sha256(source_file)}` (mempalace/miner.py:L1471-L1473).
- Returns `(drawers_added, room, None)` (mempalace/miner.py:L1488).

## Scanning a Project (`scan_project`)

Returns the list of mineable file paths under `project_dir` (mempalace/miner.py:L1496-L1567):
- Walks the resolved project tree (mempalace/miner.py:L1507-L1513).
- When `respect_gitignore`, active matchers are pruned to ancestors of the current dir and the current dir's matcher is added (mempalace/miner.py:L1516-L1524).
- Directories are pruned: skip-dirs (and gitignored dirs) are removed unless force-included (mempalace/miner.py:L1526-L1538).
- For each file: skip if in `SKIP_FILENAMES` (unless force-included); skip if its suffix is not in `READABLE_EXTENSIONS` (unless exact-force-included); skip if gitignored (unless force-included) (mempalace/miner.py:L1540-L1551).
- Symlinks are skipped and logged to stderr as `  SKIP: <rel> (symlink)` (mempalace/miner.py:L1552-L1559).
- Files larger than `MAX_FILE_SIZE` are skipped; an `OSError` stat-ing a file also skips it (mempalace/miner.py:L1560-L1565).

## Mining (`mine` / `_mine_impl`)

`mine(project_dir, palace_path, wing_override, agent="mempalace", limit=0, dry_run=False, respect_gitignore=True, include_ignored=None, files=None, max_chunks_per_file=None)` (mempalace/miner.py:L1575-L1586):
- Dry-run delegates directly to `_mine_impl` without taking the palace lock (mempalace/miner.py:L1600-L1612).
- Non-dry-run takes a palace-wide write lock (`mine_palace_lock`); a `MineAlreadyRunning` exception propagates so the CLI can exit non-zero (mempalace/miner.py:L1614-L1629).
- `files`, when supplied, skips re-walking the tree (mempalace/miner.py:L1588-L1593, L1657-L1662).

`_mine_impl` behavior:
- Loads config and `MempalaceConfig` chunk settings; wing is `wing_override` or the config's wing; rooms default to a single `general` room (mempalace/miner.py:L1646-L1655).
- Prints a header summarizing wing, room names, file count (with optional `(limit: N new)` suffix), palace path, device, and flags for dry-run / disabled gitignore / include paths (mempalace/miner.py:L1665-L1680).
- Opens the drawers and closets collections only when not dry-run (mempalace/miner.py:L1682-L1687).
- Iterates files (1-indexed), calling `process_file` per file (mempalace/miner.py:L1698-L1718).
- Counter rules: any zero-drawer outcome increments `files_skipped` (and `files_skipped_chunk_cap` when `skip_reason == "chunk_cap"`); non-zero outcomes add to `total_drawers`, increment the room count and `files_mined`, and print a `+ [i/N] name +drawers` progress line when not dry-run (mempalace/miner.py:L1724-L1740).
- `limit > 0` stops the loop once `files_mined >= limit` (mempalace/miner.py:L1741-L1742).
- After the loop (non-dry-run only), in fault-tolerant blocks that never fail the mine: computes cross-wing topic tunnels, within-wing hallways, and cross-wing entity tunnels (each printing a summary line on success or a WARNING to stderr on failure), then runs `_validate_palace_fts5_after_mine` (mempalace/miner.py:L1744-L1791).
- Prints a Done summary: `Files processed` = `files_processed - files_skipped`; a residual-skip line whose label differs by mode; an optional chunk-cap-skip line; `Drawers filed`; and a per-room histogram sorted by count descending (mempalace/miner.py:L1793-L1821).

### Interrupt / error contracts
- `KeyboardInterrupt`: prints an interrupted summary (files processed, drawers filed, last file) and a resume hint, then exits with code `130` (mempalace/miner.py:L1719-L1723, L1822-L1836).
- `MineValidationError` (post-write FTS5 validation failure): re-raised without a partial-progress banner (mempalace/miner.py:L1837-L1843).
- Any other exception: prints an "aborted by exception" summary (including `error: <Type>: <msg>`) and a resume hint, then re-raises so the traceback surfaces and the exit code is non-zero (mempalace/miner.py:L1844-L1861).
- Re-mines are idempotent: deterministic drawer ids mean already-filed drawers upsert to the same row, so partial progress is safe to resume (mempalace/miner.py:L1822-L1834).

### PID cleanup (`_cleanup_mine_pid_file`)
- Runs in a `finally` on every exit path (mempalace/miner.py:L1862-L1869).
- Reads the path from env `MEMPALACE_MINE_PID_FILE`; if unset, returns (mempalace/miner.py:L1886-L1888).
- The PID file format is `"{pid} {unix_timestamp}"` (old bare `"{pid}"` form also supported by taking the first whitespace token) (mempalace/miner.py:L1893-L1898).
- Deletes the slot only if the recorded PID equals the current process PID; any other PID is left alone; all failures are best-effort/ignored (mempalace/miner.py:L1898-L1903).

### Tunnel/hallway helpers
- `_compute_topic_tunnels_for_wing(wing)` returns the count of tunnels created/refreshed between this wing and others sharing confirmed topics, honoring `topic_tunnel_min_count`; returns 0 when no topics map exists or the wing is absent (mempalace/miner.py:L1906-L1922).
- `_compute_entity_tunnels_for_wing(wing)` returns the count of cross-wing entity tunnels derived from hallway records; returns 0 when no hallways exist (mempalace/miner.py:L1925-L1947).

## Status (`status` / `_print_status`)

`status(palace_path)` tallies drawers by wing/room (mempalace/miner.py:L1955-L1993):
- Primary path reads counts directly from `chroma.sqlite3` via `_sqlite_wing_room_counts(palace_path, "mempalace_drawers")` to avoid cold-loading the vector index (mempalace/miner.py:L1965-L1971).
- Fallback (sqlite read unavailable): opens the collection (returns early if `None`), counts total, and paginates metadata in batches of 5000 to tally wing→room, using `"?"` for missing wing/room (mempalace/miner.py:L1973-L1993).
- `_print_status(total, wing_rooms)` prints a header `MemPalace Status — N drawers`, then each wing (sorted) with its rooms sorted by drawer count descending (mempalace/miner.py:L1996-L2006).
