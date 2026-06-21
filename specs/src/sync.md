# Spec: `sync.py` — Gitignore-aware drawer prune

## Purpose

Removes ("prunes") stored drawers whose underlying source files are now gitignored, deleted, or moved out of the project. It reuses the same gitignore-matching rules used during ingest, so files that would be blocked from ingest are also cleaned up after the fact (mempalace/sync.py:L1-L12).

## Public Surface

The module exports exactly three names: `MineAlreadyRunning` (an exception type re-exported from the palace module), `SyncReport` (a result record type), and `sync_palace` (the single public function) (mempalace/sync.py:L317-L321).

### `SyncReport` record

A `SyncReport` is a structured record with the following fields and types (mempalace/sync.py:L32-L42):
- `scanned`: integer — total drawers examined.
- `kept`: integer — drawers retained.
- `gitignored`: integer — drawers whose source is gitignored.
- `missing`: integer — drawers whose source file no longer exists on disk.
- `no_source`: integer — drawers with no usable source-file metadata.
- `out_of_scope`: integer — drawers whose source lives outside all known project roots.
- `removed_drawers`: integer — count of drawer entries actually deleted.
- `removed_closets`: integer — count of closet entries actually deleted.
- `dry_run`: boolean — whether this run was a dry run.
- `by_source`: mapping from source-file path string to integer count of removable drawers attributed to that file.

## `sync_palace` — Behavior Contract

### Signature / Inputs

`sync_palace(palace_path, project_dirs=None, wing=None, dry_run=True, batch_size=1000, wal_log=None)` returns a `SyncReport` (mempalace/sync.py:L203-L210). The default batch size constant is 1000 (mempalace/sync.py:L29).

- `palace_path`: string path to the palace store.
- `project_dirs`: optional list of project root directory paths, or `None` to auto-detect roots from drawer metadata.
- `wing`: optional wing name to restrict the scan to one wing.
- `dry_run`: boolean; defaults to true (classify only, delete nothing).
- `batch_size`: integer batch size for deletes.
- `wal_log`: optional callback invoked once per delete batch (see Side Effects).

### Input Validation (precondition errors)

On an apply run (`dry_run=False`), at least one of `wing` or `project_dirs` must be provided; otherwise a value error is raised. This guard prevents accidentally pruning every wing of a multi-project palace via auto-detected roots (mempalace/sync.py:L224-L229).

If `project_dirs` is provided but is an empty list, a value error is raised; the caller must pass at least one root, or pass `None` to enable auto-detection (mempalace/sync.py:L230-L234).

### Concurrency / Locking Invariant

The entire operation (classification pass and any deletion) runs while holding the palace mine lock for `palace_path`, so the classify pass and the apply branch observe the same drawer snapshot. If another mine is already running on this palace, a `MineAlreadyRunning` error is raised (mempalace/sync.py:L218-L219, L248). The drawers collection is opened without creating it (`create=False`) (mempalace/sync.py:L249).

### Project Root Resolution

If `project_dirs` is given, each entry is resolved to an absolute path and the list is sorted so the deepest (longest path string) root comes first; ties broken by string order. This ordering guarantees the deepest matching prefix wins on first match (mempalace/sync.py:L181-L184, L251-L252).

If `project_dirs` is `None`, project roots are auto-detected by walking drawer metadata once: for each distinct `source_file` (deduplicated by string), the candidate root is the deepest ancestor directory of that file that contains a `.git` directory or a `.gitignore` file. Ancestors are inspected deepest-first, so the first hit is the deepest. Detected roots are returned sorted deepest-first (longest path string first, ties by string order) (mempalace/sync.py:L153-L178, L253-L254). Source files whose path is not absolute are skipped during auto-detection (mempalace/sync.py:L172-L173).

### Drawer Enumeration & Ordering

Drawers are read from the drawers collection in batches of 1000, paginated by offset, including only metadata. When a `wing` is supplied, enumeration is filtered to that wing only. Enumeration stops when a batch returns no IDs or returns fewer than the batch size (mempalace/sync.py:L133-L150, L138-L141). Missing IDs or metadata lists are treated as empty (mempalace/sync.py:L142-L143).

### Per-Drawer Classification

Every drawer is assigned exactly one bucket from: `kept`, `gitignored`, `missing`, `no_source`, `out_of_scope` (mempalace/sync.py:L99-L130). The classification order is:

1. Registry sentinels are always classified `kept` (never pruned). A drawer is a registry sentinel if its metadata `room` field equals `_registry`, OR its metadata `ingest_mode` field equals `registry`, OR its drawer ID begins with `_reg_`. Preserving these prevents the next mine pass from re-chunking and re-embedding an unchanged transcript (mempalace/sync.py:L84-L96, L106-L108).
2. If there is no `source_file` metadata value, the bucket is `no_source` (mempalace/sync.py:L110-L112).
3. If the `source_file` path is not absolute, the bucket is `no_source` (mempalace/sync.py:L114-L116).
4. The source path is resolved (non-strict). If it lives under no known project root, the bucket is `out_of_scope` (mempalace/sync.py:L117-L121).
5. If the resolved source path does not exist on disk, the bucket is `missing` (mempalace/sync.py:L123-L124).
6. If the source path matches the gitignore rules of its ancestor chain (root down to the file's parent directory), the bucket is `gitignored` (mempalace/sync.py:L126-L128).
7. Otherwise the bucket is `kept` (mempalace/sync.py:L130).

A source file is considered to live "under" a project root if it is relative to that root; the first (deepest, due to sort order) matching root is chosen (mempalace/sync.py:L45-L57).

Gitignore matching uses the chain of gitignore matchers from the project root down through each intermediate directory to the file's parent; matchers that fail to load for a directory are skipped (mempalace/sync.py:L60-L81). Files are matched as files (not directories) (mempalace/sync.py:L127).

### Classification Caching Invariant

Within a single call, the verdict for a given `source_file` is computed once and cached; subsequent drawers with the same `source_file` reuse the cached bucket. This is sound because the mine lock blocks concurrent writers and the loop is synchronous (mempalace/sync.py:L256-L273). Registry sentinels bypass the cache and are always re-checked as `kept` (mempalace/sync.py:L266-L267).

### Removable Set

Only drawers classified `gitignored` or `missing` are marked removable. Their IDs are collected, and for each such drawer that has a `source_file`, that source is added to the removable-sources set and its `by_source` count is incremented (mempalace/sync.py:L276-L280).

### Report Construction & Dry-Run Behavior

The report is assembled from the bucket counts plus `removed_drawers=0`, `removed_closets=0`, the `dry_run` flag, and the `by_source` map (mempalace/sync.py:L282-L288).

If `dry_run` is true, OR if there are no removable drawers, the function returns the report immediately without deleting anything (mempalace/sync.py:L290-L291).

### Apply (deletion) Behavior

On apply, removable drawer IDs are deleted from the drawers collection in chunks of `batch_size`. `removed_drawers` is set to the total number of IDs deleted (mempalace/sync.py:L187-L200, L293).

After drawer deletion, the closets collection is opened (without creating). If it is unavailable, closet purge is skipped with a warning and `removed_closets` remains 0 (mempalace/sync.py:L295-L301, L313). If the closets collection is available and there are removable sources, all closet entries whose `source_file` is one of the removable sources are deleted, and `removed_closets` is set to the number deleted (mempalace/sync.py:L302-L313).

The fully populated report is returned after the lock-held block completes (mempalace/sync.py:L314).

## Side Effects

- Reads the drawers and closets collections of the palace store (mempalace/sync.py:L249, L297).
- On apply, deletes drawer entries and closet entries (mempalace/sync.py:L292-L312).
- Probes the local filesystem for existence of source files and for `.git`/`.gitignore` markers during root detection and classification (mempalace/sync.py:L123, L175).
- Optional WAL logging: when `wal_log` is supplied, it is called once per delete batch with the operation name `sync_prune`, a payload containing `first_id` (the first drawer ID in the batch), and a result map containing `removed_count` (the number of IDs in that batch) (mempalace/sync.py:L194-L199).
- Emits a warning log when the closets collection cannot be opened (mempalace/sync.py:L299).

## Edge Cases

- Empty palace / no matching drawers: scanned count is 0 or all buckets resolve without removable entries; nothing is deleted (mempalace/sync.py:L290-L291).
- Metadata that is null/empty is treated as an empty mapping for field lookups (mempalace/sync.py:L263, L84-L96, L110).
- A 200-chunk file costs a single filesystem walk during auto-detection due to source-string deduplication (mempalace/sync.py:L161-L170).
