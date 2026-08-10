"""Tests for the repair-vs-mine-lock stranding fix.

``mempalace repair --mode from-sqlite --archive-existing`` used to rename
the existing palace aside (``palace.pre-rebuild-…``) and only *then* hit the
single-writer ``mine_palace_lock`` when the first chromadb upsert ran — so a
palace held by a live MCP server / daemon was stranded: archived, with no
rebuilt replacement and a partial dest left behind.

The fix takes ``mine_palace_lock(dest_palace)`` BEFORE the archive/rename, so
contention raises ``MineAlreadyRunning`` while the palace is still untouched.

POSIX-only: ``mine_palace_lock`` uses ``fcntl`` on Unix and ``msvcrt`` on
Windows; the cross-process contention helper mirrors the other lock tests.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import time

import pytest

from mempalace.palace import MineAlreadyRunning, mine_palace_lock

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="cross-process lock contention semantics differ on Windows",
)


def _get_mp_context():
    # ``spawn`` everywhere — ``fork`` deadlocks a multi-threaded parent under
    # 3.13 and macOS forbids fork-without-exec. Mirrors test_palace_locks.py.
    return multiprocessing.get_context("spawn")


def _hold_lock(palace_path: str, ready_flag: str, release_flag: str) -> int:
    """Acquire ``mine_palace_lock``, signal readiness, wait for release."""
    try:
        with mine_palace_lock(palace_path):
            open(ready_flag, "w").close()
            for _ in range(500):
                if os.path.exists(release_flag):
                    return 0
                time.sleep(0.01)
            return 0
    except MineAlreadyRunning:
        return 1


def _wait_for(path: str) -> bool:
    for _ in range(500):
        if os.path.exists(path):
            return True
        time.sleep(0.01)
    return False


def test_rebuild_refuses_when_lock_held_leaves_palace_untouched(tmp_path, monkeypatch):
    """A held mine-lock makes rebuild_from_sqlite fail CLEAN before archiving.

    Asserts the three stranding-bug invariants: ``MineAlreadyRunning`` is
    raised, NO ``*.pre-rebuild-*`` archive sibling is created, and the
    original palace dir is left exactly as it was (its marker file intact).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    from mempalace.repair import rebuild_from_sqlite

    palace = tmp_path / "palace"
    palace.mkdir()
    # Satisfy rebuild_from_sqlite's source validation (in_place branch checks
    # for chroma.sqlite3) so execution reaches the lock acquisition. The lock
    # check fires before any chromadb read, so the file content is irrelevant.
    marker = palace / "chroma.sqlite3"
    marker.write_bytes(b"sentinel-not-touched")

    ready = str(tmp_path / "ready")
    release = str(tmp_path / "release")
    ctx = _get_mp_context()
    holder = ctx.Process(target=_hold_lock, args=(str(palace), ready, release))
    holder.start()
    try:
        assert _wait_for(ready), "holder failed to acquire the palace lock"

        with pytest.raises(MineAlreadyRunning):
            rebuild_from_sqlite(
                source_palace=str(palace),
                dest_palace=str(palace),
                archive_existing_dest=True,
            )

        # No archive sibling was created (the bug renamed it aside first).
        archives = list(tmp_path.glob("palace.pre-rebuild-*"))
        assert archives == [], f"palace was stranded into archive(s): {archives}"

        # The palace dir is untouched: still present with its original file.
        assert palace.is_dir()
        assert marker.read_bytes() == b"sentinel-not-touched"
    finally:
        open(release, "w").close()
        holder.join(timeout=5)
