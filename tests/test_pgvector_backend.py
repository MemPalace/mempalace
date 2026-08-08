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
    _bm25_scores,
    _like_pattern,
    _token_count,
    _trgm_index_name,
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
        self.lexical_asset_calls: list = []
        # Flipped off by the tests that stand in for a palace created before the
        # token_count column existed.
        self.token_count_supported: bool = True
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
            stored = dict(row)
            # Mirrors the real client: the count is derived server-side-ish, from
            # the document that actually lands in the row, and only when the
            # table carries the column.
            if self.token_count_supported:
                stored["token_count"] = _token_count(stored.get("document") or "")
            store["rows"][row["id"]] = stored

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

    def lexical_candidates(self, table, *, tokens, where):
        """Stand-in for the server-side ``ILIKE`` filter.

        Deliberately models *substring* matching rather than tokenizer overlap:
        that is what ``document ILIKE '%token%'`` actually does, and a fake that
        re-used ``_tokenize`` here would agree with the collection by
        construction and so could never catch a predicate that is narrower than
        BM25's scoring set. No limit and no ranking, because the real query has
        neither.
        """
        self.lexical_calls.append({"tokens": list(tokens), "where": where})
        rows = []
        for row in self._filtered(table, where):
            document = (row.get("document") or "").lower()
            if any(token in document for token in tokens):
                rows.append(
                    {
                        "id": row["id"],
                        "document": row["document"],
                        "metadata": row.get("metadata") or {},
                        "embedding": None,
                        "distance": None,
                    }
                )
        return rows

    def lexical_corpus_stats(self, table, where):
        rows = self._filtered(table, where)
        if not rows:
            return None
        counts = [row.get("token_count") for row in rows]
        if any(count is None for count in counts):
            return None
        return len(rows), sum(counts) / len(rows)

    def has_token_count(self, table):
        return self.token_count_supported

    def ensure_lexical_assets(self, table):
        self.lexical_asset_calls.append(table)

    def backfill_token_counts(self, table, batch=2000):
        filled = 0
        for row in self.tables.get(table, {"rows": {}})["rows"].values():
            if row.get("token_count") is None:
                row["token_count"] = _token_count(row.get("document") or "")
                filled += 1
        return filled

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

    def has_vector_index(self, table):
        return False

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


# ── Lexical pushdown (filter in SQL, BM25 in Python) ───────────────────


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
    client._token_count_columns["drawers"] = True
    return client, captured


def test_like_pattern_escapes_wildcards_and_escape_char():
    """The pattern must be a literal-substring test, not a wildcard expression."""
    assert _like_pattern("plain") == "%plain%"
    # '_' is a LIKE single-character wildcard and IS reachable: it is a \w char,
    # so _tokenize emits it inside identifiers like foo_bar.
    assert _like_pattern("foo_bar") == r"%foo\_bar%"
    assert _like_pattern("50%") == r"%50\%%"
    assert _like_pattern("a\\b") == "%a\\\\b%"


@given(st.text(min_size=1, max_size=80))
def test_every_token_is_a_literal_substring_of_the_lowered_document(text):
    """The property the whole pushdown rests on.

    ``_bm25_scores`` can only score a document above zero if the document shares
    a token with the query, and every token the tokenizer emits is a span it
    matched inside ``text.lower()``. So an ILIKE substring test on that token
    cannot miss a document BM25 would have scored — the SQL filter is a superset
    of the scored set, never a subset.
    """
    lowered = text.lower()
    for token in _tokenize(text):
        assert token in lowered


def test_trgm_index_name_is_table_scoped_and_length_clamped():
    """Two collections in one schema must not fight over one index name."""
    assert _trgm_index_name("t_one") != _trgm_index_name("t_two")
    assert _trgm_index_name("t_one") == "t_one_trgm_idx"

    long_a = "x" * 70 + "a"
    long_b = "x" * 70 + "b"
    for name in (_trgm_index_name(long_a), _trgm_index_name(long_b)):
        assert len(name.encode("utf-8")) <= 63
    assert _trgm_index_name(long_a) != _trgm_index_name(long_b)


