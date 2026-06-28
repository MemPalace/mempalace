import os
import pickle
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import chromadb
import pytest

from mempalace.backends import (
    CollectionNotInitializedError,
    GetResult,
    PalaceNotFoundError,
    PalaceRef,
    QueryResult,
    UnsupportedFilterError,
    available_backends,
    get_backend,
)
from mempalace.backends.chroma import (
    ChromaBackend,
    ChromaCollection,
    _HNSW_MISSING_METADATA_DATA_FLOOR,
    _fix_blob_seq_ids,
    _fix_missing_collection_type,
    _pin_hnsw_threads,
    _segment_appears_healthy,
    quarantine_invalid_hnsw_metadata,
    quarantine_stale_hnsw,
)

# embeddinggemma-300m Matryoshka truncation (first 384 of 768 dims).
_TEST_EMBED_DIM = 384


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


def test_chroma_lexical_search_uses_sqlite_fts_not_full_collection_scan(tmp_path):
    db_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE collections (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE segments (id INTEGER PRIMARY KEY, collection INTEGER NOT NULL);
        CREATE TABLE embeddings (
            id INTEGER PRIMARY KEY,
            segment_id INTEGER NOT NULL,
            embedding_id TEXT,
            created_at TEXT
        );
        CREATE TABLE embedding_metadata (
            id INTEGER,
            key TEXT,
            string_value TEXT,
            int_value INTEGER,
            float_value REAL,
            bool_value INTEGER
        );
        CREATE VIRTUAL TABLE embedding_fulltext_search USING fts5(string_value);
        """
    )
    conn.execute("INSERT INTO collections(id, name) VALUES (1, 'mempalace_drawers')")
    conn.execute("INSERT INTO segments(id, collection) VALUES (1, 1)")
    ids = list(range(1, 14))
    for emb_id in ids:
        wing = "target" if emb_id == 13 else "old"
        doc = "needle shared lexical note"
        conn.execute(
            "INSERT INTO embeddings(id, segment_id, embedding_id, created_at) VALUES (?, 1, ?, ?)",
            (emb_id, f"public-{emb_id}", f"2026-01-01T00:00:{emb_id:02d}"),
        )
        conn.execute(
            "INSERT INTO embedding_fulltext_search(rowid, string_value) VALUES (?, ?)",
            (emb_id, doc),
        )
        conn.execute(
            "INSERT INTO embedding_metadata(id, key, string_value) VALUES (?, 'chroma:document', ?)",
            (emb_id, doc),
        )
        conn.execute(
            "INSERT INTO embedding_metadata(id, key, string_value) VALUES (?, 'wing', ?)",
            (emb_id, wing),
        )
    conn.commit()
    conn.close()

    class _NoScanCollection:
        name = "mempalace_drawers"

        def count(self):
            raise AssertionError("lexical_search should use Chroma sqlite FTS")

        def get(self, **_kwargs):
            raise AssertionError("lexical_search should use Chroma sqlite FTS")

    collection = ChromaCollection(_NoScanCollection(), palace_path=str(tmp_path))

    hits = collection.lexical_search(query="needle", n_results=1, where={"wing": "target"}).hits

    assert [hit.metadata["wing"] for hit in hits] == ["target"]
    # Hit ids must be the public embedding_id (so lexical_search -> get(ids=...)
    # round-trips), not the internal embeddings.id rowid.
    assert [hit.id for hit in hits] == ["public-13"]


def test_chroma_lexical_search_ids_roundtrip_through_get(tmp_path):
    """lexical_search must return public drawer ids that get(ids=...) accepts.

    Regression for the sqlite FTS path returning the internal rowid instead of
    embeddings.embedding_id, which silently broke hybrid search id round-trips.
    """
    backend = ChromaBackend()
    palace = tmp_path / "palace"
    ref = PalaceRef(id=str(palace), local_path=str(palace))
    col = backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
    col.add(
        ids=["drawer-alpha", "drawer-bravo", "drawer-charlie"],
        documents=[
            "rareterm needle lexical note",
            "unrelated content here",
            "another rareterm needle entry",
        ],
        metadatas=[{"wing": "w"}, {"wing": "w"}, {"wing": "w"}],
    )
    hits = col.lexical_search(query="rareterm needle", n_results=5).hits
    hit_ids = [hit.id for hit in hits]
    assert hit_ids, "expected lexical hits"
    assert set(hit_ids) <= {"drawer-alpha", "drawer-bravo", "drawer-charlie"}
    # Every returned id must resolve back through get() — proving it is a public id.
    fetched = col.get(ids=hit_ids)
    assert set(fetched.ids) == set(hit_ids)
    backend.close()


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


def test_chroma_close_palace_releases_sqlite_lock_for_reopen(tmp_path):
    """close_palace must release chromadb's rust-side SQLite file lock so
    a fresh PersistentClient on the same path after shutil.rmtree can
    write without hitting SQLITE_READONLY_DBMOVED."""
    backend = ChromaBackend()
    palace_path = tmp_path / "palace-a"
    ref = PalaceRef(id=str(palace_path), local_path=str(palace_path))

    col = backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
    col.upsert(documents=["hello"], ids=["a"], metadatas=[{"k": "v"}])

    backend.close_palace(ref)
    shutil.rmtree(palace_path)

    col = backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
    col.upsert(documents=["world"], ids=["b"], metadatas=[{"k": "v2"}])
    assert col.count() == 1


def test_chroma_close_releases_all_cached_clients(tmp_path):
    """close() must release every cached client's SQLite file lock so any
    of their palace paths can be reopened by a fresh backend in the same
    process."""
    backend = ChromaBackend()
    palace_a = tmp_path / "palace-a"
    palace_b = tmp_path / "palace-b"
    ref_a = PalaceRef(id=str(palace_a), local_path=str(palace_a))
    ref_b = PalaceRef(id=str(palace_b), local_path=str(palace_b))

    for ref in (ref_a, ref_b):
        backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True).upsert(
            documents=["x"], ids=["x"], metadatas=[{"k": "v"}]
        )

    backend.close()

    for path in (palace_a, palace_b):
        shutil.rmtree(path)
        ref = PalaceRef(id=str(path), local_path=str(path))
        fresh = ChromaBackend()
        col = fresh.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
        col.upsert(documents=["y"], ids=["y"], metadatas=[{"k": "v2"}])
        assert col.count() == 1
        fresh.close()


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
    """HNSW batch/sync thresholds must land on freshly-created collection metadata.

    Low thresholds (2/2 per #1579) make chromadb's Rust HNSW segment
    persist index_metadata and link_lists after any mine of 2+ drawers.
    Asserting both keys land on the persisted metadata also covers the
    #1161 "config silently dropped" concern at CI time.
    """
    palace_path = tmp_path / "palace"

    ChromaBackend().get_collection(
        str(palace_path),
        collection_name="mempalace_drawers",
        create=True,
    )

    client = chromadb.PersistentClient(path=str(palace_path))
    col = client.get_collection("mempalace_drawers")
    assert col.metadata.get("hnsw:batch_size") == 2
    assert col.metadata.get("hnsw:sync_threshold") == 2


def test_chroma_backend_create_collection_sets_hnsw_bloat_guard(tmp_path):
    """Same guard must apply via the legacy create_collection() path."""
    palace_path = tmp_path / "palace"

    ChromaBackend().create_collection(str(palace_path), "mempalace_drawers")

    client = chromadb.PersistentClient(path=str(palace_path))
    col = client.get_collection("mempalace_drawers")
    assert col.metadata.get("hnsw:batch_size") == 2
    assert col.metadata.get("hnsw:sync_threshold") == 2


def test_sub_threshold_mine_persists_hnsw_metadata(tmp_path):
    """Regression for #1579: small mines must persist HNSW metadata.

    _HNSW_BLOAT_GUARD sets batch_size=2 and sync_threshold=2 so that any
    upsert of 2+ records crosses both thresholds, triggering chromadb's
    _apply_batch and _persist.  Without this, index_metadata and link_lists
    stay empty and quarantine_stale_hnsw renames the segment on cold open.
    """
    palace_path = str(tmp_path / "palace")
    backend = ChromaBackend()
    try:
        col = backend.get_collection(palace_path, "mempalace_drawers", create=True)

        col.upsert(
            ids=["a", "b", "c"],
            documents=["doc a", "doc b", "doc c"],
            embeddings=[[0.1] * _TEST_EMBED_DIM, [0.2] * _TEST_EMBED_DIM, [0.3] * _TEST_EMBED_DIM],
            metadatas=[{"wing": "t"}, {"wing": "t"}, {"wing": "t"}],
        )
    finally:
        backend.close()

    found_healthy_segment = False
    for entry in (tmp_path / "palace").iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        meta = entry / "index_metadata.pickle"
        link = entry / "link_lists.bin"
        data = entry / "data_level0.bin"
        if data.exists() and data.stat().st_size > _HNSW_MISSING_METADATA_DATA_FLOOR:
            assert meta.exists(), "index_metadata missing after sub-threshold upsert"
            assert link.exists() and link.stat().st_size > 0, "link_lists empty"
            assert _segment_appears_healthy(str(entry))
            found_healthy_segment = True

    assert found_healthy_segment, "no VECTOR segment with data found"

    # stale_seconds=0.0 forces the stage-2 integrity gate (_segment_appears_healthy)
    # to run on every segment regardless of mtime delta, proving the fix directly.
    moved = quarantine_stale_hnsw(palace_path, stale_seconds=0.0)
    assert moved == [], f"quarantine fired on freshly-persisted segment: {moved}"


def test_single_record_upsert_not_quarantined(tmp_path):
    """A single-record upsert must not trigger quarantine.

    With batch_size=2 chromadb only persists HNSW metadata after the second
    record.  A one-record segment has no index_metadata.pickle and no
    link_lists.bin data; _segment_appears_healthy must treat that combination
    as sub-threshold (never persisted), not as corruption.
    """
    palace_path = str(tmp_path / "palace")
    backend = ChromaBackend()
    try:
        col = backend.get_collection(palace_path, "mempalace_drawers", create=True)
        col.upsert(
            ids=["solo"],
            documents=["only one drawer"],
            embeddings=[[0.5] * _TEST_EMBED_DIM],
            metadatas=[{"wing": "t"}],
        )
    finally:
        backend.close()

    for entry in (tmp_path / "palace").iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        data = entry / "data_level0.bin"
        if data.exists() and data.stat().st_size > 0:
            assert _segment_appears_healthy(str(entry)), (
                f"single-record segment flagged unhealthy: data={data.stat().st_size}B"
            )

    moved = quarantine_stale_hnsw(palace_path, stale_seconds=0.0)
    assert moved == [], f"quarantine fired on single-record segment: {moved}"


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
    assert col._collection.metadata.get("hnsw:batch_size") == 2


def test_fix_blob_seq_ids_converts_blobs_to_integers(tmp_path):
    """Simulate a ChromaDB 0.6.x database with BLOB seq_ids and verify repair."""
    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id)")
        # Insert BLOB seq_id like ChromaDB 0.6.x would
        blob_42 = (42).to_bytes(8, byteorder="big")
        conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", (blob_42,))
        conn.commit()

    _fix_blob_seq_ids(str(tmp_path))

    with closing(sqlite3.connect(str(db_path))) as conn:
        row = conn.execute("SELECT seq_id, typeof(seq_id) FROM embeddings").fetchone()
        assert row == (42, "integer")


def test_fix_blob_seq_ids_noop_without_blobs(tmp_path):
    """No error when seq_ids are already integers."""
    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id INTEGER)")
        conn.execute("INSERT INTO embeddings (seq_id) VALUES (42)")
        conn.commit()

    _fix_blob_seq_ids(str(tmp_path))

    with closing(sqlite3.connect(str(db_path))) as conn:
        row = conn.execute("SELECT seq_id, typeof(seq_id) FROM embeddings").fetchone()
        assert row == (42, "integer")


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
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id)")
        conn.execute("CREATE TABLE max_seq_id (rowid INTEGER PRIMARY KEY, seq_id)")
        sysdb10_blob = b"\x11\x11502607"
        conn.execute("INSERT INTO max_seq_id (seq_id) VALUES (?)", (sysdb10_blob,))
        conn.commit()

    _fix_blob_seq_ids(str(tmp_path))

    with closing(sqlite3.connect(str(db_path))) as conn:
        row = conn.execute("SELECT seq_id, typeof(seq_id) FROM max_seq_id").fetchone()
        assert row == (sysdb10_blob, "blob")


def test_fix_blob_seq_ids_skips_sysdb10_prefix_in_embeddings(tmp_path):
    """Defense-in-depth: sysdb-10 prefix in embeddings.seq_id is skipped."""
    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id)")
        sysdb10_blob = b"\x11\x11502607"
        conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", (sysdb10_blob,))
        conn.commit()

    _fix_blob_seq_ids(str(tmp_path))

    with closing(sqlite3.connect(str(db_path))) as conn:
        row = conn.execute("SELECT seq_id, typeof(seq_id) FROM embeddings").fetchone()
        # Still a BLOB — not converted to 1.23e18.
        assert row == (sysdb10_blob, "blob")


def test_fix_blob_seq_ids_still_converts_legacy_blobs_in_embeddings(tmp_path):
    """Regression guard: pure big-endian u64 BLOBs still convert for genuine 0.6.x."""
    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id)")
        conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", ((42).to_bytes(8, "big"),))
        conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", (b"\x11\x11502607",))
        conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", ((7).to_bytes(8, "big"),))
        conn.commit()

    _fix_blob_seq_ids(str(tmp_path))

    with closing(sqlite3.connect(str(db_path))) as conn:
        rows = conn.execute(
            "SELECT seq_id, typeof(seq_id) FROM embeddings ORDER BY rowid"
        ).fetchall()
        assert rows[0] == (42, "integer")
        assert rows[1] == (b"\x11\x11502607", "blob")  # sysdb-10 row left alone
        assert rows[2] == (7, "integer")


def test_fix_blob_seq_ids_writes_marker_after_blob_path(tmp_path):
    """The .blob_seq_ids_migrated marker is written after a successful BLOB → INTEGER conversion."""
    from mempalace.backends.chroma import _BLOB_FIX_MARKER

    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id)")
        conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", ((42).to_bytes(8, "big"),))
        conn.commit()

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
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id INTEGER)")
        conn.execute("INSERT INTO embeddings (seq_id) VALUES (42)")
        conn.commit()

    marker = tmp_path / _BLOB_FIX_MARKER
    assert not marker.exists()

    _fix_blob_seq_ids(str(tmp_path))

    assert marker.is_file(), "marker must be written even when no BLOBs found"


def test_fix_blob_seq_ids_closes_sqlite_connection(tmp_path, monkeypatch):
    """The migration closes sqlite connections after the pre-open probe."""
    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id INTEGER)")
        conn.execute("INSERT INTO embeddings (seq_id) VALUES (42)")
        conn.commit()

    closed = []
    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("mempalace.backends.chroma.sqlite3.connect", tracking_connect)

    _fix_blob_seq_ids(str(tmp_path))

    assert closed == [True]


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


# ── _fix_missing_collection_type ─────────────────────────────────────────


def test_fix_collection_type_adds_type(tmp_path):
    """Legacy config_json_str '{}' gets _type added."""
    import json

    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, config_json_str TEXT)")
        conn.execute(
            "INSERT INTO collections (id, config_json_str) VALUES (?, ?)",
            ("col-1", "{}"),
        )
        conn.commit()

    _fix_missing_collection_type(str(tmp_path))

    with closing(sqlite3.connect(str(db_path))) as conn:
        row = conn.execute("SELECT config_json_str FROM collections WHERE id = 'col-1'").fetchone()
        config = json.loads(row[0])
        assert config["_type"] == "CollectionConfigurationInternal"


def test_fix_collection_type_preserves_existing(tmp_path):
    """Config that already has _type is left unchanged."""
    import json

    original = json.dumps({"_type": "CollectionConfigurationInternal", "extra": 1})
    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, config_json_str TEXT)")
        conn.execute(
            "INSERT INTO collections (id, config_json_str) VALUES (?, ?)",
            ("col-1", original),
        )
        conn.commit()

    _fix_missing_collection_type(str(tmp_path))

    with closing(sqlite3.connect(str(db_path))) as conn:
        row = conn.execute("SELECT config_json_str FROM collections WHERE id = 'col-1'").fetchone()
        assert row[0] == original


def test_fix_collection_type_noop_without_db(tmp_path):
    """No error when palace has no chroma.sqlite3, no marker written."""
    from mempalace.backends.chroma import _COLLECTION_TYPE_MARKER

    _fix_missing_collection_type(str(tmp_path))
    assert not (tmp_path / _COLLECTION_TYPE_MARKER).exists()


def test_fix_collection_type_writes_marker(tmp_path):
    """Marker is written after a successful migration."""
    from mempalace.backends.chroma import _COLLECTION_TYPE_MARKER

    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, config_json_str TEXT)")
        conn.execute(
            "INSERT INTO collections (id, config_json_str) VALUES (?, ?)",
            ("col-1", "{}"),
        )
        conn.commit()

    marker = tmp_path / _COLLECTION_TYPE_MARKER
    assert not marker.exists()

    _fix_missing_collection_type(str(tmp_path))

    assert marker.is_file()


def test_fix_collection_type_skips_with_marker(tmp_path):
    """When the marker exists, sqlite3 is not opened."""
    from unittest.mock import patch

    from mempalace.backends.chroma import _COLLECTION_TYPE_MARKER

    db_path = tmp_path / "chroma.sqlite3"
    db_path.write_bytes(b"sentinel")
    (tmp_path / _COLLECTION_TYPE_MARKER).touch()

    with patch("mempalace.backends.chroma.sqlite3.connect") as mock_connect:
        _fix_missing_collection_type(str(tmp_path))

    mock_connect.assert_not_called()


def test_fix_collection_type_writes_marker_when_already_has_type(tmp_path):
    """Marker written even when all collections already have _type (noop case)."""
    import json

    from mempalace.backends.chroma import _COLLECTION_TYPE_MARKER

    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, config_json_str TEXT)")
        conn.execute(
            "INSERT INTO collections (id, config_json_str) VALUES (?, ?)",
            ("col-1", json.dumps({"_type": "CollectionConfigurationInternal"})),
        )
        conn.commit()

    marker = tmp_path / _COLLECTION_TYPE_MARKER
    assert not marker.exists()

    _fix_missing_collection_type(str(tmp_path))

    assert marker.is_file(), "marker must be written even when no collections needed fixing"


def test_fix_collection_type_multi_collection_mixed(tmp_path):
    """Multiple collections: NULL, empty, and already-valid configs."""
    import json

    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, config_json_str TEXT)")
        conn.execute("INSERT INTO collections VALUES (?, ?)", ("col-null", None))
        conn.execute("INSERT INTO collections VALUES (?, ?)", ("col-empty", "{}"))
        conn.execute(
            "INSERT INTO collections VALUES (?, ?)",
            ("col-ok", json.dumps({"_type": "CollectionConfigurationInternal"})),
        )
        conn.commit()

    _fix_missing_collection_type(str(tmp_path))

    with closing(sqlite3.connect(str(db_path))) as conn:
        rows = {
            r[0]: json.loads(r[1]) if r[1] else None
            for r in conn.execute("SELECT id, config_json_str FROM collections")
        }
    assert rows["col-null"]["_type"] == "CollectionConfigurationInternal"
    assert rows["col-empty"]["_type"] == "CollectionConfigurationInternal"
    assert rows["col-ok"] == {"_type": "CollectionConfigurationInternal"}


def test_fix_collection_type_skips_non_dict_json(tmp_path):
    """Non-dict JSON (array, null literal) is skipped without error."""
    import json

    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, config_json_str TEXT)")
        conn.execute("INSERT INTO collections VALUES (?, ?)", ("col-arr", "[]"))
        conn.execute("INSERT INTO collections VALUES (?, ?)", ("col-null", "null"))
        conn.execute("INSERT INTO collections VALUES (?, ?)", ("col-ok", "{}"))
        conn.commit()

    _fix_missing_collection_type(str(tmp_path))

    with closing(sqlite3.connect(str(db_path))) as conn:
        rows = dict(conn.execute("SELECT id, config_json_str FROM collections").fetchall())
    assert rows["col-arr"] == "[]"
    assert rows["col-null"] == "null"
    assert json.loads(rows["col-ok"])["_type"] == "CollectionConfigurationInternal"


def test_fix_collection_type_skips_malformed_json(tmp_path):
    """Malformed JSON in one row does not prevent fixing other rows."""
    import json

    db_path = tmp_path / "chroma.sqlite3"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, config_json_str TEXT)")
        conn.execute("INSERT INTO collections VALUES (?, ?)", ("col-bad", "{corrupt"))
        conn.execute("INSERT INTO collections VALUES (?, ?)", ("col-ok", "{}"))
        conn.commit()

    _fix_missing_collection_type(str(tmp_path))

    with closing(sqlite3.connect(str(db_path))) as conn:
        rows = dict(conn.execute("SELECT id, config_json_str FROM collections").fetchall())
    assert rows["col-bad"] == "{corrupt"
    assert json.loads(rows["col-ok"])["_type"] == "CollectionConfigurationInternal"


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


def test_quarantine_stale_hnsw_leaves_empty_segment_without_metadata_alone(tmp_path):
    """Missing metadata is okay only when the segment has no meaningful data yet."""

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


def test_segment_without_metadata_but_with_nontrivial_data_is_unhealthy(tmp_path):
    """Interrupted persist: link_lists written but metadata absent is unhealthy."""

    seg = tmp_path / "abcd-1234-5678"
    seg.mkdir()
    (seg / "data_level0.bin").write_bytes(b"\0" * (_HNSW_MISSING_METADATA_DATA_FLOOR + 1))
    (seg / "link_lists.bin").write_bytes(b"\x01" * 128)

    assert not _segment_appears_healthy(str(seg))


def test_segment_without_metadata_and_tiny_data_is_still_treated_as_fresh(tmp_path):
    """No metadata and no link_lists means no persist was attempted; treat as fresh."""

    seg = tmp_path / "abcd-1234-5678"
    seg.mkdir()
    (seg / "data_level0.bin").write_bytes(b"\0" * _HNSW_MISSING_METADATA_DATA_FLOOR)

    assert _segment_appears_healthy(str(seg))


def test_quarantine_stale_hnsw_renames_missing_metadata_with_nontrivial_data(tmp_path):
    """Regression for #1274: missing pickle + link data must quarantine."""

    now = 1_700_000_000.0
    palace, seg = _make_palace_with_segment(
        tmp_path,
        hnsw_mtime=now - 7200,
        sqlite_mtime=now,
        meta_bytes=None,
    )
    (seg / "data_level0.bin").write_bytes(b"\0" * (_HNSW_MISSING_METADATA_DATA_FLOOR + 1))
    (seg / "link_lists.bin").write_bytes(b"\x01" * 128)
    os.utime(seg / "data_level0.bin", (now - 7200, now - 7200))

    moved = quarantine_stale_hnsw(str(palace), stale_seconds=3600.0)

    assert len(moved) == 1
    assert ".drift-" in moved[0]
    assert not seg.exists()

    drift_dirs = [p for p in palace.iterdir() if ".drift-" in p.name]
    assert len(drift_dirs) == 1
    assert (drift_dirs[0] / "data_level0.bin").exists()


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
    making the mtime heuristic falsely trigger every reconnect.

    Invalid metadata quarantine shares the same cold-start gate here; the
    more aggressive refresh path lives in ``_client()``."""
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

    assert calls == [palace_path], (
        "quarantine_stale_hnsw should fire once per palace per process, not on every reconnect"
    )


def test_make_client_gates_invalid_metadata_on_first_call(tmp_path, monkeypatch):
    """Invalid metadata quarantine is gated on the first make_client() call."""
    from mempalace.backends.chroma import ChromaBackend

    palace_path = str(tmp_path / "palace")
    os.makedirs(palace_path, exist_ok=True)
    (Path(palace_path) / "chroma.sqlite3").write_text("")

    monkeypatch.setattr(ChromaBackend, "_quarantined_paths", set())

    calls: list[str] = []

    def _invalid(path, *args, **kwargs):
        calls.append(path)
        return []

    def _stale(path, stale_seconds=300.0):
        return []

    monkeypatch.setattr("mempalace.backends.chroma.quarantine_invalid_hnsw_metadata", _invalid)
    monkeypatch.setattr("mempalace.backends.chroma.quarantine_stale_hnsw", _stale)

    ChromaBackend.make_client(palace_path)
    ChromaBackend.make_client(palace_path)

    assert calls == [palace_path]


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


# ── _client() cold-start gate (#1121, #1132, #1263) ──────────────────────


def test_client_quarantines_corrupt_segment_on_first_open(tmp_path, monkeypatch):
    """The instance ``_client()`` path must run ``quarantine_stale_hnsw``
    on first open, mirroring the ``make_client()`` static helper. Before
    PR #1173's wiring was extended here, CLI mining / search / repair /
    status all skipped the quarantine pass and would SIGSEGV on a stale
    HNSW segment (#1121, #1132, #1263)."""
    now = 1_700_000_000.0
    palace, seg = _make_palace_with_segment(
        tmp_path,
        hnsw_mtime=now - 7200,
        sqlite_mtime=now,
        meta_bytes=_CORRUPT_META,
    )

    monkeypatch.setattr(ChromaBackend, "_quarantined_paths", set())

    backend = ChromaBackend()
    try:
        backend._client(str(palace))
    finally:
        backend.close()

    assert not seg.exists(), "_client() should have quarantined the corrupt segment"
    drift_dirs = [p for p in palace.iterdir() if ".drift-" in p.name]
    assert len(drift_dirs) == 1


def test_client_quarantines_only_on_first_call_per_palace(tmp_path, monkeypatch):
    """Repeated ``_client()`` calls for the same palace re-run quarantine
    at most once — the ``_quarantined_paths`` gate prevents runtime
    thrash on hot paths (``_client()`` is hit on every backend op)."""
    palace_path = str(tmp_path / "palace")
    os.makedirs(palace_path, exist_ok=True)
    (Path(palace_path) / "chroma.sqlite3").write_text("")

    monkeypatch.setattr(ChromaBackend, "_quarantined_paths", set())

    calls: list[str] = []

    def _spy(path, stale_seconds=300.0):
        calls.append(path)
        return []

    monkeypatch.setattr("mempalace.backends.chroma.quarantine_stale_hnsw", _spy)

    backend = ChromaBackend()
    try:
        backend._client(palace_path)
        backend._client(palace_path)
        backend._client(palace_path)
    finally:
        backend.close()

    assert calls == [palace_path], (
        "quarantine_stale_hnsw should fire once per palace per process from _client(), not on every call"
    )


def test_client_rearms_quarantine_on_mtime_change(tmp_path, monkeypatch):
    """When the DB file's mtime changes between ``_client()`` calls (external
    in-place write), the quarantine gate re-arms so HNSW checks run again.

    Before #1573, the gate was only cleared on *inode* change (full palace
    replacement); mtime-only changes left the gate armed, so long-running
    processes were blind to external drift."""
    palace_path = str(tmp_path / "palace")
    os.makedirs(palace_path, exist_ok=True)
    db_file = Path(palace_path) / "chroma.sqlite3"
    db_file.write_text("")

    monkeypatch.setattr(ChromaBackend, "_quarantined_paths", set())

    calls: list[str] = []

    def _spy(path, stale_seconds=300.0):
        calls.append(path)
        return []

    monkeypatch.setattr("mempalace.backends.chroma.quarantine_stale_hnsw", _spy)

    backend = ChromaBackend()
    try:
        backend._client(palace_path)
        assert len(calls) == 1, "quarantine should fire on first open"

        _, cached_mtime = backend._freshness[palace_path]
        os.utime(str(db_file), (cached_mtime + 1.0, cached_mtime + 1.0))

        backend._client(palace_path)
        assert len(calls) == 2, "quarantine should re-fire after mtime change (gate re-armed)"
    finally:
        backend.close()


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


def test_get_collection_raises_palace_not_found_when_dir_missing(tmp_path):
    """create=False on a missing dir raises PalaceNotFoundError, not the
    new CollectionNotInitializedError. The two states must be distinguishable
    so callers can render state-specific messages (#1498)."""
    missing = tmp_path / "no-such-dir"
    with pytest.raises(PalaceNotFoundError) as excinfo:
        ChromaBackend().get_collection(
            str(missing),
            collection_name="mempalace_drawers",
            create=False,
        )
    # Must be the parent class, not the new subclass: dir is genuinely absent.
    assert not isinstance(excinfo.value, CollectionNotInitializedError)


def test_get_collection_raises_collection_not_initialized_on_empty_palace(tmp_path):
    """When the palace dir + DB exist but the collection has never been
    created, ChromaBackend.get_collection(create=False) raises the new
    CollectionNotInitializedError instead of leaking chromadb.NotFoundError
    (#1498)."""
    palace_path = tmp_path / "palace"
    palace_path.mkdir()
    # PersistentClient lazily creates chroma.sqlite3 — no collection yet.
    chromadb.PersistentClient(path=str(palace_path))
    assert (palace_path / "chroma.sqlite3").is_file()

    with pytest.raises(CollectionNotInitializedError) as excinfo:
        ChromaBackend().get_collection(
            str(palace_path),
            collection_name="mempalace_drawers",
            create=False,
        )
    # Backward-compat: subclass of PalaceNotFoundError (and FileNotFoundError).
    assert isinstance(excinfo.value, PalaceNotFoundError)
    assert isinstance(excinfo.value, FileNotFoundError)


def test_quarantine_invalid_hnsw_metadata_renames_missing_dimensionality(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    seg = palace / "abcd-1234-5678"
    seg.mkdir()
    with open(seg / "index_metadata.pickle", "wb") as f:
        pickle.dump({"dimensionality": None, "id_to_label": {"a": 1}}, f)

    moved = quarantine_invalid_hnsw_metadata(str(palace))

    assert len(moved) == 1
    assert ".corrupt-" in moved[0]
    assert not seg.exists()


def test_quarantine_invalid_hnsw_metadata_keeps_consistent_missing_dimensionality(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    seg = palace / "abcd-1234-5678"
    seg.mkdir()
    (seg / "data_level0.bin").write_bytes(b"x" * 2048)
    (seg / "link_lists.bin").write_bytes(b"x" * 128)
    with open(seg / "index_metadata.pickle", "wb") as f:
        pickle.dump(
            {
                "dimensionality": None,
                "total_elements_added": 2,
                "max_seq_id": None,
                "id_to_label": {"a": 1, "b": 2},
                "label_to_id": {1: "a", 2: "b"},
                "id_to_seq_id": {},
            },
            f,
        )

    moved = quarantine_invalid_hnsw_metadata(str(palace))

    assert moved == []
    assert seg.exists()


def test_quarantine_invalid_hnsw_metadata_keeps_post_deletion_missing_dimensionality(tmp_path):
    """A deleted-from segment has total_elements_added > live label count (the
    counter is monotonic); that dim-None shape is recoverable, not corruption (#1710).
    """
    palace = tmp_path / "palace"
    palace.mkdir()
    seg = palace / "abcd-1234-5678"
    seg.mkdir()
    (seg / "data_level0.bin").write_bytes(b"x" * 2048)
    (seg / "link_lists.bin").write_bytes(b"x" * 128)
    with open(seg / "index_metadata.pickle", "wb") as f:
        pickle.dump(
            {
                "dimensionality": None,
                "total_elements_added": 5,
                "max_seq_id": None,
                "id_to_label": {"a": 1, "b": 2},
                "label_to_id": {1: "a", 2: "b"},
                "id_to_seq_id": {},
            },
            f,
        )

    moved = quarantine_invalid_hnsw_metadata(str(palace))

    assert moved == []
    assert seg.exists()


def test_quarantine_invalid_hnsw_metadata_renames_mismatched_missing_dimensionality(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    seg = palace / "abcd-1234-5678"
    seg.mkdir()
    (seg / "data_level0.bin").write_bytes(b"x" * 2048)
    (seg / "link_lists.bin").write_bytes(b"x" * 128)
    with open(seg / "index_metadata.pickle", "wb") as f:
        pickle.dump(
            {
                "dimensionality": None,
                "total_elements_added": 2,
                "max_seq_id": None,
                "id_to_label": {"a": 1, "b": 2},
                "label_to_id": {1: "b", 2: "a"},
                "id_to_seq_id": {},
            },
            f,
        )

    moved = quarantine_invalid_hnsw_metadata(str(palace))

    assert len(moved) == 1
    assert ".corrupt-" in moved[0]
    assert not seg.exists()


def test_quarantine_invalid_hnsw_metadata_allows_uninitialized_segment(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    seg = palace / "abcd-1234-5678"
    seg.mkdir()
    with open(seg / "index_metadata.pickle", "wb") as f:
        pickle.dump({"dimensionality": None, "id_to_label": {}}, f)

    moved = quarantine_invalid_hnsw_metadata(str(palace))

    assert moved == []
    assert seg.exists()


def test_quarantine_invalid_hnsw_metadata_rejects_non_dict_id_to_label(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    seg = palace / "abcd-1234-5678"
    seg.mkdir()
    with open(seg / "index_metadata.pickle", "wb") as f:
        pickle.dump({"dimensionality": 8, "id_to_label": ["a", "b"]}, f)

    moved = quarantine_invalid_hnsw_metadata(str(palace))

    assert len(moved) == 1
    assert ".corrupt-" in moved[0]
    assert not seg.exists()


def test_quarantine_invalid_hnsw_metadata_rejects_non_schema_payload(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    seg = palace / "abcd-1234-5678"
    seg.mkdir()
    with open(seg / "index_metadata.pickle", "wb") as f:
        pickle.dump(["not", "a", "metadata", "object"], f)

    moved = quarantine_invalid_hnsw_metadata(str(palace))

    assert len(moved) == 1
    assert ".corrupt-" in moved[0]
    assert not seg.exists()


def _dangerous_pickle_payload_executed():
    raise AssertionError("unsafe pickle payload executed")


class _DangerousPickle:
    def __reduce__(self):
        return (_dangerous_pickle_payload_executed, ())


def test_quarantine_invalid_hnsw_metadata_rejects_unsafe_pickle(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    seg = palace / "abcd-1234-5678"
    seg.mkdir()
    with open(seg / "index_metadata.pickle", "wb") as f:
        pickle.dump(_DangerousPickle(), f)

    moved = quarantine_invalid_hnsw_metadata(str(palace))

    assert len(moved) == 1
    assert ".corrupt-" in moved[0]
    assert not seg.exists()


def test_quarantine_invalid_hnsw_metadata_skips_transient_read_errors(tmp_path, monkeypatch):
    palace = tmp_path / "palace"
    palace.mkdir()
    seg = palace / "abcd-1234-5678"
    seg.mkdir()
    meta = seg / "index_metadata.pickle"
    meta.write_bytes(b"partial")

    monkeypatch.setattr(
        "mempalace.backends.chroma._SafePersistentDataUnpickler.load",
        lambda path: (_ for _ in ()).throw(EOFError("flush in progress")),
    )

    moved = quarantine_invalid_hnsw_metadata(str(palace))

    assert moved == []
    assert seg.exists()


def test_quarantine_invalid_hnsw_metadata_skips_truncated_pickle(tmp_path, monkeypatch):
    palace = tmp_path / "palace"
    palace.mkdir()
    seg = palace / "abcd-1234-5678"
    seg.mkdir()
    meta = seg / "index_metadata.pickle"
    meta.write_bytes(b"partial")

    monkeypatch.setattr(
        "mempalace.backends.chroma._SafePersistentDataUnpickler.load",
        lambda path: (_ for _ in ()).throw(pickle.UnpicklingError("pickle data was truncated")),
    )

    moved = quarantine_invalid_hnsw_metadata(str(palace))

    assert moved == []
    assert seg.exists()


def test_chroma_backend_preflights_metadata_before_persistent_client(tmp_path, monkeypatch):
    palace = tmp_path / "palace"
    palace.mkdir()
    calls = []

    def _record(name):
        def inner(path, *args, **kwargs):
            calls.append((name, path))
            return [] if name != "blob" else None

        return inner

    monkeypatch.setattr(
        "mempalace.backends.chroma._fix_missing_collection_type", _record("collection_type")
    )
    monkeypatch.setattr("mempalace.backends.chroma._fix_blob_seq_ids", _record("blob"))
    monkeypatch.setattr(
        "mempalace.backends.chroma.quarantine_invalid_hnsw_metadata", _record("invalid")
    )
    monkeypatch.setattr("mempalace.backends.chroma.quarantine_stale_hnsw", _record("stale"))

    class DummyClient:
        pass

    monkeypatch.setattr(
        "mempalace.backends.chroma.chromadb.PersistentClient", lambda path: DummyClient()
    )

    backend = ChromaBackend()
    backend._client(str(palace))

    assert calls == [
        ("collection_type", str(palace)),
        ("blob", str(palace)),
        ("invalid", str(palace)),
        ("stale", str(palace)),
    ]


def test_chroma_backend_quarantine_rearms_on_mtime_refresh(tmp_path, monkeypatch):
    """When the DB mtime changes between ``_client()`` calls, the quarantine
    gate re-arms and the HNSW safety checks run again (#1573)."""
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "chroma.sqlite3").write_text("")
    calls = []

    def _record(name):
        def inner(path, *args, **kwargs):
            calls.append((name, path))
            return [] if name != "blob" else None

        return inner

    monkeypatch.setattr(ChromaBackend, "_quarantined_paths", set())
    monkeypatch.setattr(
        "mempalace.backends.chroma._fix_missing_collection_type", _record("collection_type")
    )
    monkeypatch.setattr("mempalace.backends.chroma._fix_blob_seq_ids", _record("blob"))
    monkeypatch.setattr(
        "mempalace.backends.chroma.quarantine_invalid_hnsw_metadata", _record("invalid")
    )
    monkeypatch.setattr("mempalace.backends.chroma.quarantine_stale_hnsw", _record("stale"))

    class DummyClient:
        pass

    monkeypatch.setattr(
        "mempalace.backends.chroma.chromadb.PersistentClient", lambda path: DummyClient()
    )

    backend = ChromaBackend()
    stats = iter([(1, 1.0), (1, 1.0), (1, 2.0), (1, 2.0)])
    monkeypatch.setattr(backend, "_db_stat", lambda path: next(stats))

    backend._client(str(palace))
    backend._client(str(palace))

    assert calls == [
        ("collection_type", str(palace)),
        ("blob", str(palace)),
        ("invalid", str(palace)),
        ("stale", str(palace)),
        ("collection_type", str(palace)),
        ("blob", str(palace)),
        ("invalid", str(palace)),
        ("stale", str(palace)),
    ]


def test_chroma_backend_requarantines_after_inode_replacement(tmp_path, monkeypatch):
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "chroma.sqlite3").write_text("")
    calls = []

    def _record(name):
        def inner(path, *args, **kwargs):
            calls.append((name, path))
            return [] if name != "blob" else None

        return inner

    monkeypatch.setattr(ChromaBackend, "_quarantined_paths", set())
    monkeypatch.setattr(
        "mempalace.backends.chroma._fix_missing_collection_type", _record("collection_type")
    )
    monkeypatch.setattr("mempalace.backends.chroma._fix_blob_seq_ids", _record("blob"))
    monkeypatch.setattr(
        "mempalace.backends.chroma.quarantine_invalid_hnsw_metadata", _record("invalid")
    )
    monkeypatch.setattr("mempalace.backends.chroma.quarantine_stale_hnsw", _record("stale"))

    class DummyClient:
        pass

    monkeypatch.setattr(
        "mempalace.backends.chroma.chromadb.PersistentClient", lambda path: DummyClient()
    )

    backend = ChromaBackend()
    stats = iter([(1, 1.0), (1, 1.0), (2, 2.0), (2, 2.0)])
    monkeypatch.setattr(backend, "_db_stat", lambda path: next(stats))

    backend._client(str(palace))
    backend._client(str(palace))

    assert calls == [
        ("collection_type", str(palace)),
        ("blob", str(palace)),
        ("invalid", str(palace)),
        ("stale", str(palace)),
        ("collection_type", str(palace)),
        ("blob", str(palace)),
        ("invalid", str(palace)),
        ("stale", str(palace)),
    ]


def test_explain_ef_mismatch_recognizes_chromadb_conflict():
    """When ChromaDB rejects a collection read due to an EF-name mismatch
    (user changed MEMPALACE_EMBEDDING_MODEL on an existing palace), the
    backend wraps the bare ValueError with a message that tells the user
    how to recover. Without this, users hit a stack trace and don't know
    rebuild-index exists."""
    err = ValueError(
        "An embedding function already exists in the collection configuration, "
        "and a new one is provided. Embedding function conflict: new: "
        "embeddinggemma_300m vs persisted: default"
    )
    msg = ChromaBackend._explain_ef_mismatch(err, "/tmp/palace.db")
    assert msg is not None
    assert "/tmp/palace.db" in msg
    assert "MEMPALACE_EMBEDDING_MODEL" in msg
    assert "rebuild-index" in msg


def test_explain_ef_mismatch_returns_none_for_unrelated_errors():
    """Don't paper over unrelated ValueErrors with the EF-mismatch message —
    the caller needs to re-raise unmodified so debugging stays sane."""
    err = ValueError("Some other ChromaDB problem")
    assert ChromaBackend._explain_ef_mismatch(err, "/tmp/palace.db") is None


def test_get_collection_translates_ef_mismatch_to_helpful_error(tmp_path):
    """End-to-end: create a palace with the default EF, then try to read it
    with a different EF name and confirm we surface the rebuild-index hint."""
    backend = ChromaBackend()
    palace_path = str(tmp_path / "palace")
    os.makedirs(palace_path, exist_ok=True)

    # Create the collection using the default (minilm-based) EF.
    coll = backend.get_collection(palace_path, "drawers", create=True)
    coll.add(documents=["seed"], ids=["1"])

    # Now swap in an incompatible EF name (simulates the user setting
    # MEMPALACE_EMBEDDING_MODEL=embeddinggemma without rebuild-index).
    class _ConflictingEF:
        @staticmethod
        def name() -> str:
            return "embeddinggemma_300m"

        def __call__(self, input):
            return [[0.0] * _TEST_EMBED_DIM for _ in input]

    original_resolver = backend._resolve_embedding_function
    backend._resolve_embedding_function = lambda: _ConflictingEF()
    # Drop the cached client so the next call goes through the open path.
    backend.close_palace(palace_path)

    try:
        with pytest.raises(ValueError, match=r"rebuild-index"):
            backend.get_collection(palace_path, "drawers", create=False)
    finally:
        backend._resolve_embedding_function = original_resolver
        backend.close_palace(palace_path)


def test_palace_get_collection_uses_configured_collection_name(monkeypatch):
    from mempalace import palace

    captured = {}

    def fake_get_collection(palace_path, collection_name=None, create=False):
        captured["palace_path"] = palace_path
        captured["collection_name"] = collection_name
        captured["create"] = create
        return object()

    monkeypatch.setattr(palace._DEFAULT_BACKEND, "get_collection", fake_get_collection)
    monkeypatch.setattr("mempalace.config.get_configured_collection_name", lambda: "custom_drawers")

    palace.get_collection("/palace", create=False)

    assert captured == {
        "palace_path": "/palace",
        "collection_name": "custom_drawers",
        "create": False,
    }
