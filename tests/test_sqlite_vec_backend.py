"""Dedicated regression tests for the sqlite_vec backend.

Every test here maps to a real bug found in review: the upsert-existing-id
crash, filtered queries missing matches beyond a bounded ANN window,
delete_collection lifecycle (never-written / recreate-with-new-dimension /
str-path call shape), magic-header detection, and the rowid->doc_id vec table
migration. Parity with test_sqlite_exact_backend.py.
"""

import sqlite3
import struct

import pytest

from mempalace.backends import (
    DimensionMismatchError,
    PalaceRef,
    available_backends,
)
from mempalace.backends.sqlite_vec import SQLiteVecBackend

pytest.importorskip(
    "sqlite_vec",
    reason="sqlite_vec backend tests require the optional sqlite-vec dependency",
)
pytestmark = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason="interpreter's sqlite3 cannot load extensions (macOS python.org builds)",
)

DIM = 8


def _collection(tmp_path, name="mempalace_drawers", create=True):
    backend = SQLiteVecBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    return backend, backend.get_collection(palace=palace, collection_name=name, create=create)


def _vec(value):
    return [value] * DIM


def test_registry_exposes_sqlite_vec():
    assert "sqlite_vec" in available_backends()


def test_upsert_existing_id_updates_and_stays_queryable(tmp_path):
    """Re-upserting an existing id must not hit a vec0 UNIQUE constraint and
    must update the stored document in place (miner write path)."""
    backend, col = _collection(tmp_path)
    col.add(documents=["prvi"], ids=["d1"], embeddings=[_vec(0.1)])
    col.upsert(documents=["prvi-azuriran"], ids=["d1"], embeddings=[_vec(0.2)])
    col.upsert(documents=["drugi"], ids=["d2"], embeddings=[_vec(0.3)])

    got = col.get(ids=["d1"])
    assert got["documents"] == ["prvi-azuriran"]

    result = col.query(query_embeddings=[_vec(0.2)], n_results=2)
    assert result["ids"][0][0] == "d1"

    # a second upsert cycle over the same id must stay stable
    col.upsert(documents=["prvi-azuriran-2"], ids=["d1"], embeddings=[_vec(0.25)])
    assert col.get(ids=["d1"])["documents"] == ["prvi-azuriran-2"]


def _seed_groups(col):
    ids, docs, metas, embs = [], [], [], []
    for i in range(30):
        ids.append(f"a{i}")
        docs.append(f"grupa a {i}")
        metas.append({"group": "a", "n": i})
        embs.append(_vec(0.01 + i * 0.001))
    for i in range(10):
        ids.append(f"b{i}")
        docs.append(f"grupa b {i}")
        metas.append({"group": "b", "n": 100 + i})
        embs.append(_vec(0.9 + i * 0.001))
    col.add(documents=docs, ids=ids, metadatas=metas, embeddings=embs)
    return _vec(0.95)


def test_filtered_query_finds_matches_beyond_any_window(tmp_path):
    """30 nearer non-matching rows must not hide 10 farther matching rows —
    ranking covers the whole collection, not a bounded ANN window."""
    backend, col = _collection(tmp_path)
    query = _seed_groups(col)

    result = col.query(query_embeddings=[query], n_results=5, where={"group": "b"})
    found = result["ids"][0]
    assert len(found) == 5
    assert all(doc_id.startswith("b") for doc_id in found)

    no_match = col.query(query_embeddings=[query], n_results=3, where={"group": "missing"})
    assert no_match["ids"][0] == []


def test_filtered_query_scalar_operators_pushdown(tmp_path):
    backend, col = _collection(tmp_path)
    query = _seed_groups(col)

    result = col.query(query_embeddings=[query], n_results=3, where={"n": {"$gte": 102}})
    assert {100 + int(doc_id[1:]) for doc_id in result["ids"][0]} >= {102}

    result = col.query(
        query_embeddings=[query],
        n_results=4,
        where={"group": {"$in": ["b"]}, "n": {"$lt": 103}},
    )
    assert set(result["ids"][0]) == {"b0", "b1", "b2"}


def test_filtered_query_or_falls_back_to_python(tmp_path):
    backend, col = _collection(tmp_path)
    query = _seed_groups(col)

    result = col.query(
        query_embeddings=[query], n_results=4, where={"$or": [{"n": 100}, {"n": 108}]}
    )
    assert set(result["ids"][0]) == {"b0", "b8"}


def test_filtered_query_where_document(tmp_path):
    backend, col = _collection(tmp_path)
    query = _seed_groups(col)

    result = col.query(
        query_embeddings=[query],
        n_results=3,
        where={"group": "b"},
        where_document={"$contains": "b 1"},
    )
    assert set(result["ids"][0]) == {"b1"}


def test_unfiltered_query_ranks_by_direction(tmp_path):
    backend, col = _collection(tmp_path)
    e1 = [1.0, 0, 0, 0, 0, 0, 0, 0]
    e2 = [0, 1.0, 0, 0, 0, 0, 0, 0]
    col.add(
        documents=[f"a{i}" for i in range(5)] + [f"b{i}" for i in range(5)],
        ids=[f"a{i}" for i in range(5)] + [f"b{i}" for i in range(5)],
        embeddings=[e1] * 5 + [e2] * 5,
    )
    result = col.query(query_embeddings=[[0.2, 0.98, 0, 0, 0, 0, 0, 0]], n_results=3)
    assert all(doc_id.startswith("b") for doc_id in result["ids"][0])


def test_delete_collection_never_written(tmp_path):
    """Deleting a created-but-never-written collection has no vec0 table yet
    and must not raise 'no such table'."""
    backend, _ = _collection(tmp_path, name="empty_collection")
    backend.delete_collection(str(tmp_path), "empty_collection")
    assert "drawers" not in backend.list_collection_names(str(tmp_path))


