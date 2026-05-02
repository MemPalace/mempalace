import os
import sqlite3
from pathlib import Path

import chromadb
import pytest

from mempalace.backends import (
    GetResult,
    PalaceRef,
    QueryResult,
    UnsupportedFilterError,
    available_backends,
    get_backend,
)
from mempalace.backends.chroma import (
    ChromaBackend,
    ChromaCollection,
    _fix_blob_seq_ids,
    _pin_hnsw_threads,
    quarantine_stale_hnsw,
)


class _FakeCollection:
    """Stand-in for a chromadb.Collection returning raw chroma-shaped dicts."""

    def __init__(self, query_response=None, get_response=None, count_value=7):
        self.calls = []
        self._query_response = query_response or {
            "ids": [["a", "b"]],
            "documents": [["da", "db"]],
            "metadatas": [[{"wing": "w1"}, {"wing": "w2"}]],
            "distances": [[0.1, 0.2]],
        }
        self._get_response = get_response or {
            "ids": ["a"],
            "documents": ["da"],
            "metadatas": [{"wing": "w1"}],
        }
        self._count_value = count_value

    def add(self, **kwargs):
        self.calls.append(("add", kwargs))

    def upsert(self, **kwargs):
        self.calls.append(("upsert", kwargs))

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        return self._query_response

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return self._get_response

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))

    def count(self):
        self.calls.append(("count", {}))
        return self._count_value


def test_chroma_collection_returns_typed_query_result():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)

    result = collection.query(query_texts=["q"])

    assert isinstance(result, QueryResult)
    assert result.ids == [["a", "b"]]
    assert result.documents == [["da", "db"]]
    assert result.metadatas == [[{"wing": "w1"}, {"wing": "w2"}]]
    assert result.distances == [[0.1, 0.2]]
    assert result.embeddings is None


def test_chroma_collection_returns_typed_get_result():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)

    result = collection.get(where={"wing": "w1"})

    assert isinstance(result, GetResult)
    assert result.ids == ["a"]
    assert result.documents == ["da"]
    assert result.metadatas == [{"wing": "w1"}]


def test_query_result_empty_preserves_outer_dimension():
    empty = QueryResult.empty(num_queries=2)
    assert empty.ids == [[], []]
    assert empty.documents == [[], []]
    assert empty.distances == [[], []]
    assert empty.embeddings is None


def test_typed_results_support_dict_compat_access():
    """Transitional compat shim per base.py — retained until callers migrate to attrs."""
    result = GetResult(ids=["a"], documents=["da"], metadatas=[{"w": 1}])
    assert result["ids"] == ["a"]
    assert result.get("documents") == ["da"]
    assert result.get("missing", "default") == "default"
    assert "ids" in result
    assert "missing" not in result


def test_chroma_collection_query_empty_result_preserves_outer_shape():
    fake = _FakeCollection(
        query_response={"ids": [], "documents": [], "metadatas": [], "distances": []}
    )
    collection = ChromaCollection(fake)

    result = collection.query(query_texts=["q1", "q2"])
    assert result.ids == [[], []]
    assert result.documents == [[], []]
    assert result.distances == [[], []]


def test_chroma_collection_rejects_unknown_where_operator():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)

    with pytest.raises(UnsupportedFilterError):
        collection.query(query_texts=["q"], where={"$regex": "foo"})


def test_chroma_collection_delegates_writes():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)

    collection.add(documents=["d"], ids=["1"], metadatas=[{"wing": "w"}])
    collection.upsert(documents=["u"], ids=["2"], metadatas=[{"room": "r"}])
    collection.delete(ids=["1"])
    assert collection.count() == 7

    kinds = [call[0] for call in fake.calls]
    assert kinds == ["add", "upsert", "delete", "count"]


def test_registry_exposes_chroma_by_default():
    names = available_backends()
    assert "chroma" in names
    assert isinstance(get_backend("chroma"), ChromaBackend)


def test_registry_unknown_backend_raises():
    with pytest.raises(KeyError):
        get_backend("no-such-backend-exists")


