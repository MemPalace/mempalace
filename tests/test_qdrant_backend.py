import os
import uuid

import numpy as np
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
from mempalace.backends.qdrant import QdrantBackend


def _get_payload_value(payload, key):
    value = payload
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _fake_match_condition(point, condition):
    if "must" in condition or "must_not" in condition or "should" in condition:
        return _fake_match_filter(point, condition)
    if "has_id" in condition:
        return point["id"] in set(condition["has_id"])
    key = condition.get("key")
    actual = _get_payload_value(point.get("payload") or {}, key)
    if "match" in condition:
        match = condition["match"]
        if "value" in match:
            return actual == match["value"]
        if "any" in match:
            return actual in set(match["any"] or [])
        if "text_any" in match:
            haystack = str(actual or "").lower()
            return any(token in haystack for token in str(match["text_any"]).lower().split())
    if "range" in condition:
        range_spec = condition["range"]
        try:
            if "gt" in range_spec and not actual > range_spec["gt"]:
                return False
            if "gte" in range_spec and not actual >= range_spec["gte"]:
                return False
            if "lt" in range_spec and not actual < range_spec["lt"]:
                return False
            if "lte" in range_spec and not actual <= range_spec["lte"]:
                return False
        except TypeError:
            return False
        return True
    return True


def _fake_match_filter(point, qdrant_filter):
    if not qdrant_filter:
        return True
    must = qdrant_filter.get("must") or []
    must_not = qdrant_filter.get("must_not") or []
    should = qdrant_filter.get("should") or []
    if any(not _fake_match_condition(point, condition) for condition in must):
        return False
    if any(_fake_match_condition(point, condition) for condition in must_not):
        return False
    if should and not any(_fake_match_condition(point, condition) for condition in should):
        return False
    return True


class _FakeQdrantClient:
    instances = []

    def __init__(self, _config):
        self.collections = {}
        self.query_calls = []
        self.scroll_calls = []
        self.created_indexes = []
        _FakeQdrantClient.instances.append(self)

    def request(self, *_args, **_kwargs):
        return {"result": {}}

    def collection_exists(self, collection):
        return collection in self.collections

    def get_collection_info(self, collection):
        if collection not in self.collections:
            raise AssertionError("collection missing")
        return {
            "result": {
                "config": {
                    "params": {
                        "vectors": {
                            "size": self.collections[collection]["dimension"],
                            "distance": "Cosine",
                        }
                    }
                }
            }
        }

    def create_collection(self, collection, dimension):
        self.collections.setdefault(collection, {"dimension": dimension, "points": {}})

    def create_payload_index(self, collection, field_name, field_schema):
        self.created_indexes.append((collection, field_name, field_schema))

    def upsert_points(self, collection, points):
        self.collections.setdefault(
            collection,
            {"dimension": len(points[0]["vector"]) if points else 0, "points": {}},
        )
        for point in points:
            self.collections[collection]["points"][point["id"]] = dict(point)

    def query_points(self, collection, *, vector, limit, qdrant_filter, with_vector):
        self.query_calls.append(qdrant_filter)
        points = list(self.collections.get(collection, {"points": {}})["points"].values())
        points = [point for point in points if _fake_match_filter(point, qdrant_filter)]
        q = np.asarray(vector, dtype=np.float32)
        scored = []
        for point in points:
            vec = np.asarray(point["vector"], dtype=np.float32)
            denom = float(np.linalg.norm(q)) * float(np.linalg.norm(vec))
            score = 0.0 if denom <= 0 else float(np.dot(q, vec) / denom)
            out = {"id": point["id"], "payload": point["payload"], "score": score}
            if with_vector:
                out["vector"] = point["vector"]
            scored.append(out)
        scored.sort(key=lambda point: point["score"], reverse=True)
        return scored[:limit]

    def scroll_points(
        self,
        collection,
        *,
        qdrant_filter=None,
        limit=256,
        offset=None,
        with_vector=False,
    ):
        self.scroll_calls.append(qdrant_filter)
        points = list(self.collections.get(collection, {"points": {}})["points"].values())
        points = [point for point in points if _fake_match_filter(point, qdrant_filter)]
        start = int(offset or 0)
        selected = points[start : start + limit]
        next_offset = start + limit if start + limit < len(points) else None
        out = []
        for point in selected:
            item = {"id": point["id"], "payload": point["payload"]}
            if with_vector:
                item["vector"] = point["vector"]
            out.append(item)
        return out, next_offset

    def delete_points(self, collection, *, point_ids=None, qdrant_filter=None):
        points = self.collections.get(collection, {"points": {}})["points"]
        if point_ids is not None:
            for point_id in point_ids:
                points.pop(point_id, None)
            return
        for point_id, point in list(points.items()):
            if _fake_match_filter(point, qdrant_filter):
                points.pop(point_id, None)

    def count_points(self, collection):
        return len(self.collections.get(collection, {"points": {}})["points"])

    def delete_collection(self, collection):
        self.collections.pop(collection, None)


