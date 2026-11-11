"""Database snapshots may take longer than 30 seconds while making progress.

Covers the offline copy (``backup._snapshot``) and the online copy the nightly
and deploy backups use (``backup_online._Cut._copy_pinned``).
"""

import itertools
import sqlite3
from contextlib import closing
from functools import partial
from types import SimpleNamespace

import pytest

import commonplace.backup as backup_module
import commonplace.backup_online as online_module
from commonplace.backup import BackupError, _snapshot
from commonplace.backup_online import OnlineBackupError
from commonplace.backup_pause import Knobs


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


# The online copy: one pinned read transaction per database, same stall rule.


def _online_cut(tmp_path, monkeypatch):
    """A cut on a clock that advances 10 s per reading, with no gateway and no run deadline."""
    partial = tmp_path / "partial"
    partial.mkdir()
    clock = itertools.count(0, 10)
    monkeypatch.setattr(
        online_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock), time=lambda: 0.0, sleep=lambda _s: None),
    )
    cut = online_module._Cut(
        tmp_path, partial, online_module._NoGateway(), Knobs(), None, lambda *_a, **_k: None, None
    )
    cut.stage = "copy"
    return cut


def _pin(cut, source, factory=sqlite3.Connection):
    pin = sqlite3.connect(source, isolation_level=None, factory=factory)
    pin.execute("BEGIN")
    pin.execute("SELECT count(*) FROM sqlite_master").fetchone()
    cut.pins[source] = pin


def test_large_online_copy_is_not_cut_off_by_total_time(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    row_count = 8000
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA page_size = 512")
        conn.execute("CREATE TABLE memory (content BLOB)")
        conn.executemany("INSERT INTO memory VALUES (zeroblob(2048))", [()] * row_count)
        conn.commit()
        assert conn.execute("PRAGMA page_count").fetchone()[0] > 4 * 4096

    cut = _online_cut(tmp_path, monkeypatch)
    _pin(cut, source)
    try:
        cut._copy_pinned(source)
    finally:
        cut.close()

    target = tmp_path / "partial" / "source.sqlite3"
    assert cut.copied == [target]
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT count(*) FROM memory").fetchone() == (row_count,)


@pytest.mark.parametrize("sequence", [[10] * 8, [10, 9] * 6], ids=["no-progress", "restarting"])
def test_stalled_online_copy_fails_with_a_stall_error(tmp_path, monkeypatch, sequence):
    source = tmp_path / "source.sqlite3"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("CREATE TABLE memory (content BLOB)")

    class FakePin(sqlite3.Connection):
        def backup(self, target, *, pages, progress, sleep):
            for remaining in sequence:
                progress(sqlite3.SQLITE_OK, remaining, 10)
            pytest.fail("online copy without new progress did not stop")

    cut = _online_cut(tmp_path, monkeypatch)
    _pin(cut, source, FakePin)
    try:
        with pytest.raises(OnlineBackupError, match="^copy: a database copy stalled$") as raised:
            cut._copy_pinned(source)
    finally:
        cut.close()
    # The stage and the reason, not a whole-run deadline.
    assert raised.value.stage == "copy"
    assert "deadline" not in str(raised.value)
    assert cut.copied == []