def test_resolve_backend_priority_order(tmp_path):
    from mempalace.backends import resolve_backend_for_palace

    # explicit kwarg wins over everything
    assert resolve_backend_for_palace(explicit="pg", config_value="lance") == "pg"
    # config value wins over env / default
    assert resolve_backend_for_palace(config_value="lance", env_value="qdrant") == "lance"
    # env wins over default
    assert resolve_backend_for_palace(env_value="qdrant", default="chroma") == "qdrant"
    # falls back to default
    assert resolve_backend_for_palace() == "chroma"


def test_chroma_detect_matches_palace_with_chroma_sqlite(tmp_path):
    (tmp_path / "chroma.sqlite3").write_bytes(b"")
    assert ChromaBackend.detect(str(tmp_path)) is True
    assert ChromaBackend.detect(str(tmp_path.parent)) is False


def test_query_rejects_missing_input():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)
    with pytest.raises(ValueError):
        collection.query()


def test_query_rejects_both_texts_and_embeddings():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)
    with pytest.raises(ValueError):
        collection.query(query_texts=["q"], query_embeddings=[[0.1, 0.2]])


def test_query_rejects_empty_input_list():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)
    with pytest.raises(ValueError):
        collection.query(query_texts=[])


def test_query_empty_preserves_embeddings_outer_shape_when_requested():
    fake = _FakeCollection(
        query_response={"ids": [], "documents": [], "metadatas": [], "distances": []}
    )
    collection = ChromaCollection(fake)

    requested = collection.query(query_texts=["q1", "q2"], include=["documents", "embeddings"])
    assert requested.embeddings == [[], []]

    not_requested = collection.query(query_texts=["q1", "q2"], include=["documents"])
    assert not_requested.embeddings is None


def test_chroma_cache_invalidates_when_db_file_missing(tmp_path):
    """A palace rebuild that removes chroma.sqlite3 must drop the stale cache.

    Primes backend._clients/_freshness directly with a sentinel rather than
    opening a real ``PersistentClient``: on Windows the sqlite file handle
    would still be live and ``Path.unlink`` would raise ``PermissionError``,
    making the test unable to exercise the branch we care about. The decision
    logic under test is pure (no chromadb calls before the branch), so a
    sentinel is sufficient.
    """
    backend = ChromaBackend()
    palace_path = tmp_path / "palace"
    palace_path.mkdir()
    db_file = palace_path / "chroma.sqlite3"
    db_file.write_bytes(b"")  # any file is enough for _db_stat to see it
    st = db_file.stat()

    sentinel = object()
    backend._clients[str(palace_path)] = sentinel
    backend._freshness[str(palace_path)] = (st.st_ino, st.st_mtime)

    # Simulate a rebuild mid-flight: chroma.sqlite3 goes away. Safe to unlink
    # because nothing in this test is holding an OS handle on the file.
    db_file.unlink()

    prior_freshness = (st.st_ino, st.st_mtime)
    new_client = backend._client(str(palace_path))
    # Cache was replaced (not the sentinel) and freshness reflects the post-
    # rebuild stat (chromadb re-creates chroma.sqlite3 during PersistentClient
    # construction; _client re-stats after the constructor so freshness is
    # not frozen at the pre-rebuild value). The stale cached sentinel would
    # have served wrong data if returned.
    assert new_client is not sentinel
    assert backend._freshness[str(palace_path)] != prior_freshness


def test_chroma_cache_picks_up_db_created_after_first_open(tmp_path):
    """The 0 → nonzero stat transition invalidates a cache built before the DB existed."""
    backend = ChromaBackend()
    palace_path = tmp_path / "palace"
    palace_path.mkdir()

    # Seed an entry in the caches as if a prior _client() call had opened the
    # palace when chroma.sqlite3 did not exist yet. Freshness (0, 0.0) is the
    # signal that the DB was absent at cache time.
    sentinel = object()
    backend._clients[str(palace_path)] = sentinel
    backend._freshness[str(palace_path)] = (0, 0.0)

    # The DB file now appears (real chromadb would have created it by now).
    # Use a real chromadb call so _fix_blob_seq_ids and PersistentClient succeed.
    import chromadb as _chromadb

    _chromadb.PersistentClient(path=str(palace_path)).get_or_create_collection("seed")
    assert (palace_path / "chroma.sqlite3").is_file()

    # Next _client() call must detect the 0 → nonzero transition and rebuild.
    refreshed = backend._client(str(palace_path))
    assert refreshed is not sentinel
    assert backend._freshness[str(palace_path)] != (0, 0.0)


