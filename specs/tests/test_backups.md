# Behavior Spec: `tests/test_backups.py`

This is a test module that specifies the externally observable contract of the
backup-retention pruning routine `prune_backups` (imported from
`mempalace.backups`) (tests/test_backups.py:L1-L13). The tests guard against
unbounded backup growth: each run of `mempalace migrate` and
`mempalace repair max-seq-id` drops a fresh full-size timestamped backup, and
without pruning these accumulate into hundreds of GB of stale copies beside live
data (tests/test_backups.py:L1-L7).

## Subject Under Test: `prune_backups`

`prune_backups(pattern, max_backups, log=...)` accepts a glob pattern string, an
integer (or null) retention count `max_backups`, and an optional `log` callback,
and returns a list of the string paths it deleted (tests/test_backups.py:L37-L41,
L151-L154).

## Test Fixtures / Helpers

`_make_backup_dir(parent, name, mtime)` creates a directory backup named `name`
under `parent`, containing a file `chroma.sqlite3` with contents `"db"`, and sets
the directory's access and modification times to `mtime`; it returns the created
path (tests/test_backups.py:L16-L22). `_make_backup_file(parent, name, mtime)`
creates a regular file backup named `name` under `parent` with contents `"db"`,
sets its access and modification times to `mtime`, and returns the path
(tests/test_backups.py:L25-L30).

## Retention Behavior (newest-wins by modification time)

Backups are ranked by filesystem modification time (mtime); the `max_backups`
newest entries are kept and all older matching entries are removed. With 5 file
backups whose mtimes are 100, 200, 300, 400, 500 and `max_backups=2`, the two
newest (`b.4`, `b.5`) survive on disk, and the returned removed-list contains
exactly the three oldest paths (`b.1`, `b.2`, `b.3`)
(tests/test_backups.py:L33-L41).

## Directory Backups Are Removed Recursively

When matched backups are directories (as produced by a full copy-tree during
migrate), they are deleted recursively, not just unlinked. Given three directory
backups with mtimes 100, 200, 300 and `max_backups=1`, the newest directory
remains a directory on disk, exactly two are reported removed, and the two oldest
directories no longer exist (tests/test_backups.py:L44-L55).

## No-op When At Or Under The Limit

If the number of matching backups is less than `max_backups`, nothing is removed:
2 backups with `max_backups=10` returns an empty removed-list and leaves both
files on disk (tests/test_backups.py:L58-L65). If the number of matching backups
equals `max_backups` exactly, nothing is removed: 2 backups with `max_backups=2`
returns an empty removed-list (tests/test_backups.py:L68-L74).

## Pruning Disabled

When `max_backups` is `0`, `-1`, or null/None, pruning is disabled and everything
is kept: with 5 backups present, the removed-list is empty and all 5 files remain
on disk for each of those `max_backups` values (tests/test_backups.py:L77-L85).

## No Matches

When the glob pattern matches no entries, the call returns an empty removed-list
(tests/test_backups.py:L88-L89).

## Pattern Scoping — Only Matching Entries Are Touched

Only entries matching the supplied glob pattern are considered; live data and
unrelated files are never swept up. Given three timestamped backups matching
`chroma.sqlite3.max-seq-id-backup-*`, plus a live database file
`chroma.sqlite3` and an unrelated file `tunnels.json`, pruning with
`max_backups=1` leaves the live database, the unrelated file, and the newest
backup (`...-backup-3`) on disk while removing the two oldest backups
(`...-backup-1`, `...-backup-2`) (tests/test_backups.py:L92-L110).

## Glob Metacharacters In The Path Prefix

The routine prunes correctly when the directory path containing the backups
includes glob metacharacters (such as `[` and `]`), provided the caller has
escaped the literal prefix portion of the pattern so the metacharacters are
treated literally rather than as a character class. Given a directory
`weird[name]` containing three matching backups and a pattern whose prefix is
escaped, pruning with `max_backups=1` removes exactly two and the newest backup
(`...-backup-3`) survives (tests/test_backups.py:L113-L132). Without escaping the
prefix, the pattern would silently match nothing and leave backups unpruned
(tests/test_backups.py:L114-L119).

## Best-Effort Deletion (failures are logged, not raised)

Deletion is best-effort: if removing one over-limit backup fails, the failure is
reported via the `log` callback and skipped, never raised, so a pruning failure
cannot undo a migrate/repair that already succeeded
(tests/test_backups.py:L135-L137). With 4 backups (mtimes 100..400) and
`max_backups=2`, the two oldest (`b.1`, `b.2`) are over the limit; if deletion of
`b.1` raises an OS error while `b.2` deletes successfully, then: `b.2`'s path is
in the returned removed-list, `b.1`'s path is NOT in the removed-list, `b.1` still
exists on disk, and at least one log line contains the substring `"could not
remove"` (tests/test_backups.py:L138-L157). The `log` parameter is a callable
invoked with one string argument per log message (tests/test_backups.py:L150-L151).

<promise>SPEC_WRITTEN path=specs/tests/test_backups.md citations=22</promise>
