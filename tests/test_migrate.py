"""Tests for mempalace.migrate — safety guards, extraction, detection, and full flow."""

import os
import shutil
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import chromadb
import pytest

from mempalace.migrate import (
    _restore_stale_palace,
    collection_write_roundtrip_works,
    detect_chromadb_version,
    extract_drawers_from_sqlite,
    migrate,
)


# ── Destructive-operation safety tests ─────────────────────────────────


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
        patch(
            "mempalace.schema.create_reference_schema",
            return_value={"tables": {}, "indexes": {}},
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


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def chromadb_like_db(tmp_dir):
    """Create a synthetic ChromaDB-like SQLite database with drawers."""
    db_path = os.path.join(tmp_dir, "chroma.sqlite3")
    conn = sqlite3.connect(db_path)

    # Mimic ChromaDB 0.6.x schema (enough for extract_drawers_from_sqlite)
    conn.executescript(
        """
        CREATE TABLE collections (
            id TEXT PRIMARY KEY,
            name TEXT,
            dimension INTEGER
        );
        CREATE TABLE embeddings (
            id INTEGER PRIMARY KEY,
            embedding_id TEXT,
            collection_id TEXT
        );
        CREATE TABLE embedding_metadata (
            id INTEGER,
            key TEXT,
            string_value TEXT,
            int_value INTEGER,
            float_value REAL,
            bool_value INTEGER
        );
        CREATE TABLE embeddings_queue (
            seq_id INTEGER PRIMARY KEY,
            submit_ts REAL
        );

        INSERT INTO collections VALUES ('col1', 'mempalace_drawers', 384);

        INSERT INTO embeddings VALUES (1, 'drawer_proj_api_001', 'col1');
        INSERT INTO embeddings VALUES (2, 'drawer_proj_api_002', 'col1');

        INSERT INTO embedding_metadata VALUES (1, 'chroma:document', 'API auth uses JWT tokens', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (1, 'wing', 'project', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (1, 'room', 'api', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (1, 'chunk_index', NULL, 0, NULL, NULL);

        INSERT INTO embedding_metadata VALUES (2, 'chroma:document', 'Database uses PostgreSQL 15', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (2, 'wing', 'project', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (2, 'room', 'api', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (2, 'priority', NULL, NULL, 0.9, NULL);
    """
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def palace_06x(tmp_dir, chromadb_like_db):
    """A palace directory containing a 0.6.x-style ChromaDB database."""
    palace = os.path.join(tmp_dir, "palace")
    os.makedirs(palace)
    shutil.copy2(chromadb_like_db, os.path.join(palace, "chroma.sqlite3"))
    return palace


# ── extract_drawers_from_sqlite ────────────────────────────────────────


def test_extract_drawers_from_sqlite(chromadb_like_db):
    """Extracts drawers with documents and metadata from raw SQLite."""
    drawers = extract_drawers_from_sqlite(chromadb_like_db)

    assert len(drawers) == 2

    by_id = {d["id"]: d for d in drawers}
    d1 = by_id["drawer_proj_api_001"]
    assert d1["document"] == "API auth uses JWT tokens"
    assert d1["metadata"]["wing"] == "project"
    assert d1["metadata"]["room"] == "api"
    assert d1["metadata"]["chunk_index"] == 0
    # chroma:document should NOT be in metadata
    assert "chroma:document" not in d1["metadata"]

    d2 = by_id["drawer_proj_api_002"]
    assert d2["document"] == "Database uses PostgreSQL 15"
    assert d2["metadata"]["priority"] == 0.9


def test_extract_drawers_skips_empty_documents(tmp_dir):
    """Drawers without a document are skipped."""
    db_path = os.path.join(tmp_dir, "nodoc.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE embeddings (id INTEGER PRIMARY KEY, embedding_id TEXT);
        CREATE TABLE embedding_metadata (
            id INTEGER, key TEXT, string_value TEXT,
            int_value INTEGER, float_value REAL, bool_value INTEGER
        );
        INSERT INTO embeddings VALUES (1, 'no_doc_drawer');
        INSERT INTO embedding_metadata VALUES (1, 'wing', 'test', NULL, NULL, NULL);
    """
    )
    conn.commit()
    conn.close()

    drawers = extract_drawers_from_sqlite(db_path)
    assert len(drawers) == 0


# ── detect_chromadb_version ────────────────────────────────────────────


def test_detect_chromadb_version_06x(chromadb_like_db):
    """Detects 0.6.x via embeddings_queue table (no schema_str column)."""
    version = detect_chromadb_version(chromadb_like_db)
    assert version == "0.6.x"


def test_detect_chromadb_version_1x(tmp_dir):
    """Detects 1.x via schema_str column on collections."""
    db_path = os.path.join(tmp_dir, "v1.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT, schema_str TEXT)")
    conn.commit()
    conn.close()

    version = detect_chromadb_version(db_path)
    assert version == "1.x"


def test_detect_chromadb_version_unknown(tmp_dir):
    """Returns 'unknown' when schema doesn't match either pattern."""
    db_path = os.path.join(tmp_dir, "mystery.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()

    version = detect_chromadb_version(db_path)
    assert version == "unknown"


# ── migrate ────────────────────────────────────────────────────────────


def test_migrate_dry_run(palace_06x):
    """Dry run shows summary but makes no mutations."""
    result = migrate(palace_06x, dry_run=True)
    assert result is True

    # No backup should be created
    parent = os.path.dirname(palace_06x)
    backups = [f for f in os.listdir(parent) if "pre-migrate" in f]
    assert len(backups) == 0


def test_migrate_already_readable(tmp_dir):
    """Early return when palace is already compatible."""
    palace = os.path.join(tmp_dir, "good_palace")
    client = chromadb.PersistentClient(path=palace)
    col = client.get_or_create_collection("mempalace_drawers")
    col.add(ids=["d1"], documents=["test doc"], metadatas=[{"wing": "w", "room": "r"}])
    del col
    del client

    result = migrate(palace, dry_run=False)
    assert result is True

    # No backup created — nothing to migrate
    parent = os.path.dirname(palace)
    backups = [f for f in os.listdir(parent) if "pre-migrate" in f]
    assert len(backups) == 0


def test_migrate_full_flow_with_validation(palace_06x):
    """End-to-end migration with schema validation."""
    result = migrate(palace_06x, dry_run=False, confirm=True)
    assert result is True

    # Verify drawer content survived via raw SQL (avoids ChromaDB init issues)
    db_path = os.path.join(palace_06x, "chroma.sqlite3")
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert count == 2

    # Verify document content
    docs = conn.execute(
        "SELECT em.string_value FROM embedding_metadata em "
        "JOIN embeddings e ON e.id = em.id "
        "WHERE e.embedding_id = 'drawer_proj_api_001' AND em.key = 'chroma:document'"
    ).fetchone()
    assert docs[0] == "API auth uses JWT tokens"
    conn.close()

    # Backup should exist
    parent = os.path.dirname(palace_06x)
    backups = [f for f in os.listdir(parent) if "pre-migrate" in f]
    assert len(backups) == 1


def test_migrate_aborts_on_validation_failure(palace_06x):
    """Migration aborts without swapping if schema validation fails."""
    original_db = os.path.join(palace_06x, "chroma.sqlite3")

    with patch("mempalace.schema.validate_and_patch") as mock_vp:
        mock_vp.return_value = (False, ["FAILED missing_table: critical table missing"])
        result = migrate(palace_06x, dry_run=False, confirm=True)

    assert result is False

    # Original palace should still exist (swap didn't happen)
    assert os.path.isfile(original_db)

    # Verify it's still the original schema (synthetic 0.6.x, not a fresh ChromaDB palace)
    conn = sqlite3.connect(original_db)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "embeddings_queue" in tables  # 0.6.x marker from synthetic DB
    conn.close()


def test_migrate_no_palace(tmp_dir):
    """Returns False when palace doesn't exist."""
    result = migrate(os.path.join(tmp_dir, "nonexistent"), dry_run=False)
    assert result is False
