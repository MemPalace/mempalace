# Behavior Spec: `mine_palace_lock` (per-palace mine guard)

This file specifies the externally observable behavior verified by `tests/test_palace_locks.py`. The subject is a per-palace, non-blocking "mine guard" lock used to prevent runaway concurrent mining against the same palace, while still allowing parallel mines against different palaces (tests/test_palace_locks.py:L1-L8).

## Public Surface Under Test

The following symbols are imported and exercised (tests/test_palace_locks.py:L19-L24):

- `mine_palace_lock(palace_path)` — a context-manager guard keyed to a single palace path. Acquired on entry, released on exit.
- `MineAlreadyRunning` — an error raised when the palace lock is already held by another process/runner.
- `mine_global_lock` — a backward-compatibility alias for `mine_palace_lock`.
- `_write_lock_holder(lock_file)` — writes the current process's holder identity into an open lock file handle.

## Lock Acquisition Semantics

A single acquire of the lock against a fresh palace path must succeed without raising (tests/test_palace_locks.py:L71-L74).

After a holder releases the lock (exits the guarded scope), the same palace lock must be re-acquirable; release fully frees the lock (tests/test_palace_locks.py:L77-L84).

When the lock is acquired, `MineAlreadyRunning` is NOT raised; when it cannot be acquired because another process holds it, `MineAlreadyRunning` IS raised (tests/test_palace_locks.py:L52-L63).

## Cross-Process Mutual Exclusion (same palace)

The lock is a true cross-process lock. While one process holds the lock on a palace, a second independent process attempting to acquire the same palace lock must be rejected with `MineAlreadyRunning` rather than blocking/queuing (tests/test_palace_locks.py:L46-L63, L87-L113). The contending second acquirer fails immediately (non-blocking) rather than waiting (tests/test_palace_locks.py:L105-L108).

## Independence Across Different Palaces

Locks are scoped per-palace. While a process holds the lock on palace A, acquiring the lock on a different palace B must succeed and must not raise (tests/test_palace_locks.py:L115-L135).

## Path Normalization (lock key derivation)

The lock key is derived from the normalized path: the real (canonical) path with case-normalization, hashed (documented as SHA-256 of realpath + normcase) (tests/test_palace_locks.py:L168-L173). Consequently, a relative path and the absolute path that resolve to the same on-disk directory map to the same lock. While a process holds the lock using the absolute form, a second process whose working directory makes a relative form resolve to the same directory must collide and raise `MineAlreadyRunning` (tests/test_palace_locks.py:L141-L176).

## Re-entrancy Within the Same Thread

Re-acquiring the lock from the same thread that already holds it is a re-entrant pass-through: the inner acquire must neither raise nor deadlock (tests/test_palace_locks.py:L179-L193). This allows write methods that take `mine_palace_lock` to compose with an outer mine pipeline that already holds the lock (tests/test_palace_locks.py:L180-L187).

After the inner (re-entrant) scope exits, the outer scope still holds the lock; an external process attempting acquisition at that point must still observe the lock as busy (tests/test_palace_locks.py:L194-L210). The helper reports `"free"` if acquired or `"busy"` if `MineAlreadyRunning` was raised (tests/test_palace_locks.py:L213-L219).

## Error Message Contract: Holder Identification

When acquisition fails because another process holds the lock, the raised `MineAlreadyRunning` message must identify the holder by process id, containing the literal substring `PID <pid>` where `<pid>` is the holder process's id (tests/test_palace_locks.py:L238-L272). The holder's identity (pid plus the first three command-line arguments) is recorded when it acquires the lock (tests/test_palace_locks.py:L222-L235).

## On-Disk Lock File Format

Lock files live under `~/.mempalace/locks/` (resolved via the user home directory; HOME on POSIX, USERPROFILE on Windows) (tests/test_palace_locks.py:L320-L339). Per-palace lock files are named matching the pattern `mine_palace_*.lock` (tests/test_palace_locks.py:L340-L341).

The file body consists of a single byte-0 sentinel (`\x00`) followed immediately by the holder identity bytes; it is overwritten (truncated + rewritten), never appended, on each acquire (tests/test_palace_locks.py:L321-L348). The holder identity string is `"{pid} {first three argv joined by spaces}"` with surrounding whitespace stripped, encoded as UTF-8 (tests/test_palace_locks.py:L296-L297).

Repeated acquire/release cycles must not grow the file; after 5 acquire/release cycles the body remains bounded (asserted under 1024 bytes), proving no per-run accumulation (tests/test_palace_locks.py:L333-L348).

## `_write_lock_holder` Behavior

`_write_lock_holder(lock_file)` writes, into the given open binary file handle, a leading byte-0 sentinel followed by the UTF-8 encoding of the holder identity, truncating any pre-existing (stale) content so the written bytes exactly equal `b"\x00" + ident.encode("utf-8")`, where `ident = f"{pid} {' '.join(argv[:3])}".strip()` (tests/test_palace_locks.py:L278-L297). The byte count and on-disk bytes agree even for non-ASCII argv (e.g. `café/北`) because UTF-8 is always used regardless of platform codepage (tests/test_palace_locks.py:L278-L297).

Writing the holder identity is best-effort: if the underlying write raises an encoding error (e.g. a code page cannot represent the characters), `_write_lock_holder` must swallow the failure and return normally, so a holder-write failure never blocks lock acquisition (tests/test_palace_locks.py:L300-L317).

## Backward-Compatibility Alias

`mine_global_lock` must be the exact same callable object as `mine_palace_lock` (identity equality), and must accept the same `palace_path` argument and behave identically (tests/test_palace_locks.py:L351-L356).

## Side Effects and Environment

- Tests redirect the user home (HOME, and USERPROFILE on Windows) to a temp directory so lock files land under `<tmp>/.mempalace/locks/` (tests/test_palace_locks.py:L72, L330-L331, L339).
- Lock files are created on disk as a side effect of acquiring the lock (tests/test_palace_locks.py:L339-L341).
- Cross-process tests coordinate via filesystem flag files: a "ready" file written after the child acquires, and a "release" file the parent writes to signal the child to release (tests/test_palace_locks.py:L46-L63, L91-L112).

## Concurrency / Process Notes (test harness contract)

Child worker processes are spawned with a re-imported package context (not forked) and inherit `os.environ` including the redirected home, which is sufficient for lock-file behavior (tests/test_palace_locks.py:L27-L38). A child that holds the lock and then releases on signal must exit cleanly (exit code 0) (tests/test_palace_locks.py:L109-L112, L205-L206).
