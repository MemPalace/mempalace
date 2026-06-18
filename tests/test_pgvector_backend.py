import os
import sys
import threading
import types

import pytest

from _backend_conformance import assert_partition_isolation

from mempalace.backends import (
    BackendError,
    BackendMismatchError,
    CollectionNotInitializedError,
    DimensionMismatchError,
    PalaceRef,
    available_backends,
)
from mempalace.backends.pgvector import (
    PgVectorBackend,
    _PgVectorClient,
    _PgVectorConfig,
    _matches_where,
    _vector_distance,
    _as_vector_array,
)


class _FakePgVectorClient:
    """In-memory stand-in for the psycopg-backed client.

    Stores rows per table so the same-instance/different-table isolation the
    real backend gets from Postgres is exercised deterministically in CI. The
    real client pushes filters/ranking to SQL; this fake applies the same
    Python filter + cosine ranking the local-fallback path uses.
    """

    instances: list = []

    def __init__(self, _config):
        self.tables: dict = {}
        self.query_calls: list = []
        _FakePgVectorClient.instances.append(self)

    def ping(self):
        return None

    def ensure_extension(self):
        return None

    def table_exists(self, table):
        return table in self.tables

    def table_dimension(self, table):
        return self.tables.get(table, {}).get("dimension")

    def create_table(self, table, dimension):
        self.tables.setdefault(table, {"dimension": dimension, "rows": {}})

    def upsert_rows(self, table, rows):
        store = self.tables.setdefault(
            table,
            {"dimension": len(rows[0]["embedding"]) if rows else 0, "rows": {}},
        )
        for row in rows:
            store["rows"][row["id"]] = dict(row)

    def _filtered(self, table, where):
        rows = list(self.tables.get(table, {"rows": {}})["rows"].values())
        return [row for row in rows if _matches_where(row.get("metadata") or {}, where)]

    def query_rows(self, table, *, vector, limit, where, with_embedding):
        self.query_calls.append(where)
        q = _as_vector_array(vector)
        scored = []
        for row in self._filtered(table, where):
            distance = _vector_distance(q, row.get("embedding"))
            if distance is not None:
                scored.append((distance, row))
        scored.sort(key=lambda item: item[0])
        out = []
        for distance, row in scored[:limit]:
            item = {
                "id": row["id"],
                "document": row["document"],
                "metadata": row.get("metadata") or {},
                "embedding": row.get("embedding") if with_embedding else None,
                "distance": distance,
            }
            out.append(item)
        return out

    def scroll_rows(self, table, *, where=None, with_embedding=False):
        out = []
        for row in self._filtered(table, where):
            out.append(
                {
                    "id": row["id"],
                    "document": row["document"],
                    "metadata": row.get("metadata") or {},
                    "embedding": row.get("embedding") if with_embedding else None,
                    "distance": None,
                }
            )
        return out

    def delete_rows(self, table, *, ids=None, where=None):
        rows = self.tables.get(table, {"rows": {}})["rows"]
        if ids is not None:
            for doc_id in ids:
                rows.pop(doc_id, None)
            return
        for doc_id, row in list(rows.items()):
            if _matches_where(row.get("metadata") or {}, where):
                rows.pop(doc_id, None)

    def count_rows(self, table):
        return len(self.tables.get(table, {"rows": {}})["rows"])

    def drop_table(self, table):
        self.tables.pop(table, None)

    def close(self):
        return None


@pytest.fixture
def fake_pgvector(monkeypatch):
    import mempalace.backends.pgvector as pgvector

    _FakePgVectorClient.instances.clear()
    monkeypatch.setattr(pgvector, "_PgVectorClient", _FakePgVectorClient)
    monkeypatch.delenv("MEMPALACE_PGVECTOR_DSN", raising=False)
    monkeypatch.delenv("MEMPALACE_PGVECTOR_NAMESPACE", raising=False)
    return _FakePgVectorClient


def _collection(tmp_path, name="drawers"):
    backend = PgVectorBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    return backend, backend.get_collection(palace=palace, collection_name=name, create=True)


def test_registry_exposes_pgvector():
    assert "pgvector" in available_backends()


