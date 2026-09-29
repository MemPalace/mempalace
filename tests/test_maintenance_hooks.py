"""Backend maintenance hooks (RFC 001).

Maintenance is observable, not fire-and-forget: ``run_maintenance(kind)``
returns a ``MaintenanceResult`` and MUST serialize concurrent same-kind runs.
The pgvector ``reindex`` path (the opt-in HNSW build) is exercised here with a
fake client so the advisory-lock flow is tested without a live Postgres.
"""

import pytest

from mempalace.backends.base import (
    BaseCollection,
    MaintenanceResult,
    PalaceRef,
    UnsupportedMaintenanceKindError,
)


# ---------------------------------------------------------------------------
# Contract surface
# ---------------------------------------------------------------------------


def test_maintenance_result_shape():
    r = MaintenanceResult(kind="reindex", status="ran", stats={"ms": 12})
    assert r.kind == "reindex" and r.status == "ran" and r.stats["ms"] == 12
    assert MaintenanceResult(kind="analyze", status="noop").stats == {}


def test_default_collection_rejects_all_kinds():
    class _Col(BaseCollection):
        def add(self, **k): ...
        def upsert(self, **k): ...
        def query(self, **k): ...
        def get(self, **k): ...
        def delete(self, **k): ...
        def count(self):
            return 0

    col = _Col()
    assert col.maintenance_state() == {}
    with pytest.raises(UnsupportedMaintenanceKindError):
        col.run_maintenance("analyze")


def test_backend_maintenance_kinds_declared():
    from mempalace.backends.chroma import ChromaBackend
    from mempalace.backends.pgvector import PgVectorBackend
    from mempalace.backends.qdrant import QdrantBackend
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    assert SQLiteExactBackend.maintenance_kinds == frozenset({"analyze", "compact"})
    assert PgVectorBackend.maintenance_kinds == frozenset({"analyze", "reindex"})
    # qdrant self-optimizes; chroma maintenance is the separate repair CLI.
    assert QdrantBackend.maintenance_kinds == frozenset()
    assert ChromaBackend.maintenance_kinds == frozenset()


# ---------------------------------------------------------------------------
# sqlite_exact (CI-runnable, real backend)
# ---------------------------------------------------------------------------


def _sqlite_collection(tmp_path, rows=20):
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    col = SQLiteExactBackend().get_collection(
        palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
        collection_name="mempalace_drawers",
        create=True,
    )
    for i in range(rows):
        col.add(
            documents=[f"doc {i}"],
            ids=[f"id{i}"],
            metadatas=[{}],
            embeddings=[[0.1, 0.2, 0.3, 0.4]],
        )
    return col


def test_sqlite_maintenance_state(tmp_path):
    col = _sqlite_collection(tmp_path, rows=5)
    state = col.maintenance_state()
    assert state["row_count"] == 5
    assert state["vector_index"] is None  # exact scan — no ANN index
    assert "page_count" in state and "freelist_pages" in state


def test_sqlite_analyze_runs(tmp_path):
    col = _sqlite_collection(tmp_path, rows=5)
    r = col.run_maintenance("analyze")
    assert r.kind == "analyze" and r.status == "ran"


def test_sqlite_compact_runs_and_reports_pages(tmp_path):
    col = _sqlite_collection(tmp_path, rows=30)
    col.delete(ids=[f"id{i}" for i in range(20)])
    r = col.run_maintenance("compact")
    assert r.kind == "compact" and r.status == "ran"
    assert "pages_reclaimed" in r.stats


def test_sqlite_omits_reindex(tmp_path):
    # sqlite_exact has no ANN index, so reindex is omitted, not no-op'd.
    col = _sqlite_collection(tmp_path, rows=2)
    with pytest.raises(UnsupportedMaintenanceKindError):
        col.run_maintenance("reindex")


def test_sqlite_unknown_kind_raises(tmp_path):
    col = _sqlite_collection(tmp_path, rows=2)
    with pytest.raises(UnsupportedMaintenanceKindError):
        col.run_maintenance("bogus")


# ---------------------------------------------------------------------------
# pgvector advisory-lock reindex flow (fake client, no live Postgres)
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, has_index=False):
        self.has_index = has_index
        self.locked = False
        self.created = 0
        self.analyzed = 0

    def table_exists(self, table):
        return True

    def count_rows(self, table):
        return 7

    def has_vector_index(self, table):
        return self.has_index

    def try_advisory_lock(self, classid, objid):
        if self.locked:
            return False
        self.locked = True
        return True

    def advisory_unlock(self, classid, objid):
        self.locked = False

    def create_hnsw_index(self, table):
        self.has_index = True
        self.created += 1

    def analyze_table(self, table):
        self.analyzed += 1


