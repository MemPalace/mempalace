# Behavior Spec: `tests/test_migrate.py`

This is the test suite that pins the **destructive-operation safety contract** of the
migration component (`mempalace.migrate`). The behaviors below are the externally
observable guarantees the migration code MUST satisfy; each is derived from an assertion
in the test file (tests/test_migrate.py:L1-L1).

The component under test exposes four entry points exercised here: `migrate`,
`_restore_stale_palace`, `collection_write_roundtrip_works`, and
`extract_drawers_from_sqlite` (tests/test_migrate.py:L11-L16).

A "palace" is a directory; its presence is recognized by a `chroma.sqlite3` file inside it
(tests/test_migrate.py:L33-L34).

## `migrate(palace_path, *, dry_run=False, confirm=False)`

### Preconditions / safety gate

- **No database -> no migration.** Given a palace directory that contains no
  `chroma.sqlite3` file, `migrate` returns `False` and emits a message containing
  `"No palace database found"` to standard output (tests/test_migrate.py:L19-L27).

- **Confirmation required before any destructive write.** When the database exists but
  the current ChromaDB cannot read it (the readability probe raises) and the user answers
  the interactive prompt with `"n"`, `migrate` returns `False`, emits `"Aborted."` to
  stdout, and performs **no** filesystem mutation: neither a recursive copy
  (`copytree`) nor a recursive delete (`rmtree`) is invoked
  (tests/test_migrate.py:L30-L58).

### Inputs

- `palace_path`: filesystem path to a palace directory (string)
  (tests/test_migrate.py:L23).
- `dry_run`: keyword flag; when true, the rebuild logic runs and reports but does not
  finalize the swap (tests/test_migrate.py:L230, L243).
- `confirm`: keyword flag that supplies confirmation programmatically, bypassing the
  interactive prompt (tests/test_migrate.py:L285, L331).

### Output

- Returns a boolean: `False` on abort/no-database (tests/test_migrate.py:L26, L55),
  `True` when the migration path completes (including dry-run rebuild)
  (tests/test_migrate.py:L234).

### Version detection and writability probe

`migrate` determines the on-disk ChromaDB version via a version detector, and probes the
live collection for write/delete capability before deciding whether to rebuild
(tests/test_migrate.py:L43, L218-L221, L274).

- **Readable-but-not-writable triggers a rebuild from SQLite.** When the collection can be
  opened and counted (e.g. count 102) but the write/delete round-trip probe returns
  `False`, `migrate` rebuilds by extracting drawers directly from the SQLite file. The
  probe is called exactly once with the live collection
  (tests/test_migrate.py:L201-L235). The SQLite path passed to the extractor is the
  absolute join of the palace path with `chroma.sqlite3`
  (tests/test_migrate.py:L236-L238).

- **Diagnostic output for that case** contains, in stdout: a phrase noting the data is
  `"readable by chromadb <version>, but write/delete verification failed"` (with the
  backend version, e.g. `1.5.8`), `"Rebuilding from SQLite"`, and a count line of the form
  `"Extracted N drawers from SQLite"` (e.g. `"Extracted 1 drawers from SQLite"`)
  (tests/test_migrate.py:L227, L240-L242).

- **Dry run reports without finalizing.** With `dry_run=True`, the rebuild path runs to
  the report stage and stdout contains `"DRY RUN"`; the call still returns `True`
  (tests/test_migrate.py:L230, L234, L243).

### Backup creation and pruning

When proceeding with a real (confirmed) migration, `migrate` creates a full pre-migration
backup copy of the palace as a sibling directory named
`<palace>.pre-migrate.<timestamp>` (timestamp format `YYYYMMDD_HHMMSS`), performed right
after the copy step and before the chromadb rebuild step
(tests/test_migrate.py:L294-L301, L306-L311, L335).

- **Backup retention is bounded.** The number of retained `*.pre-migrate.*` sibling
  backups is capped by the environment variable `MEMPALACE_MAX_BACKUPS`. With the cap set
  to `2`, after a migration that creates one fresh backup on top of 3 pre-existing stale
  backups, exactly the 2 newest backups remain and the 2 oldest (by modification time) are
  deleted (tests/test_migrate.py:L313, L335-L340). Pruning occurs even if the migration
  later fails (the prune runs before the chromadb step)
  (tests/test_migrate.py:L298-L301, L330-L337).

### Temp-directory cleanup on failure

The rebuild creates a temporary working palace directory (via a temp-dir maker). If the
chromadb rebuild step fails after that temp directory is created, the temp directory MUST
be removed; it must not leak into the system temp root
(tests/test_migrate.py:L246-L291). The test verifies that the temp maker was actually
called (flow did not short-circuit) and that no captured temp path still exists on disk
afterward (tests/test_migrate.py:L289-L291).

### Swap-in and rollback contract

`migrate` finalizes by swapping the rebuilt palace into place rather than deleting the
original: it first renames the existing palace aside to `<palace>.old`, then renames the
temp palace into the original location (tests/test_migrate.py:L343-L376).

