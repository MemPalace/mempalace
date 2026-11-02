# Compact workspace store

The default remains thread store v1 with Chroma. A workspace opts in through
an offline administrative transition:

```sh
commonplace workspace store <workspace> compact
commonplace doctor --workspace <workspace>
commonplace workspace store <workspace> v1
```

This administrative workflow requires Linux for offline process verification.
Stop the gateway first. The transition takes the exclusive data-directory lock,
checks for orphan workers and holds the palace writer lease throughout. Writers
are refused while it runs. Restart the gateway after success or failure; a
process crash releases the locks automatically. This implementation uses the
RFC's lock-held path, so no concurrent write-log replay is needed.

Configure `COMMONPLACE_BACKUP_CMD` as an executable command with arguments. It
runs without a shell. The command receives `COMMONPLACE_WORKSPACE_DIR` (the
resolved active generation), `COMMONPLACE_WORKSPACE_ID` and a fresh
`COMMONPLACE_BACKUP_NONCE`. Back up `COMMONPLACE_WORKSPACE_DIR`; the stable
workspace container can hold a retired store. `commonplace backup` acquires
the same exclusive lock, so it cannot run inside this command. The command
must complete an off-host backup and print exactly one JSON object:

```json
{"completed_at":"2026-01-01T12:00:00.123456+00:00","off_host":true,"nonce":"<COMMONPLACE_BACKUP_NONCE>"}
```

The command must exit zero and echo the nonce from its environment. Its completion
timestamp must include a timezone, be at least the command start time and be
newer than every workspace data write. A failed command, stale timestamp, missing
receipt, local-only receipt or changed source refuses the transition. The receipt
and command stderr are recorded locally; stderr is kept in the private
`.store-backup.stderr.log` file. The backup command owns the off-host destination;
the transition never creates a backup on the workspace host. Migration staging
directories are temporary working stores, not backup destinations.

`workspace.json` in the active generation holds the settings:

```json
{"store":"compact","compact_max_collection_records":100000}
```

The administrative command publishes `store` only after verification. Do not
change it directly on an existing workspace. The worker writes `backend` into
its own configuration, which takes precedence over `MEMPALACE_BACKEND`, and
handoff uses `threads.v2.sqlite3`. The collection limit defaults to 100,000;
raise it only after the compact hot-path prerequisites have been validated.

Before backup, the transition checks source format, collection size, rollback
representability, re-embedding count and available disk space. The space estimate
allows four generations/snapshots plus SQLite overhead and accounts for expansion
when rolling compact data back. Insufficient space refuses the operation.

The transition builds a complete sibling generation, copying threads row by
row with their original ids, append-time chunk boundaries, parent chains,
handoff documents and extraction progress. Every message field, chunk text and
digest is checked. Every palace collection is copied without embedding its
existing vectors, then checked by id, metadata, document text and vector error
bound. Knowledge graph, logstream, replica state and sidecars are carried over.
SQLite sidecars include their committed WAL contents. Boards, rules, settings,
worker configuration and home files are also verified against the live source
using read-only SQLite comparisons or file hashes. Chroma is opened only on
a staging snapshot so its recovery cannot change the live palace.

