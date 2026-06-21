# Behavior Specification: Project File Miner (`tests/test_miner.py`)

This spec describes the externally-observable behavior of the project-file mining
subsystem as pinned by its test suite. The miner scans a project directory for
readable source files, chunks them into "drawers", and writes those drawers into a
persistent palace (a ChromaDB-on-disk store containing a collection named
`mempalace_drawers`). All claims cite the test that asserts the contract.

## Public Surface

The following functions/constants are part of the public contract exercised here:
`scan_project`, `mine`, `process_file`, `status`, `load_config`, `detect_room`,
`add_drawer`, `chunk_text`, `_build_drawer_metadata`, `_extract_content_date`,
`_extract_entities_for_metadata`, `_resolve_max_chunks_per_file`, plus the
constants `PHP_EXTENSIONS`, `READABLE_EXTENSIONS`, `SKIP_FILENAMES`,
`MAX_CHUNKS_PER_FILE`, `DRAWER_UPSERT_BATCH_SIZE`, `NORMALIZE_VERSION`, and the
palace helpers `file_already_mined` / `prefetch_mined_set`
(tests/test_miner.py:L12-L22).

## File Selection: `scan_project`

`scan_project(project_root, **kwargs)` returns the set of file paths to be mined.
Tests inspect it by taking each returned path relative to `project_root`,
rendering it with forward slashes, and sorting the result
(tests/test_miner.py:L30-L32).

### Readable extensions

The set `PHP_EXTENSIONS` must be a subset of `READABLE_EXTENSIONS`
(tests/test_miner.py:L35-L36). Every extension in `PHP_EXTENSIONS` is scanned, and
extension matching is case-insensitive: a file ending in `.PHP` is included exactly
as named alongside lowercase-extension files (tests/test_miner.py:L39-L50). Swift
files (`*.swift`) are included (tests/test_miner.py:L325-L332). Kotlin files
(`*.kt`) and Kotlin-script gradle files (`*.gradle.kts`) are included
(tests/test_miner.py:L335-L349).

### Generated-file exclusion

MemPalace-generated artifacts are skipped: a file named `entities.json` and the
`mempalace.yaml` config are not scanned, while genuine user content (e.g.
`notes.md`) is scanned (tests/test_miner.py:L315-L322).

### Lockfile exclusion

`SKIP_FILENAMES` contains at minimum `package-lock.json`, `pnpm-lock.yaml`, and
`yarn.lock`, so large dependency lockfiles are skipped
(tests/test_miner.py:L1397-L1405).

### gitignore handling

By default `scan_project` honors `.gitignore`. A top-level `.gitignore` listing a
filename and a directory excludes both the named file and everything under the
named directory (tests/test_miner.py:L352-L364). Nested `.gitignore` files apply to
their subtree: a `.gitignore` inside `subrepo/` excludes paths under `subrepo/` per
its rules, in addition to the parent rules (tests/test_miner.py:L367-L380). A
nested `.gitignore` may re-include (negate, via `!pattern`) a file the parent
ignored (tests/test_miner.py:L383-L395).

Negation works when the parent directory itself is still visible: `generated/*`
followed by `!generated/keep.py` keeps `generated/keep.py` while dropping
`generated/drop.py` (tests/test_miner.py:L398-L409). However, when the directory
itself is ignored (`generated/`), a later `!generated/keep.py` does NOT re-include
the file — an ignored directory is not re-descended, so the result is empty
(tests/test_miner.py:L412-L423).

`respect_gitignore=False` disables all gitignore processing so ignored directories
are scanned (tests/test_miner.py:L426-L436).

### include_ignored overrides

`include_ignored` is a list of override entries. It may name a directory to force
inclusion of an otherwise-ignored directory's files
(tests/test_miner.py:L439-L449), a specific ignored file path
(tests/test_miner.py:L452-L465), or an exact file with no known/recognized
extension such as `README` (tests/test_miner.py:L468-L478).

