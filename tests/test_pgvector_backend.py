import json
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
    _strip_nul,
    _json_dumps,
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
        self.scroll_calls: list = []
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

    def scroll_rows(self, table, *, where=None, with_embedding=False, limit=None, offset=None):
        self.scroll_calls.append({"where": where, "limit": limit, "offset": offset})
        rows = self._filtered(table, where)
        if limit is not None or offset:
            # Mirror the real backend: ORDER BY id, then LIMIT/OFFSET.
            rows = sorted(rows, key=lambda row: row["id"])
            if offset:
                rows = rows[offset:]
            if limit is not None:
                rows = rows[:limit]
        out = []
        for row in rows:
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


def test_pgvector_get_unfiltered_page_pushes_limit_offset(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c", "d"],
        documents=["da", "db", "dc", "dd"],
        metadatas=[{"wing": "x"}, {"wing": "x"}, {"wing": "x"}, {"wing": "x"}],
        embeddings=[[1, 0], [0, 1], [0.5, 0.5], [0.2, 0.8]],
    )
    client = fake_pgvector.instances[0]
    client.scroll_calls.clear()

    page = col.get(limit=2, offset=1, include=["metadatas"])

    # An unfiltered page is pushed to SQL as LIMIT/OFFSET instead of fetching
    # the whole table and slicing in Python (the O(rows x pages) path).
    assert client.scroll_calls == [{"where": None, "limit": 2, "offset": 1}]
    # ORDER BY id, then OFFSET 1 LIMIT 2 -> b, c.
    assert page.ids == ["b", "c"]


def test_pgvector_get_filtered_page_stays_on_full_scan(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=["da", "db", "dc"],
        metadatas=[{"wing": "x"}, {"wing": "y"}, {"wing": "x"}],
        embeddings=[[1, 0], [0, 1], [0.5, 0.5]],
    )
    client = fake_pgvector.instances[0]
    client.scroll_calls.clear()

    page = col.get(where={"wing": "x"}, limit=1, offset=1, include=["metadatas"])

    # A filtered get keeps the full-scan path (no LIMIT/OFFSET pushed) so the
    # exact _matches_where re-filter runs before pagination.
    assert client.scroll_calls == [{"where": {"wing": "x"}, "limit": None, "offset": None}]
    assert page.ids == ["c"]


def test_pgvector_get_offset_only_and_limit_only_push(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c", "d"],
        documents=["da", "db", "dc", "dd"],
        metadatas=[{"wing": "x"}] * 4,
        embeddings=[[1, 0], [0, 1], [0.5, 0.5], [0.2, 0.8]],
    )
    client = fake_pgvector.instances[0]

    # offset-only (limit=None) is pushed.
    client.scroll_calls.clear()
    page = col.get(offset=2, include=["metadatas"])
    assert client.scroll_calls == [{"where": None, "limit": None, "offset": 2}]
    assert page.ids == ["c", "d"]

    # limit-only (offset=None) is pushed.
    client.scroll_calls.clear()
    page = col.get(limit=2, include=["metadatas"])
    assert client.scroll_calls == [{"where": None, "limit": 2, "offset": None}]
    assert page.ids == ["a", "b"]


def test_pgvector_get_negative_bounds_use_python_slice(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=["da", "db", "dc"],
        metadatas=[{"wing": "x"}] * 3,
        embeddings=[[1, 0], [0, 1], [0.5, 0.5]],
    )
    client = fake_pgvector.instances[0]
    client.scroll_calls.clear()

    # A negative offset must not reach SQL (OFFSET -1 would error); it falls
    # through to the unchanged full-scan + Python-slice path.
    page = col.get(offset=-1, include=["metadatas"])
    assert client.scroll_calls == [{"where": None, "limit": None, "offset": None}]
    assert page.ids == ["c"]


def test_pgvector_get_pages_tile_without_overlap(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c", "d", "e"],
        documents=["da", "db", "dc", "dd", "de"],
        metadatas=[{"wing": "x"}] * 5,
        embeddings=[[1, 0], [0, 1], [0.5, 0.5], [0.2, 0.8], [0.3, 0.7]],
    )
    # Consecutive pages tile the whole table exactly once, in stable id order.
    p1 = col.get(limit=2, offset=0, include=["metadatas"]).ids
    p2 = col.get(limit=2, offset=2, include=["metadatas"]).ids
    p3 = col.get(limit=2, offset=4, include=["metadatas"]).ids
    assert p1 == ["a", "b"]
    assert p2 == ["c", "d"]
    assert p3 == ["e"]
    assert p1 + p2 + p3 == ["a", "b", "c", "d", "e"]


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


