"""Tests for the sqlite-vec backend.

The backend is gated on the ``sqlite-vec`` PyPI package being importable
(it is an optional extra: ``pip install mempalace[sqlite-vec]``). Tests
skip cleanly when the dependency is missing so CI environments without
the extra installed don't fail.
"""

from __future__ import annotations

import pytest

sqlite_vec = pytest.importorskip("sqlite_vec")

from mempalace.backends import (  # noqa: E402
    GetResult,
    PalaceRef,
    QueryResult,
    UnsupportedFilterError,
)
from mempalace.backends.base import BackendClosedError, DimensionMismatchError  # noqa: E402
from mempalace.backends.sqlite_vec import SqliteVecBackend  # noqa: E402


# A 4-dim space is enough to exercise every code path while keeping vectors
# readable in failure messages.
DIM = 4


@pytest.fixture
def backend(tmp_path):
    b = SqliteVecBackend()
    yield b
    b.close()


@pytest.fixture
def palace(tmp_path):
    return PalaceRef(id="test", local_path=str(tmp_path))


@pytest.fixture
def col(backend, palace):
    return backend.get_collection(
        palace=palace,
        collection_name="t",
        create=True,
        options={"dimension": DIM},
    )


# ---------------------------------------------------------------------------
# Backend lifecycle
# ---------------------------------------------------------------------------


def test_get_collection_create_false_raises_for_missing_collection(backend, palace):
    with pytest.raises(Exception):
        backend.get_collection(palace=palace, collection_name="missing", create=False)


def test_get_collection_idempotent_create(backend, palace):
    a = backend.get_collection(palace=palace, collection_name="t", create=True, options={"dimension": DIM})
    b = backend.get_collection(palace=palace, collection_name="t", create=True, options={"dimension": DIM})
    a.add(documents=["x"], ids=["1"], embeddings=[[1, 0, 0, 0]])
    assert b.count() == 1  # second handle sees the first's writes


def test_legacy_positional_signature(backend, tmp_path):
    """Mempalace 3.3 callers still pass ``(palace_path, collection_name, create)``."""
    col = backend.get_collection(str(tmp_path), "t", create=True, options={"dimension": DIM})
    col.add(documents=["x"], ids=["1"], embeddings=[[1, 0, 0, 0]])
    assert col.count() == 1


def test_close_palace_evicts_handle(backend, palace):
    backend.get_collection(palace=palace, collection_name="t", create=True, options={"dimension": DIM})
    backend.close_palace(palace)
    # Reopening creates a fresh connection — no error.
    col = backend.get_collection(palace=palace, collection_name="t", create=False)
    assert col.count() == 0


def test_detect_returns_true_after_db_created(backend, palace, tmp_path):
    backend.get_collection(palace=palace, collection_name="t", create=True, options={"dimension": DIM})
    backend.close()
    assert SqliteVecBackend.detect(str(tmp_path)) is True


