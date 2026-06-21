# Behavior Specification: `backups`

## Purpose

This module provides retention pruning for timestamped palace backups. Each time `mempalace migrate` and `mempalace repair max-seq-id` run, they write a fresh, full-size timestamped backup; historically these were never deleted, so they accumulate without bound and can fill the disk. This module deletes old backups so that at most a bounded number remain after each new backup is written (mempalace/backups.py:L1-L12). The retention count is sourced by callers from configuration (`MempalaceConfig.max_backups`, default 10) (mempalace/backups.py:L11-L11).

## Public Surface

### `prune_backups(pattern, max_backups, *, log=None) -> list`

Deletes the oldest backups matching `pattern` so that at most `max_backups` of the most-recent backups remain (mempalace/backups.py:L19-L20).

#### Inputs

- `pattern`: a glob pattern matching backup paths, which may be files or directories. The caller is responsible for escaping any literal, non-wildcard portion that may itself contain glob metacharacters (palace paths sometimes contain characters like `[`) (mempalace/backups.py:L23-L26).
- `max_backups`: the number of most-recent backups to keep. If `None` or any value `<= 0`, pruning is disabled and the function returns immediately without touching any backup, so an opted-out backup set is never modified (mempalace/backups.py:L27-L29, mempalace/backups.py:L41-L42).
- `log` (keyword-only, optional): an optional callable (such as a print function) invoked with a single human-readable string for progress and error reporting. Defaults to no logging (mempalace/backups.py:L19-L19, mempalace/backups.py:L30-L30).

#### Output

Returns the list of paths (strings) that were successfully removed (mempalace/backups.py:L32-L33, mempalace/backups.py:L74-L74). Returns an empty list when pruning is disabled (mempalace/backups.py:L41-L42) and when the number of matching backups is at or below `max_backups` (mempalace/backups.py:L53-L54).

## Behavior and Ordering Guarantees

The set of candidate backups is the set of filesystem paths matching `pattern` (mempalace/backups.py:L45-L45). For each matched path, its filesystem modification time (mtime) is recorded; recency is determined by mtime rather than by parsing a timestamp from the name, so it remains correct even when different backup producers use different name/timestamp formats (mempalace/backups.py:L35-L37, mempalace/backups.py:L46-L47).

If the number of matching backups is less than or equal to `max_backups`, nothing is removed and an empty list is returned (mempalace/backups.py:L53-L54).

When pruning occurs, candidates are ordered newest-first by mtime, with the path string used as a tiebreaker so that ordering is deterministic when two backups share the same mtime (mempalace/backups.py:L56-L57). The first `max_backups` entries (the newest) are retained; every remaining entry (the oldest) is deleted (mempalace/backups.py:L59-L60).

Deletion semantics depend on the path type: a path that is a directory and is not a symbolic link is removed recursively along with its contents; any other path (a file, or a symlink) is removed as a single entry (the symlink itself, not its target, is removed) (mempalace/backups.py:L62-L65).

Successfully removed paths are appended to the returned list in deletion order (oldest of the deleted set encountered first per the newest-first sort), and a progress line is logged for each removal if `log` is provided (mempalace/backups.py:L70-L72).

## Error and Edge-Case Behavior

If a matched path cannot be stat'd for its mtime (for example, it vanished between enumeration and stat due to a concurrent prune or cleanup), that path is silently skipped and excluded from the candidate set (mempalace/backups.py:L46-L51).

Deletion is best-effort: if removing a path fails, the failure is logged (if `log` is provided) as a message of the form `  Backup prune: could not remove <path>: <error>` and that path is skipped without being added to the returned list. Pruning never aborts on a deletion failure, ensuring it cannot disrupt the migrate/repair operation that already completed successfully (mempalace/backups.py:L37-L39, mempalace/backups.py:L66-L69).

## Side Effects

- Filesystem: deletes backup files and recursively deletes backup directories matching `pattern` beyond the retention count (mempalace/backups.py:L62-L65).
- Output: invokes the optional `log` callable with human-readable progress and error strings; performs no other I/O, no network access, and no environment access (mempalace/backups.py:L67-L72).

## Observable Log Message Contracts

When `log` is supplied, two message formats are emitted, each prefixed with two leading spaces:

- On successful removal: `  Backup prune: removed old backup <path>` (mempalace/backups.py:L71-L72).
- On removal failure: `  Backup prune: could not remove <path>: <error>` (mempalace/backups.py:L67-L68).
