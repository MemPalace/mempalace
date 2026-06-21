# Spec: `mempalace/migrate.py` — Palace migration & recovery

Purpose: recover a palace whose ChromaDB on-disk database was created by a
different ChromaDB version, and normalize legacy "wing" metadata names. It reads
documents/metadata directly from the palace's SQLite file (bypassing the
ChromaDB API) and re-imports into a fresh palace built with the currently
installed ChromaDB version (mempalace/migrate.py:L2-L19).

## Module-level constants of the on-disk contract

The palace directory contains a ChromaDB SQLite database file named
`chroma.sqlite3` directly under the palace path. A directory is recognized as a
palace iff that file exists as a regular file (mempalace/migrate.py:L143-L146).
The drawer collection name is `mempalace_drawers` (mempalace/migrate.py:L248, L319).

## `extract_drawers_from_sqlite(db_path) -> list`

Reads all drawers directly from the ChromaDB SQLite database, regardless of the
ChromaDB version that created it (mempalace/migrate.py:L56-L66). Returns a list
of records, each a mapping with keys `id` (embedding id), `document` (text), and
`metadata` (mapping) (mempalace/migrate.py:L60-L60, L112-L120).

Document text is read from the embedding metadata row whose key is
`chroma:document`, grouped per embedding id (mempalace/migrate.py:L71-L79).
Records whose document is empty/missing are skipped (mempalace/migrate.py:L85-L86).

Metadata: for each embedding, all metadata rows whose key does NOT start with
`chroma:` are collected (mempalace/migrate.py:L89-L98). For each key, exactly one
typed value is used, in priority order: string value, then integer value, then
float value, then boolean value; the boolean value is coerced to a true/false
type (mempalace/migrate.py:L100-L110). The SQLite connection is closed even if
extraction raises, so no file handle leaks (mempalace/migrate.py:L61-L67).

## `detect_chromadb_version(db_path) -> str`

Detects which ChromaDB version created the database by inspecting the schema and
returns one of three string values (mempalace/migrate.py:L123-L140):
- `"1.x"` if the `collections` table has a `schema_str` column
  (mempalace/migrate.py:L128-L130).
- `"0.6.x"` if there is no `schema_str` column but an `embeddings_queue` table
  exists (mempalace/migrate.py:L131-L137).
- `"unknown"` otherwise (mempalace/migrate.py:L138-L138).
The connection is always closed (mempalace/migrate.py:L139-L140).

## `contains_palace_database(path) -> bool`

Returns true iff `path/chroma.sqlite3` is a regular file
(mempalace/migrate.py:L143-L146).

## `confirm_destructive_action(operation_name, palace_path, assume_yes=False) -> bool`

Gate for destructive operations. If `assume_yes` is true, returns true without
prompting (mempalace/migrate.py:L152-L153). Otherwise prints that the operation
will replace data at the palace path and that a backup will be created first,
then prompts `Continue? [y/N]:` on stdin (mempalace/migrate.py:L155-L158).
Input is trimmed and lowercased; returns true only for `y` or `yes`, false
otherwise (mempalace/migrate.py:L163-L166). If stdin reaches end-of-file, prints
an abort message advising `--yes` and returns false (mempalace/migrate.py:L159-L161).

## `collection_write_roundtrip_works(col) -> bool`

Verifies a collection can actually upsert, read, and delete — not merely be
counted — because some migrated collections stay readable while writes/deletes
silently no-op (mempalace/migrate.py:L178-L185). It upserts one probe record
with a generated unique id prefixed `_mempalace_migrate_probe_`, a fixed probe
document, and probe metadata (`wing`/`room` = `_mempalace_probe`,
`source_file` = `mempalace_migrate_probe`, `chunk_index` = 0)
(mempalace/migrate.py:L187-L201). It then confirms the probe id is present after
upsert, deletes it, and confirms it is absent after delete; returns false if
either check fails and false on any exception, true only on full success
(mempalace/migrate.py:L203-L215). `_result_ids` extracts the id list from either
a mapping result (`ids` field) or a typed result object (`ids` attribute),
defaulting to empty (mempalace/migrate.py:L169-L175).

## `migrate(palace_path, dry_run=False, confirm=False) -> bool`

Migrates a palace to the currently installed ChromaDB version.

