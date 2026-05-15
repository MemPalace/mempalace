"""Tests for destructive-operation safety in mempalace.migrate."""

import errno
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mempalace.migrate import (
    _restore_stale_palace,
    collection_write_roundtrip_works,
    extract_drawers_from_sqlite,
    migrate,
)


def test_migrate_requires_palace_database(tmp_path, capsys):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()

    result = migrate(str(palace_dir))

    out = capsys.readouterr().out
    assert result is False
    assert "No palace database found" in out


def test_migrate_aborts_without_confirmation(tmp_path, capsys):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    # Presence of chroma.sqlite3 is the safety gate; validity is mocked below.
    (palace_dir / "chroma.sqlite3").write_text("db")

    mock_chromadb = SimpleNamespace(
        __version__="0.6.0",
        PersistentClient=MagicMock(side_effect=Exception("unreadable")),
    )

    with (
        patch.dict("sys.modules", {"chromadb": mock_chromadb}),
        patch("mempalace.migrate.detect_chromadb_version", return_value="0.5.x"),
        patch(
            "mempalace.migrate.extract_drawers_from_sqlite",
            return_value=[{"id": "id1", "document": "doc", "metadata": {"wing": "w", "room": "r"}}],
        ),
        patch("builtins.input", return_value="n"),
        patch("mempalace.migrate.shutil.copytree") as mock_copytree,
        patch("mempalace.migrate.shutil.rmtree") as mock_rmtree,
    ):
        result = migrate(str(palace_dir))

    out = capsys.readouterr().out
    assert result is False
    assert "Aborted." in out
    mock_copytree.assert_not_called()
    mock_rmtree.assert_not_called()


def test_restore_stale_palace_with_clean_destination(tmp_path):
    """Rollback when no partial copy exists at palace_path."""
    palace_path = tmp_path / "palace"
    stale_path = tmp_path / "palace.old"
    stale_path.mkdir()
    (stale_path / "chroma.sqlite3").write_bytes(b"original")

    _restore_stale_palace(str(palace_path), str(stale_path))

    assert palace_path.is_dir()
    assert (palace_path / "chroma.sqlite3").read_bytes() == b"original"
    assert not stale_path.exists()


def test_restore_stale_palace_clears_partial_copy(tmp_path):
    """Rollback must remove a partially-copied palace_path before restoring.

    Simulates the Qodo-reported hazard: shutil.move() began creating
    palace_path, then failed. A bare os.replace(stale, palace_path) would
    trip on the existing destination; _restore_stale_palace must clear it.
    """
    palace_path = tmp_path / "palace"
    stale_path = tmp_path / "palace.old"

    stale_path.mkdir()
    (stale_path / "chroma.sqlite3").write_bytes(b"original")

    palace_path.mkdir()
    (palace_path / "half-copied.bin").write_bytes(b"garbage")

    _restore_stale_palace(str(palace_path), str(stale_path))

    assert palace_path.is_dir()
    assert (palace_path / "chroma.sqlite3").read_bytes() == b"original"
    assert not (palace_path / "half-copied.bin").exists()
    assert not stale_path.exists()


def test_restore_stale_palace_logs_and_swallows_on_failure(tmp_path, capsys):
    """If restore itself fails, log both paths — don't raise from rollback."""
    palace_path = tmp_path / "palace"
    stale_path = tmp_path / "palace.old"
    stale_path.mkdir()

    # Force os.replace to fail deterministically.
    with patch("mempalace.migrate.os.replace", side_effect=OSError("boom")):
        _restore_stale_palace(str(palace_path), str(stale_path))

    out = capsys.readouterr().out
    assert "CRITICAL" in out
    assert os.fspath(palace_path) in out
    assert os.fspath(stale_path) in out


class _FakeGetResult:
    def __init__(self, ids):
        self.ids = ids


class _WritableFakeCollection:
    def __init__(self):
        self.ids = set()
        self.deleted = []

    def upsert(self, *, ids, documents, metadatas):
        self.ids.update(ids)

    def get(self, *, ids, include=None):
        return _FakeGetResult([drawer_id for drawer_id in ids if drawer_id in self.ids])

    def delete(self, *, ids=None, where=None):
        for drawer_id in ids or []:
            self.ids.discard(drawer_id)
            self.deleted.append(drawer_id)


class _SilentWriteDropCollection(_WritableFakeCollection):
    def upsert(self, *, ids, documents, metadatas):
        return None


class _SilentDeleteDropCollection(_WritableFakeCollection):
    def delete(self, *, ids=None, where=None):
        self.deleted.extend(ids or [])


def test_collection_write_roundtrip_works_when_probe_persists_and_deletes():
    col = _WritableFakeCollection()

    assert collection_write_roundtrip_works(col) is True
    assert col.ids == set()
    assert len(col.deleted) == 1


def test_collection_write_roundtrip_fails_when_upsert_silently_drops():
    col = _SilentWriteDropCollection()

    assert collection_write_roundtrip_works(col) is False
    assert col.ids == set()


