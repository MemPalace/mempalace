# Behavior Spec: `sync` (gitignore-aware drawer prune)

Derived from the test suite `tests/test_sync.py`, which exercises the public
surface of the `sync` module, its MCP tool wrapper, its CLI command, and the
daemon report renderer. Every claim cites the test that pins the behavior.

## Scope and purpose

`sync` reconciles a stored "palace" (a vector store of memory "drawers") against
the current on-disk reality of one or more project directories, removing drawers
whose source files have become gitignored or deleted, while preserving drawers
that are out of scope, source-less, or registry sentinels. It is the
implementation of gitignore-aware drawer prune (issue #1252)
(tests/test_sync.py:L1-L6).

## Storage model and on-disk contract

The palace is a persistent vector store at a filesystem `palace_path` containing
at least two collections: `mempalace_drawers` and `mempalace_closets`, each
created with cosine vector space (`hnsw:space: cosine`)
(tests/test_sync.py:L17-L18, tests/test_sync.py:L114-L117,
tests/test_sync.py:L373-L376). A drawer record carries an id, a document text,
an embedding vector, and a metadata map. The metadata fields used for
classification are: `wing`, `room`, `source_file` (an absolute filesystem path),
`chunk_index`, `added_by`, `filed_at`, and optionally `ingest_mode`
(tests/test_sync.py:L20-L69, tests/test_sync.py:L487-L495). A drawer may have no
`source_file` key at all (convo / explicit-add drawers)
(tests/test_sync.py:L54-L60).

## Public surface

- `sync_palace(...)` — core reconciliation, returns a report map
  (tests/test_sync.py:L126-L132).
- `_auto_detect_project_roots(collection, wing=...)` — derives project roots
  from drawer metadata (tests/test_sync.py:L531-L563).
- `_normalize_project_dirs([...])` — normalizes and orders project dirs
  (tests/test_sync.py:L1160-L1162).
- `_classify_drawer(...)` — per-drawer bucket classification
  (tests/test_sync.py:L992-L998).
- `_resolve_project_root(...)`, `_delete_in_batches(...)` — internal helpers
  referenced by behavior (tests/test_sync.py:L799-L801,
  tests/test_sync.py:L681-L683).
- `get_closets_collection(palace_path, create=...)` — opens the closets
  collection (tests/test_sync.py:L1053-L1059).
- MCP tool `mcp_server.tool_sync(...)` (tests/test_sync.py:L1172-L1173).
- CLI command `sync` via `cli.main()` (tests/test_sync.py:L1298-L1299).
- Daemon entry `service.run_sync(opts)` (tests/test_sync.py:L1402-L1455).

## `sync_palace` inputs

Keyword inputs observed: `palace_path` (path to the store), `project_dirs` (list
of directory paths that scope which drawers are in scope), `wing` (optional wing
filter), `dry_run` (bool), `batch_size` (int, delete chunk size), and `wal_log`
(an optional audit callback) (tests/test_sync.py:L128-L132,
tests/test_sync.py:L229-L234, tests/test_sync.py:L721-L728).

## `sync_palace` output: the report map

The report is a map whose key set is exactly:
`{scanned, kept, gitignored, missing, no_source, out_of_scope,
removed_drawers, removed_closets, dry_run, by_source}` — no more, no fewer; this
schema must never silently drop a field (tests/test_sync.py:L667-L679). The
count fields are integers; `dry_run` is a bool; `by_source` maps a source-file
path to the number of drawers attributed to it
(tests/test_sync.py:L1436-L1437, tests/test_sync.py:L1071-L1076).

## Classification buckets

Each scanned drawer is sorted into exactly one bucket. The canonical fixture has
6 drawers producing this distribution: `scanned=6`, `gitignored=2` (a file under
a gitignored `build/` directory and an `*.log` file), `missing=1` (source file
no longer exists on disk), `no_source=1` (no `source_file` key), `out_of_scope=1`
(source file outside all project dirs), and `kept=1` (a live, non-ignored,
in-scope file) (tests/test_sync.py:L133-L138). The buckets sum to `scanned`.

## Dry-run semantics

When `dry_run=True`, the report reports `dry_run: true` and `removed_drawers: 0`,
and the underlying collection is not mutated — the full set of drawer ids is
identical before and after the call (tests/test_sync.py:L139-L147,
tests/test_sync.py:L171-L189). A dry run with no scope (no wing, no project dirs)
is always allowed because preview is read-only (tests/test_sync.py:L857-L859).

## Apply semantics

When `dry_run=False`, drawers in the `gitignored` and `missing` buckets are
deleted; `removed_drawers` equals their combined count (2 gitignored + 1 missing
= 3 in the fixture) (tests/test_sync.py:L152-L158). The survivors are exactly the
`kept`, `no_source`, and `out_of_scope` drawers
(tests/test_sync.py:L160-L169). A `no_source` drawer is preserved on apply
(tests/test_sync.py:L242-L254) and an `out_of_scope` drawer is preserved on apply
(tests/test_sync.py:L256-L268). Apply is idempotent: a second apply on the same
palace removes nothing and reports `gitignored=0`, `missing=0`,
`removed_drawers=0` (tests/test_sync.py:L733-L751).

## Wing scoping

When `wing` is provided, only drawers whose `wing` metadata matches are eligible
for pruning. A drawer in a different wing survives even when its source file would
otherwise be gitignored (tests/test_sync.py:L191-L240).

## Gitignore evaluation rules

Gitignore matching honors standard `.gitignore` semantics relative to each
project root:

- Directory patterns (`build/`) and glob patterns (`*.log`) both ignore matching
  files (tests/test_sync.py:L94-L95, tests/test_sync.py:L133-L134).
- Negation rules un-ignore a specific file: with `build/` plus `!build/keep.py`,
  `build/keep.py` survives while `build/doomed.py` is removed
  (tests/test_sync.py:L270-L321).
- Nested `.gitignore` files apply: a subdirectory `.gitignore` can deny files
  that the root `.gitignore` permits (tests/test_sync.py:L323-L366).

## Project-root resolution

Source files are matched to a project root by longest-prefix (deepest) matching.
When project dirs are nested (`outer` and `outer/inner` both passed), the deeper
root wins, so the deeper directory's `.gitignore` rules govern
(tests/test_sync.py:L799-L842). For auto-detected roots, when multiple ancestor
directories carry markers, the deepest one wins exclusively; the outer ancestor
must not appear in the resolved roots (tests/test_sync.py:L525-L572). Roots are
resolved through path resolution (symlink following) so that a `source_file`
written through a symlinked directory still resolves to the same real root —
resolution is symmetric on both the project-dir side and the source-file side;
without this the drawer would be mis-bucketed as `out_of_scope`
(tests/test_sync.py:L903-L952).

## `_normalize_project_dirs` ordering

Project dirs are sorted by `(-length, string)`: deepest (longest path) first,
and equal-length paths sorted alphabetically for determinism independent of input
order (tests/test_sync.py:L1155-L1169).

## Registry sentinel protection (`_reg_*`)

Drawers that are convo-miner registry sentinels must survive apply even when
their source transcript is gitignored, because deleting them would force a full
re-mine and re-embed. A drawer is treated as a registry sentinel if any of:
its `room` metadata is `_registry`; its `ingest_mode` metadata is `registry`; or
its id begins with the `_reg_` prefix (tests/test_sync.py:L451-L523). The
registry check runs before any per-file classification cache lookup: a regular
(non-registry) drawer sharing the same `source_file` and cached as `gitignored`
must not poison a later `_reg_*` sentinel into deletion
(tests/test_sync.py:L1088-L1153).

## Relative source paths

A drawer whose `source_file` metadata is a relative path is treated as upstream
corruption. `sync` does not attempt to resolve it; it routes the drawer to the
`no_source` bucket and leaves it in place (`removed_drawers=0`)
(tests/test_sync.py:L753-L797).

## Closet purge

On apply, closet records (`mempalace_closets`) that point at a removed source
file are also deleted; `removed_closets` is at least the count of removed closets
whose `source_file` intersects the removable source set
(tests/test_sync.py:L368-L409, tests/test_sync.py:L1071-L1079). The closet purge
is batched: a single `get` and a single `delete` call covers all removable source
files at once, not one call per file (tests/test_sync.py:L1012-L1086). If the
closets collection cannot be opened (the closets accessor raises), the failure is
non-fatal: apply continues and a warning containing "Closet purge skipped" is
logged under the `mempalace.sync` logger (tests/test_sync.py:L591-L613).

## Batched deletion and WAL audit

Deletions are performed in chunks of `batch_size`. With 5 removable ids and
`batch_size=2`, deletion proceeds in chunks of 2, 2, 1, producing three separate
delete operations, and `removed_drawers` totals 5
(tests/test_sync.py:L681-L731).

When a `wal_log` callback is provided, apply emits at least one audit entry with
operation name `sync_prune`. The callback signature is
`(operation, params, result)`. The `result` payload is non-null and carries a
`removed_count` of at least 1 (or the per-chunk size when batching). The `params`
map is restricted to an allow-list: its keys must be a subset of `{first_id}` —
no `source_file`, content, or id lists may leak into the audit record
(tests/test_sync.py:L422-L449, tests/test_sync.py:L717-L731).

## Error and guard behavior of `sync_palace`

- Apply with `project_dirs=[]` (an empty list) raises a value error whose message
  contains "empty"; it must not silently classify everything as out_of_scope
  (tests/test_sync.py:L574-L589).
- Apply with both `wing=None` and `project_dirs=None` raises a value error whose
  message contains "explicit wing="; the guard fires before any work
  (tests/test_sync.py:L844-L856).
- An empty palace (drawers collection exists but has no rows) yields
  `scanned=0` and `removed_drawers=0` (tests/test_sync.py:L411-L420).

## Concurrency lock (POSIX)

`sync_palace` acquires the palace-wide mine lock for the duration of the call.
The lock file lives at `~/.mempalace/locks/mine_palace_<key>.lock`, where `<key>`
is the first 16 hex chars of the SHA-256 of the case-normalized realpath of the
expanded `palace_path`. If another process holds this lock, `sync_palace` raises
`MineAlreadyRunning` rather than running against a partial snapshot; once the lock
is released the call succeeds (tests/test_sync.py:L861-L901).

## Per-file classification cache

Multiple drawers (chunks) sharing the same `source_file` are classified once, not
once per chunk: 5 chunks of one source cost a single `_classify_drawer`
invocation (4 cache hits) while still reporting `scanned=5` and `gitignored=5`
(tests/test_sync.py:L954-L1010).

## MCP tool `tool_sync`

`tool_sync(project_dir=..., wing=..., apply=...)` is the MCP entry point. The
default is dry run: with no `apply` flag the returned report has `dry_run: true`
(tests/test_sync.py:L1181-L1190). On the dry-run success path it returns
`success: true` alongside `dry_run: true` for symmetry with error branches
(tests/test_sync.py:L1192-L1204). With `apply=True` it is destructive, returning
`dry_run: false` and `removed_drawers >= 1` (tests/test_sync.py:L1206-L1224).

`tool_sync` always returns a structured result, never raising to the client:

- When no palace is configured it returns `{success: false, error: ...}` rather
  than a legacy `{error, hint}` shape (tests/test_sync.py:L1226-L1241).
- `apply=True` with no `project_dir` and no `wing` returns
  `{success: false, error: ...}` whose error mentions `wing=` or `project_dirs`
  (tests/test_sync.py:L1243-L1257).
- Under lock contention with `apply=True`, it returns
  `{success: false, error: ...}` whose error text (lowercased) contains
  "another mine" (tests/test_sync.py:L1259-L1295).
- If the underlying `sync_palace` raises mid-apply, `tool_sync` catches it and
  returns `{success: false, error: ...}` whose error contains the original
  message; as a side effect it clears the module-level `_metadata_cache` (sets it
  to null) via a try/finally even on the failure path
  (tests/test_sync.py:L615-L656).

## CLI command `sync`

Invoked as `mempalace --palace <path> sync <project_dir> [--apply] [--wing <w>]`
(tests/test_sync.py:L1304-L1312).

- Default (no `--apply`) is dry run: stdout contains "DRY RUN" and
  "would remove", and the collection is not mutated (all 6 fixture drawers remain)
  (tests/test_sync.py:L1314-L1322).
- With `--apply --wing demo`: stdout contains "Removed" and "(removed)", and the
  survivors are exactly `{drawer_keep, drawer_no_source, drawer_out_of_scope}`
  (tests/test_sync.py:L1340-L1353).
- CLI `--apply` wires the WAL logger so deletes emit `sync_prune` audit entries
  (tests/test_sync.py:L1355-L1383).
- `--apply` with no scope (no project dir, no wing) exits with process exit code
  2 (tests/test_sync.py:L1385-L1399).

## Daemon `service.run_sync`

`service.run_sync(opts)` accepts a map with `palace_path`, `dir`, and `dry_run`,
and returns `{success: true, ...}` while printing a full report to stdout
(tests/test_sync.py:L1441-L1456). The rendered report must include all report
fields — in particular it prints `No source:`, `Out of scope:`, a "Top sources to
remove" block listing `by_source` entries as `<path>  (<count>)`, and a
"Re-run with --apply" hint when there is something to remove. It must never print
a `Deleted:` line (the old code read a non-existent `deleted` key)
(tests/test_sync.py:L1457-L1467). On apply mode (`dry_run: false`) it prints a
removed-counts line of the form "Removed N drawers, M closets" and a "Top sources
removed" block, and does not print the "Re-run with --apply" hint
(tests/test_sync.py:L1469-L1489).

## Platform notes (observable contract only)

The fcntl/symlink-based concurrency and resolution tests are POSIX-only and are
skipped on Windows, reflecting that the lock and symlink-resolution contracts are
guaranteed on POSIX platforms (tests/test_sync.py:L861-L862,
tests/test_sync.py:L903-L903, tests/test_sync.py:L1259-L1259).