- **Cross-filesystem fallback.** The swap-in rename may fail with `EXDEV`
  (cross-filesystem link); in that case a move fallback is attempted
  (tests/test_migrate.py:L374-L375, L385-L388, L350-L352).

- **Rollback on swap failure.** If both the rename and the move fallback fail, `migrate`
  rolls back by renaming the aside-copy (`<palace>.old`) back into the original location
  via `_restore_stale_palace`, then re-raises the underlying error
  (tests/test_migrate.py:L343-L394). After this rollback:
  - The original palace directory exists again and its original contents are intact
    (verified by a sentinel file whose bytes match the pre-migration value)
    (tests/test_migrate.py:L357-L358, L396-L400).
  - A `*.pre-migrate.*` backup remains on disk for post-mortem
    (tests/test_migrate.py:L402-L404).
  - The `<palace>.old` aside-copy no longer exists (consumed by the rollback rename)
    (tests/test_migrate.py:L406-L408).
  - Because rollback succeeded cleanly, stdout contains no `"CRITICAL"` message
    (tests/test_migrate.py:L410-L412).
  - `migrate` propagates the failure as a raised error to the caller
    (tests/test_migrate.py:L389).

## `_restore_stale_palace(palace_path, stale_path)`

Rolls back by moving the stale/aside copy at `stale_path` back to `palace_path`.

- **Clean destination.** If `palace_path` does not exist, the stale directory is moved into
  place: afterward `palace_path` is a directory holding the stale copy's contents, and
  `stale_path` no longer exists (tests/test_migrate.py:L61-L72).

- **Partially-copied destination is cleared first.** If `palace_path` already exists
  (e.g. a half-finished copy with stray files), the rollback first removes that partial
  destination, then restores the stale copy. Afterward `palace_path` contains only the
  stale copy's original contents (the original `chroma.sqlite3` is present, the
  garbage/half-copied file is gone), and `stale_path` no longer exists
  (tests/test_migrate.py:L75-L96).

- **Failure is logged, never raised.** If the restore operation itself fails (e.g. the
  underlying rename raises), the function does NOT propagate the exception. It emits a log
  line to stdout containing `"CRITICAL"` and including both the `palace_path` and the
  `stale_path` so an operator can recover manually
  (tests/test_migrate.py:L99-L112).

## `collection_write_roundtrip_works(collection) -> bool`

Verifies that a collection is genuinely writable and deletable by performing a probe:
upsert a probe record, confirm it persisted, delete it, and confirm the deletion
(tests/test_migrate.py:L120-L166).

- **Returns `True`** only when the probe record both persists after upsert and is removed
  after delete. After a successful round-trip, the collection holds no leftover probe ids
  and exactly one delete occurred (tests/test_migrate.py:L147-L153).

- **Returns `False` if the write silently drops.** When upsert is a no-op (the record does
  not persist), the function returns `False` and the collection remains empty
  (tests/test_migrate.py:L137-L139, L155-L159).

- **Returns `False` if the delete silently drops.** When delete is a no-op (the record
  remains), the function returns `False` and the collection still contains the probe record
  (tests/test_migrate.py:L142-L144, L162-L166).

The collection contract the probe relies on: an `upsert(ids, documents, metadatas)` write,
a `get(ids, include=None)` read returning an object exposing matching `ids`, and a
`delete(ids=..., where=...)` removal (tests/test_migrate.py:L125-L134).

## `extract_drawers_from_sqlite(db_path) -> list of drawer records`

Reads drawer records directly out of a ChromaDB SQLite file
(tests/test_migrate.py:L192-L198).

### On-disk SQLite schema contract

The extractor reads from two tables (tests/test_migrate.py:L169-L189):

- `embeddings(id INTEGER PRIMARY KEY, embedding_id TEXT)` — `embedding_id` is the drawer's
  external id (e.g. `"d-001"`).
- `embedding_metadata(id INTEGER, key TEXT, string_value TEXT, int_value INTEGER,
  float_value REAL, bool_value INTEGER)` — one row per metadata key, joined to an embedding
  by `id`.
- The reserved key `chroma:document` holds the verbatim document text (e.g. `"hello"`).
- All other keys (e.g. `wing`, `room`) become entries in the drawer's metadata map.

### Output shape

Returns a list of drawer records; each record is a map with three fields
(tests/test_migrate.py:L194-L198):

- `id`: the embedding's external id (string), e.g. `"d-001"`.
- `document`: the verbatim document text (string), e.g. `"hello"`.
- `metadata`: a map of the remaining key/value pairs, e.g.
  `{"wing": "personal", "room": "2026-04-26"}` — notably excluding the `chroma:document`
  key.

For the minimal one-embedding fixture, the result contains exactly one drawer with the
field values above (tests/test_migrate.py:L195-L198).

<promise>SPEC_WRITTEN path=specs/tests/test_migrate.md citations=40</promise>