`scan_project` also applies an internal skip-dirs rule independent of gitignore
(e.g. `.pytest_cache`). An `include_ignored` override beats that skip-dirs rule and
forces inclusion (tests/test_miner.py:L481-L494); without the override, the
skip-dirs rule still applies even when `respect_gitignore=False`
(tests/test_miner.py:L497-L507).

### Symlink handling (observable contract)

Symlinked files are never mined; they are skipped and a notice is written to
**stderr**. For each skipped symlink, stderr contains exactly one `SKIP:` token
(formatted with a two-space indent `"  SKIP:"`), the file's path, and the marker
`(symlink)` (tests/test_miner.py:L514-L531). Dangling symlinks (target deleted) are
likewise skipped and logged, yielding an empty scan result
(tests/test_miner.py:L537-L552). The logged path is the full path relative to the
project root rendered with forward slashes (e.g. `deep/subdir/nested.md`), not just
the leaf filename, even for nested symlinks (tests/test_miner.py:L559-L575). These
tests are skipped on Windows because symlink creation requires elevated privileges
(tests/test_miner.py:L510-L513,L533-L536,L555-L558).

## Configuration: `load_config`

`load_config(project_root)` returns a dictionary that always contains keys `wing`
and `rooms` (tests/test_miner.py:L282-L286). When no `mempalace.yaml` exists, the
default `wing` is the project directory's basename passed through
`normalize_wing_name` (which strips leading/trailing separators)
(tests/test_miner.py:L278-L294). A hyphenated directory name like `my-cool-app`
normalizes to `my_cool_app`, so the fallback wing matches the slug used elsewhere
(tests/test_miner.py:L297-L312).

When `mempalace.yaml` is present, its `wing` and `rooms` (a list of
`{name, description}` and optionally `keywords`) drive mining
(tests/test_miner.py:L63-L73,L1242-L1243).

## Room Routing: `detect_room`

`detect_room(file_path, content, rooms, project_root)` returns the name of the room
a file belongs to. Routing uses token-boundary matching, not substring matching: a
keyword matches a path part only when it appears as a separator-bounded token.
`views/` must NOT route to a room keyed `interviews` (even though `views` is a
substring of `interviews`), while `data/interviews/...` does route to `interviews`
via the real token (tests/test_miner.py:L1089-L1114).

Matching is bidirectional: a path part containing the keyword as a token matches
(`frontend-app` → `frontend`), and a keyword containing the path part as a token
matches (`data/data-retention/` → `data-retention`)
(tests/test_miner.py:L1117-L1139). A path part may match a room via that room's
declared `keywords` even when the room's `name` differs (folder `docs/` → room
`documentation` whose keywords include `docs`) (tests/test_miner.py:L1142-L1158).

Filename matching (priority 2) also uses token boundaries: `reviewmodule.ts` does
NOT match room `review` (substring, not token) while `review-page.ts` does;
dotted filename stems are split on `.` so `foo.test.ts` matches room `foo`
(tests/test_miner.py:L1161-L1183).

## End-to-End Mining: `mine`

`mine(project_root, palace_path, dry_run=False, limit=0, max_chunks_per_file=...)`
scans the project, chunks files, and writes drawers into a ChromaDB collection
named `mempalace_drawers` at `palace_path`. After a successful non-dry-run mine,
the collection's drawer count is greater than zero (tests/test_miner.py:L53-L82).

### Post-mine derived analytics (best-effort, non-load-bearing)

After a non-dry-run mine completes, `mine` invokes three derived-analytic passes,
each keyed on the wing name from `mempalace.yaml`:

1. `compute_hallways_for_wing` is called exactly once, with the wing name and a
   live (non-null) collection so hallways can query drawers
   (tests/test_miner.py:L85-L138).
2. `_compute_entity_tunnels_for_wing` is called exactly once with the wing name
   (tests/test_miner.py:L184-L231).
3. Cross-wing topic tunnels are computed (see below).