def test_pgvector_add_query_filters_lexical_and_marker(tmp_path, fake_pgvector):
    backend, col = _collection(tmp_path)
    assert not os.path.isfile(tmp_path / "pgvector_backend.json")

    col.add(
        ids=["a", "b", "c"],
        documents=[
            "alpha backend note",
            "rareterm pgvector backend note",
            "frontend design note",
        ],
        metadatas=[
            {"wing": "project", "room": "backend", "rank": 1},
            {"wing": "project", "room": "backend", "rank": 3},
            {"wing": "project", "room": "frontend", "rank": 2},
        ],
        embeddings=[[1, 0], [0.9, 0.1], [0, 1]],
    )

    assert PgVectorBackend.detect(str(tmp_path))
    assert os.path.isfile(tmp_path / "pgvector_backend.json")
    assert col.count() == 3

    # Equality filter is pushed down (no local fallback); $in stays pushdown.
    result = col.query(
        query_embeddings=[[1, 0]],
        n_results=3,
        where={"wing": "project"},
        include=["documents", "metadatas", "distances", "embeddings"],
    )
    assert result.ids[0][0] == "a"
    assert set(result.ids[0]) == {"a", "b", "c"}
    assert result.embeddings[0][0] == pytest.approx([1.0, 0.0])

    hits = col.lexical_search(query="rareterm backend", n_results=2, where={"wing": "project"}).hits
    assert [hit.id for hit in hits] == ["b", "a"]

    backend.close_palace(str(tmp_path))
    with pytest.raises(Exception):
        col.count()


def test_pgvector_requires_explicit_embeddings(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    with pytest.raises(ValueError, match="explicit embeddings"):
        col.add(ids=["a"], documents=["no vector"], metadatas=[{}])


def test_pgvector_marker_not_written_when_first_write_fails(tmp_path, fake_pgvector, monkeypatch):
    _backend, col = _collection(tmp_path)
    fake_client = fake_pgvector.instances[0]

    def fail_upsert(*_args, **_kwargs):
        raise RuntimeError("pg unavailable")

    monkeypatch.setattr(fake_client, "upsert_rows", fail_upsert)

    with pytest.raises(RuntimeError):
        col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])

    assert not os.path.isfile(tmp_path / "pgvector_backend.json")


def test_pgvector_dimension_mismatch(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    with pytest.raises(DimensionMismatchError):
        col.upsert(ids=["b"], documents=["two"], metadatas=[{}], embeddings=[[1, 0, 0]])


def test_pgvector_add_rejects_duplicate_ids_in_same_batch(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    with pytest.raises(ValueError, match="unique"):
        col.add(
            ids=["a", "a"], documents=["x", "y"], metadatas=[{}, {}], embeddings=[[1, 0], [0, 1]]
        )


def test_pgvector_complex_filters_use_local_fallback(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=["alpha", "beta", "gamma"],
        metadatas=[
            {"wing": "x", "rank": 1, "tags": "core,vector"},
            {"wing": "y", "rank": 3, "tags": "sqlite,exact"},
            {"wing": "z", "rank": 2, "tags": "old"},
        ],
        embeddings=[[1, 0], [0.9, 0.1], [0, 1]],
    )

    # $or, $contains and comparisons must route to the local exact path and
    # still return the correct rows.
    or_hits = col.get(where={"$or": [{"wing": "x"}, {"wing": "z"}]})
    assert set(or_hits.ids) == {"a", "c"}

    contains = col.get(where={"tags": {"$contains": "sqlite"}})
    assert contains.ids == ["b"]

    ranked = col.query(query_embeddings=[[1, 0]], n_results=3, where={"rank": {"$gte": 2}})
    assert set(ranked.ids[0]) == {"b", "c"}


def test_pgvector_marker_participates_in_backend_mismatch(tmp_path, fake_pgvector):
    from mempalace.palace import resolve_backend_name

    _backend, col = _collection(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])

    assert resolve_backend_name(str(tmp_path)) == "pgvector"
    with pytest.raises(BackendMismatchError):
        resolve_backend_name(str(tmp_path), explicit="qdrant")


def test_pgvector_marker_rejects_target_change(tmp_path, fake_pgvector, monkeypatch):
    _backend, col = _collection(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])

    backend2 = PgVectorBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    with pytest.raises(BackendMismatchError):
        backend2.get_collection(
            palace=palace,
            collection_name="drawers",
            create=True,
            options={"dsn": "postgresql://other-host:5432/other"},
        )