Path handling: `palace_path` is expanded (user home) and made absolute; the
database path is `palace_path/chroma.sqlite3` (mempalace/migrate.py:L222-L223).
If the palace path is not a directory or has no palace database, prints a
"No palace database found" message and returns false
(mempalace/migrate.py:L225-L227).

Prints a banner with the palace path, database path, database size in MB,
detected source ChromaDB version, and the target (installed) ChromaDB version
(mempalace/migrate.py:L229-L240).

Readability/writability probe: opens the `mempalace_drawers` collection and
counts it (mempalace/migrate.py:L247-L249). If the write round-trip succeeds,
prints that the palace is already readable/writable and the drawer count, and
returns true with NO changes ("No migration needed")
(mempalace/migrate.py:L251-L254). If the palace is readable but the write/delete
round-trip fails, prints that and proceeds to rebuild from SQLite
(mempalace/migrate.py:L256-L259). If opening/counting raises, prints the palace
is NOT readable and proceeds to extract from SQLite (mempalace/migrate.py:L260-L262).

Extraction & summary: extracts all drawers from SQLite and prints the count
(mempalace/migrate.py:L265-L266). If there are no drawers, prints
"Nothing to migrate." and returns true (mempalace/migrate.py:L268-L270). Builds
a per-wing/per-room drawer-count summary keyed on the `wing` and `room` metadata
fields (defaulting each to `?`) and prints it: wings sorted ascending, rooms
within a wing sorted by descending count (mempalace/migrate.py:L272-L284).

Dry-run: if `dry_run` is true, prints "DRY RUN — no changes made." and the count
that would be migrated, then returns true without modifying anything
(mempalace/migrate.py:L286-L289).

Confirmation: calls the destructive-action gate (with `assume_yes=confirm`);
returns false if not confirmed (mempalace/migrate.py:L291-L292).

Backup: copies the entire palace directory to a sibling path
`<palace_path>.pre-migrate.<YYYYMMDD_HHMMSS>` (timestamp from local time)
(mempalace/migrate.py:L294-L298). Then prunes older `*.pre-migrate.*` backup
siblings to the configured `max_backups` retention limit; the just-created
backup is the newest and survives, and pruning failure never fails the migration
(mempalace/migrate.py:L300-L308).

Rebuild & atomic swap: builds a fresh palace in a temporary directory whose name
begins with `mempalace_migrate_` to avoid reading old state
(mempalace/migrate.py:L310-L319). Re-imports drawers in batches of 500, printing
progress after each batch; each added record carries its original id, document,
and metadata (mempalace/migrate.py:L321-L332). After import, counts the fresh
collection and releases handles (mempalace/migrate.py:L334-L337).

The swap renames the existing palace to `<palace_path>.old` (removing any
pre-existing `.old` first), then moves the temp palace into the palace path,
avoiding any window where both are missing (mempalace/migrate.py:L339-L347).
If the in-place rename fails with a cross-filesystem error (EXDEV), it falls back
to a copy+delete move; any other error, or a failed fallback, triggers a rollback
of the stale `.old` palace back into place and re-raises
(mempalace/migrate.py:L348-L358). On success the stale `.old` directory is
removed (mempalace/migrate.py:L359-L359). A finally block always removes the temp
directory if it still exists (mempalace/migrate.py:L360-L366).

Rollback helper `_restore_stale_palace(palace_path, stale_path)`: clears any
partial destination at `palace_path`, then renames the stale path back into
place; if that itself fails it prints a CRITICAL message naming both paths for
manual recovery and does not re-raise from within the helper
(mempalace/migrate.py:L36-L53).

Completion: prints "Migration complete.", the migrated drawer count, and the
backup path (mempalace/migrate.py:L368-L370). If the final count differs from the
extracted drawer count, prints a WARNING with expected vs. actual
(mempalace/migrate.py:L372-L373). Returns true (mempalace/migrate.py:L375-L376).

## Wing-name normalization migration

Purpose: re-key the `wing` metadata field on drawers and closets, and the
`topics_by_wing` registry, to the normalized form (stripping leading/trailing
separators), merging collisions; the operation is idempotent and leaves
record IDs untouched (mempalace/migrate.py:L379-L395).

### `_normalized_wing_target(wing) -> str | None`

Returns the normalized wing only if it differs from the input, else None.
Returns None when the input is not a non-empty string, when normalization is a
no-op, or when it would normalize to empty (mempalace/migrate.py:L398-L407).
The target is computed by applying full wing-name normalization and then
explicitly stripping leading/trailing `_` separators; returns None if the result
is empty or equals the original (mempalace/migrate.py:L408-L416).