def test_lexical_candidates_sql_has_no_limit_and_no_ranking(monkeypatch):
    """Postgres decides which rows to *skip*, never which rows to return."""
    client, captured = _capturing_client(monkeypatch)

    client.lexical_candidates("drawers", tokens=["rareterm", "backend"], where=None)

    sql = captured[-1]["sql"]
    assert "LIMIT" not in sql.upper()
    assert "ORDER BY" not in sql.upper()
    assert "ts_rank" not in sql and "tsquery" not in sql
    assert sql.count("document ILIKE %s ESCAPE '\\'") == 2
    assert " OR " in sql
    assert captured[-1]["params"] == ["%rareterm%", "%backend%"]


def test_lexical_candidates_binds_patterns_before_where_params(monkeypatch):
    """Positional binding order must match the order the placeholders appear."""
    client, captured = _capturing_client(monkeypatch)

    client.lexical_candidates("drawers", tokens=["alpha"], where={"wing": "project"})

    call = captured[-1]
    assert call["params"][0] == "%alpha%"
    assert json.loads(call["params"][1]) == {"wing": "project"}
    assert call["sql"].index("ILIKE") < call["sql"].index("metadata @>")


def test_lexical_corpus_stats_returns_none_when_a_count_is_missing(monkeypatch):
    """A single un-backfilled row must send the query to the exact local path."""
    client, _captured = _capturing_client(monkeypatch)

    monkeypatch.setattr(client, "_execute", lambda *a, **k: [(10, 100, 1)])
    assert client.lexical_corpus_stats("drawers", None) is None

    monkeypatch.setattr(client, "_execute", lambda *a, **k: [(10, 100, 0)])
    assert client.lexical_corpus_stats("drawers", None) == (10, 10.0)

    # An empty collection has no mean length to report.
    monkeypatch.setattr(client, "_execute", lambda *a, **k: [(0, None, 0)])
    assert client.lexical_corpus_stats("drawers", None) is None


def test_create_table_adds_token_count_and_lexical_assets(monkeypatch):
    client, captured = _capturing_client(monkeypatch)

    client.create_table("drawers", 3)

    statements = " | ".join(call["sql"] for call in captured)
    assert '"token_count" integer)' in statements
    assert 'ADD COLUMN IF NOT EXISTS "token_count"' in statements
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in statements
    assert "gin (document gin_trgm_ops)" in statements
    # A tsvector expression index would enforce Postgres' 1 MB tsvector cap at
    # write time, turning a large drawer from a slow write into a failed one.
    assert "to_tsvector" not in statements


def test_lexical_asset_failure_does_not_break_table_creation(monkeypatch):
    """A role that may not ALTER or CREATE EXTENSION must still open a palace."""
    client = _PgVectorClient(_PgVectorConfig(dsn="postgresql://localhost/unused", namespace=None))
    seen = []

    def flaky_execute(sql, params=None, *, fetch=False, many=False):
        seen.append(sql)
        if "ALTER TABLE" in sql or "CREATE EXTENSION" in sql or "CREATE INDEX" in sql:
            raise BackendError("permission denied")
        return []

    monkeypatch.setattr(client, "_execute", flaky_execute)
    client.create_table("drawers", 3)  # must not raise

    assert any("CREATE TABLE" in sql for sql in seen)


def test_upsert_counts_tokens_of_the_stored_document(monkeypatch):
    """The stored length must describe the stored text, not the caller's.

    Stripping a NUL can fuse two tokens into one, and a length that disagrees
    with the document by even one token silently changes every future ranking.
    """
    client, captured = _capturing_client(monkeypatch)

    client.upsert_rows(
        "drawers",
        [{"id": "a", "document": "ab\x00cd efgh", "metadata": {}, "embedding": [1, 0]}],
    )

    call = captured[-1]
    assert '"token_count"' in call["sql"]
    document, token_count = call["params"][0][1], call["params"][0][-1]
    assert document == "abcd efgh"
    assert token_count == _token_count("abcd efgh") == 2


def test_upsert_omits_token_count_when_the_column_is_absent(monkeypatch):
    """A pre-token_count table must keep ingesting, not fail on an unknown column."""
    client, captured = _capturing_client(monkeypatch)
    client._token_count_columns["drawers"] = False

    client.upsert_rows(
        "drawers",
        [{"id": "a", "document": "alpha beta", "metadata": {}, "embedding": [1, 0]}],
    )

    call = captured[-1]
    assert "token_count" not in call["sql"]
    assert len(call["params"][0]) == 5