def test_pgvector_rejects_pure_remote_palace(tmp_path, fake_pgvector):
    """No local_path means the marker (the only mismatch-protection anchor)
    cannot be written or validated, so the backend refuses rather than silently
    opening an unprotected table (RFC 001 isolation contract, PR #1679)."""
    backend = PgVectorBackend()
    palace = PalaceRef(id="tenant-remote", local_path=None, namespace="tenant-remote")
    with pytest.raises(BackendError, match="local palace path"):
        backend.get_collection(palace=palace, collection_name="drawers", create=True)


def test_pgvector_missing_table_after_marker_is_not_initialized(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    fake_pgvector.instances[0].drop_table(col._table)

    assert col.health().ok is False
    with pytest.raises(CollectionNotInitializedError):
        col.count()


def test_pgvector_cross_palace_isolation_conformance(tmp_path, fake_pgvector):
    """Shared per-PalaceRef.id isolation conformance (RFC 001 isolation contract)."""
    backend = PgVectorBackend()
    cols = []
    for label in ("alpha", "beta"):
        path = tmp_path / label
        ref = PalaceRef(id=str(path), local_path=str(path))
        cols.append(backend.get_collection(palace=ref, collection_name="drawers", create=True))
    # Same backend + same DSN → same client instance, distinct tables.
    assert cols[0]._table != cols[1]._table
    assert_partition_isolation(backend, cols[0], cols[1], embedding=[1.0, 0.0])


def test_pgvector_namespace_isolation_conformance(tmp_path, fake_pgvector):
    """Shared per-PalaceRef.namespace isolation conformance — pgvector advertises
    ``supports_namespace_isolation`` (RFC 001 isolation contract)."""
    assert "supports_namespace_isolation" in PgVectorBackend.capabilities
    backend = PgVectorBackend()
    ref_a = PalaceRef(
        id=str(tmp_path / "tenant-a"),
        local_path=str(tmp_path / "tenant-a"),
        namespace="tenant-a",
    )
    ref_b = PalaceRef(
        id=str(tmp_path / "tenant-b"),
        local_path=str(tmp_path / "tenant-b"),
        namespace="tenant-b",
    )
    col_a = backend.get_collection(palace=ref_a, collection_name="drawers", create=True)
    col_b = backend.get_collection(palace=ref_b, collection_name="drawers", create=True)
    # Mechanism: the namespace partitions the table name.
    assert col_a._table != col_b._table
    assert "tenant_a" in col_a._table and "tenant_b" in col_b._table
    # Behaviour: a record under one namespace is invisible under the other.
    assert_partition_isolation(backend, col_a, col_b, embedding=[1.0, 0.0])


def test_pgvector_update_merges_documents_and_metadata(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b"],
        documents=["alpha", "beta"],
        metadatas=[{"wing": "x", "rank": 1}, {"wing": "y", "rank": 2}],
        embeddings=[[1, 0], [0, 1]],
    )
    col.update(ids=["a"], documents=["alpha-2"], metadatas=[{"rank": 9}])
    got = col.get(ids=["a"], include=["documents", "metadatas"])
    assert got.documents == ["alpha-2"]
    # merge keeps the untouched key and overrides the updated one.
    assert got.metadatas[0] == {"wing": "x", "rank": 9}
    # untouched row is unchanged.
    assert col.get(ids=["b"]).ids == ["b"]
    with pytest.raises(ValueError, match="at least one"):
        col.update(ids=["a"])


def test_pgvector_get_limit_offset_and_embeddings(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=["alpha", "beta", "gamma"],
        metadatas=[{"wing": "x"}, {"wing": "x"}, {"wing": "x"}],
        embeddings=[[1, 0], [0, 1], [0.5, 0.5]],
    )
    page = col.get(where={"wing": "x"}, limit=1, offset=1, include=["documents", "embeddings"])
    assert len(page.ids) == 1
    assert page.embeddings is not None and len(page.embeddings[0]) == 2


def test_pgvector_delete_by_where_pushdown_and_local(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=["alpha", "beta", "gamma"],
        metadatas=[{"wing": "x"}, {"wing": "y"}, {"wing": "z"}],
        embeddings=[[1, 0], [0, 1], [0.5, 0.5]],
    )
    # pushdown equality delete
    col.delete(where={"wing": "y"})
    assert set(col.get().ids) == {"a", "c"}
    # local-fallback delete ($or routes through the exact path)
    col.delete(where={"$or": [{"wing": "x"}, {"wing": "z"}]})
    assert col.count() == 0


def test_pgvector_query_dimension_mismatch_against_known_dim(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.add(ids=["a"], documents=["alpha"], metadatas=[{}], embeddings=[[1, 0]])
    with pytest.raises(DimensionMismatchError):
        col.query(query_embeddings=[[1, 0, 0]], n_results=1)


def test_pgvector_get_collection_positional_and_palace_path_forms(tmp_path, fake_pgvector):
    backend = PgVectorBackend()
    col = backend.get_collection(str(tmp_path / "p1"), "drawers", create=True)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    assert col.count() == 1
    col2 = backend.get_collection(
        palace_path=str(tmp_path / "p2"), collection_name="drawers", create=True
    )
    col2.upsert(ids=["b"], documents=["two"], metadatas=[{}], embeddings=[[1, 0]])
    assert col2.count() == 1
    assert col._table != col2._table


def test_pgvector_health_and_delete_collection(tmp_path, fake_pgvector):
    backend = PgVectorBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    col = backend.get_collection(palace=palace, collection_name="drawers", create=True)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    assert col.health().ok is True
    assert backend.health(palace).ok is True
    backend.delete_collection(str(tmp_path), "drawers")
    assert col.health().ok is False


def test_pgvector_close_marks_backend_closed(tmp_path, fake_pgvector):
    backend = PgVectorBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    col = backend.get_collection(palace=palace, collection_name="drawers", create=True)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    backend.close()
    with pytest.raises(BackendError):
        backend.get_collection(palace=palace, collection_name="drawers", create=True)


def test_pgvector_marker_unreadable_raises_mismatch(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    marker = tmp_path / "pgvector_backend.json"
    marker.write_text("{ not json", encoding="utf-8")
    backend2 = PgVectorBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    with pytest.raises(BackendMismatchError):
        backend2.get_collection(palace=palace, collection_name="drawers", create=True)


def test_pgvector_dsn_resolved_from_env(tmp_path, fake_pgvector, monkeypatch):
    from mempalace.backends.pgvector import _PgVectorConfig

    monkeypatch.setenv("MEMPALACE_PGVECTOR_DSN", "postgresql://example:5432/memdb")
    monkeypatch.setenv("MEMPALACE_PGVECTOR_NAMESPACE", "team-a")
    config = _PgVectorConfig.from_options()
    assert config.dsn == "postgresql://example:5432/memdb"
    assert config.namespace == "team-a"


def test_palace_wrapper_embeds_for_pgvector(tmp_path, monkeypatch, fake_pgvector):
    import mempalace.backends.embedding_wrapper as embedding_wrapper
    from mempalace import palace

    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts: [[1.0, 0.0] for _ in texts]
    )
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "pgvector")
    monkeypatch.setenv("MEMPALACE_BACKEND", "pgvector")

    col = palace.get_collection(str(tmp_path), "mempalace_drawers", create=True)
    col.add(documents=["wrapped pgvector document"], ids=["wrapped"], metadatas=[{"wing": "w"}])
    result = col.query(query_texts=["wrapped"], n_results=1)
    assert result.ids == [["wrapped"]]


def test_pgvector_live_roundtrip_when_enabled(tmp_path):
    live_url = os.environ.get("MEMPALACE_PGVECTOR_LIVE_URL")
    if not live_url:
        pytest.skip("set MEMPALACE_PGVECTOR_LIVE_URL to run live Postgres pgvector test")

    backend = PgVectorBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path), namespace="livetest")
    col = backend.get_collection(
        palace=palace,
        collection_name="drawers",
        create=True,
        options={"dsn": live_url},
    )
    try:
        col.upsert(
            ids=["live-a", "live-b"],
            documents=["rareterm live pgvector backend", "other live document"],
            metadatas=[{"wing": "live", "rank": 2}, {"wing": "other", "rank": 1}],
            embeddings=[[1.0, 0.0], [0.0, 1.0]],
        )
        assert PgVectorBackend.detect(str(tmp_path))
        assert col.count() == 2

        result = col.query(query_embeddings=[[1.0, 0.0]], n_results=2, where={"wing": "live"})
        assert result.ids == [["live-a"]]

        hits = col.lexical_search(query="rareterm", n_results=1).hits
        assert hits and hits[0].id == "live-a"

        col.delete(ids=["live-a"])
        assert col.get(ids=["live-a"]).ids == []

        # Reopen the existing table in a fresh backend and write another
        # same-dimension vector. This exercises table_dimension() against a
        # live vector(n) column — a regression guard for reading the dimension
        # off the raw atttypmod (which is not the bare n) and falsely raising
        # DimensionMismatchError on reopen.
        backend.close()
        backend = PgVectorBackend()
        reopened = backend.get_collection(
            palace=palace,
            collection_name="drawers",
            create=False,
            options={"dsn": live_url},
        )
        reopened.upsert(
            ids=["live-c"],
            documents=["third live document"],
            metadatas=[{"wing": "live", "rank": 3}],
            embeddings=[[0.5, 0.5]],
        )
        assert reopened.count() == 2
    finally:
        try:
            backend.delete_collection(str(tmp_path), "drawers")
        except Exception:
            pass
        backend.close()


