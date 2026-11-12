"""Regression tests for the #2745 review: Qdrant paging cache and two-pass query.

``_CursorClient`` follows real Qdrant scroll semantics: points ordered by
point id, and the cursor is the (inclusive) id of the next unread point.
"""

import copy
import json

import pytest
from test_qdrant_backend import _fake_match_filter, _FakeQdrantClient

import mempalace.backends.qdrant as qdrant
from mempalace.backends.base import BackendClosedError, PalaceRef
from mempalace.backends.qdrant import QdrantBackend


class _CursorClient(_FakeQdrantClient):
    def __init__(self, config):
        super().__init__(config)
        self.after_scroll = None
        self.request_bytes = []

    def scroll_points(
        self, collection, *, qdrant_filter=None, limit=256, offset=None, with_vector=False
    ):
        body = {
            "limit": limit,
            "with_vector": with_vector,
            "offset": offset,
            "filter": qdrant_filter,
        }
        self.request_bytes.append(len(json.dumps(body)))
        points = sorted(
            self.collections.get(collection, {"points": {}})["points"].values(),
            key=lambda p: p["id"],
        )
        points = [p for p in points if _fake_match_filter(p, qdrant_filter)]
        if offset is not None:
            points = [p for p in points if p["id"] >= offset]
        selected = points[:limit]
        next_offset = points[limit]["id"] if len(points) > limit else None
        out = [
            {
                "id": p["id"],
                "payload": copy.deepcopy(p["payload"]),
                **({"vector": copy.deepcopy(p["vector"])} if with_vector else {}),
            }
            for p in selected
        ]
        if self.after_scroll is not None:
            callback, self.after_scroll = self.after_scroll, None
            callback()
        return out, next_offset


@pytest.fixture
def cursor_qdrant(monkeypatch):
    _FakeQdrantClient.instances.clear()
    monkeypatch.setattr(qdrant, "_QdrantRESTClient", _CursorClient)
    for var in (
        "MEMPALACE_QDRANT_URL",
        "MEMPALACE_QDRANT_API_KEY",
        "MEMPALACE_QDRANT_NAMESPACE",
        "MEMPALACE_QDRANT_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)


def _open(tmp_path):
    backend = QdrantBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    col = backend.get_collection(palace=palace, collection_name="drawers", create=True)
    return backend, palace, col


def _put(col, ids, docs=None, metas=None):
    col.upsert(
        ids=ids,
        documents=docs or ["needle"] * len(ids),
        metadatas=metas or [{"wing": "w"}] * len(ids),
        embeddings=[[1.0, 0.0]] * len(ids),
    )


def _ordered(*ids):
    return sorted(ids, key=qdrant._point_id)


def test_paging_sees_write_from_another_handle_after_end(tmp_path, cursor_qdrant):
    backend, palace, a = _open(tmp_path)
    first, second = _ordered("doc-0", "doc-1")
    _put(a, [first])
    assert a.get(limit=1, offset=0).ids == [first]
    b = backend.get_collection(palace=palace, collection_name="drawers", create=False)
    _put(b, [second])
    assert a.get(limit=1, offset=1).ids == [second]


def test_paging_sees_own_write_when_read_lands_mid_write(tmp_path, cursor_qdrant):
    _, _, col = _open(tmp_path)
    first, second = _ordered("doc-0", "doc-1")
    _put(col, [first])
    original = col._client.upsert_points

    def read_then_write(collection, points):
        col.get(limit=1, offset=0)
        return original(collection, points)

    col._client.upsert_points = read_then_write
    _put(col, [second])
    col._client.upsert_points = original
    assert col.get(limit=1, offset=1).ids == [second]


def test_sequential_pages_still_resume_without_rescanning(tmp_path, cursor_qdrant):
    _, _, col = _open(tmp_path)
    ids = _ordered(*[f"d{i}" for i in range(10)])
    _put(col, ids)
    got, calls = [], []
    for off in range(0, 10, 3):
        before = len(col._client.request_bytes)
        got += col.get(limit=3, offset=off).ids
        calls.append(len(col._client.request_bytes) - before)
    assert got == ids
    assert max(calls) <= 2


def test_query_rechecks_document_after_vector_pass(tmp_path, cursor_qdrant):
    _, _, col = _open(tmp_path)
    _put(col, ["m"], ["needle original"])
    store = col._client.collections[col._remote_collection]["points"]

    def change():
        store[qdrant._point_id("m")]["payload"]["document"] = "replacement"

    col._client.after_scroll = change
    res = col.query(
        query_embeddings=[[1, 0]],
        n_results=1,
        where_document={"$contains": "needle"},
        include=["documents"],
    )
    assert res.ids == [[]]


def test_query_rechecks_local_metadata_after_vector_pass(tmp_path, cursor_qdrant):
    _, _, col = _open(tmp_path)
    _put(col, ["m"], metas=[{"wing": "a"}])
    store = col._client.collections[col._remote_collection]["points"]

    def change():
        store[qdrant._point_id("m")]["payload"]["metadata"]["wing"] = "zzz"

    col._client.after_scroll = change
    res = col.query(
        query_embeddings=[[1, 0]], n_results=1, where={"$or": [{"wing": "a"}, {"wing": "b"}]}
    )
    assert res.ids == [[]]


def test_vector_pass_sends_each_matched_id_once(tmp_path, cursor_qdrant):
    _, _, col = _open(tmp_path)

    def vector_bytes(n):
        _put(col, [f"id-{i}" for i in range(n)])
        col._client.request_bytes.clear()
        col.query(query_embeddings=[[1, 0]], n_results=1, where_document={"$contains": "needle"})
        return sum(col._client.request_bytes)

    small = vector_bytes(4096)
    large = vector_bytes(8192)
    assert large < small * 2.5  # linear, not quadratic


def test_get_limit_zero_on_closed_collection_raises(tmp_path, cursor_qdrant):
    _, _, col = _open(tmp_path)
    _put(col, ["x"])
    col.close()
    with pytest.raises(BackendClosedError):
        col.get(limit=0)


def test_get_limit_zero_still_validates_filter(tmp_path, cursor_qdrant):
    _, _, col = _open(tmp_path)
    _put(col, ["x"])
    with pytest.raises(Exception):
        col.get(where={"$bogus": 1}, limit=0)


def test_iter_metadata_keeps_explicit_nulls(tmp_path, cursor_qdrant):
    _, _, col = _open(tmp_path)
    _put(
        col,
        ["a", "b"],
        metas=[
            {"source_file": "/s.md", "normalize_version": None},
            {"source_file": None, "normalize_version": None},
        ],
    )
    rows = list(col.iter_metadata(["source_file", "normalize_version"]))
    assert rows == [{"source_file": "/s.md", "normalize_version": None}]


@pytest.mark.parametrize("value", ["raw\0value", "raw\udcffvalue", "\x01mpesc:literal"])
@pytest.mark.parametrize("route", ["all", "projection", "equality"])
def test_iter_metadata_decodes_values_and_removes_the_storage_flag(
    tmp_path, cursor_qdrant, value, route
):
    backend, _, col = _open(tmp_path)
    try:
        metadata = {"source_file": value, "wing": value}
        _put(col, ["a"], metas=[metadata])
        if route == "all":
            rows = col.iter_metadata()
            expected = metadata
        elif route == "projection":
            rows = col.iter_metadata(("source_file",))
            expected = {"source_file": value}
        else:
            rows = col.iter_metadata(equals={"wing": value})
            expected = metadata
        assert list(rows) == [expected]
    finally:
        backend.close()