def test_bm25_with_collection_statistics_matches_whole_collection_scoring():
    """Scoring a filtered pool with collection-wide stats == scoring everything.

    This is the arithmetic the pushdown depends on: the rows a query cannot
    match contribute nothing but their count and their length to BM25, so
    supplying those two numbers makes the filtered pool score identically.
    """
    documents = [
        "alpha beta short",
        "alpha " + " ".join(f"filler{i}" for i in range(60)),
        "beta " + " ".join(f"other{i}" for i in range(30)),
        "gamma delta epsilon with no query terms at all",
        "unrelated text",
    ]
    query = "alpha beta"

    whole = _bm25_scores(query, documents)
    matched = [doc for doc, score in zip(documents, whole) if score > 0]
    assert len(matched) < len(documents)

    tokenized_lengths = [len(_tokenize(doc)) for doc in documents]
    pooled = _bm25_scores(
        query,
        matched,
        corpus_size=len(documents),
        corpus_avgdl=sum(tokenized_lengths) / len(documents),
    )
    assert pooled == pytest.approx([score for score in whole if score > 0])

    # Without the statistics the pool scores differently — which is exactly the
    # bug this guards against.
    naive = _bm25_scores(query, matched)
    assert naive != pytest.approx([score for score in whole if score > 0])


def _local_lexical(col, query, n_results, where=None):
    """What ``lexical_search`` returns with the pushdown disabled."""
    return [
        (hit.id, hit.score)
        for hit in col._lexical_search_local(query=query, n_results=n_results, where=where).hits
    ]


def _reviewer_corpus():
    """Matches far outnumber the window, and the best hit is not the most 'relevant'
    row by any server-side term-frequency measure.

    ``short00`` carries both query terms in a short document, which is what BM25
    rewards; the ``alpha*`` rows are long and carry one term each, which is what
    a raw term-frequency ranking rewards. Any ranking done in SQL over a window
    of 9 drops ``short00``'s peers.
    """
    documents = {"short00": "we shipped the alpha and beta change today"}
    for i in range(30):
        documents[f"alpha{i:02d}"] = (
            "alpha " + " ".join(f"filler{j}" for j in range(60)) + f" release note {i}"
        )
    for i in range(15):
        documents[f"beta{i:02d}"] = (
            "beta " + " ".join(f"other{j}" for j in range(60)) + f" changelog {i}"
        )
    for i in range(4):
        documents[f"short{i + 1:02d}"] = f"alpha beta quick note number {i}"
    for i in range(10):
        documents[f"noise{i:02d}"] = f"unrelated gamma delta epsilon text {i}"
    return documents


def _seed(col, documents):
    ids = list(documents)
    col.add(
        ids=ids,
        documents=[documents[i] for i in ids],
        metadatas=[{"wing": "p"} for _ in ids],
        embeddings=[[1.0, 0.0] for _ in ids],
    )
    return ids


def test_pushdown_keeps_every_hit_when_matches_outnumber_the_window(tmp_path, fake_pgvector):
    """The reviewer's scenario: 60 drawers, a window of 9, 50 of them match.

    The searcher asks for ``n_results * 3`` candidates (searcher.py), so a
    corpus where matches outnumber that window is the normal case, not a corner
    one. Ranking in SQL picks a different 9 than BM25 does; filtering in SQL and
    ranking here cannot.
    """
    _backend, col = _collection(tmp_path)
    documents = _reviewer_corpus()
    _seed(col, documents)
    client = fake_pgvector.instances[0]

    for n_results in (1, 5, 9, 30, 200):
        client.lexical_calls.clear()
        client.scroll_calls.clear()
        pushed = [
            (hit.id, hit.score)
            for hit in col.lexical_search(query="alpha beta", n_results=n_results).hits
        ]
        assert client.lexical_calls, "the pushdown must be the path under test"
        assert client.scroll_calls == [], "no document may cross the wire unmatched"
        assert pushed == _local_lexical(col, "alpha beta", n_results)

    # The specific loss the reviewer reported: the top BM25 hit and everything
    # ranked with it survive a window smaller than the match count.
    everything = _local_lexical(col, "alpha beta", 10_000)
    assert len(everything) == 50 > 9
    top_hit = everything[0][0]
    window = [hit.id for hit in col.lexical_search(query="alpha beta", n_results=9).hits]
    assert top_hit in window
    assert window == [doc_id for doc_id, _ in everything[:9]]