def test_client_concurrent_first_connect_single_connection(monkeypatch):
    """Two threads racing ``_execute`` through the first ``_connect`` must end
    up on one shared connection.

    The barrier inside the fake ``psycopg.connect`` releases immediately only
    when both threads pass the ``self._conn is None`` check together: the
    broken interleaving, which created two connections, leaked the loser, and
    ran the threads on different connections. With ``_connect`` under
    ``self._lock`` the second thread blocks on the lock, the winner's barrier
    times out, and the loser reuses the winner's connection.
    """
    created = []
    barrier = threading.Barrier(2)

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            return None

        def executemany(self, sql, params=None):
            return None

        def fetchall(self):
            return [(1,)]

    class _FakeConn:
        def __init__(self):
            self.closed = False

        def cursor(self):
            return _FakeCursor()

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            self.closed = True

    fake_psycopg = types.ModuleType("psycopg")

    def racing_connect(dsn):
        try:
            barrier.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass
        conn = _FakeConn()
        created.append(conn)
        return conn

    fake_psycopg.connect = racing_connect
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    client = _PgVectorClient(_PgVectorConfig(dsn="postgresql://localhost/unused", namespace=None))
    errors = []

    def run_query():
        try:
            client.ping()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run_query, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert len(created) == 1
    assert client._conn is created[0]

    client.close()
    assert created[0].closed


def test_client_execute_after_close_raises(monkeypatch):
    """``close()`` is terminal: a stale client reference must get an error
    instead of silently reconnecting and leaking a session nobody closes."""
    created = []

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            return None

        def fetchall(self):
            return [(1,)]

    class _FakeConn:
        def __init__(self):
            self.closed = False

        def cursor(self):
            return _FakeCursor()

        def commit(self):
            return None

        def close(self):
            self.closed = True

    fake_psycopg = types.ModuleType("psycopg")

    def fake_connect(dsn):
        conn = _FakeConn()
        created.append(conn)
        return conn

    fake_psycopg.connect = fake_connect
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    client = _PgVectorClient(_PgVectorConfig(dsn="postgresql://localhost/unused", namespace=None))
    client.ping()
    assert len(created) == 1

    client.close()
    assert created[0].closed

    with pytest.raises(BackendError, match="closed"):
        client.ping()
    assert len(created) == 1
