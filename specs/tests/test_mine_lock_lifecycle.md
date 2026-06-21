# Behavior Spec: `tests/test_mine_lock_lifecycle.py`

This is a test module that pins down the observable lifecycle contract of the
"mine lock" — a per-source-file advisory lock used to serialize mining of a
given source file. The behaviors below are the contract that the lock
implementation (`mempalace.palace`) MUST satisfy; an implementation in any
language must reproduce them.

## Module under test and its public/internal surface

The tests exercise these symbols from `mempalace.palace`
(`tests/test_mine_lock_lifecycle.py:L10-L17`):

- `mine_lock(source_file)` — a scoped/context-managed critical section keyed by a
  source file path; entering acquires the lock, exiting releases and cleans it up
  (`tests/test_mine_lock_lifecycle.py:L11-L17`, `:L85-L93`).
- `_mine_lock_path(source_file)` — maps a source file path to the lock file path
  (`tests/test_mine_lock_lifecycle.py:L11-L17`, `:L83`).
- `_open_mine_lock_file(lock_path, *, create)` — opens (optionally creating) the
  lock file handle (`tests/test_mine_lock_lifecycle.py:L11-L17`, `:L163`,
  `:L133`).
- `_lock_mine_lock_file(lock_file, *, blocking)` — acquires the OS lock on a handle;
  returns a truthy/boolean success indicator
  (`tests/test_mine_lock_lifecycle.py:L11-L17`, `:L167`, `:L135`).
- `_unlock_mine_lock_file(lock_file)` — releases the OS lock on a handle
  (`tests/test_mine_lock_lifecycle.py:L11-L17`, `:L192`).
- `_acquire_open_mine_lock_file(lock_file, lock_path)` — given an already-opened,
  locked handle, returns whether that handle refers to the *current* lock file at
  `lock_path` (truthy = current/valid; falsy = stale)
  (`tests/test_mine_lock_lifecycle.py:L51-L60`).
- Internal collaborators referenced via monkeypatch hooks:
  `_acquire_mine_lock_file`, `_cleanup_mine_lock_file`,
  `_mine_lock_file_is_current` (`tests/test_mine_lock_lifecycle.py:L106-L138`).

## Lock-file path derivation

The lock file path is derived deterministically from the source file path and the
user's home directory. Tests set both `HOME` and `USERPROFILE` to a temp dir
before deriving paths, so the derivation honors the platform home environment
variable (`tests/test_mine_lock_lifecycle.py:L20-L22`, `:L81-L83`).

## Contract: uncontended acquire creates then removes the lock file

While inside the `mine_lock(source_file)` scope, the lock file at
`_mine_lock_path(source_file)` MUST exist
(`tests/test_mine_lock_lifecycle.py:L85-L86`). After the scope exits, the lock
file MUST no longer exist (`tests/test_mine_lock_lifecycle.py:L88`). This is
repeatable: a second acquire/release cycle on the same source again creates the
file on entry and removes it on exit (`tests/test_mine_lock_lifecycle.py:L90-L93`).

## Contract: cleanup ordering and resilience to close failure

`mine_lock` MUST run its release/cleanup steps in a fixed order even when the
handle's `close` raises. With the body, unlock, close, and cleanup steps
instrumented, the observed order is exactly: user body runs first, then the OS
lock is released (`unlock`), then the handle is closed (`close`), then the lock
file is cleaned up (`cleanup` with the lock path argument)
(`tests/test_mine_lock_lifecycle.py:L96-L119`). Specifically the expected event
sequence is `["body", "unlock", "close", ("cleanup", "source.lock")]`
(`tests/test_mine_lock_lifecycle.py:L119`).

A `close` that raises `OSError` MUST NOT abort or skip the subsequent cleanup
step — cleanup still runs and removes the lock file
(`tests/test_mine_lock_lifecycle.py:L99-L119`). The lock path passed to cleanup is
the value returned by `_mine_lock_path` for the source file
(`tests/test_mine_lock_lifecycle.py:L105`, `:L113`, `:L119`).

## Contract: Windows cleanup re-locks, then releases exactly once

`_cleanup_mine_lock_file(lock_path)` on Windows (`os.name == "nt"`) re-opens the
lock file, re-acquires the lock, verifies it is current, and then releases it.
The cleanup MUST attempt the unlock and then close the handle — and even when that
unlock raises `OSError`, cleanup MUST NOT retry the unlock; it proceeds directly
to `close` (`tests/test_mine_lock_lifecycle.py:L122-L148`). The observed event
sequence when unlock fails is exactly `["unlock", "close"]` — one unlock attempt
followed by one close, with no second unlock (`tests/test_mine_lock_lifecycle.py:L148`).