Each derived pass is wrapped so a failure is caught/logged but never propagated:
if `compute_hallways_for_wing` raises, `mine` still completes and the drawer write
remains committed (tests/test_miner.py:L141-L181); if
`_compute_entity_tunnels_for_wing` raises, `mine` still completes and the drawers
remain committed (tests/test_miner.py:L234-L275).

### Topic tunnels

When two wings have previously-confirmed topics that overlap, mining the second
wing drops a cross-wing tunnel for each shared topic. With wings `wing_one`
(topics foo,bar) and `wing_two` (topics foo,baz), mining `wing_two` produces
exactly one tunnel whose `kind` is `"topic"`, whose source/target wings are
`{wing_one, wing_two}`, and whose source/target rooms are both the synthetic room
`topic:foo` (a `topic:<name>` namespace so they cannot collide with literal
folder-derived rooms) (tests/test_miner.py:L1212-L1256).

Tunnel creation is gated by `MEMPALACE_TOPIC_TUNNEL_MIN_COUNT`: setting it to `2`
when only one topic is shared suppresses all tunnels
(tests/test_miner.py:L1259-L1288). A wing in isolation (no other wing has confirmed
topics) creates no tunnels (tests/test_miner.py:L1291-L1316). Tunnels are listed
via `palace_graph.list_tunnels()`; entity registry and tunnel-file storage paths
are redirected to test temp dirs to avoid touching the real `~/.mempalace`
directory (tests/test_miner.py:L1220-L1227).

### Dry run

In dry-run mode, no drawers are committed but the run must not crash even when a
file falls below the minimum chunk size and yields zero drawers (room resolves to
None); the summary print handles a zero-drawer file without raising
(tests/test_miner.py:L771-L794).

### `--limit` semantics (#1535)

`limit` counts only NEW work (files that actually produce drawers / are not
already mined), not skipped files. With 10 files where the first 8 return zero
drawers (already mined) and the last 2 each return 3, `mine(..., limit=5)` still
visits all 10 files and reports `Drawers filed: 6`
(tests/test_miner.py:L2246-L2269). With N unmined files all producing drawers,
`limit=3` stops after exactly 3 files (tests/test_miner.py:L2272-L2293). `limit=0`
(the default) processes every file (tests/test_miner.py:L2296-L2317). Dry-run with
a limit still counts new files toward the limit and stops at the limit
(tests/test_miner.py:L2320-L2339). When a limit causes early exit, the summary
prints `Files processed: 2`, `Drawers filed: 6`, and a `(limit: 2 new)` annotation
(tests/test_miner.py:L2342-L2366).

### KeyboardInterrupt and exception handling

A `KeyboardInterrupt` raised mid-loop causes `mine` to print a summary and exit
with code **130**. The summary contains `Mine interrupted.`, `files_processed: 1/`,
`drawers_filed:`, `last_file:`, and `upserted idempotently`
(tests/test_miner.py:L1339-L1368). The resume hint in that summary shell-quotes the
project directory so a path containing spaces yields a copy-paste-safe
`mempalace mine <quoted-path>` command (tests/test_miner.py:L1370-L1394).

A non-KeyboardInterrupt exception (e.g. `RuntimeError`) raised mid-mine prints an
`Mine aborted by exception.` banner — including `files_processed: 1/`,
`drawers_filed:`, the exception class+message (`RuntimeError: ...`), and
`upserted idempotently` — and then re-raises, preserving the traceback and yielding
a non-zero exit (tests/test_miner.py:L1710-L1740).

### PID-file lifecycle

`mine` honors a per-target PID slot whose path is supplied via the
`MEMPALACE_MINE_PID_FILE` environment variable. On interrupt the slot is removed in
a finally clause if it holds the current process's PID
(tests/test_miner.py:L1743-L1768); on clean exit it is also removed
(tests/test_miner.py:L1771-L1786). A slot whose contents are a foreign PID (not the
current process) is left untouched and unchanged (tests/test_miner.py:L1789-L1806).

## Per-file Processing: `process_file`

