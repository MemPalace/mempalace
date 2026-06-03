"""Tests for the SQLiteVec storage backend (RFC 001).

Follows the same structure and patterns as tests/test_backends.py:

  - Pure-Python utility tests  (no I/O, no real DB)
  - _meta_matches filter logic (pure Python)
  - SqliteVecCollection unit tests (real SQLite file, no sqlite-vec extension)
  - query() tests using pre-computed embeddings (brute-force path, CI-safe)
  - SqliteVecBackend backend-level tests
  - detect() classmethod tests
  - Registry integration tests
  - Integration / round-trip tests (real file, multiple round-trips)
"""

from __future__ import annotations

import os
import threading

import pytest

from mempalace.backends.base import (
    BackendClosedError,
    BackendError,
    GetResult,
    PalaceNotFoundError,
    PalaceRef,
    QueryResult,
)
from mempalace.backends.sqlite_vec import (
    SqliteVecBackend,
    SqliteVecCollection,
    _ANN_OVERFETCH,
    _cosine_brute,
    _cosine_distance,
    _pack_f32,
    _safe_table_name,
    _unpack_f32,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _unit_vec(dim: int, hot: int = 0) -> list[float]:
    """Return a unit vector of `dim` dimensions with a 1.0 at index `hot`."""
    v = [0.0] * dim
    v[hot % dim] = 1.0
    return v


def _make_col(tmp_path) -> SqliteVecCollection:
    col = SqliteVecCollection(str(tmp_path / "test.db"), "drawers")
    col.count()  # trigger _init_schema
    return col


def _palace(tmp_path) -> PalaceRef:
    p = str(tmp_path / "palace")
    return PalaceRef(id=p, local_path=p)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def col(tmp_path):
    c = _make_col(tmp_path)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _reset_registry():
    """Ensure the backend registry is clean between tests."""
    from mempalace.backends.registry import reset_backends

    yield
    reset_backends()


# ===========================================================================
# Section 1 — Pure-Python utility functions
# ===========================================================================


def test_pack_unpack_f32_roundtrip():
    original = [1.0, -2.5, 0.0, 3.14]
    assert _unpack_f32(_pack_f32(original)) == pytest.approx(original)


def test_unpack_f32_none_returns_none():
    assert _unpack_f32(None) is None


def test_unpack_f32_empty_bytes_returns_none():
    assert _unpack_f32(b"") is None


def test_cosine_distance_identical_vectors_is_zero():
    v = [1.0, 0.0, 0.0]
    assert _cosine_distance(v, v) == pytest.approx(0.0)


def test_cosine_distance_orthogonal_vectors_is_one():
    assert _cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)


