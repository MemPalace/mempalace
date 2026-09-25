"""Python ``sqlite3`` access to a database another SQLite library in this process holds.

ChromaDB 1.5+ opens ``chroma.sqlite3`` through the SQLite statically linked
into ``chromadb_rust_bindings``. mempalace's fast paths (status, taxonomy,
``list_drawers``, BM25, the integrity gate, repair) open the same file through
Python's ``sqlite3``: a second, independent SQLite library in the same
process. Two consequences, both measured on Linux:

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
  locks. POSIX platforms without OFD locks (macOS) keep the same promise with
  a short-lived helper process: its read transaction is another process's
  SHARED lock, so it conflicts with Chroma where an in-process fcntl lock
  would not, and closing a connection in this process cannot drop it. The
  in-process anchor is installed only after that helper is holding, so
  Chroma cannot recreate ``-wal``/``-shm`` under a parked wal-index.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import struct
import subprocess
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
# either path. OFD locks are what make that anchor safe against Chroma
# replacing the wal-index; without them the anchor waits for the helper below.
_ANCHORED = _F_OFD_SETLK is not None

# POSIX without OFD locks cannot park a descriptor without also letting Chroma
# recreate the wal-index. A helper process holds the cross-process SHARED lock
# instead. Tests pin this off when they need the plain open.
_HOLD_LOCKS = os.name == "posix" and not _ANCHORED

# A guard that conflicts is somebody's short EXCLUSIVE or DMS recovery. Retry
# briefly, then serve the read and try again on the next one.
_GUARD_ATTEMPTS = 3
_GUARD_RETRY_SECONDS = 0.002
_HOLDER_READY_SECONDS = 2.0

_registry_lock = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_anchors: dict[str, "_Anchor"] = {}
_holders: dict[str, "_Holder"] = {}

# Self-contained: the helper must not import this package. Importing
# mempalace.backends pulls Chroma into the process whose only job is to hold
# a read transaction open.
_HOLD_SCRIPT = """
import os, sqlite3, sys
from urllib.request import pathname2url

db_path = sys.argv[1]
uri = "file:" + pathname2url(db_path) + "?mode=ro"

def open_conn():
    # Same sidecar rule as connect_sqlite_read. A WAL file with no sidecars
    # cannot be opened mode=ro on Apple's SQLite; every other file can.
    if not os.path.exists(db_path + "-wal") and not os.path.exists(db_path + "-shm"):
        try:
            with open(db_path, "rb") as handle:
                header = handle.read(19)
        except OSError:
            header = b""
        if (
            len(header) == 19
            and header[:16] == b"SQLite format 3\\x00"
            and header[18] == 2
        ):
            return sqlite3.connect(db_path, timeout=0.0)
    return sqlite3.connect(uri, uri=True, timeout=0.0)

try:
    conn = open_conn()
except sqlite3.Error:
    sys.stdout.write("failed\\n")
    sys.stdout.flush()
    raise SystemExit(0)

def prime():
    # A deferred transaction takes no lock until it reads. schema_version in
    # autocommit locks and releases immediately, which leaves the helper holding
    # nothing. Read inside the transaction so SHARED (and, in WAL mode, the
    # -shm DMS lock) stays until this process exits.
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()

def report(line):
    sys.stdout.write(line + "\\n")
    sys.stdout.flush()

try:
    prime()
except sqlite3.Error:
    report("busy")
else:
    report("ready")
    sys.stdin.read()
    raise SystemExit(0)

while True:
    if sys.stdin.readline() == "":
        raise SystemExit(0)
    try:
        prime()
    except sqlite3.Error:
        report("busy")
    else:
        report("ready")
        sys.stdin.read()
        raise SystemExit(0)
"""


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
        "primed",
    )

    def __init__(self, conn: sqlite3.Connection, ident: tuple[int, int]):
        self.conn = conn
        self.ident = ident
        self.db_fd: Optional[int] = None
        self.shm_fd: Optional[int] = None
        self.db_guarded = False
        self.shm_guarded = False
        self.shm_ino: Optional[int] = None
        # False until schema_version has succeeded. A failed prime must not
        # close the connection: that close drops this process's POSIX locks.
        self.primed = False

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


class _Holder:
    """A child process whose read transaction outlives this process's closes."""

    __slots__ = ("proc", "ident", "ready", "shm_ino")

    def __init__(self, proc: subprocess.Popen, ident: tuple[int, int]):
        self.proc = proc
        self.ident = ident
        self.ready = False
        self.shm_ino: Optional[int] = None

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stale(self, db_path: str) -> bool:
        if self.shm_ino is None:
            return False
        try:
            return os.stat(db_path + "-shm").st_ino != self.shm_ino
        except OSError:
            return True