`process_file(source, project_root, col, wing, rooms, agent, dry_run,
max_chunks_per_file=...)` returns a tuple `(drawers, room, skip_reason)`
(tests/test_miner.py:L1024-L1037). On success it returns the drawer count, the
resolved room name, and `skip_reason=None` (tests/test_miner.py:L1034-L1036).

### Bounded upsert batching

Drawers are written via `col.upsert(documents, ids, metadatas)` in batches of at
most `DRAWER_UPSERT_BATCH_SIZE`. With the batch size set to 2 and 5 chunks, the
upsert batch sizes are `[2, 2, 1]` and the returned drawer count is 5
(tests/test_miner.py:L999-L1037).

### Chunk cap (#1296 / #1455)

A file whose chunk count exceeds `MAX_CHUNKS_PER_FILE` is skipped: it returns
`(0, room, "chunk_cap")` and never calls `col.upsert`
(tests/test_miner.py:L1408-L1441). The skip notice goes to **stderr** (matching the
symlink-skip convention) and contains `[skip]`, the CLI hint
`--max-chunks-per-file`, and the env-var name `MEMPALACE_MAX_CHUNKS_PER_FILE`;
`[skip]` does NOT appear on stdout (tests/test_miner.py:L1442-L1448). The chunk-cap
skip is also tagged under dry-run, returning `(0, room, "chunk_cap")`
(tests/test_miner.py:L1624-L1652).

When `max_chunks_per_file=0` (sentinel for "no cap"), even a pathologically large
chunk count (20 chunks against a cap of 5) is fully processed: returns 20 drawers,
`skip_reason=None`, and calls upsert (tests/test_miner.py:L1509-L1544).

### Chunk-cap resolution: `_resolve_max_chunks_per_file(override)`

The cap is resolved at call time with this precedence: explicit override > env var
`MEMPALACE_MAX_CHUNKS_PER_FILE` > module default `MAX_CHUNKS_PER_FILE`
(tests/test_miner.py:L1451-L1472). A sentinel `0` from either override or env means
"no cap" and is returned as `0` (tests/test_miner.py:L1475-L1483). An invalid
(non-integer) env value (e.g. `banana`) warns to stderr — the message includes both
the env-var name and the offending value — and falls back to the module default
(tests/test_miner.py:L1486-L1495). A negative env value (e.g. `-5`) likewise warns
and falls back (tests/test_miner.py:L1498-L1506); a negative override (e.g. `-5`)
warns with `--max-chunks-per-file` plus the value and falls back, symmetric with
the env path (tests/test_miner.py:L1599-L1610). The resolver reads
`MAX_CHUNKS_PER_FILE` lazily at call time, so a value changed after import is
honored (tests/test_miner.py:L1613-L1621).

### Mine summary lines for chunk-cap skips

`mine` separates ordinary skips from chunk-cap skips. When at least one file hits
the cap, the summary prints `Files skipped (already filed or other): 1`,
`Files skipped (chunk cap...`, and the hint `--max-chunks-per-file`
(tests/test_miner.py:L1547-L1575). When no file hits the cap, the chunk-cap line is
omitted entirely and `Files skipped (already filed or other): 0` is shown with no
occurrence of `chunk cap` (tests/test_miner.py:L1578-L1596). Under dry-run the same
split applies, with the chunk-cap count line including `1 (raise via`
(tests/test_miner.py:L1655-L1684).

### `max_chunks_per_file` plumbing

`mine(max_chunks_per_file=0)` reaches `process_file` as the keyword argument
`max_chunks_per_file=0`, wiring the sentinel-disable path end-to-end
(tests/test_miner.py:L1687-L1707).

## Drawer Records: `add_drawer` and metadata

`add_drawer(collection, wing, room, content, source_file, chunk_index, agent)`
returns `True` when a drawer is added and stamps the stored metadata with
`normalize_version == NORMALIZE_VERSION` (tests/test_miner.py:L1186-L1209).

`_build_drawer_metadata(...)` includes optional `line_start` / `line_end` keys only
when those values are provided; when omitted, neither key appears in the metadata
(tests/test_miner.py:L1864-L1902).