def test_cosine_distance_zero_vector_returns_one():
    assert _cosine_distance([0.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_brute_returns_top_n_sorted_by_distance():
    # 5 unit vectors along each axis; query along axis 0 → axis-0 doc is nearest
    dim = 5
    rows = [(f"id{i}", f"doc{i}", {}, _pack_f32(_unit_vec(dim, i))) for i in range(dim)]
    result = _cosine_brute(_unit_vec(dim, 0), rows, 3)
    assert len(result) == 3
    ids, _, _, dists = zip(*result)
    assert ids[0] == "id0"  # nearest is axis-0
    # distances must be non-decreasing
    assert list(dists) == sorted(dists)


def test_cosine_brute_skips_rows_with_null_embedding():
    rows = [
        ("id1", "doc1", {}, _pack_f32([1.0, 0.0])),
        ("id2", "doc2", {}, None),  # no embedding — must be skipped
    ]
    result = _cosine_brute([1.0, 0.0], rows, 5)
    ids = [r[0] for r in result]
    assert "id1" in ids
    assert "id2" not in ids


def test_cosine_brute_returns_fewer_than_n_when_fewer_rows_exist():
    rows = [
        ("id1", "doc1", {}, _pack_f32([1.0, 0.0])),
        ("id2", "doc2", {}, _pack_f32([0.0, 1.0])),
    ]
    result = _cosine_brute([1.0, 0.0], rows, 10)
    assert len(result) == 2


def test_cosine_brute_accepts_dict_meta_without_json_roundtrip():
    """Passing pre-parsed dict meta must work — no JSON round-trip needed."""
    rows = [
        ("id1", "doc", {"wing": "project"}, _pack_f32([1.0, 0.0])),
    ]
    result = _cosine_brute([1.0, 0.0], rows, 5)
    assert len(result) == 1
    _id, _doc, meta, _dist = result[0]
    assert isinstance(meta, dict)
    assert meta["wing"] == "project"


def test_cosine_brute_accepts_json_string_meta():
    """Passing JSON string meta (from sqlite-vec fallback path) must also work."""
    import json

    rows = [
        ("id1", "doc", json.dumps({"wing": "notes"}), _pack_f32([1.0, 0.0])),
    ]
    result = _cosine_brute([1.0, 0.0], rows, 5)
    assert len(result) == 1
    _id, _doc, meta, _dist = result[0]
    assert isinstance(meta, dict)
    assert meta["wing"] == "notes"


# ===========================================================================
# Section 2 — _meta_matches filter logic
# ===========================================================================

_mm = SqliteVecCollection._meta_matches


def test_meta_matches_none_where_always_true():
    assert _mm({"k": 1}, None) is True


def test_meta_matches_empty_where_always_true():
    assert _mm({"k": 1}, {}) is True


def test_meta_matches_implicit_eq_passes():
    assert _mm({"wing": "project"}, {"wing": "project"}) is True


def test_meta_matches_implicit_eq_fails():
    assert _mm({"wing": "notes"}, {"wing": "project"}) is False


def test_meta_matches_explicit_eq_operator():
    assert _mm({"wing": "project"}, {"wing": {"$eq": "project"}}) is True
    assert _mm({"wing": "notes"}, {"wing": {"$eq": "project"}}) is False


def test_meta_matches_ne_operator():
    assert _mm({"wing": "project"}, {"wing": {"$ne": "notes"}}) is True
    assert _mm({"wing": "notes"}, {"wing": {"$ne": "notes"}}) is False


def test_meta_matches_in_operator():
    assert _mm({"wing": "project"}, {"wing": {"$in": ["project", "notes"]}}) is True
    assert _mm({"wing": "archive"}, {"wing": {"$in": ["project", "notes"]}}) is False


def test_meta_matches_nin_operator():
    assert _mm({"wing": "project"}, {"wing": {"$nin": ["archive"]}}) is True
    assert _mm({"wing": "archive"}, {"wing": {"$nin": ["archive"]}}) is False


def test_meta_matches_gt_gte_lt_lte_operators():
    assert _mm({"score": 6}, {"score": {"$gt": 5}}) is True
    assert _mm({"score": 5}, {"score": {"$gt": 5}}) is False
    assert _mm({"score": 5}, {"score": {"$gte": 5}}) is True
    assert _mm({"score": 4}, {"score": {"$lt": 5}}) is True
    assert _mm({"score": 5}, {"score": {"$lt": 5}}) is False
    assert _mm({"score": 5}, {"score": {"$lte": 5}}) is True


def test_meta_matches_gt_with_none_value_fails():
    assert _mm({}, {"score": {"$gt": 5}}) is False


def test_meta_matches_contains_operator():
    assert _mm({"tag": "python"}, {"tag": {"$contains": "py"}}) is True
    assert _mm({"tag": "ruby"}, {"tag": {"$contains": "py"}}) is False


def test_meta_matches_contains_non_string_value_fails():
    assert _mm({"val": 42}, {"val": {"$contains": "4"}}) is False


def test_meta_matches_and_conjunction():
    where = {"$and": [{"wing": "project"}, {"room": "backend"}]}
    assert _mm({"wing": "project", "room": "backend"}, where) is True
    assert _mm({"wing": "project", "room": "frontend"}, where) is False
    assert _mm({"wing": "notes", "room": "backend"}, where) is False


def test_meta_matches_or_disjunction():
    where = {"$or": [{"wing": "project"}, {"wing": "notes"}]}
    assert _mm({"wing": "project"}, where) is True
    assert _mm({"wing": "notes"}, where) is True
    assert _mm({"wing": "archive"}, where) is False


def test_meta_matches_nested_and_or():
    where = {
        "$and": [
            {"wing": "project"},
            {"$or": [{"room": "backend"}, {"room": "frontend"}]},
        ]
    }
    assert _mm({"wing": "project", "room": "backend"}, where) is True
    assert _mm({"wing": "project", "room": "frontend"}, where) is True
    assert _mm({"wing": "project", "room": "other"}, where) is False
    assert _mm({"wing": "notes", "room": "backend"}, where) is False


# ===========================================================================
# Section 3 — SqliteVecCollection unit tests
# ===========================================================================


def test_collection_initial_count_is_zero(col):
    assert col.count() == 0


def test_add_then_count(col):
    col.add(ids=["a"], documents=["hello world"])
    assert col.count() == 1


def test_add_duplicate_id_raises_backend_error(col):
    col.add(ids=["a"], documents=["first"])
    with pytest.raises(BackendError, match="already exist"):
        col.add(ids=["a"], documents=["second"])
    # Original data must be intact
    result = col.get(ids=["a"])
    assert result.documents == ["first"]


def test_upsert_duplicate_id_overwrites(col):
    col.add(ids=["a"], documents=["original"])
    col.upsert(ids=["a"], documents=["updated"])
    assert col.count() == 1
    result = col.get(ids=["a"])
    assert result.documents == ["updated"]


def test_add_then_get_by_ids(col):
    col.add(ids=["x"], documents=["my document"], metadatas=[{"wing": "proj"}])
    result = col.get(ids=["x"])
    assert result.ids == ["x"]
    assert result.documents == ["my document"]
    assert result.metadatas == [{"wing": "proj"}]


def test_get_returns_typed_get_result(col):
    col.add(ids=["a"], documents=["doc"])
    result = col.get(ids=["a"])
    assert isinstance(result, GetResult)


def test_get_returns_empty_result_for_missing_id(col):
    result = col.get(ids=["does-not-exist"])
    assert result.ids == []
    assert result.documents == []


def test_get_include_embeddings(col):
    emb = [1.0, 0.0, 0.0, 0.5]
    col.add(ids=["a"], documents=["doc"], embeddings=[emb])
    result = col.get(ids=["a"], include=["embeddings"])
    assert result.embeddings is not None
    assert len(result.embeddings) == 1
    assert result.embeddings[0] == pytest.approx(emb)


def test_get_all_without_ids(col):
    col.add(ids=["a", "b", "c"], documents=["d1", "d2", "d3"])
    result = col.get()
    assert len(result.ids) == 3


def test_get_with_where_filter(col):
    col.add(
        ids=["a", "b"],
        documents=["d1", "d2"],
        metadatas=[{"wing": "project"}, {"wing": "notes"}],
    )
    result = col.get(where={"wing": "project"})
    assert result.ids == ["a"]


def test_get_with_where_document_filter(col):
    col.add(ids=["a", "b"], documents=["auth token", "database pool"])
    result = col.get(where_document={"$contains": "auth"})
    assert result.ids == ["a"]


def test_get_limit_only(col):
    for i in range(5):
        col.add(ids=[f"id{i}"], documents=[f"doc{i}"])
    result = col.get(limit=2)
    assert len(result.ids) == 2


def test_get_offset_only(col):
    for i in range(5):
        col.add(ids=[f"id{i}"], documents=[f"doc{i}"])
    all_ids = col.get().ids
    result = col.get(offset=3)
    assert result.ids == all_ids[3:]


def test_get_limit_and_offset(col):
    """Validates Fix 2 — correct LIMIT … OFFSET SQL ordering."""
    for i in range(10):
        col.add(ids=[f"id{i:02d}"], documents=[f"doc{i}"])
    all_ids = col.get().ids
    result = col.get(limit=3, offset=2)
    assert len(result.ids) == 3
    assert result.ids == all_ids[2:5]


def test_get_offset_zero_is_not_falsy_bug(col):
    """offset=0 must not suppress the LIMIT clause."""
    for i in range(5):
        col.add(ids=[f"id{i}"], documents=[f"doc{i}"])
    result = col.get(limit=2, offset=0)
    assert len(result.ids) == 2


def test_get_with_ids_and_limit_offset_slices_python_side(col):
    col.add(ids=["a", "b", "c"], documents=["d1", "d2", "d3"])
    result = col.get(ids=["a", "b", "c"], limit=2, offset=1)
    assert len(result.ids) == 2


def test_delete_by_ids(col):
    col.add(ids=["a", "b"], documents=["d1", "d2"])
    col.delete(ids=["a"])
    assert col.count() == 1
    assert col.get(ids=["a"]).ids == []


def test_delete_by_where(col):
    col.add(
        ids=["a", "b", "c"],
        documents=["d1", "d2", "d3"],
        metadatas=[{"wing": "notes"}, {"wing": "project"}, {"wing": "notes"}],
    )
    col.delete(where={"wing": "notes"})
    assert col.count() == 1
    assert col.get().ids == ["b"]


def test_delete_noop_on_missing_id(col):
    col.delete(ids=["does-not-exist"])  # must not raise


def test_update_changes_document(col):
    col.add(ids=["a"], documents=["original"], metadatas=[{"k": "v"}])
    col.update(ids=["a"], documents=["updated"])
    result = col.get(ids=["a"])
    assert result.documents == ["updated"]
    assert result.metadatas == [{"k": "v"}]  # metadata preserved


def test_update_merges_metadata(col):
    col.add(ids=["a"], documents=["doc"], metadatas=[{"existing": "yes"}])
    col.update(ids=["a"], metadatas=[{"new_key": "hello"}])
    result = col.get(ids=["a"])
    meta = result.metadatas[0]
    assert meta["existing"] == "yes"  # original key preserved
    assert meta["new_key"] == "hello"  # new key added


def test_update_validates_length_mismatch(col):
    """Validates Fix 6 — length-mismatch guards on update()."""
    col.add(ids=["a", "b"], documents=["d1", "d2"])
    with pytest.raises(ValueError, match="documents"):
        col.update(ids=["a", "b"], documents=["only-one"])
    with pytest.raises(ValueError, match="metadatas"):
        col.update(ids=["a", "b"], metadatas=[{"k": 1}])


def test_update_raises_when_no_fields_given(col):
    col.add(ids=["a"], documents=["doc"])
    with pytest.raises(ValueError):
        col.update(ids=["a"])


def test_close_makes_collection_unusable(tmp_path):
    col = _make_col(tmp_path)
    col.close()
    with pytest.raises(BackendClosedError):
        col.count()
    with pytest.raises(BackendClosedError):
        col.add(ids=["a"], documents=["d"])
    with pytest.raises(BackendClosedError):
        col.get(ids=["a"])


def test_double_close_is_safe(tmp_path):
    col = _make_col(tmp_path)
    col.close()
    col.close()  # must not raise


def test_health_returns_healthy_on_open_collection(col):
    assert col.health().ok is True


def test_health_returns_unhealthy_after_close(tmp_path):
    col = _make_col(tmp_path)
    col.close()
    assert col.health().ok is False


# ===========================================================================
# Section 4 — query() tests (brute-force path, no sqlite-vec extension needed)
# ===========================================================================

DIM = 4  # keep embeddings tiny for test speed


def _seeded_col(col):
    """Seed col with 3 documents having orthogonal unit embeddings."""
    embeddings = [_unit_vec(DIM, i) for i in range(3)]
    col.add(
        ids=["doc0", "doc1", "doc2"],
        documents=["auth token", "database pool", "sprint plan"],
        metadatas=[
            {"wing": "project", "room": "backend"},
            {"wing": "project", "room": "db"},
            {"wing": "notes", "room": "planning"},
        ],
        embeddings=embeddings,
    )
    return col


def test_query_rejects_both_texts_and_embeddings(col):
    with pytest.raises(ValueError):
        col.query(query_texts=["q"], query_embeddings=[[0.1] * DIM])


def test_query_rejects_neither_texts_nor_embeddings(col):
    with pytest.raises(ValueError):
        col.query()


def test_query_returns_typed_query_result(col):
    _seeded_col(col)
    result = col.query(query_embeddings=[_unit_vec(DIM, 0)])
    assert isinstance(result, QueryResult)


def test_query_returns_correct_outer_dimension_for_multiple_queries(col):
    _seeded_col(col)
    result = col.query(query_embeddings=[_unit_vec(DIM, 0), _unit_vec(DIM, 1)])
    assert len(result.ids) == 2
    assert len(result.documents) == 2


def test_query_returns_nearest_neighbour(col):
    """Query with doc1's own embedding — it must be the first hit."""
    _seeded_col(col)
    result = col.query(query_embeddings=[_unit_vec(DIM, 1)], n_results=1)
    assert result.ids[0] == ["doc1"]


def test_query_respects_n_results(col):
    _seeded_col(col)
    result = col.query(query_embeddings=[_unit_vec(DIM, 0)], n_results=2)
    assert len(result.ids[0]) == 2


def test_query_with_where_filter_excludes_non_matching(col):
    _seeded_col(col)
    result = col.query(
        query_embeddings=[_unit_vec(DIM, 0)],
        n_results=5,
        where={"wing": "notes"},
    )
    for row_id in result.ids[0]:
        assert row_id == "doc2"  # only notes wing doc


def test_query_with_where_document_filter(col):
    _seeded_col(col)
    result = col.query(
        query_embeddings=[_unit_vec(DIM, 0)],
        n_results=5,
        where_document={"$contains": "auth"},
    )
    assert result.ids[0] == ["doc0"]


def test_query_include_only_distances(col):
    _seeded_col(col)
    result = col.query(
        query_embeddings=[_unit_vec(DIM, 0)],
        include=["distances"],
    )
    assert result.distances[0]  # non-empty
    assert result.documents == [[]]
    assert result.metadatas == [[]]


def test_query_empty_collection_returns_empty_result(col):
    result = col.query(query_embeddings=[_unit_vec(DIM, 0)])
    assert result.ids == [[]]


def test_query_distances_sorted_ascending(col):
    _seeded_col(col)
    result = col.query(query_embeddings=[_unit_vec(DIM, 0)], n_results=3)
    dists = result.distances[0]
    assert dists == sorted(dists)


def test_query_distances_are_non_negative(col):
    _seeded_col(col)
    result = col.query(query_embeddings=[_unit_vec(DIM, 0)], n_results=3)
    assert all(d >= 0.0 for d in result.distances[0])


# ===========================================================================
# Section 5 — SqliteVecBackend backend-level tests
# ===========================================================================


def test_backend_get_collection_creates_directory_and_db(tmp_path):
    backend = SqliteVecBackend()
    ref = _palace(tmp_path)
    col = backend.get_collection(palace=ref, collection_name="test", create=True)
    col.count()  # trigger lazy connection → creates palace.db
    assert os.path.isdir(ref.local_path)
    assert os.path.isfile(os.path.join(ref.local_path, "palace.db"))
    assert isinstance(col, SqliteVecCollection)
    backend.close()


def test_backend_get_collection_create_false_raises_when_missing(tmp_path):
    backend = SqliteVecBackend()
    ref = _palace(tmp_path)
    with pytest.raises(PalaceNotFoundError):
        backend.get_collection(palace=ref, collection_name="test", create=False)
    backend.close()


def test_backend_get_collection_returns_sqlite_vec_collection(tmp_path):
    backend = SqliteVecBackend()
    ref = _palace(tmp_path)
    col = backend.get_collection(palace=ref, collection_name="test", create=True)
    assert isinstance(col, SqliteVecCollection)
    backend.close()


def test_backend_get_collection_requires_local_path():
    backend = SqliteVecBackend()
    ref = PalaceRef(id="no-path")  # local_path=None
    with pytest.raises(PalaceNotFoundError):
        backend.get_collection(palace=ref, collection_name="test", create=True)
    backend.close()


def test_backend_caches_collection_on_repeated_calls(tmp_path):
    backend = SqliteVecBackend()
    ref = _palace(tmp_path)
    col1 = backend.get_collection(palace=ref, collection_name="test", create=True)
    col2 = backend.get_collection(palace=ref, collection_name="test", create=True)
    assert col1 is col2
    backend.close()


def test_backend_returns_new_collection_after_close(tmp_path):
    backend = SqliteVecBackend()
    ref = _palace(tmp_path)
    col1 = backend.get_collection(palace=ref, collection_name="test", create=True)
    col1.close()
    col2 = backend.get_collection(palace=ref, collection_name="test", create=True)
    assert col2 is not col1
    assert not col2._closed
    backend.close()


def test_backend_close_closes_all_cached_collections(tmp_path):
    backend = SqliteVecBackend()
    ref = _palace(tmp_path)
    col = backend.get_collection(palace=ref, collection_name="test", create=True)
    backend.close()
    assert col._closed


def test_backend_get_collection_raises_after_backend_close(tmp_path):
    backend = SqliteVecBackend()
    ref = _palace(tmp_path)
    backend.close()
    with pytest.raises(BackendClosedError):
        backend.get_collection(palace=ref, collection_name="test", create=True)


def test_backend_close_palace_evicts_only_that_palace(tmp_path):
    backend = SqliteVecBackend()
    ref_a = PalaceRef(id="a", local_path=str(tmp_path / "palace_a"))
    ref_b = PalaceRef(id="b", local_path=str(tmp_path / "palace_b"))
    col_a = backend.get_collection(palace=ref_a, collection_name="test", create=True)
    col_b = backend.get_collection(palace=ref_b, collection_name="test", create=True)
    backend.close_palace(ref_a)
    assert col_a._closed
    assert not col_b._closed
    backend.close()


def test_backend_health_healthy_when_open():
    backend = SqliteVecBackend()
    assert backend.health().ok is True
    backend.close()


def test_backend_health_unhealthy_after_close():
    backend = SqliteVecBackend()
    backend.close()
    assert backend.health().ok is False


# ===========================================================================
# Section 6 — detect() classmethod
# ===========================================================================


def test_detect_returns_true_when_palace_db_present_and_no_chroma(tmp_path):
    (tmp_path / "palace.db").write_bytes(b"")
    assert SqliteVecBackend.detect(str(tmp_path)) is True


def test_detect_returns_false_when_palace_db_absent(tmp_path):
    assert SqliteVecBackend.detect(str(tmp_path)) is False


def test_detect_returns_false_when_chroma_sqlite3_also_present(tmp_path):
    (tmp_path / "palace.db").write_bytes(b"")
    (tmp_path / "chroma.sqlite3").write_bytes(b"")
    assert SqliteVecBackend.detect(str(tmp_path)) is False


def test_detect_returns_false_on_empty_directory(tmp_path):
    assert SqliteVecBackend.detect(str(tmp_path)) is False


def test_detect_returns_false_on_nonexistent_path(tmp_path):
    assert SqliteVecBackend.detect(str(tmp_path / "missing")) is False


# ===========================================================================
# Section 7 — Registry integration (requires Fix 7)
# ===========================================================================


def test_sqlite_vec_registered_in_registry():
    from mempalace.backends import available_backends

    assert "sqlite_vec" in available_backends()


def test_get_backend_sqlite_vec_returns_sqlite_vec_backend():
    from mempalace.backends import get_backend

    backend = get_backend("sqlite_vec")
    assert isinstance(backend, SqliteVecBackend)


def test_registry_resolve_backend_detects_sqlite_vec_from_palace_db(tmp_path):
    from mempalace.backends import resolve_backend_for_palace

    palace_path = str(tmp_path / "palace")
    os.makedirs(palace_path)
    (tmp_path / "palace" / "palace.db").write_bytes(b"")
    result = resolve_backend_for_palace(palace_path=palace_path)
    assert result == "sqlite_vec"


def test_manual_register_sqlite_vec_backend():
    from mempalace.backends.registry import get_backend, register, reset_backends, unregister

    reset_backends()
    unregister("sqlite_vec")
    register("sqlite_vec", SqliteVecBackend)
    backend = get_backend("sqlite_vec")
    assert isinstance(backend, SqliteVecBackend)


# ===========================================================================
# Section 8 — Integration tests (real file, multiple round-trips)
# ===========================================================================


def test_integration_add_get_roundtrip(tmp_path):
    col = _make_col(tmp_path)
    embs = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
    col.add(
        ids=["a", "b"],
        documents=["doc A", "doc B"],
        metadatas=[{"wing": "proj"}, {"wing": "notes"}],
        embeddings=embs,
    )
    result = col.get(ids=["a", "b"], include=["documents", "metadatas", "embeddings"])
    assert set(result.ids) == {"a", "b"}
    idx_a = result.ids.index("a")
    assert result.documents[idx_a] == "doc A"
    assert result.metadatas[idx_a] == {"wing": "proj"}
    assert result.embeddings is not None
    assert result.embeddings[idx_a] == pytest.approx(embs[0])
    col.close()


def test_integration_upsert_replaces_document(tmp_path):
    col = _make_col(tmp_path)
    col.add(ids=["a"], documents=["original"])
    col.upsert(ids=["a"], documents=["replaced"])
    result = col.get(ids=["a"])
    assert result.documents == ["replaced"]
    col.close()


def test_integration_delete_and_count(tmp_path):
    col = _make_col(tmp_path)
    col.add(ids=[f"id{i}" for i in range(5)], documents=[f"doc{i}" for i in range(5)])
    col.delete(ids=["id0", "id2"])
    assert col.count() == 3
    col.close()


def test_integration_query_returns_nearest_in_brute_force_mode(tmp_path):
    """End-to-end query through the brute-force path (no sqlite-vec needed)."""
    col = _make_col(tmp_path)
    embs = [_unit_vec(4, i) for i in range(4)]
    col.add(
        ids=[f"doc{i}" for i in range(4)],
        documents=[f"content {i}" for i in range(4)],
        embeddings=embs,
    )
    result = col.query(query_embeddings=[_unit_vec(4, 2)], n_results=1)
    assert result.ids[0] == ["doc2"]
    col.close()


def test_integration_get_limit_offset_sql_ordering(tmp_path):
    """Directly validates Fix 2 — correct LIMIT … OFFSET in SQL."""
    col = _make_col(tmp_path)
    for i in range(10):
        col.add(ids=[f"id{i:02d}"], documents=[f"doc{i}"])
    all_ids = col.get().ids
    result = col.get(limit=3, offset=5)
    assert len(result.ids) == 3
    assert result.ids == all_ids[5:8]
    col.close()


def test_integration_collection_persists_across_reconnect(tmp_path):
    db_path = str(tmp_path / "test.db")
    col1 = SqliteVecCollection(db_path, "drawers")
    col1.add(ids=["a", "b"], documents=["d1", "d2"])
    col1.close()

    col2 = SqliteVecCollection(db_path, "drawers")
    assert col2.count() == 2
    col2.close()


def test_integration_concurrent_writes_do_not_corrupt(tmp_path):
    col = _make_col(tmp_path)
    errors: list[Exception] = []

    def _add_batch(start: int) -> None:
        try:
            for i in range(start, start + 50):
                col.add(ids=[f"id{i}"], documents=[f"doc{i}"])
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_add_batch, args=(0,))
    t2 = threading.Thread(target=_add_batch, args=(50,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"Concurrent write errors: {errors}"
    assert col.count() == 100
    col.close()


def test_integration_update_preserves_embedding_when_only_metadata_updated(tmp_path):
    """Validates Fix 6 behavior — original embedding is preserved on metadata-only update."""
    col = _make_col(tmp_path)
    emb = [1.0, 0.5, 0.0, 0.0]
    col.add(ids=["a"], documents=["doc"], embeddings=[emb])
    col.update(ids=["a"], metadatas=[{"tag": "new"}])
    result = col.get(ids=["a"], include=["embeddings", "metadatas"])
    assert result.metadatas == [{"tag": "new"}]
    assert result.embeddings is not None
    assert result.embeddings[0] == pytest.approx(emb)
    col.close()


# ===========================================================================
# Section 9 — Multi-collection and collection_name validation
# ===========================================================================


def test_safe_table_name_accepts_valid_identifiers():
    assert _safe_table_name("drawers") == "drawers"
    assert _safe_table_name("my_collection") == "my_collection"
    assert _safe_table_name("Col123") == "Col123"
    assert _safe_table_name("_private") == "_private"


def test_safe_table_name_rejects_invalid_identifiers():
    with pytest.raises(ValueError):
        _safe_table_name("has space")
    with pytest.raises(ValueError):
        _safe_table_name("has-dash")
    with pytest.raises(ValueError):
        _safe_table_name("123starts_with_digit")
    with pytest.raises(ValueError):
        _safe_table_name("drop; table--")
    with pytest.raises(ValueError):
        _safe_table_name("")


def test_two_collections_in_same_db_are_isolated(tmp_path):
    """Two SqliteVecCollections with different names share the same DB file
    but store data in separate tables — writes to one must not appear in the other."""
    db_path = str(tmp_path / "palace.db")
    col_a = SqliteVecCollection(db_path, "alpha")
    col_b = SqliteVecCollection(db_path, "beta")

    col_a.add(ids=["a1"], documents=["doc from alpha"])
    col_b.add(ids=["b1"], documents=["doc from beta"])

    assert col_a.count() == 1
    assert col_b.count() == 1
    assert col_a.get(ids=["b1"]).ids == []  # beta's id not visible in alpha
    assert col_b.get(ids=["a1"]).ids == []  # alpha's id not visible in beta

    col_a.close()
    col_b.close()


def test_backend_two_collections_same_palace_are_independent(tmp_path):
    """get_collection() with different names on the same palace returns distinct objects
    that store data independently."""
    backend = SqliteVecBackend()
    ref = _palace(tmp_path)

    col_x = backend.get_collection(palace=ref, collection_name="col_x", create=True)
    col_y = backend.get_collection(palace=ref, collection_name="col_y", create=True)

    assert col_x is not col_y

    col_x.add(ids=["x1"], documents=["only in x"])
    assert col_y.count() == 0  # y is still empty

    backend.close()


# ===========================================================================
# Section 10 — Dynamic embedding dimension + ANN over-fetch
# ===========================================================================


def test_dynamic_dim_detected_from_first_write(tmp_path):
    """vec_dim must be None before any write, then set to the actual dimension."""
    col = _make_col(tmp_path)
    assert col._vec_dim is None  # not yet determined

    col.add(ids=["a"], documents=["doc"], embeddings=[_unit_vec(8, 0)])
    # After the first write the dim should be recorded (only when sqlite-vec is available).
    # When sqlite-vec is absent the attribute stays None — that's also valid.
    if col._has_vec:
        assert col._vec_dim == 8
    col.close()


def test_dynamic_dim_no_vec_table_when_no_embeddings(tmp_path):
    """If no embedding is ever written, the vec table must NOT be created."""
    col = SqliteVecCollection(str(tmp_path / "test.db"), "drawers")
    col.add(ids=["a", "b"], documents=["doc1", "doc2"])
    assert col._vec_dim is None
    col.close()


def test_dynamic_dim_mismatch_raises_dimension_mismatch_error(tmp_path):
    """Writing embeddings of two different dimensions must raise DimensionMismatchError."""
    from mempalace.backends.base import DimensionMismatchError

    col = SqliteVecCollection(str(tmp_path / "test.db"), "drawers")

    if not col._has_vec:
        pytest.skip("sqlite-vec not available — dimension enforcement requires vec table")

    col.add(ids=["a"], documents=["doc a"], embeddings=[_unit_vec(4, 0)])
    assert col._vec_dim == 4

    with pytest.raises(DimensionMismatchError, match="dimension"):
        col.add(ids=["b"], documents=["doc b"], embeddings=[_unit_vec(8, 0)])
    col.close()


def test_dynamic_dim_persists_across_reconnect(tmp_path):
    """Dimension detected on first connect must be readable on a subsequent connection."""
    db_path = str(tmp_path / "test.db")

    col1 = SqliteVecCollection(db_path, "drawers")
    if not col1._has_vec:
        col1.close()
        pytest.skip("sqlite-vec not available")

    col1.add(ids=["a"], documents=["doc"], embeddings=[_unit_vec(6, 0)])
    assert col1._vec_dim == 6
    col1.close()

    col2 = SqliteVecCollection(db_path, "drawers")
    col2.count()  # trigger _init_schema + _read_vec_dim
    assert col2._vec_dim == 6
    col2.close()


def test_dynamic_dim_different_dims_in_different_collections(tmp_path):
    """Two collections in the same DB can have different embedding dimensions."""
    db_path = str(tmp_path / "test.db")
    col_4 = SqliteVecCollection(db_path, "col4d")
    col_8 = SqliteVecCollection(db_path, "col8d")

    col_4.add(ids=["a"], documents=["doc"], embeddings=[_unit_vec(4, 0)])
    col_8.add(ids=["b"], documents=["doc"], embeddings=[_unit_vec(8, 0)])

    if col_4._has_vec:
        assert col_4._vec_dim == 4
    if col_8._has_vec:
        assert col_8._vec_dim == 8

    col_4.close()
    col_8.close()


def test_ann_overfetch_fallback_to_brute_force_when_filter_removes_all(tmp_path):
    """When every ANN candidate is filtered out by where, brute-force must supply results.

    This simulates the over-fetch scenario: all ANN candidates fail the where filter,
    so the query must fall back to a full scan and still return a correct result.
    """
    col = _make_col(tmp_path)
    # Add two docs: only "doc1" has wing=notes
    col.add(
        ids=["doc0", "doc1"],
        documents=["content zero", "content one"],
        metadatas=[{"wing": "project"}, {"wing": "notes"}],
        embeddings=[_unit_vec(DIM, 0), _unit_vec(DIM, 1)],
    )
    # Query with where=notes — regardless of ANN or brute-force path,
    # only doc1 should be returned.
    result = col.query(
        query_embeddings=[_unit_vec(DIM, 0)],
        n_results=5,
        where={"wing": "notes"},
    )
    assert result.ids[0] == ["doc1"]
    col.close()


def test_ann_overfetch_constant_is_positive():
    """Sanity-check the module-level _ANN_OVERFETCH constant."""
    assert isinstance(_ANN_OVERFETCH, int)
    assert _ANN_OVERFETCH > 1


def test_query_single_brute_force_no_sqlite_vec(tmp_path):
    """_query_single falls back to brute-force when _has_vec is False."""
    col = _make_col(tmp_path)
    # Force brute-force mode regardless of environment
    col._has_vec = False
    col._vec_dim = None

    col.add(
        ids=["a", "b", "c"],
        documents=["d1", "d2", "d3"],
        embeddings=[_unit_vec(DIM, 0), _unit_vec(DIM, 1), _unit_vec(DIM, 2)],
    )
    result = col.query(query_embeddings=[_unit_vec(DIM, 2)], n_results=1)
    assert result.ids[0] == ["c"]
    col.close()


def test_doc_matches_static_method():
    """_doc_matches returns correct bool for None and $contains filters."""
    dm = SqliteVecCollection._doc_matches

    assert dm("hello world", None) is True
    assert dm("hello world", {}) is True
    assert dm("hello world", {"$contains": "hello"}) is True
    assert dm("hello world", {"$contains": "xyz"}) is False
    assert dm("", {"$contains": "x"}) is False