class _FakeBackend:
    _closed = False


def _pg_collection(client):
    from mempalace.backends.pgvector import PgVectorCollection, _PgVectorConfig

    return PgVectorCollection(
        backend=_FakeBackend(),
        client=client,
        config=_PgVectorConfig(dsn="postgresql://example", namespace=None),
        palace=PalaceRef(id="/tmp/p", local_path="/tmp/p"),
        collection_name="mempalace_drawers",
        table="mp_drawers_t",
    )


def test_pgvector_reindex_builds_index_under_lock():
    client = _FakeClient(has_index=False)
    col = _pg_collection(client)
    r = col.run_maintenance("reindex")
    assert r.status == "ran" and r.stats.get("vector_index") == "hnsw"
    assert client.created == 1
    assert client.locked is False  # lock released in finally


def test_pgvector_reindex_noop_when_index_exists():
    client = _FakeClient(has_index=True)
    col = _pg_collection(client)
    r = col.run_maintenance("reindex")
    assert r.status == "noop"
    assert client.created == 0  # never attempted a build


def test_pgvector_reindex_already_running_when_lock_held():
    client = _FakeClient(has_index=False)
    client.locked = True  # another session is building
    col = _pg_collection(client)
    r = col.run_maintenance("reindex")
    assert r.status == "already_running"
    assert client.created == 0  # did not re-trigger the build


def test_pgvector_analyze_runs():
    client = _FakeClient()
    col = _pg_collection(client)
    r = col.run_maintenance("analyze")
    assert r.status == "ran" and client.analyzed == 1


def test_pgvector_unknown_kind_raises():
    col = _pg_collection(_FakeClient())
    with pytest.raises(UnsupportedMaintenanceKindError):
        col.run_maintenance("compact")  # pgvector omits compact (autovacuum)


def test_pgvector_maintenance_state_reports_index():
    col = _pg_collection(_FakeClient(has_index=True))
    state = col.maintenance_state()
    assert state["row_count"] == 7
    assert state["vector_index"] == "hnsw" and state["index_build_complete"] is True


def test_pgvector_maintenance_noop_when_table_missing():
    # Collection opened create=True but never written: no table yet. Maintenance
    # must noop, not let a raw "relation does not exist" error escape.
    client = _FakeClient()
    client.table_exists = lambda table: False
    col = _pg_collection(client)
    assert col.run_maintenance("reindex").status == "noop"
    assert col.run_maintenance("analyze").status == "noop"
    assert col.maintenance_state()["row_count"] == 0


def test_hnsw_index_name_never_collides_with_table_name():
    # A naive [:63] truncation would return a 63-char table name verbatim,
    # colliding in pg_class. _pg_identifier hashes the overflow instead.
    from mempalace.backends.pgvector import _hnsw_index_name

    for table in ("t", "mp_drawers", "x" * 63, "y" * 200):
        name = _hnsw_index_name(table)
        assert name != table
        assert len(name.encode("utf-8")) <= 63


def test_pgvector_advisory_key_is_signed_int4_and_stable():
    from mempalace.backends.pgvector import _MAINTENANCE_LOCK_CLASSID, _advisory_objid

    for table in ("a", "mempalace_drawers_xyz", "x" * 80):
        objid = _advisory_objid(table)
        assert -(2**31) <= objid < 2**31
        assert _advisory_objid(table) == objid  # stable
    assert -(2**31) <= _MAINTENANCE_LOCK_CLASSID < 2**31


# ---------------------------------------------------------------------------
# EmbeddingCollection delegation
# ---------------------------------------------------------------------------


def test_embeddingcollection_delegates_maintenance():
    from mempalace.backends.embedding_wrapper import EmbeddingCollection

    class _Inner(BaseCollection):
        def add(self, **k): ...
        def upsert(self, **k): ...
        def query(self, **k): ...
        def get(self, **k): ...
        def delete(self, **k): ...
        def count(self):
            return 0

        def maintenance_state(self):
            return {"row_count": 3}

        def run_maintenance(self, kind):
            return MaintenanceResult(kind=kind, status="ran")

    wrapped = EmbeddingCollection(_Inner())
    assert wrapped.maintenance_state() == {"row_count": 3}
    assert wrapped.run_maintenance("analyze").status == "ran"