def test_delete_collection_allows_recreate_with_new_dimension(tmp_path):
    """Dropping must remove the vec0 table and its meta rows, so recreating
    the same collection with a different dimension works."""
    backend, col = _collection(tmp_path)
    col.add(documents=["x"], ids=["x1"], embeddings=[_vec(0.5)])
    backend.delete_collection(str(tmp_path), "mempalace_drawers")

    _, fresh = _collection(tmp_path)
    fresh.add(documents=["y"], ids=["y1"], embeddings=[[0.5] * 16])
    result = fresh.query(query_embeddings=[[0.5] * 16], n_results=1)
    assert result["ids"][0] == ["y1"]

    with open(tmp_path / "sqlite_vec.sqlite3", "rb") as db:
        header = db.read(16)
    assert header == b"SQLite format 3\x00"


def test_delete_collection_accepts_palace_ref_and_path(tmp_path):
    backend, col = _collection(tmp_path)
    col.add(documents=["x"], ids=["x1"], embeddings=[_vec(0.5)])
    backend.delete_collection(
        PalaceRef(id=str(tmp_path), local_path=str(tmp_path)), "mempalace_drawers"
    )
    _, col2 = _collection(tmp_path)
    col2.add(documents=["x"], ids=["x1"], embeddings=[_vec(0.5)])
    backend.delete_collection(str(tmp_path), "mempalace_drawers")
    assert "mempalace_drawers" not in backend.list_collection_names(str(tmp_path))


def test_delete_on_never_written_collection(tmp_path):
    """delete() on a created-but-never-written collection has no vec0 table
    and must not raise 'no such table' (cross-palace isolation regression)."""
    backend, col = _collection(tmp_path, name="empty_collection")
    col.delete(ids=["ghost"])
    col.delete(where={"wing": "empty"})
    assert col.count() == 0
    assert col.query(query_embeddings=[_vec(0.1)], n_results=3)["ids"][0] == []


def test_delete_keeps_vec_table_consistent(tmp_path):
    backend, col = _collection(tmp_path)
    col.add(documents=["y"], ids=["y1"], embeddings=[[0.9] * 16])
    col.add(documents=["z"], ids=["z1"], embeddings=[[0.1] * 16])
    col.delete(ids=["y1"])
    result = col.query(query_embeddings=[[0.9] * 16], n_results=5)
    assert result["ids"][0] == ["z1"]


def test_legacy_rowid_vec_table_rebuilds_on_write(tmp_path):
    """A pre-merge rowid-keyed vec0 table is rebuilt as doc_id-keyed on the
    next write, preserving already-stored vectors."""
    import sqlite_vec

    backend, col = _collection(tmp_path)
    col.add(documents=["staro"], ids=["l1"], embeddings=[_vec(0.1)])

    old_blob = struct.pack("8f", *(_vec(0.1)))
    conn = sqlite3.connect(tmp_path / "sqlite_vec.sqlite3")
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    conn.execute("DROP TABLE doc_vec_mempalace_drawers")
    conn.execute(
        "CREATE VIRTUAL TABLE doc_vec_mempalace_drawers USING vec0("
        "  embedding float[8] distance_metric=cosine)"
    )
    rowid = conn.execute("SELECT rowid FROM documents WHERE id = 'l1'").fetchone()[0]
    conn.execute(
        "INSERT INTO doc_vec_mempalace_drawers (rowid, embedding) VALUES (?, ?)",
        (rowid, old_blob),
    )
    conn.commit()
    conn.close()

    col.upsert(documents=["novo"], ids=["l2"], embeddings=[_vec(0.8)])
    assert "l1" in col.query(query_embeddings=[_vec(0.1)], n_results=5)["ids"][0]
    assert col.query(query_embeddings=[_vec(0.8)], n_results=1)["ids"][0] == ["l2"]


def test_detect_requires_sqlite_magic_header(tmp_path):
    assert SQLiteVecBackend.detect(str(tmp_path)) is False

    (tmp_path / "sqlite_vec.sqlite3").write_bytes(b"\x00" * 16)
    assert SQLiteVecBackend.detect(str(tmp_path)) is False

    with open(tmp_path / "sqlite_vec.sqlite3", "wb") as db:
        db.write(b"SQLite format 3\x00" + b"\x00" * 16)
    assert SQLiteVecBackend.detect(str(tmp_path)) is True


def test_query_dimension_mismatch_raises(tmp_path):
    backend, col = _collection(tmp_path)
    col.add(documents=["x"], ids=["x1"], embeddings=[_vec(0.5)])
    with pytest.raises(DimensionMismatchError):
        col.query(query_embeddings=[[0.1] * 4], n_results=1)


def test_numeric_filter_coercion_matches_python_fallback(tmp_path):
    """$gt/$lt pushdown must coerce like the Python fallback does: a string
    '95' is numerically below 100, never 'greater than every number' as raw
    SQLite cross-type ordering would have it."""
    backend, col = _collection(tmp_path)
    col.add(
        documents=["s1", "s2"],
        ids=["s1", "s2"],
        metadatas=[{"n": "105"}, {"n": "95"}],
        embeddings=[_vec(0.1), _vec(0.2)],
    )
    result = col.query(query_embeddings=[_vec(0.1)], n_results=5, where={"n": {"$gt": 100}})
    assert set(result["ids"][0]) == {"s1"}

    result = col.query(query_embeddings=[_vec(0.1)], n_results=5, where={"n": {"$lt": 100}})
    assert set(result["ids"][0]) == {"s2"}