def test_base_collection_update_default_rejects_mismatched_lengths():
    """The ABC default update() raises ValueError rather than silently misaligning."""
    from mempalace.backends.base import BaseCollection

    collection = ChromaCollection(_FakeCollection())

    with pytest.raises(ValueError, match="documents length"):
        BaseCollection.update(collection, ids=["1", "2"], documents=["only-one"])

    with pytest.raises(ValueError, match="metadatas length"):
        BaseCollection.update(collection, ids=["1", "2"], metadatas=[{"k": 9}])


def test_chroma_backend_accepts_palace_ref_kwarg(tmp_path):
    palace_path = tmp_path / "palace"
    backend = ChromaBackend()
    collection = backend.get_collection(
        palace=PalaceRef(id=str(palace_path), local_path=str(palace_path)),
        collection_name="mempalace_drawers",
        create=True,
    )
    assert palace_path.is_dir()
    assert isinstance(collection, ChromaCollection)


def test_chroma_backend_create_false_raises_without_creating_directory(tmp_path):
    palace_path = tmp_path / "missing-palace"

    with pytest.raises(FileNotFoundError):
        ChromaBackend().get_collection(
            str(palace_path),
            collection_name="mempalace_drawers",
            create=False,
        )

    assert not palace_path.exists()


def test_chroma_backend_create_true_creates_directory_and_collection(tmp_path):
    palace_path = tmp_path / "palace"

    collection = ChromaBackend().get_collection(
        str(palace_path),
        collection_name="mempalace_drawers",
        create=True,
    )

    assert palace_path.is_dir()
    assert isinstance(collection, ChromaCollection)

    client = chromadb.PersistentClient(path=str(palace_path))
    client.get_collection("mempalace_drawers")


def test_chroma_backend_creates_collection_with_cosine_distance(tmp_path):
    palace_path = tmp_path / "palace"

    ChromaBackend().get_collection(
        str(palace_path),
        collection_name="mempalace_drawers",
        create=True,
    )

    client = chromadb.PersistentClient(path=str(palace_path))
    col = client.get_collection("mempalace_drawers")
    assert col.metadata.get("hnsw:space") == "cosine"


def test_chroma_backend_sets_hnsw_bloat_guard_on_creation(tmp_path):
    """The HNSW guard from #344 must land on freshly-created collection metadata.

    Without batch_size + sync_threshold, mining ~10K+ drawers triggers the
    resize+persist drift that bloats link_lists.bin into hundreds of GB sparse
    and segfaults `status` / `search` / `repair`. The guard belongs at
    collection-creation time so every fresh palace gets it without needing
    a runtime retrofit. Asserting both keys land on the persisted metadata
    also covers the #1161 "config silently dropped" concern at CI time.
    """
    palace_path = tmp_path / "palace"

    ChromaBackend().get_collection(
        str(palace_path),
        collection_name="mempalace_drawers",
        create=True,
    )

    client = chromadb.PersistentClient(path=str(palace_path))
    col = client.get_collection("mempalace_drawers")
    assert col.metadata.get("hnsw:batch_size") == 50_000
    assert col.metadata.get("hnsw:sync_threshold") == 50_000


def test_chroma_backend_create_collection_sets_hnsw_bloat_guard(tmp_path):
    """Same guard must apply via the legacy create_collection() path."""
    palace_path = tmp_path / "palace"

    ChromaBackend().create_collection(str(palace_path), "mempalace_drawers")

    client = chromadb.PersistentClient(path=str(palace_path))
    col = client.get_collection("mempalace_drawers")
    assert col.metadata.get("hnsw:batch_size") == 50_000
    assert col.metadata.get("hnsw:sync_threshold") == 50_000