In this Windows cleanup path the handle open is non-creating-agnostic but uses
the `create` keyword (`tests/test_mine_lock_lifecycle.py:L132-L134`), the lock is
acquired (returns true) (`tests/test_mine_lock_lifecycle.py:L135`), and currency
is confirmed via `_mine_lock_file_is_current` returning true
(`tests/test_mine_lock_lifecycle.py:L136-L138`).

## Contract: stale-inode waiter must not enter the critical section (POSIX, issue #1800)

This test is skipped on Windows (`os.name == "nt"`) because it targets a POSIX
inode-replacement regression (`tests/test_mine_lock_lifecycle.py:L151`). It models
a three-party race: process A removes the lock path after releasing it, while
process B was already blocked waiting on the now-unlinked inode, and process C has
locked a freshly created replacement file at the same path. B MUST reject the
stale inode and retry rather than enter the critical section
(`tests/test_mine_lock_lifecycle.py:L152-L158`).

The required observable behavior of a waiter that wakes on an unlinked lock inode
(driven through `_stale_waiter_target`,
`tests/test_mine_lock_lifecycle.py:L42-L77`):

1. The waiter opens the original lock file (with `create=True`) and signals it has
   opened by creating an `opened` flag file
   (`tests/test_mine_lock_lifecycle.py:L58-L59`, `:L186`).
2. When the waiter's lock wait completes on the unlinked inode, calling
   `_acquire_open_mine_lock_file` on that handle MUST return falsy (not current),
   reported as `("first-acquire-current", False)`
   (`tests/test_mine_lock_lifecycle.py:L60-L61`, `:L196`).
3. On detecting the stale (non-current) inode, the waiter MUST close that handle
   and retry by re-entering the public `mine_lock(source_file)` scope, reported as
   `("retrying", True)` (`tests/test_mine_lock_lifecycle.py:L70-L72`, `:L197`).
4. While the replacement lock at the same path is still held by another party, the
   waiter MUST NOT enter the critical section: the `entered` flag file MUST remain
   absent for the duration it is checked (here ~0.5s)
   (`tests/test_mine_lock_lifecycle.py:L34-L39`, `:L198`).
5. Once the replacement lock is released, the waiter acquires the lock on the
   replacement path and creates the `entered` flag, signaling it entered the
   critical section (`tests/test_mine_lock_lifecycle.py:L73`, `:L200-L204`).
6. The waiter remains in the critical section until a `release` flag file appears,
   then releases and exits cleanly, reporting `("done", True)`
   (`tests/test_mine_lock_lifecycle.py:L64-L75`, `:L205-L206`).

After the waiter completes, the child process MUST exit with code 0 and the lock
file MUST no longer exist (`tests/test_mine_lock_lifecycle.py:L207-L209`).

The orchestration that produces this race in the parent
(`tests/test_mine_lock_lifecycle.py:L163-L209`): the parent opens and non-blocking
locks the original lock file (`:L163`, `:L167`); spawns the waiter child (`spawn`
start method) and waits for the `opened` flag (`:L172-L186`); removes the lock
path and creates+locks a replacement at the same path (`:L188-L190`); then
releases and closes the original handle (`:L192-L194`), which wakes the waiter on
the unlinked inode. The replacement lock is held until after the test confirms the
waiter did not enter, then released to let the waiter proceed
(`:L196-L206`).

## Side effects and externally observable artifacts

- Creates and removes a lock file on disk at the path returned by
  `_mine_lock_path`, under the home directory set via `HOME`/`USERPROFILE`
  (`tests/test_mine_lock_lifecycle.py:L20-L22`, `:L83-L88`).
- Uses flag files on disk (`opened`, `entered`, `release`) purely as
  cross-process synchronization signals; their presence/absence encodes lifecycle
  state (`tests/test_mine_lock_lifecycle.py:L25-L39`, `:L169-L171`).
- Spawns a separate OS process (spawn start method) that imports the lock module
  and shares results back over an inter-process queue
  (`tests/test_mine_lock_lifecycle.py:L42-L77`, `:L172-L185`).
- Cleanup is best-effort in test teardown: dangling handles are unlocked/closed,
  swallowing exceptions, and a live child is terminated then joined
  (`tests/test_mine_lock_lifecycle.py:L210-L225`).

## Helper contracts (test-internal)

- `_wait_for_path(path, timeout=10.0)` polls every 10ms until the path exists or
  the timeout elapses, returning whether it ultimately exists
  (`tests/test_mine_lock_lifecycle.py:L25-L31`).
- `_assert_path_absent_for(path, duration=0.5)` asserts the path stays absent for
  the whole duration, polling every 10ms; failure message:
  "waiter entered while replacement lock was held"
  (`tests/test_mine_lock_lifecycle.py:L34-L39`).
- The waiter target reports any unexpected exception back over the queue as
  `("error", repr(exc))` rather than crashing silently
  (`tests/test_mine_lock_lifecycle.py:L76-L77`).