def _read_holder_line(proc: subprocess.Popen) -> str:
    """One stdout line from the helper, or ``""`` when it does not answer."""
    box: list = []

    def _read() -> None:
        stream = proc.stdout
        if stream is None:
            box.append("")
            return
        try:
            box.append(stream.readline())
        except Exception:
            box.append("")

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    thread.join(_HOLDER_READY_SECONDS)
    if not box:
        return ""
    return str(box[0] or "").strip()


def _stop_holder(holder: _Holder, *, wait: bool) -> None:
    proc = holder.proc
    stdin = proc.stdin
    if stdin is not None and not stdin.closed:
        try:
            stdin.close()
        except OSError:
            pass
    if not wait:
        return
    try:
        proc.wait(timeout=_HOLDER_READY_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=_HOLDER_READY_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _start_holder(db_path: str, ident: tuple[int, int]) -> Optional[_Holder]:
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLD_SCRIPT, db_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        logger.debug("could not start the lock holder for %s", db_path, exc_info=True)
        return None
    holder = _Holder(proc, ident)
    status = _read_holder_line(proc)
    if status == "ready":
        holder.ready = True
        try:
            holder.shm_ino = os.stat(db_path + "-shm").st_ino
        except OSError:
            holder.shm_ino = None
        return holder
    if status == "busy" and proc.poll() is None:
        # The open already touched the database. Leave the process up so its
        # close cannot run, and ask it to prime again on the next read.
        return holder
    _stop_holder(holder, wait=True)
    return None


def _ensure_holder(db_path: str, key: str) -> None:
    """Hold SHARED from another process before this process opens the file.

    Caller holds the key's lock. A holder that is not ready yet is kept: the
    next read nudges it. Replacing the database or its wal-index starts a new
    holder, which is allowed to exit because the inode it locked is gone.
    """
    try:
        st = os.stat(db_path)
    except (OSError, ValueError):
        return
    if not stat.S_ISREG(st.st_mode):
        return
    ident = (st.st_dev, st.st_ino)
    holder = _holders.get(key)
    if holder is not None and (
        not holder.alive() or holder.ident != ident or holder.stale(db_path)
    ):
        _holders.pop(key, None)
        _stop_holder(holder, wait=True)
        holder = None
    if holder is None:
        started = _start_holder(db_path, ident)
        if started is None:
            return
        _holders[key] = started
        return
    if holder.ready or holder.proc.stdin is None:
        return
    try:
        holder.proc.stdin.write("\n")
        holder.proc.stdin.flush()
    except OSError:
        return
    if _read_holder_line(holder.proc) == "ready":
        holder.ready = True
        try:
            holder.shm_ino = os.stat(db_path + "-shm").st_ino
        except OSError:
            holder.shm_ino = None


def _ensure_anchor(db_path: str, key: str) -> None:
    """Keep an anchor on ``db_path`` before a per-call connection opens it.

    Caller holds the key's lock. Best effort: a database the anchor cannot read
    is reported by the per-call open that follows, and a busy one stays open
    and is primed on a later read. Closing that connection is what drops this
    process's POSIX locks, so a failed prime never closes it.
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
        anchor = _anchors[key] = _Anchor(conn, ident)

    if not anchor.primed:
        try:
            anchor.conn.execute("PRAGMA schema_version").fetchone()
        except sqlite3.Error:
            return
        anchor.primed = True

    if _F_OFD_SETLK is None:
        return
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
        if _HOLD_LOCKS:
            _ensure_holder(db_path, key)
        # The in-process anchor parks descriptors so a per-call close does not
        # drop Chroma's locks. Without OFD locks that anchor is only safe once
        # another process is already holding SHARED, which stops Chroma from
        # recreating the wal-index underneath it.
        holder = _holders.get(key)
        if _ANCHORED or (holder is not None and holder.ready):
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
        holder = _holders.pop(key, None)
        if holder is not None:
            _stop_holder(holder, wait=True)


def release_all() -> None:
    """Close every anchor and lock holder. Tests and shutdown only (see :func:`release`)."""
    with _registry_lock:
        anchors = list(_anchors.values())
        holders = list(_holders.values())
        _anchors.clear()
        _holders.clear()
    for anchor in anchors:
        anchor.discard()
    for holder in holders:
        _stop_holder(holder, wait=True)


def _reset_after_fork_in_child() -> None:
    # SQLite connections must not cross fork(); the child starts empty and
    # leaves the inherited descriptors alone (closing them would drop locks).
    # The helper belongs to the parent: drop this process's pipe dup and do
    # not signal the helper to exit.
    global _registry_lock, _locks, _anchors, _holders
    holders = list(_holders.values())
    _registry_lock = threading.Lock()
    _locks = {}
    _anchors = {}
    _holders = {}
    for holder in holders:
        _stop_holder(holder, wait=False)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork_in_child)