Before publishing compact, a disposable staging generation receives a new
thread, a uniquely named probe collection and a keyword-only drawer in the real
drawers collection. It rolls back and verifies all records and threads. The
keyword probe exercises the workspace embedder at the stored vector dimension;
a model or dimension mismatch refuses the transition. An atomic,
durable generation pointer then publishes palace, threads, boards and settings
together. Interruptions before that pointer leave the previous generation
active. Interruptions afterwards leave the fully verified generation active.
Publication is recorded as `published` immediately after the pointer write and
before sealing retired files. A seal failure remains `published` with sealing
marked `failed`; doctor reports it, and the next maintenance run retries sealing.
Recovery finishes publication when a pointer copy names the staging generation,
including a crash before the publication state could be recorded. It never
overrides a valid main pointer naming another existing generation: maintenance
reports that conflict, refuses with a non-zero exit and changes nothing. Preserve
both generations and reconcile their writes and pointers before retrying.
To keep the manual main pointer, first recover any wanted writes from the recorded
generation, then clear the pending `published` state in `.store-backfill.json`
(replace it atomically with `{}`). To adopt the recorded publication instead,
atomically restore its `pointer` object from the `published` state over
`.store-pointer.json`, preserving its full retained list, then retry maintenance
to finish sealing. Resolve any remaining prev-copy difference as described below.
A failed operation removes its unpublished staging copies immediately. After a
process crash, rerunning either store command or pruning removes orphan staging
copies and resets the interrupted backfill state. Cleanup validates the generation
pointer first. If it is missing or invalid, only the state file's provably
unpublished staging copy can be removed; other generations are preserved and
the operation refuses until the pointer is repaired. Workers, the gateway and
doctor also refuse to resolve an absent or invalid pointer when sibling
generations exist, so they cannot resume writing into the original root. A prev
copy without a main pointer also refuses, even with no sibling generations.
Follow the state-specific repair hint after verifying the data. With sibling
generations and no recorded publication, keep prev and restore it as the main
pointer. With no sibling generation, deleting prev can instead accept the original
root. When a valid pending
`published` state records the complete pointer, deleting prev and rerunning
`workspace prune-stores` republishes that recorded generation. A main pointer
deleted during pending publication therefore needs operator action first. Restoring
prev also works if it matches the recorded publication; a stale prev is not offered
as that recovery route. Deleting prev without a recorded publication while sibling
generations exist would discard history without resolving the pointer.
New publications also save the complete pointer and its checksum in
`.store-publication.json` immediately after writing the main pointer. If that
pointer is later lost, the repair hint additionally offers `adopt <generation>`
when this receipt, the generation marker, retained history and inventories are
intact. Stop the gateway and verify the selected data, then atomically restore
the receipt's full `pointer` object as `.store-pointer.json` and rerun maintenance.
This preserves writes made after publication and leaves rollback available.
A missing or damaged receipt never enables this route; older publications keep
their existing recovery choices. The hint does not automatically select a store.
Completed retained generations are preserved. With a valid pointer, cleanup
preserves and reports unlisted generations carrying `.store-comparison.json`;
named retained generations follow the fourteen-day and fingerprint checks.
Recorded staging with a marker is removed only when its data is no newer than
its markers. Newer data is preserved and listed by doctor and report-only pruning.
This protection applies to base generations; disposable scratch copies are removed
after interrupted backfills and rehearsals. Report-only pruning lists every
leftover generation-related directory, including scratch copies.

The tool's pointer writes atomically update a last-good recovery copy in
`.store-pointer.prev.json` at the stable workspace root. Maintenance preserves that
copy after an external edit of the main pointer. It replaces the copy only when
it is absent or its contents cannot be parsed as JSON, or when the main pointer
differs solely by removed retained entries whose directories are gone. A parseable
copy is preserved even when its active directory is temporarily missing. Other
differences make switch and prune refuse before changing any files; doctor and
report-only pruning exit 3, name the differing entries and explain the two choices:
restore `.store-pointer.prev.json` over `.store-pointer.json` to undo the pointer
change, or delete `.store-pointer.prev.json` to accept the current pointer. Stop
the gateway and verify the selected generation and its writes before either
choice. This also covers a prune interrupted between its two pointer writes:
dropping the root entry leaves a difference because the stable root directory
still exists. If that root contains only the tool's administration files, it is
treated as gone: pruning heals the interrupted deletion, including after restoring
prev. Doctor and report-only name an emptied root still listed as retained and
recommend completing the prune. Any other root contents retain the normal checks.
Before deleting a verified expired generation, pruning durably records its intent
and per-file fingerprints in `.store-backfill.json`. A crash partway through
deletion leaves a named interrupted deletion in doctor and report-only; the next
prune verifies the surviving files and finishes it. Added or changed files stop
recovery. Switching waits until that deletion is resolved. A partial deletion
without a record requires the documented manual removal procedure instead.
Doctor checks expected files with filesystem metadata only and shows findings
recorded by the last real prune. Report-only may additionally compare fingerprints
after retention expires; it never hashes unexpired generations. Missing expected
files are named `incomplete (interrupted deletion)`. Otherwise a changed fingerprint
with newer writes is named `newer writes`; other mismatches are named
`changed since retirement`. A refused prune records the generation, kind and time
so doctor can show the cause without rehashing retained data.
New retirement entries reference a checksum-checked relative file inventory in
the adjacent `.store-retired-files.json` file. The inventory is written atomically
before publishing its pointer reference. Runtime resolution reads only the pointer;
maintenance reads the inventory and refuses cleanup when referenced evidence is
missing or damaged. Older inline `expected_files` inventories remain supported.
Entries without an inventory use the known thread and palace layouts plus saved
prune findings.
Legacy layouts permit an absent thread database. Prune uses their stored content
fingerprint as its authority: a match is healthy. When doctor cannot identify a
legacy layout, it reports `unverified (legacy entry; prune checks it)` as information
with exit 0; missing required files still identify an incomplete generation.
Legacy compact layouts with threads but no palace database are also unverified;
doctor cannot distinguish an originally empty palace from a deleted database.
Prune still requires the stored fingerprint to match before deleting them.
Missing `retained` and `retained: []` are equivalent.
Recovery can finish an interrupted publication when its pointer carries forward
the previous copy's complete retained history. A partial pointer cannot bypass
the history check through a pending publication state.
To repair a lost pointer,
stop the gateway, preserve all generation directories and inspect that copy or
a full data-tree snapshot made with `commonplace backup`. The configured off-host
command backs up only the active generation and does not retain the pointer or
retirement history. Confirm the candidate's active generation exists
and contains the latest writes before atomically restoring `.store-pointer.json`.
A crash between the two writes can leave the recovery copy one update behind;
publication recovery handles that interrupted write. Doctor reports that the
pointer differs from the prev copy and names the differing entries, without
assuming that a hand edit is a stale copy or modifying either file.
For manual pointer repair, verify the copy rather than treating it as a fallback.
Restore the full retained
list rather than writing a minimal pointer by hand. Unlisted published generations
are preserved until their retirement evidence is recovered and checked.

