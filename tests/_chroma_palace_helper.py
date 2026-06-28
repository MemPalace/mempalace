"""Shared helper: create a minimal valid `chroma.sqlite3` for tests.

Many tests want to stand up "a chroma palace" cheaply — historically they did
this with ``(path / "chroma.sqlite3").touch()`` or ``write_bytes(b"")``,
relying on ``ChromaBackend.detect()``'s old ``os.path.isfile()`` semantics.
Post-#1893, ``detect()`` requires a valid SQLite magic header, so the empty
stand-in no longer registers. This helper creates the minimum required to
make detection fire without standing up a full chroma collection.

This module is intentionally not a ``test_*`` file: it ships utilities, not
tests.
"""

import sqlite3
from pathlib import Path
from typing import Union


def make_minimal_chroma_sqlite(palace_path: Union[Path, str]) -> Path:
    """Create ``<palace_path>/chroma.sqlite3`` with a valid SQLite header.

    Returns the path to the file. Writing any statement is sufficient to land
    the 16-byte ``SQLite format 3\\x00`` magic prefix that
    :py:meth:`mempalace.backends.chroma.ChromaBackend.detect` checks.
    """

    db_path = Path(palace_path) / "chroma.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE _detect_smoke(x)")
        conn.commit()
    finally:
        conn.close()
    return db_path
