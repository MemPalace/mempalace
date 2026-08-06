import json
import os
import sys
import threading
import types

import pytest
from hypothesis import given
from hypothesis import strategies as st

from _backend_conformance import assert_partition_isolation

from mempalace.backends import (
    BackendError,
    BackendMismatchError,
    CollectionNotInitializedError,
    DimensionMismatchError,
    PalaceRef,
    available_backends,
)
from mempalace.backends.base import UnsupportedCapabilityError
from mempalace.backends.pgvector import (
    PgVectorBackend,
    _PgVectorClient,
    _PgVectorConfig,
    _matches_where,
    _vector_distance,
    _as_vector_array,
    _strip_nul,
    _json_dumps,
    _tokenize,
    _escape_tsquery_token,
    _tsquery_or,
    _fts_index_name,
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
        self.facet_calls: list = []
        self.lexical_calls: list = []
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

    def lexical_rows(self, table, *, tokens, limit, where):
        """Stand-in for the server-side ``to_tsquery``/``ts_rank_cd`` ranking.

        Mirrors the two properties the real SQL guarantees and the collection
        relies on: OR semantics over the tokens, and a descending rank where a
        document matching more of the query outranks one matching less.
        """
        self.lexical_calls.append({"tokens": list(tokens), "limit": limit, "where": where})
        wanted = set(tokens)
        scored = []
        for row in self._filtered(table, where):
            overlap = len(wanted & set(_tokenize(row.get("document") or "")))
            if overlap:
                scored.append((float(overlap), row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "id": row["id"],
                "document": row["document"],
                "metadata": row.get("metadata") or {},
                "embedding": None,
                "distance": None,
                "score": score,
            }
            for score, row in scored[:limit]
        ]

    def create_fts_index(self, table):
        return None

    def scroll_rows(
        self,
        table,
        *,
        where=None,
        with_embedding=False,
        with_document=True,
        limit=None,
        offset=None,
    ):
        self.scroll_calls.append(
            {"where": where, "limit": limit, "offset": offset, "with_document": with_document}
        )
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
                    # Match the real backend: NULL document becomes empty string
                    # via the SELECT NULL::text projection when with_document=False.
                    "document": row["document"] if with_document else "",
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

    def facet_counts(self, table, *, field, where=None, limit=1000):
        self.facet_calls.append((table, field, where, limit))
        counts: dict[str, int] = {}
        for row in self._filtered(table, where):
            meta = row.get("metadata") or {}
            value = meta.get(field)
            if value is None:
                continue
            key = str(value)
            counts[key] = counts.get(key, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return dict(ordered[:limit])

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
    assert client.scroll_calls == [{"where": None, "limit": 2, "offset": 1, "with_document": True}]
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
    assert client.scroll_calls == [
        {"where": {"wing": "x"}, "limit": None, "offset": None, "with_document": True}
    ]
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
    assert client.scroll_calls == [
        {"where": None, "limit": None, "offset": 2, "with_document": True}
    ]
    assert page.ids == ["c", "d"]

    # limit-only (offset=None) is pushed.
    client.scroll_calls.clear()
    page = col.get(limit=2, include=["metadatas"])
    assert client.scroll_calls == [
        {"where": None, "limit": 2, "offset": None, "with_document": True}
    ]
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
    assert client.scroll_calls == [
        {"where": None, "limit": None, "offset": None, "with_document": True}
    ]
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


def test_pgvector_get_all_metadata_skips_document_column(tmp_path, fake_pgvector):
    """The metadata-only fast path must NOT pull document text over the wire.

    Default base ``get_all_metadata`` pages through ``get(include=["metadatas"])``,
    which used to route here via scroll_rows with documents always selected — the
    "separate follow-up" #1840 flagged. This override calls scroll_rows with
    with_document=False so the SELECT projects NULL into the document slot,
    dropping per-row payload for remote (TLS over WAN) clients where status
    otherwise dominates wall time.
    """
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=["doc_a", "doc_b", "doc_c"],
        metadatas=[
            {"wing": "p", "room": "backend"},
            {"wing": "p", "room": "frontend"},
            {"wing": "q", "room": "backend"},
        ],
        embeddings=[[1, 0], [0, 1], [0.5, 0.5]],
    )
    client = fake_pgvector.instances[0]
    client.scroll_calls.clear()

    metas = col.get_all_metadata()

    # Exactly one scroll, with_document=False (no document text on the wire).
    assert client.scroll_calls == [
        {"where": None, "limit": None, "offset": None, "with_document": False}
    ]
    # Returns just the metadata dicts (full set, any order — sort by wing+room for stability).
    metas_sorted = sorted(metas, key=lambda m: (m["wing"], m["room"]))
    assert metas_sorted == [
        {"wing": "p", "room": "backend"},
        {"wing": "p", "room": "frontend"},
        {"wing": "q", "room": "backend"},
    ]


def test_pgvector_get_all_metadata_filtered_uses_fast_path(tmp_path, fake_pgvector):
    """Filtered get_all_metadata uses the single-pass metadata-only fast path.

    ``_matches_where`` only reads ``metadata``, so we keep ``with_document=False``
    and apply the post-filter locally on the metadata dicts. SQL pushdown still
    happens when the filter is pushdownable; the local ``_matches_where`` re-runs
    for array/object semantics #1840's filtered path required.
    """
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=["doc_a", "doc_b", "doc_c"],
        metadatas=[{"wing": "x"}, {"wing": "y"}, {"wing": "x"}],
        embeddings=[[1, 0], [0, 1], [0.5, 0.5]],
    )
    client = fake_pgvector.instances[0]
    client.scroll_calls.clear()

    metas = col.get_all_metadata(where={"wing": "x"})

    # Exactly one scroll with with_document=False — pushdown forwards the
    # equality filter to SQL; no document text on the wire.
    assert client.scroll_calls == [
        {"where": {"wing": "x"}, "limit": None, "offset": None, "with_document": False}
    ]
    assert sorted(metas, key=lambda m: m["wing"]) == [{"wing": "x"}, {"wing": "x"}]


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