@pytest.fixture
def fake_qdrant(monkeypatch):
    import mempalace.backends.qdrant as qdrant

    _FakeQdrantClient.instances.clear()
    monkeypatch.setattr(qdrant, "_QdrantRESTClient", _FakeQdrantClient)
    monkeypatch.delenv("MEMPALACE_QDRANT_URL", raising=False)
    monkeypatch.delenv("MEMPALACE_QDRANT_API_KEY", raising=False)
    monkeypatch.delenv("MEMPALACE_QDRANT_NAMESPACE", raising=False)
    monkeypatch.delenv("MEMPALACE_QDRANT_TIMEOUT", raising=False)
    return _FakeQdrantClient


def _collection(tmp_path, name="drawers"):
    backend = QdrantBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    return backend, backend.get_collection(palace=palace, collection_name=name, create=True)


def test_registry_exposes_qdrant():
    assert "qdrant" in available_backends()


def test_qdrant_add_query_filters_lexical_and_marker(tmp_path, fake_qdrant):
    backend, col = _collection(tmp_path)
    assert not os.path.isfile(tmp_path / "qdrant_backend.json")

    col.add(
        ids=["a", "b", "c"],
        documents=[
            "alpha backend note",
            "rareterm qdrant backend note",
            "frontend design note",
        ],
        metadatas=[
            {"wing": "project", "room": "backend", "rank": 1},
            {"wing": "project", "room": "backend", "rank": 3},
            {"wing": "project", "room": "frontend", "rank": 2},
        ],
        embeddings=[[1, 0], [0.9, 0.1], [0, 1]],
    )

    assert QdrantBackend.detect(str(tmp_path))
    assert os.path.isfile(tmp_path / "qdrant_backend.json")
    assert col.count() == 3

    result = col.query(
        query_embeddings=[[1, 0]],
        n_results=3,
        where={"rank": {"$gte": 2}},
        include=["documents", "metadatas", "distances", "embeddings"],
    )
    assert result.ids == [["b", "c"]]
    assert result.documents[0][0] == "rareterm qdrant backend note"
    assert result.embeddings[0][0] == pytest.approx([0.9, 0.1])

    hits = col.lexical_search(query="rareterm backend", n_results=2, where={"wing": "project"}).hits
    assert [hit.id for hit in hits] == ["b", "a"]
    assert fake_qdrant.instances[0].created_indexes[0][1:] == ("document", "text")

    backend.close_palace(str(tmp_path))
    with pytest.raises(Exception):
        col.count()


def test_qdrant_marker_not_written_when_first_write_fails(tmp_path, fake_qdrant, monkeypatch):
    _backend, col = _collection(tmp_path)
    fake_client = fake_qdrant.instances[0]

    def fail_upsert(*_args, **_kwargs):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(fake_client, "upsert_points", fail_upsert)

    with pytest.raises(RuntimeError):
        col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])

    assert not os.path.isfile(tmp_path / "qdrant_backend.json")


def test_qdrant_upsert_update_delete_get_order_and_multi_collection(tmp_path, fake_qdrant):
    backend, drawers = _collection(tmp_path, "drawers")
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    closets = backend.get_collection(palace=palace, collection_name="closets", create=True)

    drawers.upsert(
        ids=["one", "two"],
        documents=["first document", "second document"],
        metadatas=[{"wing": "a"}, {"wing": "b"}],
        embeddings=[[1, 0], [0, 1]],
    )
    closets.upsert(
        ids=["one"],
        documents=["closet document"],
        metadatas=[{"wing": "closet"}],
        embeddings=[[0.5, 0.5]],
    )

    got = drawers.get(ids=["two", "one", "two"], include=["documents", "metadatas"])
    assert got.ids == ["two", "one", "two"]
    assert got.documents == ["second document", "first document", "second document"]

    drawers.update(ids=["one"], metadatas=[{"room": "updated"}])
    assert drawers.get(ids=["one"]).metadatas == [{"wing": "a", "room": "updated"}]

    drawers.delete(where={"wing": "b"})
    assert drawers.get().ids == ["one"]
    assert closets.get().ids == ["one"]