def test_pushdown_matches_local_path_across_queries_and_filters(tmp_path, fake_pgvector):
    """Equivalence, not just recall: same ids, same scores, same order."""
    _backend, col = _collection(tmp_path)
    documents = _reviewer_corpus()
    documents["mixed"] = "قصر الذاكرة يحفظ كلماتك alpha"
    documents["cjk"] = "记忆宫殿笔记 beta"
    documents["dev"] = "contact atk@atkabli.com about /etc/nginx/nginx.conf and npm.install"
    _seed(col, documents)

    queries = [
        "alpha beta",
        "alpha",
        "gamma",
        "الذاكرة",
        "记忆宫殿笔记",
        "atkabli",
        "nginx",
        "npm",
        "nothingmatchesthisatall",
    ]
    for query in queries:
        for n_results in (3, 9, 60):
            assert [
                (hit.id, hit.score)
                for hit in col.lexical_search(query=query, n_results=n_results).hits
            ] == _local_lexical(col, query, n_results), f"{query!r} n={n_results}"


def test_pushdown_ranking_is_deterministic_on_ties(tmp_path, fake_pgvector):
    """Equal scores must not make the cut at ``n_results`` a coin flip."""
    _backend, col = _collection(tmp_path)
    documents = {f"d{i:02d}": "alpha note here" for i in range(20)}
    _seed(col, documents)

    first = [hit.id for hit in col.lexical_search(query="alpha", n_results=5).hits]
    assert first == sorted(first)
    for _ in range(5):
        assert [hit.id for hit in col.lexical_search(query="alpha", n_results=5).hits] == first


def test_lexical_search_falls_back_for_non_pushdown_filter(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    _seed(col, {"a": "alpha note", "b": "beta note"})
    client = fake_pgvector.instances[0]
    client.lexical_calls.clear()
    client.scroll_calls.clear()

    hits = col.lexical_search(query="alpha", n_results=5, where={"$or": [{"wing": "p"}]}).hits

    assert [hit.id for hit in hits] == ["a"]
    assert client.lexical_calls == []
    assert client.scroll_calls, "an $or filter belongs on the exact local path"


def test_lexical_search_falls_back_for_null_operand(tmp_path, fake_pgvector):
    """``metadata @> '{"k": null}'`` needs the key present; ``_matches_where`` does not.

    The containment predicate is narrower than the Python filter here, so a row
    with the key absent would never reach the client and no post-filter could
    put it back.
    """
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b"],
        documents=["alpha note", "alpha other"],
        metadatas=[{"wing": "p", "k": None}, {"wing": "p"}],
        embeddings=[[1, 0], [1, 0]],
    )
    client = fake_pgvector.instances[0]
    client.lexical_calls.clear()

    hits = col.lexical_search(query="alpha", n_results=5, where={"k": None}).hits

    assert client.lexical_calls == []
    assert {hit.id for hit in hits} == {"a", "b"}


def test_lexical_search_falls_back_when_token_counts_are_missing(tmp_path, fake_pgvector):
    """A palace written before the column existed keeps working, just slower."""
    _backend, col = _collection(tmp_path)
    _seed(col, {"a": "alpha note", "b": "beta note"})
    client = fake_pgvector.instances[0]
    client.token_count_supported = False
    for row in client.tables[col._table]["rows"].values():
        row.pop("token_count", None)
    client.lexical_calls.clear()
    client.scroll_calls.clear()

    hits = col.lexical_search(query="alpha", n_results=5).hits

    assert [hit.id for hit in hits] == ["a"]
    assert client.lexical_calls == []
    assert client.scroll_calls