def test_pgvector_backend_advertises_supports_metadata_facets():
    assert "supports_metadata_facets" in PgVectorBackend.capabilities


def test_pgvector_facet_counts(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.upsert(
        ids=["1", "2", "3", "4"],
        documents=["a", "b", "c", "d"],
        metadatas=[
            {"wing": "alpha"},
            {"wing": "alpha"},
            {"wing": "beta"},
            {"wing": "gamma"},
        ],
        embeddings=[[1, 0], [1, 0], [1, 0], [1, 0]],
    )
    assert col.facet_counts("wing") == {
        "alpha": 2,
        "beta": 1,
        "gamma": 1,
    }


def test_pgvector_facet_counts_where(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    col.upsert(
        ids=["1", "2", "3"],
        documents=["a", "b", "c"],
        metadatas=[
            {"wing": "engineering", "room": "backend"},
            {"wing": "engineering", "room": "frontend"},
            {"wing": "design", "room": "ux"},
        ],
        embeddings=[[1, 0], [1, 0], [1, 0]],
    )
    assert col.facet_counts("room", where={"wing": "engineering"}) == {
        "backend": 1,
        "frontend": 1,
    }


def test_pgvector_facet_counts_rejects_local_filters(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    with pytest.raises(UnsupportedCapabilityError):
        col.facet_counts(
            "room",
            where={"$or": [{"wing": "a"}, {"wing": "b"}]},
        )


def test_pgvector_facet_counts_ignores_missing_metadata(tmp_path, fake_pgvector):
    """Rows without the faceted field are excluded — mcp_server reconciles them
    into the ``unknown`` bucket via ``count(*) - sum(facet_values)``. Mirrors
    Qdrant's facet semantics."""
    _backend, col = _collection(tmp_path)
    col.upsert(
        ids=["1", "2", "3"],
        documents=["a", "b", "c"],
        metadatas=[
            {"wing": "alpha"},
            {"wing": "beta"},
            {},
        ],
        embeddings=[[1, 0], [1, 0], [1, 0]],
    )
    assert col.facet_counts("wing") == {"alpha": 1, "beta": 1}


def test_pgvector_facet_counts_empty_collection(tmp_path, fake_pgvector):
    """An empty/unmaterialized collection returns ``{}`` rather than raising —
    matches Qdrant; gives ``mempalace_status`` a clean zero-state path."""
    _backend, col = _collection(tmp_path)
    assert col.facet_counts("wing") == {}


def test_pgvector_facet_counts_passes_filter_to_sql_layer(tmp_path, fake_pgvector):
    """Pushdown filters reach the SQL layer (proves the where path goes through
    ``_where_to_sql``, not the local ``_matches_where`` post-filter)."""
    _backend, col = _collection(tmp_path)
    col.upsert(
        ids=["1"],
        documents=["doc"],
        metadatas=[{"wing": "engineering", "room": "backend"}],
        embeddings=[[1, 0]],
    )
    col.facet_counts("room", where={"wing": "engineering"})
    client = fake_pgvector.instances[0]
    assert len(client.facet_calls) == 1
    _table, field, where, _limit = client.facet_calls[0]
    assert field == "room"
    assert where == {"wing": "engineering"}


# ── Postgres FTS lexical search ────────────────────────────────────────


def _capturing_client(monkeypatch):
    """Real ``_PgVectorClient`` with ``_execute`` stubbed, plus the capture list.

    Exercises the actual SQL-building code without psycopg or a server: every
    statement and its bound parameters land in ``captured``.
    """
    captured = []

    def fake_execute(sql, params=None, *, fetch=False, many=False):
        captured.append({"sql": sql, "params": list(params or []), "fetch": fetch})
        return []

    client = _PgVectorClient(_PgVectorConfig(dsn="postgresql://localhost/unused", namespace=None))
    monkeypatch.setattr(client, "_execute", fake_execute)
    return client, captured


def _unquote_tsquery_lexeme(escaped):
    """Decode one escaped lexeme the way the Postgres tsquery lexer does.

    Inside single quotes only ``''`` (a literal quote) and ``\\\\`` (a literal
    backslash) are special; everything else is taken verbatim.
    """
    assert escaped.startswith("'") and escaped.endswith("'") and len(escaped) >= 2
    body = escaped[1:-1]
    out = []
    i = 0
    while i < len(body):
        pair = body[i : i + 2]
        if pair in ("''", "\\\\"):
            out.append(pair[0])
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def test_escape_tsquery_token_neutralizes_operators():
    """tsquery operators inside a token must become part of the lexeme, not syntax.

    ``to_tsquery`` parses its argument as an expression, so an unescaped token
    would let ``&``/``|``/``!``/``<->``/parens change which rows match — or abort
    the search with a syntax error.
    """
    for raw in ("a&b", "a|b", "!a", "(a)", "a:b", "a:*", "a<->b", "a & b | !c"):
        escaped = _escape_tsquery_token(raw)
        assert escaped.startswith("'") and escaped.endswith("'")
        assert _unquote_tsquery_lexeme(escaped) == raw


def test_escape_tsquery_token_doubles_quotes_and_backslashes():
    assert _escape_tsquery_token("it's") == "'it''s'"
    assert _escape_tsquery_token("a\\b") == "'a\\\\b'"
    # The classic break-out attempt: closing the quote to append an operator.
    assert _escape_tsquery_token("x' | 'y") == "'x'' | ''y'"
    # A trailing backslash must not escape the closing quote.
    assert _escape_tsquery_token("x\\") == "'x\\\\'"


def test_escape_tsquery_token_handles_non_latin_text():
    """Arabic and CJK tokens pass through untouched — 'simple' is script-neutral."""
    for raw in ("ذاكرة", "قصر_الذاكرة", "记忆", "メモリ"):
        assert _escape_tsquery_token(raw) == f"'{raw}'"


@given(st.text(min_size=0, max_size=40))
def test_escape_tsquery_token_round_trips_any_input(raw):
    """Any string survives escaping as exactly one lexeme.

    Two properties together mean "cannot inject": the escaped form decodes back
    to the input, and the quoted body holds no unpaired quote that could end the
    lexeme early.
    """
    escaped = _escape_tsquery_token(raw)
    assert _unquote_tsquery_lexeme(escaped) == raw
    body = escaped[1:-1]
    assert body.replace("''", "").count("'") == 0


def test_tsquery_or_joins_tokens_with_or():
    """OR semantics, not AND: the lexical arm is a recall-oriented candidate pool.

    ``websearch_to_tsquery`` would AND the terms, so a multi-word question would
    return nothing where the BM25 path it replaces scored every partial match.
    """
    assert _tsquery_or(["rareterm", "backend"]) == "'rareterm' | 'backend'"
    assert _tsquery_or(["solo"]) == "'solo'"


def test_tsquery_or_drops_empty_tokens():
    """An empty quoted lexeme is a tsquery syntax error, so it must never appear."""
    assert _tsquery_or([]) == ""
    assert _tsquery_or(["", "kept", ""]) == "'kept'"


@given(st.text(max_size=60))
def test_tsquery_or_from_tokenizer_emits_one_lexeme_per_token(text):
    tokens = _tokenize(text)
    built = _tsquery_or(tokens)
    assert built.count(" | ") == max(len(tokens) - 1, 0)
    assert not built.endswith("|")
    # Tokenizer output is \w-only, so nothing needs escaping and every token
    # appears verbatim inside its own quotes.
    for token in tokens:
        assert f"'{token}'" in built


def test_lexical_rows_sql_uses_simple_config_and_binds_query():
    """The pushed-down query must rank server-side with the language-neutral config."""
    client = _PgVectorClient(_PgVectorConfig(dsn="postgresql://localhost/unused", namespace=None))
    captured = {}

    def fake_execute(sql, params=None, *, fetch=False, many=False):
        captured["sql"] = sql
        captured["params"] = list(params or [])
        return [("a", "doc", {"wing": "x"}, 0.5)]

    client._execute = fake_execute
    rows = client.lexical_rows("drawers", tokens=["rareterm", "backend"], limit=7, where=None)

    sql = captured["sql"]
    assert "to_tsquery('simple', %s)" in sql
    assert "ts_rank_cd(to_tsvector('simple', document), q)" in sql
    # Must match the indexed expression verbatim or the GIN index is not used.
    assert "to_tsvector('simple', document) @@ q" in sql
    assert "ORDER BY score DESC LIMIT %s" in sql
    assert 'FROM "drawers"' in sql
    assert "english" not in sql
    # tsquery first, LIMIT last — the SQL text order.
    assert captured["params"] == ["'rareterm' | 'backend'", 7]
    assert rows == [
        {
            "id": "a",
            "document": "doc",
            "metadata": {"wing": "x"},
            "embedding": None,
            "distance": None,
            "score": 0.5,
        }
    ]


def test_lexical_rows_binds_where_between_query_and_limit():
    """Pushdown filters bind as JSONB containment, ordered as they appear in the SQL."""
    client = _PgVectorClient(_PgVectorConfig(dsn="postgresql://localhost/unused", namespace=None))
    captured = {}

    def fake_execute(sql, params=None, *, fetch=False, many=False):
        captured["sql"] = sql
        captured["params"] = list(params or [])
        return []

    client._execute = fake_execute
    client.lexical_rows("drawers", tokens=["alpha"], limit=3, where={"wing": "project"})

    assert "metadata @> %s::jsonb" in captured["sql"]
    assert captured["params"] == ["'alpha'", _json_dumps({"wing": "project"}), 3]


def test_create_table_also_creates_fts_gin_index(monkeypatch):
    """Fresh collections get the FTS index automatically — no manual migration."""
    client, captured = _capturing_client(monkeypatch)
    client.create_table("mempalace_abc_drawers", 384)

    statements = [item["sql"] for item in captured]
    assert any(sql.startswith("CREATE TABLE IF NOT EXISTS") for sql in statements)
    index_sql = [sql for sql in statements if sql.startswith("CREATE INDEX IF NOT EXISTS")]
    assert len(index_sql) == 1
    assert "USING gin (to_tsvector('simple', document))" in index_sql[0]
    assert '"mempalace_abc_drawers_fts_idx"' in index_sql[0]
    assert 'ON "mempalace_abc_drawers"' in index_sql[0]


def test_fts_index_name_is_table_scoped_and_length_clamped():
    """Two collections must not collide on one index name (both live in pg_class)."""
    assert _fts_index_name("t_one") != _fts_index_name("t_two")
    assert _fts_index_name("t_one") == "t_one_fts_idx"

    long_a = "mempalace_" + "a" * 60
    long_b = "mempalace_" + "a" * 59 + "b"
    for name in (_fts_index_name(long_a), _fts_index_name(long_b)):
        assert len(name.encode("utf-8")) <= 63
    # Clamping hashes the overflow instead of truncating into a collision.
    assert _fts_index_name(long_a) != _fts_index_name(long_b)
    assert _fts_index_name(long_a) != long_a


def test_create_fts_index_failure_does_not_break_table_creation():
    """The index is a performance asset; a role that cannot build it still works."""
    client = _PgVectorClient(_PgVectorConfig(dsn="postgresql://localhost/unused", namespace=None))
    seen = []

    def fake_execute(sql, params=None, *, fetch=False, many=False):
        seen.append(sql)
        if sql.startswith("CREATE INDEX"):
            raise BackendError("permission denied")
        return []

    client._execute = fake_execute
    client.create_table("drawers", 2)  # must not raise
    assert any(sql.startswith("CREATE INDEX") for sql in seen)


def test_lexical_search_pushes_down_instead_of_scrolling(tmp_path, fake_pgvector):
    """The default path must never pull the table to the client."""
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=[
            "alpha backend note",
            "rareterm pgvector backend note",
            "frontend design note",
        ],
        metadatas=[{"wing": "project"}, {"wing": "project"}, {"wing": "other"}],
        embeddings=[[1, 0], [0.9, 0.1], [0, 1]],
    )
    client = fake_pgvector.instances[0]
    client.scroll_calls.clear()

    hits = col.lexical_search(query="rareterm backend", n_results=5, where={"wing": "project"}).hits

    assert [hit.id for hit in hits] == ["b", "a"]
    assert client.scroll_calls == [], "lexical search must not scroll the table"
    assert client.lexical_calls == [
        {"tokens": ["rareterm", "backend"], "limit": 5, "where": {"wing": "project"}}
    ]


def test_lexical_search_falls_back_for_non_pushdown_filter(tmp_path, fake_pgvector):
    """A filter SQL cannot express keeps the exact client-side path."""
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b"],
        documents=["alpha backend note", "rareterm backend note"],
        metadatas=[{"wing": "project", "rank": 1}, {"wing": "project", "rank": 3}],
        embeddings=[[1, 0], [0.9, 0.1]],
    )
    client = fake_pgvector.instances[0]
    client.scroll_calls.clear()

    hits = col.lexical_search(query="backend", n_results=5, where={"rank": {"$gt": 1}}).hits

    assert [hit.id for hit in hits] == ["b"]
    assert client.lexical_calls == [], "$gt is not pushdown-safe"
    assert client.scroll_calls, "fallback path scrolls"


def test_lexical_search_falls_back_when_postgres_fails(tmp_path, fake_pgvector, monkeypatch):
    """A server-side failure must degrade to the slow path, not drop the arm.

    Dropping it would silently halve hybrid search recall, which is worse than
    being slow.
    """
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b"],
        documents=["alpha backend note", "rareterm backend note"],
        metadatas=[{"wing": "project"}, {"wing": "project"}],
        embeddings=[[1, 0], [0.9, 0.1]],
    )
    client = fake_pgvector.instances[0]

    def boom(*_args, **_kwargs):
        raise BackendError("to_tsquery: syntax error")

    monkeypatch.setattr(client, "lexical_rows", boom)
    client.scroll_calls.clear()

    hits = col.lexical_search(query="rareterm backend", n_results=5).hits

    assert [hit.id for hit in hits] == ["b", "a"]
    assert client.scroll_calls, "failure must fall back to the client-side scan"