## Chunk Line Ranges: `chunk_text`

`chunk_text(content, source_file, chunk_size, chunk_overlap, min_chunk_size=...)`
returns a list of chunk dicts. Each chunk carries integer `line_start` and
`line_end` keys, both 1-indexed, with `line_start >= 1` and
`line_end >= line_start` (tests/test_miner.py:L1824-L1834). The first chunk starts
at `line_start == 1` (tests/test_miner.py:L1836-L1841). The last chunk's `line_end`
covers (reaches at least) the final line of the source
(tests/test_miner.py:L1843-L1852). For input smaller than `chunk_size`, a single
chunk is produced spanning all lines (`line_start == 1`, `line_end == line_count`)
(tests/test_miner.py:L1854-L1861).

## Entity Tagging: `_extract_entities_for_metadata`

`_extract_entities_for_metadata(content)` returns a `;`-separated string of matched
entity names (or empty). It finds non-Latin (e.g. Cyrillic) names when the
configured `entity_languages` includes the relevant locale: with languages
`("en","ru")`, the Cyrillic name `Михаил` is found in Russian content
(tests/test_miner.py:L578-L592). Matching against known-entity names is
case-insensitive: seeded names `Aya`/`Lumi` match lowercase mentions `aya`/`lumi`
and mixed-case mentions like `AYA`, returning the canonical seeded casing
(tests/test_miner.py:L595-L626).

## Content-Date Extraction: `_extract_content_date`

`_extract_content_date(source_file, content)` returns an ISO `YYYY-MM-DD` string or
`None`. Priority order (first match wins): filename pattern, YAML frontmatter, then
content body (first ~10 lines), then filesystem mtime, then `None`
(tests/test_miner.py:L1911-L1922).

Filename patterns recognized: ISO `2024-11-08.md` → `2024-11-08`
(tests/test_miner.py:L1924-L1929); natural language with ordinal
`April-6th-2011-notes.md` → `2011-04-06` (tests/test_miner.py:L1931-L1936); compact
dash `Nov-8-2024.md` → `2024-11-08` (tests/test_miner.py:L1938-L1943).

Frontmatter: a `date`, `created`, or `published` field is read (`date: 2024-11-08`
→ `2024-11-08`; `created: 2023-07-15` → `2023-07-15`)
(tests/test_miner.py:L1945-L1961).

Content body: a Claude session preamble (`Session resumed from compact on
2024-11-08`) (tests/test_miner.py:L1963-L1969), an ISO date in the first line
(tests/test_miner.py:L1971-L1977), and natural-language dates (`November 8, 2024`)
(tests/test_miner.py:L1979-L1985) are all extracted.

Ambiguous slash-separated dates auto-disambiguate per file: if any date in the file
has a day component over 12 (impossible as a month), the file locale locks to
DD/MM, so `04/11/22` (with sibling `25/03/21`) → `2022-11-04`
(tests/test_miner.py:L1987-L1996). With no disambiguator, the default is US MM/DD,
so `04/11/22` → `2022-04-11` (tests/test_miner.py:L1998-L2005).

Priority enforcement: a filename date beats a content-body date
(`2020-01-01.md` containing `2024-11-08` → `2020-01-01`)
(tests/test_miner.py:L2007-L2014); frontmatter beats content body
(tests/test_miner.py:L2016-L2024). When filename/frontmatter/content yield nothing,
fall back to filesystem mtime (mtime `1689422400.0` → `2023-07-15`)
(tests/test_miner.py:L2026-L2038). When nothing is extractable and the file does
not exist, return `None` (tests/test_miner.py:L2040-L2046); empty content with a
missing file also returns `None` (tests/test_miner.py:L2048-L2052).

### No-hallucination guarantees