class _FakeUpsertCursor:
    """Captures the params bound by ``upsert_rows`` -> ``_execute(many=True)``."""

    def __init__(self, captured):
        self._captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return None

    def executemany(self, sql, params=None):
        self._captured.extend(params or [])

    def fetchall(self):
        return []


class _FakeUpsertConn:
    def __init__(self, captured):
        self._captured = captured

    def cursor(self):
        return _FakeUpsertCursor(self._captured)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def _fake_upsert_client(monkeypatch):
    """Install a fake psycopg whose connection captures bound params, and return
    ``(client, captured)`` for driving the real ``upsert_rows`` write path."""
    captured = []
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = lambda *args, **kwargs: _FakeUpsertConn(captured)
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    client = _PgVectorClient(_PgVectorConfig(dsn="postgresql://localhost/unused", namespace=None))
    return client, captured


def test_pgvector_upsert_strips_nul_bytes(monkeypatch):
    """A NUL (0x00) byte in id/document/metadata must never reach Postgres.

    psycopg's text/jsonb dumpers reject NUL outright ("PostgreSQL text fields
    cannot contain NUL (0x00) bytes"), which aborts the entire mine run (#1829)
    when a single transcript captured a NUL in tool output. ChromaDB and the
    SQLite backend store the byte verbatim, so pgvector strips it to keep the
    same inputs ingestible. Strip, not reject: rejecting would re-abort the
    mine or drop the drawer entirely (recall loss).
    """
    client, captured = _fake_upsert_client(monkeypatch)
    client.upsert_rows(
        "drawers",
        [
            {
                "id": "draw\x00er",
                "document": "before\x00after",
                "metadata": {"go\x00od": "v\x00w", "nested": ["a\x00b", 7]},
                "embedding": [1.0, 0.0],
                "updated_at": "2026-06-20T00:00:00Z",
            }
        ],
    )

    assert len(captured) == 1, "upsert_rows should bind exactly one row"
    row_id, document, metadata_json = captured[0][0], captured[0][1], captured[0][2]

    # No NUL survives into any text-bound parameter (id, document, metadata).
    assert "\x00" not in row_id
    assert "\x00" not in document
    assert "\x00" not in metadata_json

    # Stripping removes only the NUL; surrounding content is otherwise preserved.
    assert row_id == "drawer"
    assert document == "beforeafter"
    assert json.loads(metadata_json) == {"good": "vw", "nested": ["ab", 7]}


def test_strip_nul_helper():
    """``_strip_nul`` removes NUL from strings, list/tuple items, and dict keys
    and values; NUL-free input and non-string scalars are returned unchanged."""
    assert _strip_nul("a\x00b") == "ab"
    assert _strip_nul("clean") == "clean"
    assert _strip_nul("") == ""
    assert _strip_nul("\x00") == ""
    # Keys, values, list items, and nested structures are all stripped.
    assert _strip_nul({"k\x00": "v\x00", "n": [1, "x\x00y"]}) == {"k": "v", "n": [1, "xy"]}
    assert _strip_nul([{"a\x00": "b\x00"}, "c\x00"]) == [{"a": "b"}, "c"]
    # Tuples recurse too and stay tuples (defends direct callers that pass
    # un-normalized metadata before the JSON round-trip).
    assert _strip_nul(("a\x00b", 1, ["c\x00"])) == ("ab", 1, ["c"])
    # Keys differing only by a NUL collapse, last wins (documented, harmless:
    # real metadata keys are fixed field names, never NUL-only-distinguished).
    assert _strip_nul({"a\x00": 1, "a": 2}) == {"a": 2}
    # Non-string scalars pass through unchanged (bool stays bool, not int).
    assert _strip_nul(7) == 7
    assert _strip_nul(3.5) == 3.5
    assert _strip_nul(True) is True
    assert _strip_nul(None) is None


