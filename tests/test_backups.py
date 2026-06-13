"""Tests for backup retention pruning (mempalace.backups.prune_backups).

These guard the fix for unbounded backup growth: ``mempalace migrate`` and
``mempalace repair max-seq-id`` each drop a fresh full-size, timestamped copy
every run, and used to never delete the old ones — a palace was found with
hundreds of GB of stale backups beside a few hundred MB of live data.
"""

import os

import pytest

from mempalace.backups import prune_backups


def _make_backup_dir(parent, name, mtime):
    """Create a directory backup with a fixed mtime."""
    path = parent / name
    path.mkdir()
    (path / "chroma.sqlite3").write_text("db")
    os.utime(path, (mtime, mtime))
    return path


def _make_backup_file(parent, name, mtime):
    """Create a file backup with a fixed mtime."""
    path = parent / name
    path.write_text("db")
    os.utime(path, (mtime, mtime))
    return path


def test_prune_keeps_newest_and_removes_oldest(tmp_path):
    # 5 backups, mtimes 100..500; keep 2 newest (400, 500).
    paths = [_make_backup_file(tmp_path, f"b.{i}", mtime=i * 100) for i in range(1, 6)]

    removed = prune_backups(str(tmp_path / "b.*"), max_backups=2)

    surviving = {p.name for p in tmp_path.iterdir()}
    assert surviving == {"b.4", "b.5"}
    assert set(removed) == {str(paths[0]), str(paths[1]), str(paths[2])}


def test_prune_removes_directory_backups(tmp_path):
    """migrate writes directory backups (full copytree) — must rmtree them."""
    _make_backup_dir(tmp_path, "palace.pre-migrate.1", mtime=100)
    _make_backup_dir(tmp_path, "palace.pre-migrate.2", mtime=200)
    keep = _make_backup_dir(tmp_path, "palace.pre-migrate.3", mtime=300)

    removed = prune_backups(str(tmp_path / "palace.pre-migrate.*"), max_backups=1)

    assert keep.is_dir()
    assert len(removed) == 2
    assert not (tmp_path / "palace.pre-migrate.1").exists()
    assert not (tmp_path / "palace.pre-migrate.2").exists()


def test_prune_noop_when_under_limit(tmp_path):
    _make_backup_file(tmp_path, "b.1", mtime=100)
    _make_backup_file(tmp_path, "b.2", mtime=200)

    removed = prune_backups(str(tmp_path / "b.*"), max_backups=10)

    assert removed == []
    assert len(list(tmp_path.iterdir())) == 2


def test_prune_noop_when_exactly_at_limit(tmp_path):
    _make_backup_file(tmp_path, "b.1", mtime=100)
    _make_backup_file(tmp_path, "b.2", mtime=200)

    removed = prune_backups(str(tmp_path / "b.*"), max_backups=2)

    assert removed == []


@pytest.mark.parametrize("disabled", [0, -1, None])
def test_prune_disabled_keeps_everything(tmp_path, disabled):
    for i in range(1, 6):
        _make_backup_file(tmp_path, f"b.{i}", mtime=i * 100)

    removed = prune_backups(str(tmp_path / "b.*"), max_backups=disabled)

    assert removed == []
    assert len(list(tmp_path.iterdir())) == 5


def test_prune_no_matches(tmp_path):
    assert prune_backups(str(tmp_path / "nope.*"), max_backups=3) == []


def test_prune_only_touches_matching_pattern(tmp_path):
    """Live data and unrelated files must never be swept up by a backup glob."""
    _make_backup_file(tmp_path, "chroma.sqlite3.max-seq-id-backup-1", mtime=100)
    _make_backup_file(tmp_path, "chroma.sqlite3.max-seq-id-backup-2", mtime=200)
    _make_backup_file(tmp_path, "chroma.sqlite3.max-seq-id-backup-3", mtime=300)
    # The live database and an unrelated file — must survive.
    live = _make_backup_file(tmp_path, "chroma.sqlite3", mtime=400)
    other = _make_backup_file(tmp_path, "tunnels.json", mtime=400)

    prune_backups(
        str(tmp_path / "chroma.sqlite3.max-seq-id-backup-*"),
        max_backups=1,
    )

    assert live.exists()
    assert other.exists()
    assert (tmp_path / "chroma.sqlite3.max-seq-id-backup-3").exists()
    assert not (tmp_path / "chroma.sqlite3.max-seq-id-backup-1").exists()
    assert not (tmp_path / "chroma.sqlite3.max-seq-id-backup-2").exists()


def test_prune_respects_glob_escape_for_metacharacter_paths(tmp_path):
    """Palace paths can contain glob metacharacters like ``[``.

    Without ``glob.escape`` the pattern would silently match nothing (the
    bracket is read as a character class), leaving backups unpruned. Callers
    escape the literal prefix; this confirms the helper prunes correctly once
    they do.
    """
    import glob

    weird = tmp_path / "weird[name]"
    weird.mkdir()
    for i in range(1, 4):
        _make_backup_file(weird, f"chroma.sqlite3.max-seq-id-backup-{i}", mtime=i * 100)

    pattern = os.path.join(glob.escape(str(weird)), "chroma.sqlite3.max-seq-id-backup-*")
    removed = prune_backups(pattern, max_backups=1)

    assert len(removed) == 2
    assert (weird / "chroma.sqlite3.max-seq-id-backup-3").exists()


def test_prune_is_best_effort_on_delete_failure(tmp_path, monkeypatch):
    """A failed deletion is logged and skipped, never raised — pruning must
    not undo a migrate/repair that already succeeded."""
    for i in range(1, 5):
        _make_backup_file(tmp_path, f"b.{i}", mtime=i * 100)

    real_remove = os.remove

    def flaky_remove(path):
        if path.endswith("b.1"):
            raise OSError("permission denied")
        return real_remove(path)

    monkeypatch.setattr(os, "remove", flaky_remove)

    logs = []
    removed = prune_backups(str(tmp_path / "b.*"), max_backups=2, log=logs.append)

    # b.1 and b.2 were over the limit; b.1 failed, b.2 succeeded.
    assert str(tmp_path / "b.2") in removed
    assert str(tmp_path / "b.1") not in removed
    assert (tmp_path / "b.1").exists()
    assert any("could not remove" in line for line in logs)
