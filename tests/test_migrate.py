"""Tests for destructive-operation safety in mempalace.migrate."""

import errno
import os
import socket
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
        patch("mempalace.migrate.copy_palace_dir") as mock_backup_copy,
        patch("mempalace.migrate.shutil.rmtree") as mock_rmtree,
    ):
        result = migrate(str(palace_dir))

    out = capsys.readouterr().out
    assert result is False
    assert "Aborted." in out
    mock_backup_copy.assert_not_called()
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
    """Schema ``extract_via_sqlite`` reads, with a closet row that must not leak."""
    db = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE segments (id TEXT PRIMARY KEY, collection TEXT, scope TEXT);
        CREATE TABLE embeddings (
            id INTEGER PRIMARY KEY, embedding_id TEXT, segment_id TEXT
        );
        CREATE TABLE embedding_metadata (
            id INTEGER, key TEXT,
            string_value TEXT, int_value INTEGER,
            float_value REAL, bool_value INTEGER
        );
        INSERT INTO collections VALUES ('col-d', 'mempalace_drawers');
        INSERT INTO collections VALUES ('col-c', 'mempalace_closets');
        INSERT INTO segments VALUES ('seg-d', 'col-d', 'METADATA');
        INSERT INTO segments VALUES ('seg-c', 'col-c', 'METADATA');
        INSERT INTO embeddings VALUES (1, 'd-001', 'seg-d');
        INSERT INTO embeddings VALUES (2, 'c-001', 'seg-c');
        INSERT INTO embedding_metadata VALUES (1, 'chroma:document', 'hello', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (1, 'wing', 'personal', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (1, 'room', '2026-04-26', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (2, 'chroma:document', 'closet line', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (2, 'wing', 'personal', NULL, NULL, NULL);
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
    assert all(row["id"] != "c-001" for row in drawers)


def test_migrate_skips_count_on_hnsw_divergence(tmp_path, capsys):
    """count() on a diverged HNSW segment can hard-crash the process
    (#1222); migrate() must route straight to the SQLite-extraction
    fallback -- the same path the except Exception branch already falls
    back to -- instead of ever calling col.count() when
    hnsw_capacity_status reports divergence (#90)."""
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("db")

    with (
        patch("mempalace.migrate.detect_chromadb_version", return_value="1.x"),
        patch("mempalace.backends.chroma.ChromaBackend") as mock_backend,
        patch(
            "mempalace.backends.chroma.hnsw_capacity_status",
            return_value={"diverged": True, "message": "test divergence"},
        ),
        patch("mempalace.migrate.extract_drawers_from_sqlite", return_value=[]),
    ):
        mock_backend.backend_version.return_value = "1.5.8"
        migrate(str(palace_dir), dry_run=True)

    mock_backend.return_value.get_collection.assert_not_called()
    out = capsys.readouterr().out
    assert "HNSW index diverged" in out


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


_ONE_DRAWER = [{"id": "id1", "document": "doc", "metadata": {"wing": "w", "room": "r"}}]


def _unreadable_palace_patches(rebuild):
    """Route migrate past the probe to the rebuild, which ``rebuild`` replaces."""
    import mempalace.backends.chroma as _chroma_mod

    failing_backend = MagicMock()
    failing_backend.get_collection.side_effect = Exception("unreadable")
    return (
        patch("mempalace.migrate.detect_chromadb_version", return_value="0.5.x"),
        patch("mempalace.migrate.extract_drawers_from_sqlite", return_value=_ONE_DRAWER),
        patch.object(_chroma_mod, "ChromaBackend", return_value=failing_backend),
        patch("mempalace.repair.rebuild_from_sqlite", side_effect=rebuild),
    )


def test_migrate_cleans_temp_palace_on_rebuild_failure(tmp_path):
    """A failed rebuild must not leave its work dir beside the palace."""
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("db")
    seen = []

    def rebuild(source, dest):
        seen.append(dest)
        os.makedirs(dest)
        raise RuntimeError("chromadb boom")

    p1, p2, p3, p4 = _unreadable_palace_patches(rebuild)
    with p1, p2, p3, p4, patch("mempalace.migrate.copy_palace_dir"):
        with pytest.raises(RuntimeError, match="chromadb boom"):
            migrate(str(palace_dir), confirm=True)

    assert seen, "the rebuild was never reached"
    # Same volume as the palace, so the swap is a rename, not a copy.
    assert os.path.dirname(os.path.dirname(seen[0])) == str(tmp_path)
    assert not os.path.exists(os.path.dirname(seen[0]))
    assert (palace_dir / "chroma.sqlite3").read_text() == "db"


def test_migrate_prunes_old_pre_migrate_backups(tmp_path, monkeypatch):
    """Repeated migrations must not accumulate full-palace copies forever.

    The backup + prune happen before the rebuild, so even a migration that
    fails afterward still trims the backup set.
    """
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("db")

    for i in range(3):
        stale = tmp_path / f"palace.pre-migrate.2026010{i}_000000"
        stale.mkdir()
        (stale / "chroma.sqlite3").write_text("old")
        os.utime(stale, (1_700_000_000 + i, 1_700_000_000 + i))

    monkeypatch.setenv("MEMPALACE_MAX_BACKUPS", "2")

    p1, p2, p3, p4 = _unreadable_palace_patches(RuntimeError("chromadb boom"))
    with p1, p2, p3, p4, pytest.raises(RuntimeError):
        migrate(str(palace_dir), confirm=True)

    backups = sorted(p.name for p in tmp_path.glob("palace.pre-migrate.*"))
    # 3 stale + 1 fresh = 4 created; retention keeps only the 2 newest.
    assert len(backups) == 2
    assert "palace.pre-migrate.20260100_000000" not in backups
    assert "palace.pre-migrate.20260101_000000" not in backups


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"),
    reason="Unix domain socket files are POSIX-only",
)
def test_migrate_backup_survives_a_socket_in_the_palace_directory(tmp_path, monkeypatch, capsys):
    """#2207 in the second full-palace copy: migrate takes the same backup.

    The rebuild is mocked to fail after the backup, so the expected
    RuntimeError proves the copy got past the socket instead of dying on it.
    """
    monkeypatch.setenv("MEMPALACE_MAX_BACKUPS", "2")
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("db")
    # Pins that migrate still copies with ``symlinks=True``: links are
    # recreated as links, so a link to a file outside the palace does not get
    # duplicated by content into every ``.pre-migrate.*`` copy.
    (tmp_path / "outside.json").write_text("payload", encoding="utf-8")
    try:
        (palace_dir / "tunnels.json").symlink_to(tmp_path / "outside.json")
    except OSError as exc:
        if os.name != "nt" and exc.errno not in (errno.EPERM, errno.EACCES):
            raise
        pytest.skip(f"symlink creation not permitted for this user: {exc}")
    # Bound by relative name: the absolute tmp path can exceed the sun_path
    # limit, which is tight on macOS.
    monkeypatch.chdir(palace_dir)
    leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        leftover.bind("mcp.sock")
    finally:
        leftover.close()

    p1, p2, p3, p4 = _unreadable_palace_patches(RuntimeError("chromadb boom"))
    with p1, p2, p3, p4, pytest.raises(RuntimeError, match="chromadb boom"):
        migrate(str(palace_dir), confirm=True)

    out = capsys.readouterr().out
    assert "    mcp.sock (socket)" in out.splitlines()
    backups = list(tmp_path.glob("palace.pre-migrate.*"))
    assert len(backups) == 1
    assert (backups[0] / "chroma.sqlite3").read_text() == "db"
    assert not (backups[0] / "mcp.sock").exists()
    assert (palace_dir / "mcp.sock").exists()
    assert (backups[0] / "tunnels.json").is_symlink()


def _fake_rebuild(source, dest):
    os.makedirs(dest)
    with open(os.path.join(dest, "chroma.sqlite3"), "w") as f:
        f.write("rebuilt")
    return {"mempalace_drawers": 1}


def test_migrate_restores_palace_on_swap_failure(tmp_path, capsys):
    """If moving the rebuild into place fails, the aside copy comes back."""
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("dummy db")
    (palace_dir / "sentinel.txt").write_text("original")

    real_os_replace = os.replace

    def selective_replace(src, dst):
        if os.fspath(dst) == os.fspath(palace_dir) and "mempalace_migrate" in os.fspath(src):
            raise OSError(errno.EIO, "swap-in failed")
        return real_os_replace(src, dst)

    p1, p2, p3, p4 = _unreadable_palace_patches(_fake_rebuild)
    with (
        p1,
        p2,
        p3,
        p4,
        patch("mempalace.migrate._rebuilt_counts_match", return_value=True),
        patch("mempalace.migrate.os.replace", side_effect=selective_replace),
        pytest.raises(OSError, match="swap-in failed"),
    ):
        migrate(str(palace_dir), confirm=True)

    assert (palace_dir / "sentinel.txt").read_text() == "original"
    assert (palace_dir / "chroma.sqlite3").read_text() == "dummy db"
    assert list(tmp_path.glob("palace.pre-migrate.*")), "pre-migrate backup missing"
    assert not list(tmp_path.glob("palace.old*")), "aside copy should be renamed back"
    assert not list(tmp_path.glob(".mempalace_migrate_*")), "work dir left behind"
    assert "CRITICAL" not in capsys.readouterr().out


def test_migrate_reports_a_palace_held_open(tmp_path, capsys):
    """Windows refuses to rename a directory with an open file inside it."""
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (palace_dir / "chroma.sqlite3").write_text("dummy db")

    real_os_replace = os.replace

    def refuse_aside(src, dst):
        if os.fspath(src) == os.fspath(palace_dir):
            raise PermissionError(errno.EACCES, "in use")
        return real_os_replace(src, dst)

    p1, p2, p3, p4 = _unreadable_palace_patches(_fake_rebuild)
    with (
        p1,
        p2,
        p3,
        p4,
        patch("mempalace.migrate._rebuilt_counts_match", return_value=True),
        patch("mempalace.migrate.os.replace", side_effect=refuse_aside),
    ):
        assert migrate(str(palace_dir), confirm=True) is False

    out = capsys.readouterr().out
    assert "held open by another process" in out
    assert "left unchanged" in out
    assert (palace_dir / "chroma.sqlite3").read_text() == "dummy db"
    assert not list(tmp_path.glob(".mempalace_migrate_*"))


def _ids_and_docs(collection):
    result = collection.get(include=["documents"])
    return dict(zip(result.ids, result.documents))


def _seed_real_palace(palace):
    """Drawers, a closet, a live knowledge graph, and palace state files."""
    from mempalace.backends.chroma import ChromaBackend

    backend = ChromaBackend()
    try:
        backend.get_or_create_collection(str(palace), "mempalace_drawers").add(
            ids=["d1", "d2"],
            documents=["drawer words stay here", ""],
            metadatas=[{"wing": "w", "room": "r"}, {"wing": "w", "room": "r"}],
        )
        backend.get_or_create_collection(str(palace), "mempalace_closets").add(
            ids=["c1"],
            documents=["topic|Alice|→d1"],
            metadatas=[{"wing": "w", "source_file": "a.md"}],
        )
    finally:
        backend.close()
    for name in ("knowledge_graph.sqlite3", "logstream.sqlite3"):
        conn = sqlite3.connect(str(palace / name))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES (?)", (name,))
        conn.commit()
        conn.close()
    for name in ("replica.json", "peers.json", "entities.json", "sentinel.txt"):
        (palace / name).write_text(name, encoding="utf-8")
    (palace / "drawer_state").mkdir()
    (palace / "drawer_state" / "nested.txt").write_text("nested", encoding="utf-8")


def _table_value(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT v FROM t").fetchone()[0]
    finally:
        conn.close()


def test_migrate_rebuild_keeps_every_palace_file(tmp_path, monkeypatch):
    """Un-mocked destructive path: a real palace is rebuilt and swapped.

    Closets must not come back as drawers, empty-document drawers must not
    be dropped, and every non-Chroma file in the palace must survive.
    """
    monkeypatch.setenv("MEMPALACE_MAX_BACKUPS", "4")
    palace = tmp_path / "palace"
    palace.mkdir()
    _seed_real_palace(palace)
    # A writer still holds the graph open with an unflushed WAL write.
    live = sqlite3.connect(str(palace / "knowledge_graph.sqlite3"))
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("UPDATE t SET v = 'kg-latest'")
    live.commit()
    if os.name == "nt":
        # Windows will not rename a directory with a file open inside it;
        # that refusal is covered by test_migrate_reports_a_palace_held_open.
        live.close()
    if os.name != "nt" and hasattr(socket, "AF_UNIX"):
        # A leftover runtime socket must not stop the carry-over.
        monkeypatch.chdir(palace)
        leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        leftover.bind("mcp.sock")
        leftover.close()
    rollback = tmp_path / "palace.old"
    rollback.mkdir()
    (rollback / "DO_NOT_DELETE").write_text("previous rollback", encoding="utf-8")

    try:
        with patch("mempalace.migrate.collection_write_roundtrip_works", return_value=False):
            assert migrate(str(palace), confirm=True) is True
    finally:
        live.close()

    from mempalace.backends.chroma import ChromaBackend

    backend = ChromaBackend()
    try:
        drawers = _ids_and_docs(backend.get_collection(str(palace), "mempalace_drawers"))
        closets = _ids_and_docs(backend.get_collection(str(palace), "mempalace_closets"))
    finally:
        backend.close()

    assert drawers == {"d1": "drawer words stay here", "d2": ""}
    assert closets == {"c1": "topic|Alice|→d1"}
    assert _table_value(palace / "knowledge_graph.sqlite3") == "kg-latest"
    assert _table_value(palace / "logstream.sqlite3") == "logstream.sqlite3"
    for name in ("replica.json", "peers.json", "entities.json", "sentinel.txt"):
        assert (palace / name).read_text(encoding="utf-8") == name
    assert (palace / "drawer_state" / "nested.txt").read_text(encoding="utf-8") == "nested"
    assert (palace / "mempalace_embedder.json").is_file()
    assert (rollback / "DO_NOT_DELETE").read_text(encoding="utf-8") == "previous rollback"
    assert not list(tmp_path.glob("palace.old.*"))
    assert not list(tmp_path.glob(".mempalace_migrate_*"))


def test_count_mismatch_against_the_source_does_not_swap(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MEMPALACE_MAX_BACKUPS", "4")
    palace = tmp_path / "palace"
    palace.mkdir()
    _seed_real_palace(palace)

    import mempalace.repair as repair

    real_count = repair.sqlite_drawer_count

    def source_has_one_more(path, name=None):
        n = real_count(path, name)
        return n + 1 if os.path.abspath(path) == str(palace) and n is not None else n

    monkeypatch.setattr(repair, "sqlite_drawer_count", source_has_one_more)
    with patch("mempalace.migrate.collection_write_roundtrip_works", return_value=False):
        assert migrate(str(palace), confirm=True) is False

    out = capsys.readouterr().out
    assert "left unchanged" in out
    assert "Migration complete" not in out
    assert (palace / "sentinel.txt").read_text(encoding="utf-8") == "sentinel.txt"
    assert not list(tmp_path.glob(".mempalace_migrate_*"))


def test_migrate_refuses_assets_it_cannot_re_embed(tmp_path, monkeypatch, capsys):
    """Media vectors are never silently replaced by text vectors."""
    from mempalace.backends.chroma import ChromaBackend

    monkeypatch.setenv("MEMPALACE_MAX_BACKUPS", "4")
    palace = tmp_path / "palace"
    palace.mkdir()
    _seed_real_palace(palace)
    backend = ChromaBackend()
    try:
        backend.get_or_create_collection(str(palace), "mempalace_assets").add(
            ids=["a1"],
            documents=["photo of the dock"],
            metadatas=[{"wing": "w", "room": "r", "source_path": str(tmp_path / "gone.jpg")}],
        )
    finally:
        backend.close()

    with patch("mempalace.migrate.collection_write_roundtrip_works", return_value=False):
        assert migrate(str(palace), confirm=True) is False

    out = capsys.readouterr().out
    assert "left unchanged" in out
    assert (palace / "sentinel.txt").read_text(encoding="utf-8") == "sentinel.txt"


def test_migrate_reports_a_held_palace_lock(tmp_path, capsys):
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "chroma.sqlite3").write_bytes(b"db")
    from mempalace.palace import MineAlreadyRunning

    with (
        patch(
            "mempalace.palace.mine_palace_lock",
            side_effect=MineAlreadyRunning("palace held by 9"),
        ),
        pytest.raises(SystemExit) as caught,
    ):
        migrate(str(palace), dry_run=True)

    assert caught.value.code == 2
    out = capsys.readouterr().out
    assert "Another writer already holds this palace" in out
    assert "nothing changed" in out
    assert (palace / "chroma.sqlite3").read_bytes() == b"db"