def test_pgvector_upsert_replaces_lone_surrogates(monkeypatch):
    """A lone UTF-16 surrogate in id/document/metadata must never reach Postgres.

    psycopg encodes text/jsonb parameters as UTF-8, and a lone surrogate has no
    UTF-8 encoding, so it raises UnicodeEncodeError ("surrogates not allowed") and
    aborts the entire mine run (the surrogate sibling of the NUL abort in #1829).
    ChromaDB sanitizes document text via config.strip_lone_surrogates;
    pgvector matches it (for document and metadata) by replacing the surrogate with
    U+FFFD rather than dropping the drawer (recall loss) or re-aborting the mine.
    """
    # Build the surrogates with chr() so this source file stays valid UTF-8 (a raw
    # lone surrogate has no UTF-8 encoding and would not parse).
    hi, lo, s3, s4, s5 = (chr(c) for c in (0xD800, 0xDFFF, 0xD834, 0xDCA1, 0xDC00))
    repl = chr(0xFFFD)
    client, captured = _fake_upsert_client(monkeypatch)
    client.upsert_rows(
        "drawers",
        [
            {
                "id": f"draw{hi}er",
                "document": f"before{lo}after",
                "metadata": {f"go{s3}od": f"v{s4}w", "nested": [f"a{s5}b", 7]},
                "embedding": [1.0, 0.0],
                "updated_at": "2026-06-20T00:00:00Z",
            }
        ],
    )

    assert len(captured) == 1, "upsert_rows should bind exactly one row"
    row_id, document, metadata_json = captured[0][0], captured[0][1], captured[0][2]

    # Every text-bound parameter must now be UTF-8 encodable (what psycopg does to
    # bind it); a surviving lone surrogate would raise here.
    for field in (row_id, document, metadata_json):
        field.encode("utf-8")

    # Surrogates are replaced with U+FFFD, not dropped: surrounding content stays
    # and each lone surrogate maps to exactly one replacement character.
    assert row_id == f"draw{repl}er"
    assert document == f"before{repl}after"
    assert json.loads(metadata_json) == {f"go{repl}od": f"v{repl}w", "nested": [f"a{repl}b", 7]}


def test_pgvector_upsert_strips_nul_and_surrogate_together(monkeypatch):
    """A single row carrying *both* a NUL and a lone surrogate must come out
    clean on every text-bound field.

    This pins the composition of the two sibling fixes (#1829 NUL, #1833
    surrogate), which edit the same ``upsert_rows`` binding: NUL is stripped
    pre-serialization and the surrogate replaced post-serialization. A rebase
    that kept only one strip would regress the other byte class silently, since
    neither sibling test exercises both at once.
    """
    sur = chr(0xD800)
    repl = chr(0xFFFD)
    client, captured = _fake_upsert_client(monkeypatch)
    client.upsert_rows(
        "drawers",
        [
            {
                "id": f"id\x00{sur}x",
                "document": f"doc\x00{sur}y",
                "metadata": {f"k\x00{sur}": f"v\x00{sur}", "nested": [f"a\x00{sur}b", 7]},
                "embedding": [1.0, 0.0],
                "updated_at": "2026-06-20T00:00:00Z",
            }
        ],
    )

    assert len(captured) == 1, "upsert_rows should bind exactly one row"
    row_id, document, metadata_json = captured[0][0], captured[0][1], captured[0][2]

    # Neither unstorable byte survives, and each bound field is UTF-8 encodable.
    for field in (row_id, document, metadata_json):
        assert "\x00" not in field
        assert sur not in field
        field.encode("utf-8")

    # NUL dropped, surrogate -> U+FFFD, surrounding content preserved.
    assert row_id == f"id{repl}x"
    assert document == f"doc{repl}y"
    assert json.loads(metadata_json) == {f"k{repl}": f"v{repl}", "nested": [f"a{repl}b", 7]}


def test_strip_lone_surrogates_reuses_config_util():
    """The pgvector write path strips surrogates via ``config.strip_lone_surrogates``
    applied to id/document and the serialized metadata JSON (no pgvector-local
    helper). End-to-end coverage is ``test_pgvector_upsert_replaces_lone_surrogates``;
    the utility's own edge cases live in ``tests/test_clean_lone_surrogates.py``."""
    from mempalace.config import strip_lone_surrogates

    # ensure_ascii=False leaves a metadata surrogate raw in the JSON, so a single
    # pass over the serialized string cleans it (the property the write path relies on).
    raw = _json_dumps({"k": f"v{chr(0xD800)}w"})
    cleaned = strip_lone_surrogates(raw)
    assert chr(0xD800) not in cleaned
    assert json.loads(cleaned) == {"k": f"v{chr(0xFFFD)}w"}