def test_qdrant_complex_filters_use_exact_local_fallback(tmp_path, fake_qdrant):
    _backend, col = _collection(tmp_path)
    col.upsert(
        ids=["a", "b", "c"],
        documents=[
            "needle exact substring",
            "needle other wing",
            "boring filler",
        ],
        metadatas=[
            {"wing": "target", "room": "backend", "tag": "alpha-beta"},
            {"wing": "other", "room": "backend", "tag": "beta"},
            {"wing": "target", "room": "front", "tag": "gamma"},
        ],
        embeddings=[[1, 0], [0.8, 0.2], [0, 1]],
    )
    fake_client = fake_qdrant.instances[0]

    result = col.query(
        query_embeddings=[[1, 0]],
        n_results=5,
        where={"$or": [{"wing": "target"}, {"tag": {"$contains": "alpha"}}]},
        where_document={"$contains": "needle"},
    )

    assert result.ids == [["a"]]
    assert fake_client.query_calls == []


def test_qdrant_lexical_empty_text_filter_does_not_full_scan(tmp_path, fake_qdrant):
    _backend, col = _collection(tmp_path)
    col.upsert(
        ids=["a", "b"],
        documents=["alpha backend note", "beta frontend note"],
        metadatas=[{"wing": "project"}, {"wing": "project"}],
        embeddings=[[1, 0], [0, 1]],
    )
    fake_client = fake_qdrant.instances[0]
    fake_client.scroll_calls.clear()

    hits = col.lexical_search(query="missingterm", n_results=2).hits

    assert hits == []
    assert len(fake_client.scroll_calls) == 1
    assert "text_any" in str(fake_client.scroll_calls[0])


def test_qdrant_dimension_mismatch(tmp_path, fake_qdrant):
    _backend, col = _collection(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])

    with pytest.raises(DimensionMismatchError):
        col.upsert(ids=["b"], documents=["two"], metadatas=[{}], embeddings=[[1, 0, 0]])


def test_qdrant_add_rejects_duplicate_ids_in_same_batch(tmp_path, fake_qdrant):
    _backend, col = _collection(tmp_path)

    with pytest.raises(ValueError, match="unique"):
        col.add(
            ids=["dup", "dup"],
            documents=["first", "second"],
            metadatas=[{}, {}],
            embeddings=[[1, 0], [0, 1]],
        )

    assert not os.path.isfile(tmp_path / "qdrant_backend.json")


def test_qdrant_marker_participates_in_backend_mismatch(tmp_path, monkeypatch, fake_qdrant):
    from mempalace.palace import resolve_backend_name

    backend, col = _collection(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    backend.close()
    (tmp_path / "chroma.sqlite3").write_bytes(b"")
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "chroma")

    with pytest.raises(BackendMismatchError):
        resolve_backend_name(str(tmp_path))


def test_qdrant_marker_rejects_remote_target_change(tmp_path, monkeypatch, fake_qdrant):
    backend, col = _collection(tmp_path)
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])

    monkeypatch.setenv("MEMPALACE_QDRANT_URL", "http://other-qdrant.example:6333")

    with pytest.raises(BackendMismatchError, match="remote target"):
        backend.get_collection(palace=palace, collection_name="drawers", create=False)


def test_qdrant_namespace_does_not_mix_palaces(tmp_path, fake_qdrant):
    backend = QdrantBackend()
    palace_a_path = tmp_path / "a"
    palace_b_path = tmp_path / "b"
    palace_a = PalaceRef(id=str(palace_a_path), local_path=str(palace_a_path), namespace="shared")
    palace_b = PalaceRef(id=str(palace_b_path), local_path=str(palace_b_path), namespace="shared")

    col_a = backend.get_collection(palace=palace_a, collection_name="drawers", create=True)
    col_b = backend.get_collection(palace=palace_b, collection_name="drawers", create=True)
    col_a.upsert(ids=["same"], documents=["palace a"], metadatas=[{}], embeddings=[[1, 0]])
    col_b.upsert(ids=["same"], documents=["palace b"], metadatas=[{}], embeddings=[[1, 0]])

    assert col_a.get(ids=["same"]).documents == ["palace a"]
    assert col_b.get(ids=["same"]).documents == ["palace b"]
    assert col_a._remote_collection != col_b._remote_collection


