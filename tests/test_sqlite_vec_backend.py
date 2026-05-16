"""Tests for the sqlite-vec backend.

Skipped if the ``sqlite-vec`` optional dependency is not installed
(``pip install mempalace[sqlite-vec]``).
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

sqlite_vec = pytest.importorskip("sqlite_vec")

from mempalace.backends import (
    BackendClosedError,
    DimensionMismatchError,
    GetResult,
    PalaceNotFoundError,
    PalaceRef,
    QueryResult,
    SQLiteVecBackend,
    SQLiteVecCollection,
    UnsupportedFilterError,
    available_backends,
    get_backend,
)
from mempalace.backends.sqlite_vec import (
    DB_FILENAME,
    _sanitize_table_suffix,
    _translate_where,
    _translate_where_document,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def palace_dir():
    d = tempfile.mkdtemp(prefix="mp-svec-test-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def backend():
    b = SQLiteVecBackend()
    yield b
    b.close()


@pytest.fixture
def collection(backend, palace_dir):
    palace = PalaceRef(id="t", local_path=palace_dir)
    return backend.get_collection(
        palace=palace,
        collection_name="drawers",
        create=True,
        options={"dimension": 4},
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_sqlite_vec_registered_as_builtin():
    assert "sqlite-vec" in available_backends()


def test_get_backend_returns_sqlite_vec_instance():
    b = get_backend("sqlite-vec")
    assert isinstance(b, SQLiteVecBackend)


def test_detect_returns_true_when_db_present(palace_dir):
    db_path = os.path.join(palace_dir, DB_FILENAME)
    open(db_path, "wb").close()
    assert SQLiteVecBackend.detect(palace_dir) is True


def test_detect_returns_false_when_db_absent(palace_dir):
    assert SQLiteVecBackend.detect(palace_dir) is False


# ---------------------------------------------------------------------------
# get_collection lifecycle
# ---------------------------------------------------------------------------


def test_get_collection_missing_raises_palace_not_found(backend, palace_dir):
    palace = PalaceRef(id="t", local_path=palace_dir)
    with pytest.raises(PalaceNotFoundError):
        backend.get_collection(palace=palace, collection_name="missing")


def test_create_requires_dimension(backend, palace_dir):
    palace = PalaceRef(id="t", local_path=palace_dir)
    with pytest.raises(ValueError, match="dimension"):
        backend.get_collection(palace=palace, collection_name="x", create=True)


def test_create_then_reopen_preserves_metadata(backend, palace_dir):
    palace = PalaceRef(id="t", local_path=palace_dir)
    c1 = backend.get_collection(
        palace=palace,
        collection_name="cc",
        create=True,
        options={"dimension": 8},
    )
    assert c1._dimension == 8
    backend.close_palace(palace)

    c2 = backend.get_collection(palace=palace, collection_name="cc")
    assert c2._dimension == 8


def test_legacy_positional_get_collection(backend, palace_dir):
    """ChromaBackend supports legacy (palace_path, collection_name) calls;
    SQLiteVecBackend mirrors the same shim for callers not yet migrated."""
    c = backend.get_collection(
        palace_dir, "drawers", create=True, options={"dimension": 4}
    )
    assert isinstance(c, SQLiteVecCollection)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def test_add_requires_embeddings_when_no_embedder():
    # Build a collection without an embedder (skip lazy resolve)
    backend = SQLiteVecBackend(embedder=False)  # falsy disables embedder
    d = tempfile.mkdtemp(prefix="mp-svec-noemb-")
    try:
        palace = PalaceRef(id="t", local_path=d)
        c = backend.get_collection(
            palace=palace, collection_name="x", create=True, options={"dimension": 4}
        )
        # _embedder is False (truthy check fails), so add without embeddings errs
        with pytest.raises(ValueError, match="requires explicit embeddings"):
            c.add(documents=["x"], ids=["x"])
    finally:
        backend.close()
        shutil.rmtree(d, ignore_errors=True)


def test_add_dimension_mismatch_raises(collection):
    with pytest.raises(DimensionMismatchError):
        collection.add(
            documents=["x"],
            ids=["x"],
            embeddings=[[1.0, 2.0]],  # dim 2, expected 4
        )


def test_add_then_count(collection):
    collection.add(
        documents=["a", "b"],
        ids=["a", "b"],
        embeddings=[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
    )
    assert collection.count() == 2


def test_upsert_inserts_and_updates(collection):
    collection.add(
        documents=["a"],
        ids=["a"],
        metadatas=[{"score": 1}],
        embeddings=[[1.0, 0.0, 0.0, 0.0]],
    )
    collection.upsert(
        documents=["a-new", "b"],
        ids=["a", "b"],
        metadatas=[{"score": 99}, {"score": 5}],
        embeddings=[[0.9, 0.1, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
    )
    assert collection.count() == 2
    g = collection.get(ids=["a"])
    assert g.documents[0] == "a-new"
    assert g.metadatas[0]["score"] == 99


def test_update_preserves_embeddings_when_not_supplied(collection):
    collection.add(
        documents=["a"],
        ids=["a"],
        metadatas=[{"k": "v"}],
        embeddings=[[1.0, 0.0, 0.0, 0.0]],
    )
    collection.update(ids=["a"], metadatas=[{"k": "v2"}])
    g = collection.get(ids=["a"], include=["documents", "metadatas", "embeddings"])
    assert g.metadatas[0]["k"] == "v2"
    # embedding round-trips
    assert pytest.approx(g.embeddings[0][0], abs=1e-6) == 1.0


def test_update_atomic_per_field(collection):
    collection.add(
        documents=["a"], ids=["a"], embeddings=[[1.0, 0.0, 0.0, 0.0]]
    )
    collection.update(ids=["a"], documents=["a-renamed"])
    g = collection.get(ids=["a"])
    assert g.documents[0] == "a-renamed"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_query_returns_typed_result(collection):
    collection.add(
        documents=["x", "y", "z"],
        ids=["x", "y", "z"],
        embeddings=[
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
        ],
    )
    r = collection.query(query_embeddings=[[1.0, 0.0, 0.0, 0.0]], n_results=2)
    assert isinstance(r, QueryResult)
    assert r.ids[0][0] == "x"
    assert len(r.ids[0]) == 2


def test_query_with_metadata_filter(collection):
    collection.add(
        documents=["a", "b", "c"],
        ids=["a", "b", "c"],
        metadatas=[{"wing": "x"}, {"wing": "y"}, {"wing": "x"}],
        embeddings=[
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
        ],
    )
    r = collection.query(
        query_embeddings=[[1.0, 0.0, 0.0, 0.0]],
        n_results=5,
        where={"wing": "x"},
    )
    assert set(r.ids[0]) == {"a", "c"}


def test_query_with_in_operator(collection):
    collection.add(
        documents=["a", "b", "c"],
        ids=["a", "b", "c"],
        metadatas=[{"wing": "x"}, {"wing": "y"}, {"wing": "z"}],
        embeddings=[
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
        ],
    )
    r = collection.query(
        query_embeddings=[[1.0, 0.0, 0.0, 0.0]],
        n_results=5,
        where={"wing": {"$in": ["x", "y"]}},
    )
    assert set(r.ids[0]) == {"a", "b"}


def test_query_with_and_logical(collection):
    collection.add(
        documents=["a", "b", "c"],
        ids=["a", "b", "c"],
        metadatas=[
            {"wing": "x", "score": 1},
            {"wing": "x", "score": 5},
            {"wing": "y", "score": 5},
        ],
        embeddings=[
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
        ],
    )
    r = collection.query(
        query_embeddings=[[1.0, 0.0, 0.0, 0.0]],
        n_results=5,
        where={"$and": [{"wing": "x"}, {"score": {"$gte": 5}}]},
    )
    assert set(r.ids[0]) == {"b"}


def test_query_where_document_contains(collection):
    collection.add(
        documents=["hello world", "world series", "goodbye"],
        ids=["a", "b", "c"],
        embeddings=[
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
        ],
    )
    r = collection.query(
        query_embeddings=[[1.0, 0.0, 0.0, 0.0]],
        n_results=5,
        where_document={"$contains": "world"},
    )
    assert set(r.ids[0]) == {"a", "b"}


def test_query_unsupported_operator_raises(collection):
    collection.add(
        documents=["a"], ids=["a"], embeddings=[[1.0, 0.0, 0.0, 0.0]]
    )
    with pytest.raises(UnsupportedFilterError, match="\\$regex"):
        collection.query(
            query_embeddings=[[1.0, 0.0, 0.0, 0.0]],
            where={"k": {"$regex": "x"}},
        )


def test_get_with_ids(collection):
    collection.add(
        documents=["a", "b"],
        ids=["a", "b"],
        embeddings=[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
    )
    g = collection.get(ids=["a"])
    assert isinstance(g, GetResult)
    assert g.ids == ["a"]


def test_get_with_limit_and_offset(collection):
    collection.add(
        documents=["a", "b", "c"],
        ids=["a", "b", "c"],
        embeddings=[
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
        ],
    )
    g = collection.get(limit=2, offset=1)
    assert g.ids == ["b", "c"]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_by_ids(collection):
    collection.add(
        documents=["a", "b"],
        ids=["a", "b"],
        embeddings=[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
    )
    collection.delete(ids=["a"])
    assert collection.count() == 1
    assert collection.get(ids=["a"]).ids == []


def test_delete_by_where(collection):
    collection.add(
        documents=["a", "b", "c"],
        ids=["a", "b", "c"],
        metadatas=[{"wing": "x"}, {"wing": "y"}, {"wing": "x"}],
        embeddings=[
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
        ],
    )
    collection.delete(where={"wing": "x"})
    assert collection.count() == 1


# ---------------------------------------------------------------------------
# Health + close
# ---------------------------------------------------------------------------


def test_health_after_close_marked_unhealthy(backend):
    h = backend.health()
    assert h.ok
    backend.close()
    assert backend.health().ok is False


def test_collection_close_blocks_further_ops(collection):
    collection.close()
    with pytest.raises(BackendClosedError):
        collection.count()


# ---------------------------------------------------------------------------
# Multi-collection isolation
# ---------------------------------------------------------------------------


def test_two_collections_isolated_in_same_palace(backend, palace_dir):
    palace = PalaceRef(id="t", local_path=palace_dir)
    a = backend.get_collection(
        palace=palace, collection_name="a", create=True, options={"dimension": 4}
    )
    b = backend.get_collection(
        palace=palace, collection_name="b", create=True, options={"dimension": 4}
    )
    a.add(documents=["x"], ids=["x"], embeddings=[[1.0, 0.0, 0.0, 0.0]])
    b.add(documents=["y"], ids=["y"], embeddings=[[0.0, 1.0, 0.0, 0.0]])
    assert a.count() == 1
    assert b.count() == 1
    assert a.get(ids=["y"]).ids == []


# ---------------------------------------------------------------------------
# Where translator (unit)
# ---------------------------------------------------------------------------


def test_translate_where_eq():
    sql, params = _translate_where({"wing": "x"})
    # json_extract(metadata_json, '$.wing') is rendered as
    # json_extract(metadata_json, ?), with '$.wing' bound as a parameter.
    assert "json_extract" in sql
    assert "= ?" in sql
    assert params == ["$.wing", "x"]


def test_translate_where_and_or_combination():
    sql, params = _translate_where(
        {"$and": [{"wing": "x"}, {"$or": [{"score": 1}, {"score": 2}]}]}
    )
    assert "AND" in sql
    assert "OR" in sql
    # Each json_extract emission adds one path param plus the comparison value.
    # Three field accesses (wing, score, score) → three path params.
    assert params.count("$.wing") == 1
    assert params.count("$.score") == 2
    assert "x" in params
    assert 1 in params
    assert 2 in params


def test_translate_where_field_name_rejects_injection():
    from mempalace.backends.sqlite_vec import _FIELD_NAME_RE  # type: ignore
    assert _FIELD_NAME_RE.match("wing")
    assert _FIELD_NAME_RE.match("tenant_id")
    assert not _FIELD_NAME_RE.match("wing'; DROP TABLE items--")
    assert not _FIELD_NAME_RE.match("wing.nested")
    assert not _FIELD_NAME_RE.match("$wing")
    with pytest.raises(UnsupportedFilterError, match="metadata field"):
        _translate_where({"wing'; DROP TABLE items--": "x"})


def test_translate_where_in_empty_list_raises():
    with pytest.raises(UnsupportedFilterError, match="non-empty list"):
        _translate_where({"wing": {"$in": []}})


def test_translate_where_unknown_operator_raises():
    with pytest.raises(UnsupportedFilterError, match="\\$mod"):
        _translate_where({"k": {"$mod": 5}})


def test_translate_where_document_only_contains():
    sql, params = _translate_where_document({"$contains": "needle"})
    assert "LIKE ?" in sql
    assert params == ["%needle%"]


def test_translate_where_document_other_op_raises():
    with pytest.raises(UnsupportedFilterError):
        _translate_where_document({"$regex": "x"})


def test_sanitize_table_suffix_handles_special_chars():
    # Short cleanly-sanitised names round-trip unchanged.
    assert _sanitize_table_suffix("hello_world") == "hello_world"
    # Names that required substitution get a hash suffix to keep distinct
    # originals distinguishable post-sanitisation.
    s_dash = _sanitize_table_suffix("hello-world")
    assert s_dash.startswith("hello_world_") and len(s_dash) == len("hello_world_") + 8
    assert _sanitize_table_suffix("123abc").startswith("c_")
    # Empty input still produces a usable identifier (with stable hash suffix).
    empty = _sanitize_table_suffix("")
    assert empty.startswith("_anon_") and len(empty) == len("_anon_") + 8


def test_sanitize_table_suffix_distinguishes_long_prefixes():
    a = _sanitize_table_suffix("a" * 45 + "_one")
    b = _sanitize_table_suffix("a" * 45 + "_two")
    assert a != b, "long collection names with identical 40-char prefixes must differ"


def test_pack_vec_uses_little_endian():
    import struct
    from mempalace.backends.sqlite_vec import _pack_vec, _unpack_vec  # type: ignore
    blob = _pack_vec([1.0, 2.0, 3.0])
    # Little-endian float32 of [1, 2, 3].
    assert blob == struct.pack("<3f", 1.0, 2.0, 3.0)
    assert _unpack_vec(blob) == [1.0, 2.0, 3.0]
