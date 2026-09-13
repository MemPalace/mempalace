# Loaded into mempalace.palace via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.palace":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.palace")


@contextlib.contextmanager
def mine_lock(source_file: str):
    """Cross-platform file lock for mine operations.

    Prevents multiple agents from mining the same file simultaneously,
    which causes duplicate drawers when the delete+insert cycle interleaves.
    """
    _maybe_reap_stale_mine_locks()
    lock_path = _mine_lock_path(source_file)
    lf = _acquire_mine_lock_file(lock_path)
    try:
        yield
    finally:
        try:
            _unlock_mine_lock_file(lf)
        except Exception:
            logger.debug("Mine-lock release failed", exc_info=True)
        try:
            lf.close()
        except Exception:
            logger.debug("Mine-lock close failed", exc_info=True)
        _cleanup_mine_lock_file(lock_path)


def _mine_lock_path(source_file: str) -> str:
    lock_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    return os.path.join(lock_dir, hashlib.sha256(source_file.encode()).hexdigest()[:16] + ".lock")


def _open_mine_lock_file(lock_path: str, *, create: bool):
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    fd = os.open(lock_path, flags, 0o600)
    return os.fdopen(fd, "r+b")


def _lock_mine_lock_file(lock_file, *, blocking: bool) -> bool:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(lock_file.fileno(), mode, 1)
        except OSError:
            if not blocking:
                return False
            raise
        return True

    import fcntl

    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(lock_file, flags)
    except BlockingIOError:
        if not blocking:
            return False
        raise
    return True


def _unlock_mine_lock_file(lock_file) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file, fcntl.LOCK_UN)


def _mine_lock_file_is_current(lock_file, lock_path: str) -> bool:
    """Return whether ``lock_file`` is still the inode reached by ``lock_path``.

    POSIX advisory locks attach to the opened inode, not the pathname. If a
    lock file is unlinked while a contender is waiting, that contender can later
    acquire a lock on an inode no new process will use. We reject that stale
    handle and retry on the current pathname.
    """
    if os.name == "nt":
        return True
    try:
        path_stat = os.stat(lock_path)
        file_stat = os.fstat(lock_file.fileno())
    except OSError:
        return False
    return (path_stat.st_dev, path_stat.st_ino) == (file_stat.st_dev, file_stat.st_ino)


def _acquire_open_mine_lock_file(lock_file, lock_path: str) -> bool:
    """Acquire ``lock_file`` and return False if cleanup made it stale."""
    _lock_mine_lock_file(lock_file, blocking=True)
    if _mine_lock_file_is_current(lock_file, lock_path):
        return True
    try:
        _unlock_mine_lock_file(lock_file)
    except Exception:
        logger.debug("Mine-lock stale-handle release failed", exc_info=True)
    return False


def _acquire_mine_lock_file(lock_path: str):
    while True:
        lf = _open_mine_lock_file(lock_path, create=True)
        try:
            if _acquire_open_mine_lock_file(lf, lock_path):
                return lf
        except Exception:
            lf.close()
            raise
        lf.close()


def _cleanup_mine_lock_file(lock_path: str) -> None:
    """Best-effort removal that preserves flock rendezvous semantics.

    A plain ``os.remove(lock_path)`` after closing the critical-section lock is
    unsafe on POSIX: a waiter may already be blocked on the old inode while a
    later process creates and locks a new inode at the same pathname. Instead,
    cleanup briefly re-acquires the current file nonblocking. If it wins, it can
    unlink that inode as cleanup-only work; waiters on the old inode will detect
    the stale handle after waking and retry on the current path.
    """
    try:
        lf = _open_mine_lock_file(lock_path, create=False)
    except FileNotFoundError:
        return
    except OSError:
        logger.debug("Mine-lock cleanup open failed for %s", lock_path, exc_info=True)
        return

    acquired = False
    closed = False
    try:
        try:
            acquired = _lock_mine_lock_file(lf, blocking=False)
        except OSError:
            logger.debug("Mine-lock cleanup acquire failed for %s", lock_path, exc_info=True)
            return
        if not acquired:
            return
        if not _mine_lock_file_is_current(lf, lock_path):
            return

        if os.name == "nt":
            # Windows generally cannot unlink an open locked file. Release and
            # close first; if another process opens the file in the gap,
            # os.remove should fail and we leave the rendezvous file in place.
            try:
                _unlock_mine_lock_file(lf)
            except Exception:
                logger.debug("Mine-lock cleanup release failed", exc_info=True)
                acquired = False
                return
            acquired = False
            lf.close()
            closed = True
            try:
                os.remove(lock_path)
            except OSError:
                pass
            return

        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("Mine-lock cleanup remove failed for %s", lock_path, exc_info=True)
    finally:
        if not closed:
            if acquired:
                try:
                    _unlock_mine_lock_file(lf)
                except Exception:
                    logger.debug("Mine-lock cleanup release failed", exc_info=True)
            lf.close()