Inputs that previously produced fabricated dates must now return `None`: junk
filenames with trailing digits (`tmp_random_file_5`)
(tests/test_miner.py:L2061-L2068), `untitled-1` (tests/test_miner.py:L2070-L2075),
year-only filenames (`notes.2024.md`) (tests/test_miner.py:L2077-L2087),
year+month-only filenames (`2024-06.md`) (tests/test_miner.py:L2089-L2100). A
filename must carry a complete year+month+day OR a recognizable month-name token to
be accepted. Content with issue numbers (`issue 42 in module 7`)
(tests/test_miner.py:L2102-L2110), counts (`1000 drawers`)
(tests/test_miner.py:L2112-L2120), and version numbers (`Version 3.3.6`)
(tests/test_miner.py:L2122-L2128) must all return `None`.

### Two-digit year boundary

The two-digit-year convention is `70-99 → 19xx`, `00-69 → 20xx`. Pinned cases (all
forced to DD/MM by a day > 12): `25/03/69` → `2069-03-25`
(tests/test_miner.py:L2133-L2140); `25/03/70` → `1970-03-25`
(tests/test_miner.py:L2142-L2148); `25/12/99` → `1999-12-25`
(tests/test_miner.py:L2150-L2156); `25/01/00` → `2000-01-25`
(tests/test_miner.py:L2158-L2164).

## Already-Mined Detection: `file_already_mined` / `prefetch_mined_set`

`file_already_mined(collection, source_file, check_mtime=False,
extract_mode=None)` returns whether a source file is already represented in the
palace. Drawer metadata used by this check includes `source_file`, `source_mtime`
(stored as a string), `extract_mode`, and `normalize_version`
(tests/test_miner.py:L650-L660).

Without `check_mtime`, a file with any matching drawer returns `True`; with
`check_mtime=True`, it returns `True` only if the stored `source_mtime` equals the
file's current mtime (tests/test_miner.py:L646-L675). A record with no stored mtime
returns `False` under `check_mtime` (tests/test_miner.py:L677-L688). After the
file's mtime changes, the no-mtime check still returns `True` but the mtime check
returns `False` (re-mine needed) (tests/test_miner.py:L667-L675).

`extract_mode` scopes the check: a drawer written with `extract_mode="exchange"`
makes the file count as mined for `exchange` but NOT for `general`, and vice versa
once a `general` drawer is added. `prefetch_mined_set(col, extract_mode=...)`
returns the set of source files mined under that mode and is scoped the same way
(tests/test_miner.py:L695-L739). The extract-mode path paginates across large
sources: with 1000 `exchange` rows plus 1 `general` row under one source file, the
`general` query still returns `True` via paginated `get(where, limit, offset)`
iteration (tests/test_miner.py:L742-L768).

`normalize_version` acts as a schema gate: drawers with no `normalize_version`
field, or an older integer version, do NOT short-circuit (return `False`); only the
current `NORMALIZE_VERSION` returns `True` (tests/test_miner.py:L1047-L1083). This
forces silent rebuild of pre-version drawers on the next mine.

Under the additive-mining model a single `source_file` may have multiple
`parent_drawer_id` groups, each with its own stored `source_mtime`.
`file_already_mined(check_mtime=True)` must return `True` if ANY group's stored
mtime matches the current file mtime, regardless of which group a `limit=1` query
returns first; a correct implementation iterates all groups via paginated
`get(where, limit, offset)` rather than trusting `limit=1` ordering
(tests/test_miner.py:L2167-L2240). The `get` contract used here accepts keyword
arguments `where`, `limit`, `offset`, and `include`, returning a dict with `ids`
and `metadatas` lists (tests/test_miner.py:L761-L766,L2223-L2228).

## Status Reporting: `status`

`status(palace_path)` reports palace state on **stdout** without ever destroying or
lazily creating data, and across multiple distinct states.

- Missing palace directory: prints `No palace found` and does NOT create the
  directory as a side effect (tests/test_miner.py:L797-L804).
- Directory exists but `chroma.sqlite3` is absent (State B): prints
  `has no chroma.sqlite3 yet` and must short-circuit before invoking ChromaDB —
  the directory remains empty (no DB file is lazily created)
  (tests/test_miner.py:L827-L839).