def test_get_collection_create_true_is_idempotent(tmp_path):
    """Calling get_collection(create=True) twice on the same name must not crash.

    ChromaDB 1.5.x's Rust bindings SIGSEGV when get_or_create_collection is
    called with metadata that differs from the stored collection metadata. The
    fix splits the call into get_collection -> fallback create_collection so the
    metadata-comparison codepath in chromadb_rust_bindings is never reached for
    existing collections. Regression guard for issue #1089.
    """
    palace = str(tmp_path / "palace")
    backend = ChromaBackend()
    backend.get_collection(palace, collection_name="mempalace_drawers", create=True)
    col2 = backend.get_collection(palace, collection_name="mempalace_drawers", create=True)
    assert isinstance(col2, ChromaCollection)


def test_get_collection_create_true_preserves_existing_metadata(tmp_path):
    """Existing collection metadata is not overwritten when reopened with create=True."""
    palace = str(tmp_path / "palace")
    backend = ChromaBackend()
    backend.get_collection(palace, collection_name="mempalace_drawers", create=True)
    col = backend.get_collection(palace, collection_name="mempalace_drawers", create=True)
    assert col._collection.metadata["hnsw:space"] == "cosine"
    assert col._collection.metadata.get("hnsw:batch_size") == 50_000


def test_fix_blob_seq_ids_converts_blobs_to_integers(tmp_path):
    """Simulate a ChromaDB 0.6.x database with BLOB seq_ids and verify repair."""
    db_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id)")
    # Insert BLOB seq_id like ChromaDB 0.6.x would
    blob_42 = (42).to_bytes(8, byteorder="big")
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", (blob_42,))
    conn.commit()
    conn.close()

    _fix_blob_seq_ids(str(tmp_path))

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT seq_id, typeof(seq_id) FROM embeddings").fetchone()
    assert row == (42, "integer")
    conn.close()


def test_fix_blob_seq_ids_noop_without_blobs(tmp_path):
    """No error when seq_ids are already integers."""
    db_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id INTEGER)")
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (42)")
    conn.commit()
    conn.close()

    _fix_blob_seq_ids(str(tmp_path))

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT seq_id, typeof(seq_id) FROM embeddings").fetchone()
    assert row == (42, "integer")
    conn.close()


def test_fix_blob_seq_ids_noop_without_database(tmp_path):
    """No error when palace has no chroma.sqlite3."""
    _fix_blob_seq_ids(str(tmp_path))  # should not raise


def test_fix_blob_seq_ids_does_not_touch_max_seq_id(tmp_path):
    """chromadb 1.5.x owns max_seq_id; the shim must not interpret its BLOBs.

    Regression guard for the 2026-04-20 incident: the old shim ran
    int.from_bytes(..., 'big') over chromadb 1.5.x's native
    b'\\x11\\x11' + ASCII-digit BLOB, producing a ~1.23e18 integer that
    silently suppressed every subsequent embeddings_queue write.
    """
    db_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id)")
    conn.execute("CREATE TABLE max_seq_id (rowid INTEGER PRIMARY KEY, seq_id)")
    sysdb10_blob = b"\x11\x11502607"
    conn.execute("INSERT INTO max_seq_id (seq_id) VALUES (?)", (sysdb10_blob,))
    conn.commit()
    conn.close()

    _fix_blob_seq_ids(str(tmp_path))

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT seq_id, typeof(seq_id) FROM max_seq_id").fetchone()
    assert row == (sysdb10_blob, "blob")
    conn.close()


def test_fix_blob_seq_ids_skips_sysdb10_prefix_in_embeddings(tmp_path):
    """Defense-in-depth: sysdb-10 prefix in embeddings.seq_id is skipped."""
    db_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id)")
    sysdb10_blob = b"\x11\x11502607"
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", (sysdb10_blob,))
    conn.commit()
    conn.close()

    _fix_blob_seq_ids(str(tmp_path))

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT seq_id, typeof(seq_id) FROM embeddings").fetchone()
    # Still a BLOB — not converted to 1.23e18.
    assert row == (sysdb10_blob, "blob")
    conn.close()