def reap_stale_mine_locks(*, min_age_seconds: int = 3600) -> tuple[int, int]:
    """Best-effort garbage collection for orphaned per-source-file mine locks.

    ``_cleanup_mine_lock_file`` reclaims a lock file correctly on the happy
    path (see its docstring) — but only for the *specific* lock a
    :func:`mine_lock` context manager just released. A process that dies
    before reaching its own ``finally`` block (killed, crashed, force-quit,
    host reboot) never runs that cleanup, and nothing else in this codebase
    later revisits that lock file. Locks in ``~/.mempalace/locks/`` can
    accumulate unboundedly over time as a result — one long-lived
    installation was found with 5,636 stale entries, the oldest several
    months old, none held by any live process (confirmed via ``lsof``).

    This reuses :func:`_cleanup_mine_lock_file` itself for the actual
    removal — same nonblocking-flock-reacquire safety mechanism, same
    Windows/POSIX handling, no duplicated locking logic. A lock is only
    ever removed after *this* process re-acquires it, so anything
    genuinely held by a live process is left untouched regardless of
    ``min_age_seconds``. ``min_age_seconds`` is a courtesy throttle only —
    it avoids racing a lock that was *just* released and may still be
    mid-rendezvous with a waiter on the same pathname; it is not a
    substitute for the flock check, which is what actually makes removal
    safe.

    Skips ``mine_palace_*.lock`` files — those belong to the newer
    palace-level :func:`mine_palace_lock` and have their own
    lifecycle/holder tracking; this targets only the per-source-file locks
    :func:`mine_lock` creates via :func:`_mine_lock_path`.

    Returns ``(reaped, skipped)`` counts, for logging/testing — callers
    don't need to act on them.
    """
    lock_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
    try:
        entries = os.listdir(lock_dir)
    except OSError:
        return 0, 0

    now = time.time()
    reaped = 0
    skipped = 0
    for name in entries:
        if not name.endswith(".lock") or name.startswith("mine_palace_"):
            continue
        lock_path = os.path.join(lock_dir, name)
        try:
            if now - os.path.getmtime(lock_path) < min_age_seconds:
                continue
        except OSError:
            continue
        _cleanup_mine_lock_file(lock_path)
        if os.path.exists(lock_path):
            skipped += 1
        else:
            reaped += 1
    return reaped, skipped


_LOCK_REAP_INTERVAL_SECONDS = 900  # 15 minutes between opportunistic sweeps


def _maybe_reap_stale_mine_locks() -> None:
    """Throttled, opportunistic call site for :func:`reap_stale_mine_locks`.

    Runs at most once per ``_LOCK_REAP_INTERVAL_SECONDS``, piggybacking on
    the natural cadence of mine operations rather than requiring a
    background thread, a scheduled task, or any new CLI surface. Failures
    are swallowed — lock maintenance must never be allowed to break an
    actual mine.
    """
    lock_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
    marker = os.path.join(lock_dir, ".last_reap")
    try:
        if (
            os.path.exists(marker)
            and time.time() - os.path.getmtime(marker) < _LOCK_REAP_INTERVAL_SECONDS
        ):
            return
        os.makedirs(lock_dir, exist_ok=True)
        open(marker, "a").close()
        os.utime(marker, None)
        reap_stale_mine_locks()
    except Exception:
        logger.debug("Opportunistic mine-lock reap failed", exc_info=True)
