"""Plaintext snapshots may take longer than 30 seconds while making progress."""

import itertools
import sqlite3
from contextlib import closing
from functools import partial
from types import SimpleNamespace

import pytest

import commonplace.backup as backup_module
from commonplace.backup import BackupError, _snapshot


def test_large_snapshot_is_not_cut_off_by_total_time(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "snapshot.sqlite3"
    row_count = 8000
    with closing(sqlite3.connect(source)) as conn:
        # Small pages give this small database several 4096-page backup steps.
        conn.execute("PRAGMA page_size = 512")
        conn.execute("CREATE TABLE memory (content BLOB)")
        conn.executemany("INSERT INTO memory VALUES (zeroblob(2048))", [()] * row_count)
        conn.commit()
        assert conn.execute("PRAGMA page_count").fetchone()[0] > 4 * 4096

    clock = itertools.count(0, 10)
    monkeypatch.setattr(backup_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    error = None
    try:
        _snapshot(source, target)
    except BackupError as exc:
        error = str(exc)
    assert error is None, error  # RED

    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT count(*) FROM memory").fetchone() == (row_count,)


def _fake_backup(monkeypatch, tmp_path, sequence):
    source = tmp_path / "source.sqlite3"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("CREATE TABLE memory (content BLOB)")

    class FakeConnection(sqlite3.Connection):
        def backup(self, target, *, pages, progress, sleep):
            for remaining in sequence:
                progress(sqlite3.SQLITE_OK, remaining, 10)
            pytest.fail("snapshot without new progress did not time out")

    clock = itertools.count(0, 10)
    monkeypatch.setattr(backup_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(
        backup_module.sqlite3, "connect", partial(sqlite3.connect, factory=FakeConnection)
    )
    return source


def test_stalled_snapshot_still_times_out(tmp_path, monkeypatch):
    source = _fake_backup(monkeypatch, tmp_path, [10] * 8)
    with pytest.raises(BackupError, match="database backup timed out: source.sqlite3"):
        _snapshot(source, tmp_path / "snapshot.sqlite3")


def test_restarting_snapshot_without_new_low_times_out(tmp_path, monkeypatch):
    # A copy that keeps restarting drops "remaining" each cycle but never below
    # its earlier low; it must still time out instead of running forever.
    source = _fake_backup(monkeypatch, tmp_path, [10, 9] * 6)
    with pytest.raises(BackupError, match="database backup timed out: source.sqlite3"):
        _snapshot(source, tmp_path / "snapshot.sqlite3")