### `plan_wing_renames(items) -> (summary, updates)`

Pure planner over `(id, metadata)` pairs. Returns `summary` mapping
`(old_wing, new_wing)` to a count, and `updates` as a list of
`(id, new_metadata)` for only records whose wing changes
(mempalace/migrate.py:L419-L425). Each record's metadata is copied; only the
`wing` key is rewritten to the normalized target; records needing no change are
skipped (mempalace/migrate.py:L426-L436).

### Collection iteration & application helpers

`_iter_collection_items(col, batch_size=1000)` yields `(id, metadata)` for every
record, paging by count/offset and reading the `metadatas` include; stops when a
page returns no ids (mempalace/migrate.py:L439-L451). `_apply_wing_updates(col,
updates, batch_size=500)` writes the new metadata in batches of 500 via the
collection update operation (mempalace/migrate.py:L454-L458).

### `topics_by_wing` registry helpers

`_plan_topics_by_wing_renames()` loads the known-entities registry and returns
`{old_wing: new_wing}` for `topics_by_wing` keys needing normalization; returns
an empty mapping if loading fails or `topics_by_wing` is not a mapping
(mempalace/migrate.py:L461-L477). `_apply_topics_by_wing_renames(renames)`
re-keys `topics_by_wing` in the on-disk `known_entities.json`, merging the
old key's topic list into the new key (appending only topics not already present)
on collision (mempalace/migrate.py:L480-L507). It writes the registry as JSON
with 2-space indentation and non-ASCII preserved, using a temp file in the
registry's directory and an atomic replace; on write failure the temp file is
removed and the error re-raised (mempalace/migrate.py:L508-L517). Empty rename
sets are a no-op (mempalace/migrate.py:L482-L483).

### `migrate_wing_names(palace_path, dry_run=False, confirm=False) -> bool`

Normalizes legacy wing names in the palace, returning true if anything was (or in
dry-run would be) migrated (mempalace/migrate.py:L520-L528).

Opens the drawer collection without creating it; if it cannot be opened, prints
"No drawer collection found" and returns false (mempalace/migrate.py:L531-L535).
Plans drawer renames from all drawer items and records the set of existing wing
names (mempalace/migrate.py:L537-L539). Optionally opens the closets collection
(without creating) and plans its renames; if closets cannot be opened it is
treated as absent with empty plans (mempalace/migrate.py:L541-L547). Plans
`topics_by_wing` renames (mempalace/migrate.py:L549-L549).

If there are no drawer updates, no closet updates, and no topic renames, prints
"All wing names are already normalized — nothing to migrate." and returns false
(mempalace/migrate.py:L551-L553).

Prints a plan: for each `(old, new)` pair (sorted) it prints the old and new
names, the drawer count and closet count, and a "(MERGE into existing wing)"
note when the new wing already exists among current wings; if there are topic
renames it prints how many keys are re-keyed (mempalace/migrate.py:L555-L565).

Dry-run: prints "DRY RUN — no changes made." and returns true
(mempalace/migrate.py:L567-L569). If not pre-confirmed, prompts
`Apply this wing-name migration? [y/N]`; only `y`/`yes` proceeds, EOF/other
aborts with false (mempalace/migrate.py:L571-L578).

Application: applies drawer updates; applies closet updates only if closets exist
and have updates; applies the `topics_by_wing` renames
(mempalace/migrate.py:L580-L583). Prints a summary listing the number of drawers,
and (only if nonzero) closets and topic keys migrated, then returns true
(mempalace/migrate.py:L585-L591).

## Observable contracts summary

- Palace directory is identified by a `chroma.sqlite3` file at its root
  (mempalace/migrate.py:L143-L146).
- Migration backup directory name format: `<palace>.pre-migrate.<YYYYMMDD_HHMMSS>`
  (mempalace/migrate.py:L295-L296).
- Temporary build directory name prefix: `mempalace_migrate_`
  (mempalace/migrate.py:L315-L315).
- Stale-palace sidestep directory name: `<palace>.old`
  (mempalace/migrate.py:L342-L342).
- All public functions return boolean success indicators; there are no process
  exit codes set within this module (mempalace/migrate.py:L218-L218, L520-L520).
- The known-entities registry is persisted as indented JSON via an atomic
  temp-file-then-replace write (mempalace/migrate.py:L508-L513).