def test_fix_blob_seq_ids_still_converts_legacy_blobs_in_embeddings(tmp_path):
    """Regression guard: pure big-endian u64 BLOBs still convert for genuine 0.6.x."""
    db_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id)")
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", ((42).to_bytes(8, "big"),))
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", (b"\x11\x11502607",))
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", ((7).to_bytes(8, "big"),))
    conn.commit()
    conn.close()

    _fix_blob_seq_ids(str(tmp_path))

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT seq_id, typeof(seq_id) FROM embeddings ORDER BY rowid").fetchall()
    assert rows[0] == (42, "integer")
    assert rows[1] == (b"\x11\x11502607", "blob")  # sysdb-10 row left alone
    assert rows[2] == (7, "integer")
    conn.close()


def test_fix_blob_seq_ids_writes_marker_after_blob_path(tmp_path):
    """The .blob_seq_ids_migrated marker is written after a successful BLOB → INTEGER conversion."""
    from mempalace.backends.chroma import _BLOB_FIX_MARKER

    db_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id)")
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", ((42).to_bytes(8, "big"),))
    conn.commit()
    conn.close()

    marker = tmp_path / _BLOB_FIX_MARKER
    assert not marker.exists()

    _fix_blob_seq_ids(str(tmp_path))

    assert marker.is_file(), "marker must be written after a successful migration"


def test_fix_blob_seq_ids_writes_marker_when_already_integer(tmp_path):
    """The marker is written even when the migration is a no-op (already INTEGER).

    The point of the marker is to skip the sqlite3 open on subsequent calls,
    not to record that a conversion happened. So a clean palace gets the
    marker on first run too — next ``_fix_blob_seq_ids`` call short-circuits
    before touching the sqlite3 file.
    """
    from mempalace.backends.chroma import _BLOB_FIX_MARKER

    db_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id INTEGER)")
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (42)")
    conn.commit()
    conn.close()

    marker = tmp_path / _BLOB_FIX_MARKER
    assert not marker.exists()

    _fix_blob_seq_ids(str(tmp_path))

    assert marker.is_file(), "marker must be written even when no BLOBs found"


def test_fix_blob_seq_ids_skips_sqlite_when_marker_present(tmp_path):
    """When the marker exists, ``_fix_blob_seq_ids`` does not open sqlite3.

    This is the load-bearing property of the marker — opening Python's
    sqlite3 against a live ChromaDB 1.5.x WAL DB corrupts the next
    PersistentClient call (#1090). Once a palace has been migrated, we
    never want to open it again, even read-only.
    """
    from unittest.mock import patch
    from mempalace.backends.chroma import _BLOB_FIX_MARKER

    # Pre-create the marker so the function should short-circuit.
    db_path = tmp_path / "chroma.sqlite3"
    db_path.write_bytes(b"sentinel")  # presence required for the function to proceed
    (tmp_path / _BLOB_FIX_MARKER).touch()

    with patch("mempalace.backends.chroma.sqlite3.connect") as mock_connect:
        _fix_blob_seq_ids(str(tmp_path))

    mock_connect.assert_not_called()


# ── quarantine_stale_hnsw ─────────────────────────────────────────────────


# Marker bytes for the chromadb segment metadata file. A complete
# write begins with PROTO opcode (0x80) and ends with STOP opcode
# (0x2e); _segment_appears_healthy sniffs these bytes without parsing
# the file.
_HEALTHY_META = b"\x80\x04" + b"\x00" * 32 + b"\x2e"
_CORRUPT_META = b"\x00" * 64


def _make_palace_with_segment(tmp_path, hnsw_mtime, sqlite_mtime, meta_bytes=_HEALTHY_META):
    """Helper: build a palace dir with one HNSW segment + sqlite at given
    mtimes. ``meta_bytes`` controls whether the segment looks healthy
    (default), corrupt (``_CORRUPT_META``), or has no metadata file at
    all (``None``)."""
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "chroma.sqlite3").write_text("")
    seg = palace / "abcd-1234-5678"
    seg.mkdir()
    (seg / "data_level0.bin").write_text("")
    if meta_bytes is not None:
        (seg / "index_metadata.pickle").write_bytes(meta_bytes)
    os.utime(seg / "data_level0.bin", (hnsw_mtime, hnsw_mtime))
    os.utime(palace / "chroma.sqlite3", (sqlite_mtime, sqlite_mtime))
    return palace, seg


