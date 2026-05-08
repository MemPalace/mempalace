"""Tests for destructive-operation safety in mempalace.migrate."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mempalace.migrate import collection_write_roundtrip_works, _restore_stale_palace, migrate


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
