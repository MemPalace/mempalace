"""Lock-safe SQLite header probes for the file-backed backends.

``detect()`` on the file-backed backends verifies the SQLite header before
claiming a palace directory. It runs on essentially every MCP tool
call (``resolve_backend_name`` -> ``detect_backends_for_path``) and used to do
a plain ``open()`` on the very database file the backend already holds a
long-lived WAL connection to. ``SQLiteExactBackend._database_signature``
did the same on the read-only query path.

POSIX fcntl advisory locks belong to the ``(process, inode)`` pair, not to a
descriptor: closing ANY descriptor on that inode drops EVERY lock the process
holds on it. SQLite's unix VFS carries the ``unixInodeInfo`` / ``pUnused``
machinery precisely so that closing one of *its own* connections cannot do
this to a sibling connection, but a descriptor opened outside SQLite is not
covered. A WAL-mode connection keeps a SHARED lock on the main database for
its whole life, and that lock is the only thing stopping another process's
clean ``close()`` from checkpointing and unlinking the ``-wal`` / ``-shm``
sidecars underneath it. After one plain open+close the server was
lock-naked: any external ``connect()`` + ``close()`` orphaned the
sidecars while the server kept writing (successfully, as far as it could
tell) into the unlinked inodes.

The fix is the one SQLite documents: every read of the database file goes
through SQLite itself. A throwaway ``mode=ro&immutable=1`` connection takes
no locks and creates no sidecars. For ``SQLiteExactBackend``, which shares
Python's ``libsqlite3`` instance, when the throwaway connection closes the unix
VFS parks its descriptor in ``pUnused`` instead of closing it while the writer
connection holds locks on the inode.

However, ChromaDB (1.5+) embeds its own statically linked SQLite inside native
Rust bindings (``chromadb_rust_bindings``), with an entirely separate VFS state
and inode table from Python's ``sqlite3`` module. Closing a throwaway connection
from Python on a live Chroma database issues a libc ``close()``, causing the
Linux kernel to drop Chroma's writer lock. To prevent this on POSIX,
``has_sqlite_magic()`` retains the read-only probe connection for the lifetime
of the process so its descriptor is never closed while the process is alive.
On Windows, where file locking is handle-based and closing handles never drops
sibling locks, the probe connection is closed normally to avoid holding open
file handles that prevent directory deletion. ``os.stat`` opens no descriptor,
so the cheap checks around the probe are lock-safe everywhere.

``has_sqlite_magic`` caches POSITIVE results by ``(st_dev, st_ino)``: once a
file is known to be a SQLite database it stays one until it is replaced
(new inode). Negative results are never cached, because a 0-byte file left
behind by a bare ``connect()`` becomes a real database as soon as the
header is written.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from typing import Optional

from mempalace.sqlite_crypto import connect

from ..config import sqlite_read_uri

SQLITE_MAGIC = b"SQLite format 3\x00"

# Header fields SQLite exposes through pragmas. Together with the stat
# identity/size/times they replace the raw 100-byte header that
# ``_database_signature`` used to read through a plain descriptor.
_HEADER_PRAGMAS = (
    "schema_version",
    "page_count",
    "freelist_count",
    "user_version",
    "application_id",
)

_lock = threading.Lock()
_positive: dict[tuple[int, int], bool] = {}
_retained_conns: dict[tuple[int, int], sqlite3.Connection] = {}
# Key by inode, just like the cache, so aliases cannot create competing probes.
# Never wait for a probe lock while holding the dictionary lock.
_probe_locks: dict[tuple[int, int], threading.Lock] = {}
_generation = 0
# Do not use or close inherited SQLite connections in a forked child.
_inherited_conns: list[sqlite3.Connection] = []


def _stat_regular_file(db_path: str) -> Optional[os.stat_result]:
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return st


def read_header_fields(db_path: str, *, extra: Optional[tuple] = None) -> Optional[tuple]:
    """Return SQLite header fields of ``db_path`` without a plain descriptor.

    Reads page 1 through a throwaway ``immutable=1`` connection, which is the
    only lock-safe way to look at a database file another connection in this
    process may hold POSIX locks on. Returns ``None`` when the file is empty
    or is not a SQLite database. Raises ``OSError`` when the file cannot be
    stat'ed, matching the old ``open()``-based readers.

    ``extra``, when given, is ``(sql, params)`` executed on that same
    connection. The first column of the first row is appended to the tuple.
    A missing row or a failed query appends None and still returns the header
    fields: a leaf commit does not change those pragmas, so callers probe a
    generation row here instead of opening a second connection.
    """
    st = os.stat(db_path)
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        # A 0-byte file is a valid *empty* database to SQLite, but for palace
        # detection it is a bare-connect artifact, not a palace.
        return None
    uri = sqlite_read_uri(db_path) + "&immutable=1"
    try:
        conn = connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        fields = tuple(
            int(conn.execute(f"PRAGMA {name}").fetchone()[0]) for name in _HEADER_PRAGMAS
        )
        if extra is None:
            return fields
        sql, params = extra
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.Error:
            row = None
        return (*fields, None if row is None else row[0])
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def has_sqlite_magic(db_path: str) -> bool:
    """Return True when ``db_path`` is a regular file starting with the SQLite magic.

    Opens a plain descriptor only on files SQLite itself refuses to open (which
    no connection in this process can be holding), and opens nothing at all on
    a path already confirmed in this process (keyed by inode).
    """
    st = _stat_regular_file(db_path)
    if st is None:
        return False
    key = (st.st_dev, st.st_ino)
    with _lock:
        if _positive.get(key):
            return True
        probe_lock = _probe_locks.setdefault(key, threading.Lock())

    with probe_lock:
        with _lock:
            if _positive.get(key):
                return True
            retained = _retained_conns.get(key)
            generation = _generation

        if retained is not None:
            try:
                retained.execute("PRAGMA schema_version").fetchone()
                with _lock:
                    if generation == _generation:
                        _positive[key] = True
                return True
            except sqlite3.Error:
                with _lock:
                    _retained_conns.pop(key, None)

        if st.st_size == 0:
            return False

        # Check, create and retain under this inode's lock. A cold probe must
        # reuse this connection: replacing it would leave an orphan whose GC
        # close can drop a native writer's process-wide POSIX locks.
        uri = sqlite_read_uri(db_path) + "&immutable=1"
        try:
            # Retained probes may be closed by teardown after their creating
            # thread has finished. Reads are serialized by the probe lock.
            conn = connect(uri, uri=True, check_same_thread=False)
        except sqlite3.Error:
            conn = None

        if conn is not None:
            try:
                conn.execute("PRAGMA schema_version").fetchone()
                if os.name == "posix":
                    with _lock:
                        _retained_conns[key] = conn
                else:
                    conn.close()
                with _lock:
                    if generation == _generation:
                        _positive[key] = True
                return True
            except sqlite3.Error:
                conn.close()

        # SQLite refused the file (empty, truncated, garbage). Nothing SQLite
        # cannot open can be held open by a SQLite connection in this process,
        # so a plain prefix read cannot drop anybody's locks here. Preserve
        # the 16-byte-prefix semantics for markers without a valid page 1.
        if st.st_size < len(SQLITE_MAGIC):
            return False
        try:
            with open(db_path, "rb") as f:
                return f.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
        except OSError:
            return False


def forget(db_path: Optional[str] = None) -> None:
    """Drop cached detections (all of them when ``db_path`` is None).

    Test/maintenance hook; the inode key makes it unnecessary in normal use.
    """
    global _generation
    st = None if db_path is None else _stat_regular_file(db_path)
    with _lock:
        # A probe already in flight may retain its connection but must not
        # publish a detection from before this invalidation.
        _generation += 1
        if st is None:
            _positive.clear()
            return
        _positive.pop((st.st_dev, st.st_ino), None)


def close_retained() -> None:
    """Close all retained probe connections. Test/maintenance hook only."""
    with _lock:
        keys = _probe_locks.keys() | _retained_conns.keys()
        probes = [(key, _probe_locks.setdefault(key, threading.Lock())) for key in keys]
    for key, probe_lock in probes:
        with probe_lock:
            with _lock:
                conn = _retained_conns.get(key)
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
                with _lock:
                    _retained_conns.pop(key, None)


def _reset_after_fork_in_child() -> None:
    global _lock, _probe_locks, _positive, _retained_conns, _generation
    # A parent thread may have held either kind of lock or used a connection
    # at fork. Start fresh without acquiring those locks or closing SQLite.
    _inherited_conns.extend(_retained_conns.values())
    _lock = threading.Lock()
    _probe_locks = {}
    _positive = {}
    _retained_conns = {}
    _generation = 0


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork_in_child)