def test_quarantine_stale_hnsw_renames_corrupt_segment(tmp_path):
    """Segment with stale mtime AND a malformed metadata file gets renamed."""
    now = 1_700_000_000.0
    palace, seg = _make_palace_with_segment(
        tmp_path,
        hnsw_mtime=now - 7200,
        sqlite_mtime=now,
        meta_bytes=_CORRUPT_META,
    )
    moved = quarantine_stale_hnsw(str(palace), stale_seconds=3600.0)
    assert len(moved) == 1
    assert ".drift-" in moved[0]
    assert not seg.exists()
    renamed = list(palace.iterdir())
    drift_dirs = [p for p in renamed if ".drift-" in p.name]
    assert len(drift_dirs) == 1
    assert (drift_dirs[0] / "data_level0.bin").exists()


def test_quarantine_stale_hnsw_leaves_healthy_segment_with_drift_alone(tmp_path):
    """Segment with stale mtime but a complete metadata file is NOT
    renamed — this is the chromadb-1.5.x async-flush steady state, not
    corruption. Production case at 06:24 PDT 2026-04-26: cold-start
    quarantine renamed three healthy segments after a clean shutdown,
    leaving 151K-drawer palace with vector_ranked=0."""
    now = 1_700_000_000.0
    palace, seg = _make_palace_with_segment(
        tmp_path,
        hnsw_mtime=now - 7200,
        sqlite_mtime=now,
        meta_bytes=_HEALTHY_META,
    )
    moved = quarantine_stale_hnsw(str(palace), stale_seconds=3600.0)
    assert moved == []
    assert seg.exists()


def test_quarantine_stale_hnsw_leaves_segment_without_metadata_alone(tmp_path):
    """Segment with no metadata file is treated as fresh / never-flushed
    and not quarantined — renaming an empty dir orphans nothing."""
    now = 1_700_000_000.0
    palace, seg = _make_palace_with_segment(
        tmp_path,
        hnsw_mtime=now - 7200,
        sqlite_mtime=now,
        meta_bytes=None,
    )
    moved = quarantine_stale_hnsw(str(palace), stale_seconds=3600.0)
    assert moved == []
    assert seg.exists()


def test_quarantine_stale_hnsw_renames_truncated_metadata(tmp_path):
    """Segment with a truncated (under-floor-size) metadata file is
    quarantined — shape of a partial-flush during process kill."""
    now = 1_700_000_000.0
    palace, seg = _make_palace_with_segment(
        tmp_path,
        hnsw_mtime=now - 7200,
        sqlite_mtime=now,
        meta_bytes=b"\x80\x04",
    )
    moved = quarantine_stale_hnsw(str(palace), stale_seconds=3600.0)
    assert len(moved) == 1
    assert ".drift-" in moved[0]


def test_quarantine_stale_hnsw_leaves_fresh_segment_alone(tmp_path):
    """Segment with recent mtime vs sqlite is not touched (mtime gate
    short-circuits before integrity gate)."""
    now = 1_700_000_000.0
    palace, seg = _make_palace_with_segment(tmp_path, hnsw_mtime=now - 10, sqlite_mtime=now)
    moved = quarantine_stale_hnsw(str(palace), stale_seconds=3600.0)
    assert moved == []
    assert seg.exists()


def test_quarantine_stale_hnsw_no_palace(tmp_path):
    """Missing palace path or chroma.sqlite3: return [] without raising."""
    assert quarantine_stale_hnsw(str(tmp_path / "missing")) == []
    empty = tmp_path / "empty"
    empty.mkdir()
    assert quarantine_stale_hnsw(str(empty)) == []


def test_quarantine_stale_hnsw_skips_already_quarantined(tmp_path):
    """Directories already named with ``.drift-`` suffix are never re-renamed."""
    now = 1_700_000_000.0
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "chroma.sqlite3").write_text("")
    os.utime(palace / "chroma.sqlite3", (now, now))
    drift = palace / "abcd-1234.drift-20260101-000000"
    drift.mkdir()
    (drift / "data_level0.bin").write_text("")
    os.utime(drift / "data_level0.bin", (now - 99999, now - 99999))

    moved = quarantine_stale_hnsw(str(palace), stale_seconds=3600.0)
    assert moved == []
    assert drift.exists()


