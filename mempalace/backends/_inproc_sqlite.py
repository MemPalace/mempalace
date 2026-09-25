"""Python ``sqlite3`` access to a database another SQLite library in this process holds.

ChromaDB 1.5+ opens ``chroma.sqlite3`` through the SQLite statically linked
into ``chromadb_rust_bindings``. mempalace's fast paths (status, taxonomy,
``list_drawers``, BM25, the integrity gate, repair) open the same file through
Python's ``sqlite3``: a second, independent SQLite library in the same
process. Two consequences, both measured on Linux (#2302):

1. POSIX fcntl locks belong to the (process, inode) pair. When a Python
   connection closes its descriptor, the kernel drops every lock the process
   holds on that file, Chroma's included: the SHARED lock a WAL connection
   keeps for its whole life and the DMS lock on ``-shm``. SQLite's unix VFS
   parks a descriptor instead of closing it only while a connection of *its
   own* library holds locks on the inode. After a single fast-path read Chroma
   never takes those locks back, so the palace stays lock-naked to every
   other process: a newcomer takes the "first opener" path and truncates
   ``-shm``, or a clean close checkpoints and unlinks ``-wal``/``-shm`` under
   the live writer.
2. fcntl locks never conflict within one process, so neither library sees the
   other's. A ``quick_check`` running while Chroma commits reads a torn page
   set (``database disk image is malformed``), and a close in that window
   also releases the write transaction's locks.

This module is the one door for that access.

* :func:`palace_db_lock` is a process-local re-entrant lock per database file.
  Every connection opened here holds it from open to close, and
  ``ChromaBackend`` holds it around its writes and client opens, so the two
  libraries never overlap inside the process. That fixes (2) everywhere.
* On Linux, before a reader opens, an idle *anchor* connection is kept on the
  database inode for the life of the process. It reads nothing but the schema
  cookie, which in WAL mode leaves it holding SHARED and the wal-index mapping,
  so Python's SQLite parks the descriptor of every per-call connection that
  closes afterwards instead of closing it, and Chroma keeps its locks (1).
  The anchor also holds open-file-description (OFD) read locks on SQLite's
  SHARED range and on the ``-shm`` DMS byte. OFD locks belong to a descriptor
  rather than to the process: no other close releases them, and they conflict
  with the process-associated locks of this very process. Without them a
  long-lived Python connection is worse than none. Chroma's last close takes
  EXCLUSIVE straight past the anchor's in-process locks, the next open
  recreates ``-wal``/``-shm``, and every Python connection stays attached to
  the stale wal-index, blind to later commits (measured: a fresh connection saw
  1 of 3 committed rows). With the guards held, nobody can take EXCLUSIVE or
  the DMS write lock while the anchor lives, exactly as if another process held
  a WAL reader. In rollback-journal mode the anchor holds nothing between reads
  and Chroma holds nothing between transactions, so the lock covers the close.
* The reads themselves always use a fresh connection. A long-lived one is not a
  substitute: after Chroma's commits, Python's SQLite (3.45.1 measured) kept
  reporting ``malformed inverted index for FTS5 table`` from it, in either
  journal mode and after Chroma had closed, while fresh connections and a later
  ``integrity_check`` said ``ok``.
* Everywhere else connections open and close per call, still under the lock.
  Windows locks belong to a handle, so a close never drops another handle's
  locks. POSIX platforms without OFD locks (macOS) keep the cross-process
  exposure described in (1).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import struct
import sys
import threading
import time
from typing import Callable, Optional

from ..config import connect_sqlite_read

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

# os_unix.c: PENDING_BYTE = 0x40000000, SHARED_FIRST = PENDING_BYTE + 2,
# SHARED_SIZE = 510. The -shm lock bytes start at UNIX_SHM_BASE = 120 and the
# DMS byte follows the SQLITE_SHM_NLOCK = 8 WAL lock slots.
_SHARED_FIRST = 0x40000000 + 2
_SHARED_SIZE = 510
_SHM_DMS = 120 + 8

_F_OFD_SETLK = (
    getattr(fcntl, "F_OFD_SETLK", None)
    if fcntl is not None and sys.platform.startswith("linux")
    else None
)

# Whether readers keep an anchor on the database. Module-level so tests can pin
# either path.
_ANCHORED = _F_OFD_SETLK is not None

# A guard that conflicts is somebody's short EXCLUSIVE or DMS recovery. Retry
# briefly, then serve the read and try again on the next one.
_GUARD_ATTEMPTS = 3
_GUARD_RETRY_SECONDS = 0.002

_registry_lock = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_anchors: dict[str, "_Anchor"] = {}


def _key(db_path: str) -> str:
    return os.path.normcase(os.path.realpath(db_path))


def _lock_for_key(key: str) -> threading.RLock:
    with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def palace_db_lock(db_path) -> threading.RLock:
    """Return the process-local lock that serializes all access to ``db_path``.

    Re-entrant, so a thread that already holds it (a Chroma client open that
    runs a migration helper, say) can take it again.
    """
    return _lock_for_key(_key(os.fspath(db_path)))


class PalaceSqliteConnection:
    """A ``sqlite3.Connection`` stand-in that holds :func:`palace_db_lock`.

    Everything but ``close`` and the context-manager protocol is forwarded to
    the real connection. ``close()`` closes the connection and releases the
    lock; calling it again does nothing. Leaving a ``with`` block commits or
    rolls back as ``sqlite3`` does and then closes.
    """

    __slots__ = ("_conn", "_release", "_closed")

    def __init__(self, conn: sqlite3.Connection, release: Callable[[], None]):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_release", release)
        object.__setattr__(self, "_closed", False)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        try:
            return self._conn.__exit__(*exc_info)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        self._release()


class _Anchor:
    __slots__ = (
        "conn",
        "ident",
        "db_fd",
        "shm_fd",
        "db_guarded",
        "shm_guarded",
        "shm_ino",
    )

    def __init__(self, conn: sqlite3.Connection, ident: tuple[int, int]):
        self.conn = conn
        self.ident = ident
        self.db_fd: Optional[int] = None
        self.shm_fd: Optional[int] = None
        self.db_guarded = False
        self.shm_guarded = False
        self.shm_ino: Optional[int] = None

    def stale(self, db_path: str) -> bool:
        """True when the wal-index this anchor mapped is no longer on disk."""
        if self.shm_ino is None:
            return False
        try:
            return os.stat(db_path + "-shm").st_ino != self.shm_ino
        except OSError:
            return True

    def discard(self) -> None:
        # Closing any of these descriptors drops this process's POSIX locks on
        # the file. Only called for a replaced inode, a stale wal-index, or by
        # release()/release_all().
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
        for fd in (self.db_fd, self.shm_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _open_for_guard(path: str) -> Optional[int]:
    try:
        return os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None


def _ofd_read_lock(fd: Optional[int], start: int, length: int) -> bool:
    if fd is None:
        return False
    # struct flock; l_pid must be 0 for OFD requests.
    request = struct.pack("@hhqqi", fcntl.F_RDLCK, os.SEEK_SET, start, length, 0)
    for attempt in range(_GUARD_ATTEMPTS):
        try:
            fcntl.fcntl(fd, _F_OFD_SETLK, request)
            return True
        except OSError:
            if attempt + 1 < _GUARD_ATTEMPTS:
                time.sleep(_GUARD_RETRY_SECONDS)
    return False


def _guard(anchor: _Anchor, db_path: str) -> None:
    """Take the OFD guards once the database is in WAL mode.

    Descriptors opened for the guards are kept even when a lock cannot be taken
    yet: closing one would drop the very locks this module protects.
    """
    if anchor.db_guarded and anchor.shm_guarded:
        return
    if not os.path.exists(db_path + "-wal"):
        return
    row = anchor.conn.execute("PRAGMA journal_mode").fetchone()
    if not row or str(row[0]).lower() != "wal":
        return
    if anchor.shm_ino is None:
        try:
            anchor.shm_ino = os.stat(db_path + "-shm").st_ino
        except OSError:
            pass
    if not anchor.db_guarded:
        if anchor.db_fd is None:
            anchor.db_fd = _open_for_guard(db_path)
        anchor.db_guarded = _ofd_read_lock(anchor.db_fd, _SHARED_FIRST, _SHARED_SIZE)
    if not anchor.shm_guarded:
        if anchor.shm_fd is None:
            anchor.shm_fd = _open_for_guard(db_path + "-shm")
        anchor.shm_guarded = _ofd_read_lock(anchor.shm_fd, _SHM_DMS, 1)
        if anchor.shm_guarded:
            anchor.shm_ino = os.fstat(anchor.shm_fd).st_ino
    if not (anchor.db_guarded and anchor.shm_guarded):
        logger.debug("OFD guard for %s not taken yet; retrying on the next read", db_path)


def _ensure_anchor(db_path: str, key: str) -> None:
    """Keep an anchor on ``db_path`` before a per-call connection opens it.

    Caller holds the key's lock. Best effort: a database the anchor cannot read
    is reported by the per-call open that follows, and a busy one is anchored on
    a later read.
    """
    try:
        st = os.stat(db_path)
    except (OSError, ValueError):
        return
    if not stat.S_ISREG(st.st_mode):
        return
    ident = (st.st_dev, st.st_ino)

    anchor = _anchors.get(key)
    if anchor is not None and (anchor.ident != ident or anchor.stale(db_path)):
        if anchor.ident == ident:
            logger.warning("wal-index of %s was replaced under its anchor; re-anchoring", db_path)
        _anchors.pop(key, None)
        anchor.discard()
        anchor = None

    if anchor is None:
        try:
            conn = connect_sqlite_read(db_path, timeout=0.0, check_same_thread=False)
        except (sqlite3.Error, ValueError):
            return
        try:
            conn.execute("PRAGMA schema_version").fetchone()
        except sqlite3.Error:
            conn.close()
            return
        anchor = _anchors[key] = _Anchor(conn, ident)

    try:
        _guard(anchor, db_path)
    except sqlite3.Error:
        logger.debug("OFD guard for %s failed", db_path, exc_info=True)


def _close_and_release(conn: sqlite3.Connection, lock: threading.RLock) -> None:
    try:
        conn.close()
    finally:
        lock.release()


def open_reader(db_path, *, timeout: Optional[float] = None) -> PalaceSqliteConnection:
    """Open ``db_path`` for reading under :func:`palace_db_lock`.

    Same failure contract as :func:`mempalace.config.connect_sqlite_read`
    (``sqlite3.Error`` for a database SQLite cannot open, ``ValueError`` for a
    path the URI cannot carry). The caller must ``close()`` the result, which
    releases the lock.
    """
    db_path = os.fspath(db_path)
    key = _key(db_path)
    lock = _lock_for_key(key)
    lock.acquire()
    try:
        if _ANCHORED:
            _ensure_anchor(db_path, key)
        kwargs = {} if timeout is None else {"timeout": timeout}
        conn = connect_sqlite_read(db_path, **kwargs)
    except BaseException:
        lock.release()
        raise
    return PalaceSqliteConnection(conn, lambda: _close_and_release(conn, lock))


def open_writer(db_path, **connect_kwargs) -> PalaceSqliteConnection:
    """``sqlite3.connect(db_path, **connect_kwargs)`` under :func:`palace_db_lock`.

    For maintenance writes (migrations, FTS rebuilds, repair) while Chroma's
    handles are closed or not yet open; the lock keeps this process's own Chroma
    writes out while the connection is open. The caller must close it.
    """
    db_path = os.fspath(db_path)
    lock = _lock_for_key(_key(db_path))
    lock.acquire()
    try:
        conn = sqlite3.connect(db_path, **connect_kwargs)
    except BaseException:
        lock.release()
        raise
    return PalaceSqliteConnection(conn, lambda: _close_and_release(conn, lock))


def release(db_path) -> None:
    """Close the anchor on ``db_path`` and drop its guards.

    For maintenance that needs the file to itself (VACUUM) once Chroma's handles
    are closed: closing drops this process's POSIX locks on the file.
    """
    db_path = os.fspath(db_path)
    key = _key(db_path)
    with _lock_for_key(key):
        anchor = _anchors.pop(key, None)
        if anchor is not None:
            anchor.discard()


def release_all() -> None:
    """Close every anchor. Tests and shutdown only (see :func:`release`)."""
    with _registry_lock:
        anchors = list(_anchors.values())
        _anchors.clear()
    for anchor in anchors:
        anchor.discard()


def _reset_after_fork_in_child() -> None:
    # SQLite connections must not cross fork(); the child starts empty and
    # leaves the inherited descriptors alone (closing them would drop locks).
    global _registry_lock, _locks, _anchors
    _registry_lock = threading.Lock()
    _locks = {}
    _anchors = {}


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork_in_child)
