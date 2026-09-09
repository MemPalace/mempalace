"""Lock-safe SQLite header probes for the file-backed backends.

``detect()`` on the file-backed backends verifies the SQLite header before
claiming a palace directory (#1893). It runs on essentially every MCP tool
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
lock-naked: any external ``sqlite3.connect()`` + ``close()`` orphaned the
sidecars while the server kept writing (successfully, as far as it could
tell) into the unlinked inodes. See #2302 for the same kernel mechanism.

The fix is the one SQLite documents: every read of the database file goes
through SQLite itself. A throwaway ``mode=ro&immutable=1`` connection takes
no locks and creates no sidecars, and when it closes the unix VFS parks its
descriptor instead of closing it while a sibling connection still holds a
lock on the inode. ``os.stat`` opens no descriptor, so the cheap checks
around the probe are lock-safe too.

``has_sqlite_magic`` caches POSITIVE results by ``(st_dev, st_ino)``: once a
file is known to be a SQLite database it stays one until it is replaced
(new inode). Negative results are never cached, because a 0-byte file left
behind by a bare ``sqlite3.connect()`` becomes a real database as soon as the
header is written (#1893).
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from typing import Optional

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


def _stat_regular_file(db_path: str) -> Optional[os.stat_result]:
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return st


def read_header_fields(db_path: str) -> Optional[tuple[int, ...]]:
    """Return SQLite header fields of ``db_path`` without a plain descriptor.

    Reads page 1 through a throwaway ``immutable=1`` connection, which is the
    only lock-safe way to look at a database file another connection in this
    process may hold POSIX locks on. Returns ``None`` when the file is empty
    or is not a SQLite database. Raises ``OSError`` when the file cannot be
    stat'ed, matching the old ``open()``-based readers.
    """
    st = os.stat(db_path)
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        # A 0-byte file is a valid *empty* database to SQLite, but for palace
        # detection it is the bare-connect artifact of #1893, not a palace.
        return None
    uri = sqlite_read_uri(db_path) + "&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        return tuple(int(conn.execute(f"PRAGMA {name}").fetchone()[0]) for name in _HEADER_PRAGMAS)
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
    try:
        found = read_header_fields(db_path) is not None
    except OSError:
        return False
    if found:
        with _lock:
            _positive[key] = True
        return True
    # SQLite refused the file (empty, truncated, garbage). Nothing SQLite
    # cannot open can be held open by a SQLite connection in this process, so
    # a plain read of the prefix cannot drop anybody's locks here. This keeps
    # the documented 16-byte-prefix semantics for marker files that carry the
    # magic but no valid page 1.
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
    with _lock:
        if db_path is None:
            _positive.clear()
            return
        st = _stat_regular_file(db_path)
        if st is None:
            _positive.clear()
            return
        _positive.pop((st.st_dev, st.st_ino), None)