def test_detect_returns_false_for_empty_dir(tmp_path):
    assert SqliteVecBackend.detect(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def test_add_persists_documents_metadata_and_vectors(col):
    col.add(
        documents=["red apple", "green apple"],
        ids=["a", "b"],
        metadatas=[{"color": "red"}, {"color": "green"}],
        embeddings=[[1, 0, 0, 0], [0.9, 0.1, 0, 0]],
    )
    assert col.count() == 2
    g = col.get(ids=["a"], include=["documents", "metadatas", "embeddings"])
    assert g.ids == ["a"]
    assert g.documents == ["red apple"]
    assert g.metadatas == [{"color": "red"}]
    assert g.embeddings is not None
    assert g.embeddings[0] == pytest.approx([1.0, 0.0, 0.0, 0.0], rel=1e-5)


def test_upsert_replaces_existing_row(col):
    col.add(documents=["v1"], ids=["a"], embeddings=[[1, 0, 0, 0]])
    col.upsert(
        documents=["v2"],
        ids=["a"],
        metadatas=[{"k": "v"}],
        embeddings=[[0, 1, 0, 0]],
    )
    assert col.count() == 1
    g = col.get(ids=["a"], include=["documents", "metadatas", "embeddings"])
    assert g.documents == ["v2"]
    assert g.metadatas == [{"k": "v"}]
    assert g.embeddings[0] == pytest.approx([0.0, 1.0, 0.0, 0.0], rel=1e-5)


def test_dimension_mismatch_raises(col):
    with pytest.raises(DimensionMismatchError):
        col.add(documents=["x"], ids=["1"], embeddings=[[1, 0, 0]])  # dim 3, not 4


def test_update_merges_metadata_and_replaces_embedding(col):
    col.add(
        documents=["d"],
        ids=["a"],
        metadatas=[{"keep": "yes", "old": "v1"}],
        embeddings=[[1, 0, 0, 0]],
    )
    col.update(
        ids=["a"],
        metadatas=[{"old": "v2", "new": "added"}],
        embeddings=[[0, 0, 1, 0]],
    )
    g = col.get(ids=["a"], include=["metadatas", "embeddings"])
    assert g.metadatas == [{"keep": "yes", "old": "v2", "new": "added"}]
    assert g.embeddings[0] == pytest.approx([0.0, 0.0, 1.0, 0.0], rel=1e-5)


def test_update_skips_missing_id_silently(col):
    col.add(documents=["x"], ids=["a"], embeddings=[[1, 0, 0, 0]])
    col.update(ids=["nonexistent"], documents=["should not appear"])
    assert col.count() == 1


def test_delete_by_ids(col):
    col.add(documents=["a", "b", "c"], ids=["a", "b", "c"], embeddings=[[1, 0, 0, 0]] * 3)
    col.delete(ids=["a", "b"])
    g = col.get()
    assert g.ids == ["c"]


def test_delete_by_where(col):
    col.add(
        documents=["a", "b", "c"],
        ids=["a", "b", "c"],
        metadatas=[{"keep": True}, {"keep": False}, {"keep": False}],
        embeddings=[[1, 0, 0, 0]] * 3,
    )
    col.delete(where={"keep": False})
    assert col.count() == 1
    assert col.get().ids == ["a"]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def test_query_returns_typed_query_result(col):
    col.add(
        documents=["a", "b"],
        ids=["a", "b"],
        embeddings=[[1, 0, 0, 0], [0, 1, 0, 0]],
    )
    qr = col.query(query_embeddings=[[1, 0, 0, 0]], n_results=2)
    assert isinstance(qr, QueryResult)
    assert qr.ids == [["a", "b"]]
    assert qr.distances[0][0] == pytest.approx(0.0, abs=1e-5)


def test_query_rejects_missing_input(col):
    with pytest.raises(ValueError):
        col.query(n_results=1)


def test_query_rejects_both_inputs(col):
    with pytest.raises(ValueError):
        col.query(query_texts=["x"], query_embeddings=[[1, 0, 0, 0]], n_results=1)


def test_query_empty_collection_returns_empty_outer(col):
    qr = col.query(query_embeddings=[[1, 0, 0, 0], [0, 1, 0, 0]], n_results=3)
    assert qr.ids == [[], []]
    assert qr.documents == [[], []]
    assert qr.distances == [[], []]


def test_query_with_where_filter(col):
    col.add(
        documents=["a", "b", "c"],
        ids=["a", "b", "c"],
        metadatas=[{"wing": "x"}, {"wing": "y"}, {"wing": "x"}],
        embeddings=[[1, 0, 0, 0], [0.9, 0.1, 0, 0], [0.8, 0, 0.2, 0]],
    )
    qr = col.query(query_embeddings=[[1, 0, 0, 0]], n_results=5, where={"wing": "x"})
    assert set(qr.ids[0]) == {"a", "c"}


def test_query_supports_include_embeddings(col):
    col.add(documents=["a"], ids=["a"], embeddings=[[1, 0, 0, 0]])
    qr = col.query(
        query_embeddings=[[1, 0, 0, 0]],
        n_results=1,
        include=["documents", "metadatas", "distances", "embeddings"],
    )
    assert qr.embeddings is not None
    assert qr.embeddings[0][0] == pytest.approx([1.0, 0.0, 0.0, 0.0], rel=1e-5)


def test_query_dict_compat_access(col):
    col.add(documents=["a"], ids=["a"], embeddings=[[1, 0, 0, 0]])
    qr = col.query(query_embeddings=[[1, 0, 0, 0]], n_results=1)
    # transitional dict-protocol access still works for legacy callers
    assert qr["ids"] == [["a"]]
    assert qr.get("distances")[0][0] == pytest.approx(0.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Where-clause compiler
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "where, expected_ids",
    [
        ({"color": "red"}, {"a"}),
        ({"color": {"$eq": "red"}}, {"a"}),
        ({"color": {"$ne": "red"}}, {"b", "c"}),
        ({"size": {"$gt": 5}}, {"b", "c"}),
        ({"size": {"$gte": 5}}, {"a", "b", "c"}),
        ({"size": {"$lt": 7}}, {"a"}),
        ({"size": {"$lte": 5}}, {"a"}),
        ({"color": {"$in": ["red", "blue"]}}, {"a", "c"}),
        ({"color": {"$nin": ["red"]}}, {"b", "c"}),
        ({"$and": [{"color": "red"}, {"size": 5}]}, {"a"}),
        ({"$or": [{"color": "red"}, {"size": 9}]}, {"a", "c"}),
        ({"color": "red", "size": 5}, {"a"}),  # implicit AND
    ],
)
def test_where_compiler_supported_operators(col, where, expected_ids):
    col.add(
        documents=["a", "b", "c"],
        ids=["a", "b", "c"],
        metadatas=[
            {"color": "red", "size": 5},
            {"color": "green", "size": 7},
            {"color": "blue", "size": 9},
        ],
        embeddings=[[1, 0, 0, 0]] * 3,
    )
    g = col.get(where=where)
    assert set(g.ids) == expected_ids


def test_where_compiler_rejects_unknown_operator(col):
    col.add(documents=["a"], ids=["a"], metadatas=[{"k": 1}], embeddings=[[1, 0, 0, 0]])
    with pytest.raises(UnsupportedFilterError):
        col.get(where={"k": {"$regex": ".*"}})


def test_where_document_contains_filter(col):
    col.add(
        documents=["red apple pie", "green apple", "yellow banana"],
        ids=["a", "b", "c"],
        embeddings=[[1, 0, 0, 0]] * 3,
    )
    g = col.get(where_document={"$contains": "apple"})
    assert set(g.ids) == {"a", "b"}


def test_where_document_not_contains_filter(col):
    col.add(
        documents=["red apple pie", "green apple", "yellow banana"],
        ids=["a", "b", "c"],
        embeddings=[[1, 0, 0, 0]] * 3,
    )
    g = col.get(where_document={"$not_contains": "apple"})
    assert g.ids == ["c"]


# ---------------------------------------------------------------------------
# get with limit / offset
# ---------------------------------------------------------------------------


def test_get_limit_and_offset(col):
    col.add(
        documents=[f"d{i}" for i in range(5)],
        ids=[f"r{i}" for i in range(5)],
        embeddings=[[1, 0, 0, 0]] * 5,
    )
    page1 = col.get(limit=2)
    page2 = col.get(limit=2, offset=2)
    assert len(page1.ids) == 2
    assert len(page2.ids) == 2
    assert set(page1.ids).isdisjoint(set(page2.ids))


def test_get_returns_typed_get_result(col):
    col.add(documents=["d"], ids=["a"], metadatas=[{"k": 1}], embeddings=[[1, 0, 0, 0]])
    g = col.get(ids=["a"])
    assert isinstance(g, GetResult)
    assert g.ids == ["a"]
    assert g.documents == ["d"]
    assert g.metadatas == [{"k": 1}]


# ---------------------------------------------------------------------------
# Backend selection via the public registry
# ---------------------------------------------------------------------------


def test_registry_exposes_sqlite_vec():
    from mempalace.backends import available_backends

    assert "sqlite_vec" in available_backends()


def test_registry_get_backend_returns_singleton():
    from mempalace.backends import get_backend

    a = get_backend("sqlite_vec")
    b = get_backend("sqlite_vec")
    assert a is b