If `.store-backfill.json` is corrupt, switching refuses with a repair hint.
Pruning instead returns a read-only generation report and changes no workspace
files. Repair the state from verified evidence before retrying destructive cleanup.
Doctor reports a corrupt state with the same repair hint.

A sample of live thread reads, palace reads and searches is compared with the
retained store. Comparison logs contain only match status and fixed diagnostic
labels. Queries,
content, reusable content hashes and exception messages are never logged.
Palace comparison is skipped when the retained backend is Chroma, whose client
can mutate files during reads. Thread comparisons remain enabled. Ranking
differences and new writes can produce mismatches without interrupting the live read.

Rollback copies the current compact generation back, including writes made
since the switch. Keyword-only records need the workspace's configured embedder:
the command logs their count and a rough estimate before embedding them, removes
the keyword-only marker and preserves document text. An unavailable embedder
refuses rollback. The estimate assumes 50 milliseconds per record and excludes
model startup. Threads or handoff documents containing text v1 cannot represent,
including lone surrogates, refuse rollback with counts. Compact stays active
with every byte intact.

Retired generations have their data files made read-only and are retained for at
least fourteen days. An older binary can restore write permissions, so pruning
also checks for writes newer than retirement and compares a retirement content
fingerprint, detecting changes even within the filesystem timestamp resolution.
A changed generation or missing retirement evidence refuses pruning. If pruning
refuses, keep that generation intact, stop the gateway and compare it with the
active generation and a full data-tree `commonplace backup` snapshot. Recover any missing records before
discarding anything. Restoring files can reset their timestamps: a quiet timestamp
check in `doctor` does not prove that restored content matches retirement. Do not
reset the retirement timestamp or fingerprint to bypass refusal. Preserve a
verified full data-tree `commonplace backup` snapshot before explicitly removing it;
a later prune drops entries for generations already removed. Run the offline
retention command periodically:

```sh
commonplace workspace prune-stores <workspace>
commonplace workspace prune-stores <workspace> --report-only
```

Report-only mode never changes workspace files or repairs pointers. It exits 0
when no findings remain and 3 when it reports leftover directories, preserved
generations, pointer differences, recovery conflicts, incomplete backfill, failed sealing or corrupt state, so scheduled
checks can detect outstanding work. Ordinary command errors use exit code 2.
Doctor also exits 3 for recovery conflicts and emptied retained roots. Both
reports give the recovery conflict hint priority over a prev difference. A missing
or unparseable prev copy yields exit 3 with an instruction to run maintenance,
which refreshes it from the valid main pointer.
If retirement evidence cannot be recovered from the pointer copy or a full
`commonplace backup` snapshot, preserve the generation, recover its missing
records, verify a backup of those contents, then remove that generation manually.
Do not add a new retirement timestamp or digest merely to make pruning accept it.

Pruning removes abandoned staging directories and never removes the active
generation. Existing off-host backups follow their own retention policy. `doctor` reports the selected format, thread index version,
palace schema version, backfill state, active path and retained generations older
than fourteen days for each workspace. Doctor uses timestamps without hashing
retained content; pruning checks fingerprints only after the retention period.

To restore from an off-host backup, stop the gateway and prepare a fresh data
directory with the matching control database and workspace id. Restore the
backed-up active generation's contents into `workspaces/<id>` there, preserving
`workspace.json`, palace files, threads and sidecars. A single-generation restore
uses that directory directly and needs no generation pointer; a full data-tree
restore must preserve the pointers, retirement inventories and every generation
they name. Keep publication receipts for pointer-loss recovery too. Verify
the backup manifest and run `commonplace --data <restored-data> doctor` before
starting the gateway against the restored tree. Keep the previous data tree
intact for recovery and its normal retention period.