def test_lexical_search_empty_query_issues_no_sql(tmp_path, fake_pgvector):
    """No token means no match, so neither a tsquery nor a scroll is worth sending."""
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a"],
        documents=["alpha backend note"],
        metadatas=[{"wing": "project"}],
        embeddings=[[1, 0]],
    )
    client = fake_pgvector.instances[0]
    client.scroll_calls.clear()

    for query in ("", "   ", "!!!", "a"):  # single chars are below the token floor
        assert col.lexical_search(query=query, n_results=5).hits == []

    assert client.lexical_calls == []
    assert client.scroll_calls == []


def test_lexical_search_matches_arabic_and_cjk(tmp_path, fake_pgvector):
    """A mixed-script palace is the reason the config is 'simple', not 'english'."""
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["ar", "cjk", "en"],
        documents=["قصر الذاكرة يحفظ كلماتك", "记忆 宫殿", "memory palace note"],
        metadatas=[{"wing": "p"}, {"wing": "p"}, {"wing": "p"}],
        embeddings=[[1, 0], [0, 1], [0.5, 0.5]],
    )

    assert [hit.id for hit in col.lexical_search(query="الذاكرة", n_results=5).hits] == ["ar"]
    assert [hit.id for hit in col.lexical_search(query="记忆", n_results=5).hits] == ["cjk"]
    # And the tokens reach SQL quoted, not mangled.
    assert _tsquery_or(_tokenize("الذاكرة")) == "'الذاكرة'"


def test_lexical_search_raises_when_table_missing_but_marker_exists(tmp_path, fake_pgvector):
    """Same not-initialized contract as the scan path it replaces."""
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a"],
        documents=["alpha backend note"],
        metadatas=[{"wing": "project"}],
        embeddings=[[1, 0]],
    )
    fake_pgvector.instances[0].tables.clear()  # marker on disk, table gone

    with pytest.raises(CollectionNotInitializedError):
        col.lexical_search(query="backend", n_results=5)