def test_collection_write_roundtrip_fails_when_delete_silently_drops():
    col = _SilentDeleteDropCollection()

    assert collection_write_roundtrip_works(col) is False
    assert len(col.ids) == 1


def _make_minimal_chromadb_sqlite(tmp_path):
    """Build a SQLite file with the minimal schema extract_drawers_from_sqlite reads."""
    db = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE embeddings (id INTEGER PRIMARY KEY, embedding_id TEXT);
        CREATE TABLE embedding_metadata (
            id INTEGER, key TEXT,
            string_value TEXT, int_value INTEGER,
            float_value REAL, bool_value INTEGER
        );
        INSERT INTO embeddings VALUES (1, 'd-001');
        INSERT INTO embedding_metadata VALUES (1, 'chroma:document', 'hello', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (1, 'wing', 'personal', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (1, 'room', '2026-04-26', NULL, NULL, NULL);
        """
    )
    conn.commit()
    conn.close()
    return str(db)


def test_extract_drawers_returns_drawers(tmp_path):
    db_path = _make_minimal_chromadb_sqlite(tmp_path)
    drawers = extract_drawers_from_sqlite(db_path)
    assert len(drawers) == 1
    assert drawers[0]["id"] == "d-001"
    assert drawers[0]["document"] == "hello"
    assert drawers[0]["metadata"] == {"wing": "personal", "room": "2026-04-26"}


def test_migrate_dry_run_rebuilds_when_collection_is_readable_but_not_writable(tmp_path, capsys):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("db")

    fake_col = MagicMock()
    fake_col.count.return_value = 102

    drawers = [
        {
            "id": "id1",
            "document": "hello",
            "metadata": {"wing": "test-wing", "room": "general"},
        }
    ]

    with (
        patch("mempalace.migrate.detect_chromadb_version", return_value="1.x"),
        patch("mempalace.backends.chroma.ChromaBackend") as mock_backend,
        patch(
            "mempalace.migrate.collection_write_roundtrip_works", return_value=False
        ) as mock_probe,
        patch(
            "mempalace.migrate.extract_drawers_from_sqlite", return_value=drawers
        ) as mock_extract,
    ):
        mock_backend.backend_version.return_value = "1.5.8"
        mock_backend.return_value.get_collection.return_value = fake_col

        result = migrate(str(palace_dir), dry_run=True)

    out = capsys.readouterr().out

    assert result is True
    mock_probe.assert_called_once_with(fake_col)
    mock_extract.assert_called_once_with(
        os.path.join(os.path.abspath(os.fspath(palace_dir)), "chroma.sqlite3")
    )

    assert "readable by chromadb 1.5.8, but write/delete verification failed" in out
    assert "Rebuilding from SQLite" in out
    assert "Extracted 1 drawers from SQLite" in out
    assert "DRY RUN" in out


def test_migrate_cleans_temp_palace_on_chromadb_failure(tmp_path):
    """If chromadb fails after the temp palace is created, mkdtemp's
    directory must be removed — without try/finally it leaked into the
    system temp root forever."""
    import tempfile as _tempfile

    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("db")

    captured_temp_paths = []
    real_mkdtemp = _tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        captured_temp_paths.append(path)
        return path

    failing_backend = MagicMock()
    # First ChromaBackend().get_collection() must raise so we drop into
    # the SQL-extraction path; the second ChromaBackend().get_or_create_collection()
    # raises to trigger the cleanup we are testing.
    failing_backend.get_collection.side_effect = Exception("unreadable")
    failing_backend.get_or_create_collection.side_effect = RuntimeError("chromadb boom")

    import mempalace.backends.chroma as _chroma_mod

    with (
        patch("mempalace.migrate.detect_chromadb_version", return_value="0.5.x"),
        patch(
            "mempalace.migrate.extract_drawers_from_sqlite",
            return_value=[{"id": "id1", "document": "doc", "metadata": {"wing": "w", "room": "r"}}],
        ),
        patch("builtins.input", return_value="y"),
        patch("mempalace.migrate.shutil.copytree"),
        patch("mempalace.migrate.tempfile.mkdtemp", side_effect=tracking_mkdtemp),
        patch.object(_chroma_mod, "ChromaBackend", return_value=failing_backend),
    ):
        try:
            migrate(str(palace_dir), confirm=True)
        except Exception:
            pass

    assert captured_temp_paths, "mkdtemp was never called — flow short-circuited"
    for p in captured_temp_paths:
        assert not os.path.exists(p), f"temp palace was not cleaned up: {p}"


def test_migrate_prunes_old_pre_migrate_backups(tmp_path, monkeypatch):
    """Repeated migrations must not accumulate full-palace copies forever.

    The backup + prune happen right after copytree, before the (mocked)
    chromadb step, so even a migration that fails afterward still trims the
    backup set. We let copytree run for real so the fresh backup exists on
    disk for the prune to evaluate.
    """
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("db")

    # Pre-seed 3 stale .pre-migrate.* sibling dirs with old mtimes.
    for i in range(3):
        stale = tmp_path / f"palace.pre-migrate.2026010{i}_000000"
        stale.mkdir()
        (stale / "chroma.sqlite3").write_text("old")
        os.utime(stale, (1_700_000_000 + i, 1_700_000_000 + i))

    monkeypatch.setenv("MEMPALACE_MAX_BACKUPS", "2")

    failing_backend = MagicMock()
    failing_backend.get_collection.side_effect = Exception("unreadable")
    failing_backend.get_or_create_collection.side_effect = RuntimeError("chromadb boom")

    import mempalace.backends.chroma as _chroma_mod

    with (
        patch("mempalace.migrate.detect_chromadb_version", return_value="0.5.x"),
        patch(
            "mempalace.migrate.extract_drawers_from_sqlite",
            return_value=[{"id": "id1", "document": "doc", "metadata": {"wing": "w", "room": "r"}}],
        ),
        patch("builtins.input", return_value="y"),
        patch.object(_chroma_mod, "ChromaBackend", return_value=failing_backend),
    ):
        try:
            migrate(str(palace_dir), confirm=True)
        except Exception:
            pass

    backups = sorted(p.name for p in tmp_path.glob("palace.pre-migrate.*"))
    # 3 stale + 1 fresh = 4 created; retention keeps only the 2 newest.
    assert len(backups) == 2
    # The two oldest stale backups must be gone.
    assert "palace.pre-migrate.20260100_000000" not in backups
    assert "palace.pre-migrate.20260101_000000" not in backups


def test_migrate_restores_palace_on_swap_failure(tmp_path, capsys):
    """End-to-end coverage for swap-failure rollback.

    `migrate` swaps the old palace aside via `os.replace` rather than
    deleting it. If `os.replace(temp_palace, palace_path)` raises EXDEV
    (cross-filesystem) AND its `shutil.move` fallback ALSO fails,
    `_restore_stale_palace` rolls back by renaming the aside-copy back
    into place. This exercises that full failure path through the public
    `migrate()` entry point; develop already has unit-level tests for the
    helper itself.
    """
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("dummy db")
    # Sentinel file we verify survives the failed swap via rename-aside rollback.
    (palace_dir / "sentinel.txt").write_text("original")

    fake_col = MagicMock()
    fake_col.count.return_value = 1
    fake_col.add.return_value = None

    drawers = [{"id": "id1", "document": "doc", "metadata": {"wing": "w", "room": "r"}}]

    # Selective os.replace mock: pass-through for the rename-aside (call A,
    # palace -> palace.old) and the rollback (call C, palace.old -> palace);
    # raise EXDEV exactly once on the swap-in (call B, temp -> palace).
    real_os_replace = os.replace
    fail_state = {"swap_in_failed": False}

    def selective_replace(src, dst):
        if os.fspath(dst) == os.fspath(palace_dir) and not fail_state["swap_in_failed"]:
            fail_state["swap_in_failed"] = True
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_os_replace(src, dst)

    with (
        patch("mempalace.migrate.detect_chromadb_version", return_value="0.5.x"),
        patch("mempalace.backends.chroma.ChromaBackend") as mock_backend_cls,
        patch("mempalace.migrate.collection_write_roundtrip_works", return_value=False),
        patch("mempalace.migrate.extract_drawers_from_sqlite", return_value=drawers),
        patch("mempalace.migrate.confirm_destructive_action", return_value=True),
        patch("mempalace.migrate.os.replace", side_effect=selective_replace),
        patch(
            "mempalace.migrate.shutil.move",
            side_effect=OSError("fallback move also failed"),
        ),
        pytest.raises(OSError),
    ):
        mock_backend_cls.backend_version.return_value = "1.5.4"
        mock_backend_cls.return_value.get_collection.return_value = fake_col
        mock_backend_cls.return_value.get_or_create_collection.return_value = fake_col
        migrate(str(palace_dir))

    # Palace directory restored from the rename-aside copy.
    assert palace_dir.is_dir(), "palace directory missing after rollback"
    sentinel = palace_dir / "sentinel.txt"
    assert sentinel.is_file(), "sentinel file not restored"
    assert sentinel.read_text() == "original", "restored contents differ from original"

    # Pre-migrate backup remains on disk for post-mortem.
    backups = [p for p in tmp_path.iterdir() if p.name.startswith("palace.pre-migrate.")]
    assert backups, "pre-migrate backup directory missing"

    # Stale .old aside-copy was consumed by the rollback (renamed back).
    stale_path = tmp_path / "palace.old"
    assert not stale_path.exists(), "stale .old should have been consumed by rollback"

    # No CRITICAL message — rollback succeeded cleanly.
    out = capsys.readouterr().out
    assert "CRITICAL" not in out