# ── make_client cold-start gate ──────────────────────────────────────────


def test_make_client_quarantines_only_on_first_call_per_palace(tmp_path, monkeypatch):
    """Quarantine fires on first ``make_client()`` for a palace, then is
    skipped on subsequent calls — prevents runtime thrash where a daemon's
    own steady writes bump ``chroma.sqlite3`` faster than HNSW flushes,
    making the mtime heuristic falsely trigger every reconnect."""
    from mempalace.backends.chroma import ChromaBackend

    palace_path = str(tmp_path / "palace")
    os.makedirs(palace_path, exist_ok=True)
    (Path(palace_path) / "chroma.sqlite3").write_text("")

    # Reset the per-process cache so this test is independent of others.
    monkeypatch.setattr(ChromaBackend, "_quarantined_paths", set())

    calls: list[str] = []

    def _spy(path, stale_seconds=300.0):
        calls.append(path)
        return []

    monkeypatch.setattr("mempalace.backends.chroma.quarantine_stale_hnsw", _spy)

    ChromaBackend.make_client(palace_path)
    ChromaBackend.make_client(palace_path)
    ChromaBackend.make_client(palace_path)

    assert calls == [
        palace_path
    ], "quarantine_stale_hnsw should fire once per palace per process, not on every reconnect"


def test_make_client_quarantines_each_palace_independently(tmp_path, monkeypatch):
    """Two distinct palaces each get one quarantine attempt — the gate is
    keyed by palace path, not global."""
    from mempalace.backends.chroma import ChromaBackend

    palace_a = str(tmp_path / "palace_a")
    palace_b = str(tmp_path / "palace_b")
    for p in (palace_a, palace_b):
        os.makedirs(p, exist_ok=True)
        (Path(p) / "chroma.sqlite3").write_text("")

    monkeypatch.setattr(ChromaBackend, "_quarantined_paths", set())

    calls: list[str] = []

    def _spy(path, stale_seconds=300.0):
        calls.append(path)
        return []

    monkeypatch.setattr("mempalace.backends.chroma.quarantine_stale_hnsw", _spy)

    ChromaBackend.make_client(palace_a)
    ChromaBackend.make_client(palace_b)
    ChromaBackend.make_client(palace_a)  # already gated
    ChromaBackend.make_client(palace_b)  # already gated

    assert calls == [palace_a, palace_b]


# ── _pin_hnsw_threads (per-process retrofit, separate from this PR's gate) ──


def test_pin_hnsw_threads_retrofits_legacy_collection(tmp_path):
    """Legacy collections (created without num_threads) get the retrofit applied."""
    palace_path = tmp_path / "legacy-palace"
    palace_path.mkdir()

    client = chromadb.PersistentClient(path=str(palace_path))
    col = client.create_collection(
        "mempalace_drawers",
        metadata={"hnsw:space": "cosine"},  # no num_threads — legacy
    )
    assert col.configuration_json.get("hnsw", {}).get("num_threads") is None

    _pin_hnsw_threads(col)

    assert col.configuration_json["hnsw"]["num_threads"] == 1


def test_pin_hnsw_threads_swallows_all_errors():
    """Retrofit never raises even when collection.modify explodes."""

    class _ExplodingCollection:
        def modify(self, *args, **kwargs):
            raise RuntimeError("boom")

    _pin_hnsw_threads(_ExplodingCollection())  # must not raise


def test_get_collection_applies_retrofit_on_existing_palace(tmp_path):
    """ChromaBackend.get_collection(create=False) applies the retrofit."""
    palace_path = tmp_path / "palace"
    palace_path.mkdir()

    # Simulate a legacy palace: create collection without num_threads
    bootstrap_client = chromadb.PersistentClient(path=str(palace_path))
    bootstrap_client.create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
    del bootstrap_client  # drop reference so a fresh client reopens cleanly

    wrapper = ChromaBackend().get_collection(
        str(palace_path),
        collection_name="mempalace_drawers",
        create=False,
    )

    assert wrapper._collection.configuration_json["hnsw"]["num_threads"] == 1