def test_qdrant_missing_remote_after_marker_is_unhealthy(tmp_path, fake_qdrant):
    _backend, col = _collection(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    fake_client = fake_qdrant.instances[0]
    fake_client.delete_collection(col._remote_collection)

    assert col.health().ok is False
    with pytest.raises(CollectionNotInitializedError):
        col.count()


def test_search_reports_backend_error_distinct_from_missing_palace(tmp_path, monkeypatch):
    from mempalace import searcher

    def fail_open(*_args, **_kwargs):
        raise BackendError("qdrant unavailable")

    monkeypatch.setattr(searcher, "get_collection", fail_open)

    result = searcher.search_memories("needle", str(tmp_path))

    assert result["error"] == "Backend error"
    assert "qdrant unavailable" in result["details"]


def test_palace_wrapper_embeds_for_qdrant(tmp_path, monkeypatch, fake_qdrant):
    import mempalace.backends.embedding_wrapper as embedding_wrapper
    from mempalace import palace

    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts: [[1.0, 0.0] for _ in texts]
    )
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "qdrant")
    monkeypatch.setenv("MEMPALACE_BACKEND", "qdrant")

    col = palace.get_collection(str(tmp_path), "mempalace_drawers", create=True)
    col.add(documents=["wrapped qdrant document"], ids=["wrapped"], metadatas=[{"wing": "w"}])
    result = col.query(query_texts=["wrapped"], n_results=1)
    assert result.ids == [["wrapped"]]


def test_qdrant_rejects_pure_remote_palace(tmp_path, fake_qdrant):
    """No local_path means the marker (the only mismatch-protection anchor)
    cannot be written or validated, so the backend must refuse rather than
    silently open an unprotected remote collection (RFC 001 isolation contract, PR #1679)."""
    backend = QdrantBackend()
    palace = PalaceRef(id="tenant-remote", local_path=None, namespace="tenant-remote")
    with pytest.raises(BackendError, match="local palace path"):
        backend.get_collection(palace=palace, collection_name="drawers", create=True)


def test_qdrant_cross_palace_isolation_conformance(tmp_path, fake_qdrant):
    """Shared per-PalaceRef.id isolation conformance (RFC 001 isolation contract)."""
    backend = QdrantBackend()
    cols = []
    for label in ("alpha", "beta"):
        path = tmp_path / label
        ref = PalaceRef(id=str(path), local_path=str(path))
        cols.append(backend.get_collection(palace=ref, collection_name="drawers", create=True))
    assert_partition_isolation(backend, cols[0], cols[1], embedding=[1.0, 0.0])


def test_qdrant_namespace_isolation_conformance(tmp_path, fake_qdrant):
    """Shared per-PalaceRef.namespace isolation conformance — qdrant advertises
    ``supports_namespace_isolation`` so it must satisfy the cross-namespace MUST
    (RFC 001 isolation contract)."""
    assert "supports_namespace_isolation" in QdrantBackend.capabilities
    backend = QdrantBackend()
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
    # Mechanism: the namespace partitions the remote collection name.
    assert col_a._remote_collection != col_b._remote_collection
    # Behaviour: a record under one namespace is invisible under the other.
    assert_partition_isolation(backend, col_a, col_b, embedding=[1.0, 0.0])


def test_qdrant_live_rest_roundtrip_when_enabled(tmp_path):
    live_url = os.environ.get("MEMPALACE_QDRANT_LIVE_URL")
    if not live_url:
        pytest.skip("set MEMPALACE_QDRANT_LIVE_URL to run live Qdrant REST test")

    backend = QdrantBackend()
    namespace = f"live_{uuid.uuid4().hex}"
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path), namespace=namespace)
    col = backend.get_collection(
        palace=palace,
        collection_name="drawers",
        create=True,
        options={
            "url": live_url,
            "api_key": os.environ.get("MEMPALACE_QDRANT_LIVE_API_KEY"),
        },
    )
    try:
        col.upsert(
            ids=["live-a", "live-b"],
            documents=["rareterm live qdrant backend", "other live document"],
            metadatas=[{"wing": "live", "rank": 2}, {"wing": "other", "rank": 1}],
            embeddings=[[1.0, 0.0], [0.0, 1.0]],
        )
        assert QdrantBackend.detect(str(tmp_path))

        result = col.query(
            query_embeddings=[[1.0, 0.0]],
            n_results=2,
            where={"wing": "live"},
        )
        assert result.ids == [["live-a"]]

        hits = col.lexical_search(query="rareterm", n_results=1).hits
        assert hits and hits[0].id == "live-a"

        col.delete(ids=["live-a"])
        assert col.get(ids=["live-a"]).ids == []
    finally:
        try:
            col._client.delete_collection(col._remote_collection)
        except Exception:
            pass
        backend.close()