def test_lexical_search_falls_back_when_postgres_fails(tmp_path, fake_pgvector, monkeypatch):
    _backend, col = _collection(tmp_path)
    _seed(col, {"a": "alpha note", "b": "beta note"})
    client = fake_pgvector.instances[0]

    def boom(*_args, **_kwargs):
        raise BackendError("server exploded")

    monkeypatch.setattr(client, "lexical_candidates", boom)
    client.scroll_calls.clear()

    hits = col.lexical_search(query="alpha", n_results=5).hits

    assert [hit.id for hit in hits] == ["a"]
    assert client.scroll_calls, "a server failure must fall back, not drop the arm"


def test_lexical_search_propagates_unsupported_filter(tmp_path, fake_pgvector, monkeypatch):
    """RFC 001: an operator the backend cannot honour is an error, not a slow path."""
    from mempalace.backends import UnsupportedFilterError

    _backend, col = _collection(tmp_path)
    _seed(col, {"a": "alpha note"})
    client = fake_pgvector.instances[0]

    def boom(*_args, **_kwargs):
        raise UnsupportedFilterError("operator '$nope' not pushed down by pgvector")

    monkeypatch.setattr(client, "lexical_candidates", boom)

    with pytest.raises(UnsupportedFilterError):
        col.lexical_search(query="alpha", n_results=5)


def test_lexical_search_empty_query_issues_no_sql(tmp_path, fake_pgvector):
    _backend, col = _collection(tmp_path)
    _seed(col, {"a": "alpha note"})
    client = fake_pgvector.instances[0]
    client.lexical_calls.clear()
    client.scroll_calls.clear()

    assert col.lexical_search(query="!!!", n_results=5).hits == []
    assert client.lexical_calls == []
    assert client.scroll_calls == []


def test_lexical_search_raises_when_table_missing_but_marker_exists(tmp_path, fake_pgvector):
    """Same not-initialized contract as the scan path, for every query shape.

    The table check has to come before the empty-query shortcut, or the same
    collection raises or returns ``[]`` depending only on the query text.
    """
    _backend, col = _collection(tmp_path)
    _seed(col, {"a": "alpha backend note"})
    fake_pgvector.instances[0].tables.clear()  # marker on disk, table gone

    with pytest.raises(CollectionNotInitializedError):
        col.lexical_search(query="backend", n_results=5)
    with pytest.raises(CollectionNotInitializedError):
        col.lexical_search(query="!!!", n_results=5)


def test_existing_collection_gains_lexical_assets_on_open(tmp_path, fake_pgvector):
    """A palace created before this release must pick the column up, not stay slow."""
    backend, col = _collection(tmp_path)
    _seed(col, {"a": "alpha note"})
    client = fake_pgvector.instances[0]
    client.lexical_asset_calls.clear()

    # A second handle on the same, already-populated table: the open path, not
    # the create path, is what an existing palace goes through.
    reopened = backend.get_collection(
        palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
        collection_name="drawers",
        create=True,
    )
    reopened.add(ids=["b"], documents=["beta note"], metadatas=[{}], embeddings=[[0, 1]])

    assert client.lexical_asset_calls == [col._table]
    backend.close()


def test_backfill_token_counts_uses_the_python_tokenizer(monkeypatch):
    """Backfilled lengths must come from ``_tokenize``, not a Postgres regex.

    A backfilled length that is off by one silently changes every future ranking
    for that collection, so the count is computed by the same function BM25 uses.
    """
    client = _PgVectorClient(_PgVectorConfig(dsn="postgresql://localhost/unused", namespace=None))
    client._token_count_columns["drawers"] = True
    pending = [[("a", "alpha beta gamma"), ("b", "solo")], []]
    updates = []

    def fake_execute(sql, params=None, *, fetch=False, many=False):
        if fetch:
            return pending.pop(0)
        updates.extend(params or [])
        return []

    monkeypatch.setattr(client, "_execute", fake_execute)

    assert client.backfill_token_counts("drawers") == 2
    assert updates == [(3, "a"), (1, "b")]


def test_maintenance_state_reports_whether_the_pushdown_is_available(tmp_path, fake_pgvector):
    """An operator has to be able to see which path their queries take."""
    _backend, col = _collection(tmp_path)
    _seed(col, {"a": "alpha note"})
    assert col.maintenance_state()["lexical_pushdown"] is True

    client = fake_pgvector.instances[0]
    for row in client.tables[col._table]["rows"].values():
        row["token_count"] = None
    assert col.maintenance_state()["lexical_pushdown"] is False
