"""Shared helpers: create minimal valid palace-marker SQLite files for tests.

Many tests want to stand up "a chroma / sqlite_exact palace" cheaply —
historically they did this with ``(path / "<marker>.sqlite3").touch()`` or
``write_bytes(b"")``, relying on the backend ``detect()`` methods' old
``os.path.isfile()`` semantics. Post-#1893, both ``ChromaBackend.detect()``
and ``SQLiteExactBackend.detect()`` require a valid SQLite magic header, so
the empty stand-in no longer registers. These helpers create the minimum
required to make detection fire without standing up a full palace.

This module is intentionally not a ``test_*`` file: it ships utilities, not
tests.
"""

import sqlite3
from pathlib import Path
from typing import Union


def _write_minimal_sqlite_file(db_path: Path) -> None:
    """Write a valid SQLite magic header at ``db_path``.

    Writing any statement is sufficient to land the 16-byte
    ``SQLite format 3\\x00`` magic prefix that the backend ``detect()``
    methods check.
    """

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE _detect_smoke(x)")
        conn.commit()
    finally:
        conn.close()


def make_minimal_chroma_sqlite(palace_path: Union[Path, str]) -> Path:
    """Create ``<palace_path>/chroma.sqlite3`` with a valid SQLite header.

    Returns the path to the file. Backs
    :py:meth:`mempalace.backends.chroma.ChromaBackend.detect`.
    """

    db_path = Path(palace_path) / "chroma.sqlite3"
    _write_minimal_sqlite_file(db_path)
    return db_path


def make_minimal_sqlite_exact_sqlite(palace_path: Union[Path, str]) -> Path:
    """Create ``<palace_path>/sqlite_exact.sqlite3`` with a valid SQLite header.

    Returns the path to the file. Backs
    :py:meth:`mempalace.backends.sqlite_exact.SQLiteExactBackend.detect`.
    """

    db_path = Path(palace_path) / "sqlite_exact.sqlite3"
    _write_minimal_sqlite_file(db_path)
    return db_path