- Directory + `chroma.sqlite3` exist but no drawers mined (State C): prints
  `initialized but empty` and suggests `mempalace mine`, NOT `No palace found`
  (tests/test_miner.py:L807-L824).

A healthy `status` must NOT cold-load the vector index (opening the collection is
~60s of CPU on large palaces); counts come from `chroma.sqlite3` directly. If the
happy path is rerouted through `_open_collection_or_explain`, a sentinel fires. A
healthy status prints `MemPalace Status — 4 drawers` and per-wing lines like
`WING: project` / `WING: notes` (tests/test_miner.py:L872-L893).

`status` tolerates `None` entries in the metadata list returned by `col.get`: a
`None`-metadata row is counted under a `?`/`?` fallback, so both `WING: ?` and the
real `WING: proj` appear without crashing (tests/test_miner.py:L842-L870). The
`col.get` return shape includes `ids`, `documents`, and `metadatas` lists
(tests/test_miner.py:L855-L860).

When the sqlite fast path returns `None` (exotic schema / read error), `status`
falls back to the ChromaDB client path and still reports the correct tally
(`MemPalace Status — 4 drawers`, `WING: project`) rather than crashing or printing
nothing (tests/test_miner.py:L915-L927).

## SQLite Tally Fast Path: `_sqlite_wing_room_counts`

`_sqlite_wing_room_counts(palace_path, collection_name)` returns either `None` or a
tuple `(total, wing_rooms)` where `wing_rooms` maps each wing to a map of room →
count. For a seeded palace of 4 drawers (2 project/backend, 1 project/frontend,
1 notes/planning) it returns `total == 4` and exactly that structure, with no
double counting from the double metadata join
(tests/test_miner.py:L895-L912).

It returns `None` when the DB exists but the drawers collection was never
bootstrapped (so `status` routes to the "initialized but empty" message rather than
a misleading `0 drawers` tally) (tests/test_miner.py:L929-L938). A numerically
stored wing/room (e.g. `wing=2026`, `room=7`) is tallied under its stringified
value (`{"2026": {"7": 1}}`), matching the ChromaDB path, not bucketed under `?`
(tests/test_miner.py:L941-L957). A drawer with a wing but no room (or vice versa)
is counted under `?` for the missing axis and never dropped — `{wing: "alpha"}` and
`{room: "beta"}` together yield `total == 2` with `{"alpha": {"?": 1}, "?":
{"beta": 1}}`, proving outer (not inner) joins (tests/test_miner.py:L960-L979). A
sustained sqlite lock (read raising `OperationalError`) degrades to `None` so
`status` falls back to the ChromaDB path rather than raising
(tests/test_miner.py:L982-L996).

## On-Disk / Observable Contracts Summary

- The palace is a directory containing `chroma.sqlite3` and a ChromaDB collection
  named `mempalace_drawers` (tests/test_miner.py:L78-L80,L816-L817).
- Drawer metadata keys observed: `source_file`, `source_mtime` (string),
  `extract_mode`, `normalize_version`, `wing`, `room`, `parent_drawer_id`,
  `chunk_index`, and optional `line_start` / `line_end`
  (tests/test_miner.py:L653-L660,L711-L715,L859-L860,L1886-L1887,L2201-L2214).
- Skip/symlink/chunk-cap notices go to stderr; status and mine summaries go to
  stdout (tests/test_miner.py:L526-L530,L1445-L1448,L802-L803,L1362-L1367).
- Exit code 130 on KeyboardInterrupt; non-zero (re-raise) on other exceptions
  (tests/test_miner.py:L1361,L1710-L1733).
- Tunnels are persisted to a `tunnels.json` file under `~/.mempalace` and listed
  via `palace_graph.list_tunnels()`, each tunnel carrying `source`, `target`,
  and `kind` fields with `wing`/`room` sub-keys
  (tests/test_miner.py:L1225-L1226,L1248-L1256).
