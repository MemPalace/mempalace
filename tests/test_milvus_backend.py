import json
import os
import sys
import uuid

import pytest

from _backend_conformance import assert_partition_isolation

from mempalace.backends import (
    BackendError,
    BackendMismatchError,
    DimensionMismatchError,
    PalaceRef,
    UnsupportedFilterError,
    available_backends,
)
from mempalace.backends.milvus import (
    DOCUMENT_MAX_LENGTH,
    MilvusBackend,
    translate_where,
    translate_where_document,
)


def _require_milvus_lite():
    pytest.importorskip("pymilvus")
    if sys.platform == "win32":
        pytest.skip("milvus-lite is not distributed on Windows")
    pytest.importorskip("milvus_lite")


def _new_lite_collection(tmp_path, name="drawers", namespace=None):
    _require_milvus_lite()
    backend = MilvusBackend()
    palace_path = tmp_path / (namespace or "palace")
    palace = PalaceRef(
        id=str(palace_path),
        local_path=str(palace_path),
        namespace=namespace,
    )
    collection = backend.get_collection(palace=palace, collection_name=name, create=True)
    return backend, palace, collection


def test_registry_exposes_milvus():
    assert "milvus" in available_backends()


def test_translate_where_supports_portable_filter_subset():
    assert translate_where(None) == ""
    assert translate_where({"wing": "project"}) == 'wing == "project"'
    assert translate_where({"rank": {"$gte": 2}}) == "rank >= 2"
    assert translate_where({"wing": {"$in": ["a", "b"]}}) == 'wing in ["a", "b"]'
    assert (
        translate_where({"$and": [{"wing": "p"}, {"room": "r"}]}) == '(wing == "p" and room == "r")'
    )
    assert (
        translate_where({"$or": [{"wing": "p"}, {"wing": "q"}]}) == '(wing == "p" or wing == "q")'
    )
    assert translate_where({"wing": "p", "room": "r"}) == 'wing == "p" and room == "r"'
    assert translate_where_document({"$contains": "needle"}) == 'document like "%needle%"'


def test_translate_where_rejects_unsafe_fields_and_operators():
    with pytest.raises(UnsupportedFilterError):
        translate_where({"bad field": "x"})
    with pytest.raises(UnsupportedFilterError):
        translate_where({"rank": {"$regex": "x"}})
    with pytest.raises(UnsupportedFilterError):
        translate_where({"$nor": [{"wing": "x"}]})


def test_milvus_lite_add_query_filter_lexical_and_marker(tmp_path):
    backend, palace, col = _new_lite_collection(tmp_path)
    try:
        col.add(
            ids=["a", "b", "c"],
            documents=[
                "alpha backend note",
                "rareterm milvus backend note",
                "frontend design note",
            ],
            metadatas=[
                {"wing": "project", "room": "backend", "rank": 1},
                {"wing": "project", "room": "backend", "rank": 3},
                {"wing": "project", "room": "frontend", "rank": 2},
            ],
            embeddings=[[1, 0], [0.9, 0.1], [0, 1]],
        )

        assert MilvusBackend.detect(palace.local_path)
        assert os.path.isfile(os.path.join(palace.local_path, "milvus_backend.json"))
        assert os.path.exists(os.path.join(palace.local_path, "milvus.db"))
        assert col._has_native_lexical()
        assert col.count() == 3

        result = col.query(
            query_embeddings=[[1, 0]],
            n_results=3,
            where={"rank": {"$gte": 2}},
            include=["documents", "metadatas", "distances", "embeddings"],
        )
        assert result.ids == [["b", "c"]]
        assert result.documents[0][0] == "rareterm milvus backend note"
        assert result.metadatas[0][0]["rank"] == 3
        assert result.embeddings[0][0] == pytest.approx([0.9, 0.1])
        assert result.distances[0] == sorted(result.distances[0])

        hits = col.lexical_search(query="rareterm backend", n_results=2).hits
        assert [hit.id for hit in hits] == ["b", "a"]
    finally:
        backend.close()


def test_milvus_lite_upsert_update_delete_get_order_and_multi_collection(tmp_path):
    backend, palace, drawers = _new_lite_collection(tmp_path, "drawers")
    closets = backend.get_collection(palace=palace, collection_name="closets", create=True)
    try:
        drawers.upsert(
            ids=["one", "two"],
            documents=["first document", "second document"],
            metadatas=[{"wing": "a"}, {"wing": "b"}],
            embeddings=[[1, 0], [0, 1]],
        )
        closets.add(
            ids=["one"],
            documents=["closet document"],
            metadatas=[{"wing": "closet"}],
            embeddings=[[0.5, 0.5]],
        )

        got = drawers.get(ids=["two", "one", "two"], include=["documents", "metadatas"])
        assert got.ids == ["two", "one", "two"]
        assert got.documents == ["second document", "first document", "second document"]

        drawers.update(ids=["one", "missing"], metadatas=[{"room": "updated"}, {"room": "skip"}])
        assert drawers.get(ids=["one"]).metadatas == [{"wing": "a", "room": "updated"}]
        assert drawers.get(ids=["missing"]).ids == []

        drawers.delete(where={"wing": "b"})
        assert drawers.get().ids == ["one"]
        assert closets.get().ids == ["one"]
    finally:
        backend.close()


def test_milvus_lite_db_name_and_consistency_options(tmp_path):
    _require_milvus_lite()
    backend = MilvusBackend()
    palace = PalaceRef(id=str(tmp_path / "palace"), local_path=str(tmp_path / "palace"))
    col = backend.get_collection(
        palace=palace,
        collection_name="drawers",
        create=True,
        options={"db_name": "mempalace_test", "consistency_level": "eventually"},
    )
    try:
        assert col._config.db_name == "mempalace_test"
        assert col._config.consistency_level == "Eventually"

        col.upsert(ids=["a"], documents=["native bm25 document"], embeddings=[[1, 0]])

        with open(os.path.join(palace.local_path, "milvus_backend.json"), encoding="utf-8") as f:
            marker = json.load(f)
        assert marker["milvus"]["db_name"] == "mempalace_test"
        assert col.lexical_search(query="native", n_results=1).hits[0].id == "a"
    finally:
        backend.close()


def test_milvus_rejects_invalid_consistency_level(tmp_path):
    _require_milvus_lite()
    backend = MilvusBackend()
    palace = PalaceRef(id=str(tmp_path / "palace"), local_path=str(tmp_path / "palace"))
    try:
        with pytest.raises(BackendError, match="consistency_level"):
            backend.get_collection(
                palace=palace,
                collection_name="drawers",
                create=True,
                options={"consistency_level": "linearizable"},
            )
    finally:
        backend.close()


def test_milvus_lite_dimension_and_validation_errors(tmp_path):
    backend, _palace, col = _new_lite_collection(tmp_path)
    try:
        col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])

        with pytest.raises(DimensionMismatchError):
            col.upsert(ids=["b"], documents=["two"], metadatas=[{}], embeddings=[[1, 0, 0]])
        with pytest.raises(ValueError, match="unique"):
            col.add(ids=["dup", "dup"], documents=["a", "b"], embeddings=[[1, 0], [0, 1]])
        with pytest.raises(ValueError, match="reserved"):
            col.add(ids=["reserved"], documents=["x"], metadatas=[{"id": "x"}], embeddings=[[1, 0]])
        with pytest.raises(ValueError, match="delete requires"):
            col.delete()
        with pytest.raises(ValueError, match="document byte length"):
            col.add(
                ids=["long"], documents=["你" * (DOCUMENT_MAX_LENGTH // 3 + 1)], embeddings=[[1, 0]]
            )
    finally:
        backend.close()


def test_milvus_writes_marker_when_upserting_existing_remote_collection(tmp_path):
    _require_milvus_lite()
    shared_uri = str(tmp_path / "shared.db")

    backend_a = MilvusBackend()
    palace_a = PalaceRef(id="palace-a", local_path=str(tmp_path / "palace-a"))
    col_a = backend_a.get_collection(
        palace=palace_a,
        collection_name="drawers",
        create=True,
        options={"uri": shared_uri},
    )
    try:
        col_a.upsert(ids=["a"], documents=["first"], embeddings=[[1, 0]])
    finally:
        backend_a.close()

    backend_b = MilvusBackend()
    palace_b = PalaceRef(id="palace-b", local_path=str(tmp_path / "palace-b"))
    marker_b = os.path.join(palace_b.local_path, "milvus_backend.json")
    col_b = backend_b.get_collection(
        palace=palace_b,
        collection_name="drawers",
        create=True,
        options={"uri": shared_uri},
    )
    try:
        assert not os.path.exists(marker_b)
        col_b.upsert(ids=["b"], documents=["second"], embeddings=[[0, 1]])
        assert os.path.isfile(marker_b)
    finally:
        backend_b.close()


def test_milvus_marker_rejects_remote_target_change(tmp_path, monkeypatch):
    backend, palace, col = _new_lite_collection(tmp_path)
    try:
        col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
        backend.close()

        monkeypatch.setenv("MEMPALACE_MILVUS_URI", str(tmp_path / "other.db"))
        with pytest.raises(BackendMismatchError, match="target"):
            MilvusBackend().get_collection(palace=palace, collection_name="drawers", create=False)
    finally:
        backend.close()


def test_palace_wrapper_embeds_for_milvus(tmp_path, monkeypatch):
    _require_milvus_lite()
    import mempalace.backends.embedding_wrapper as embedding_wrapper
    from mempalace import palace
    from mempalace.backends import reset_backends

    monkeypatch.setattr(
        embedding_wrapper,
        "_embed_texts",
        lambda texts: [[1.0, 0.0] for _ in texts],
    )
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "milvus")
    monkeypatch.setenv("MEMPALACE_BACKEND", "milvus")
    reset_backends()
    try:
        col = palace.get_collection(str(tmp_path / "wrapped"), "drawers", create=True)
        col.add(documents=["wrapped milvus document"], ids=["wrapped"], metadatas=[{"wing": "w"}])
        result = col.query(query_texts=["wrapped"], n_results=1)
        assert result.ids == [["wrapped"]]
    finally:
        reset_backends()


def test_milvus_cross_palace_isolation_conformance(tmp_path):
    _require_milvus_lite()
    backend = MilvusBackend()
    cols = []
    try:
        for label in ("alpha", "beta"):
            path = tmp_path / label
            ref = PalaceRef(id=str(path), local_path=str(path))
            cols.append(backend.get_collection(palace=ref, collection_name="drawers", create=True))
        assert_partition_isolation(backend, cols[0], cols[1], embedding=[1.0, 0.0, 0.0, 0.0])
    finally:
        backend.close()


def test_milvus_namespace_isolation_conformance(tmp_path):
    _require_milvus_lite()
    assert "supports_namespace_isolation" in MilvusBackend.capabilities
    backend = MilvusBackend()
    try:
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
        assert col_a._remote_collection != col_b._remote_collection
        assert_partition_isolation(backend, col_a, col_b, embedding=[1.0, 0.0, 0.0, 0.0])
    finally:
        backend.close()


def test_milvus_zilliz_cloud_roundtrip_when_enabled(tmp_path):
    pytest.importorskip("pymilvus")
    uri = os.environ.get("MEMPALACE_MILVUS_URI")
    token = os.environ.get("MEMPALACE_MILVUS_TOKEN")
    if not uri or not token:
        pytest.skip(
            "set MEMPALACE_MILVUS_URI and MEMPALACE_MILVUS_TOKEN "
            "to run live Milvus/Zilliz Cloud test"
        )

    backend = MilvusBackend()
    namespace = f"live_{uuid.uuid4().hex}"
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path), namespace=namespace)
    col = backend.get_collection(
        palace=palace,
        collection_name="drawers",
        create=True,
        options={"uri": uri, "token": token, "namespace": namespace},
    )
    try:
        col.upsert(
            ids=["cloud-a", "cloud-b"],
            documents=["rareterm live milvus backend", "other live document"],
            metadatas=[{"wing": "live", "rank": 2}, {"wing": "other", "rank": 1}],
            embeddings=[[1.0, 0.0], [0.0, 1.0]],
        )
        assert col.count() == 2

        result = col.query(
            query_embeddings=[[1.0, 0.0]],
            n_results=2,
            where={"wing": "live"},
        )
        assert result.ids == [["cloud-a"]]

        ranked = col.query(
            query_embeddings=[[1.0, 0.0]],
            n_results=2,
            include=["distances"],
        )
        assert ranked.ids == [["cloud-a", "cloud-b"]]
        assert ranked.distances[0] == pytest.approx([0.0, 1.0])

        hits = col.lexical_search(query="rareterm", n_results=1).hits
        assert hits and hits[0].id == "cloud-a"

        col.delete(ids=["cloud-a"])
        assert col.get(ids=["cloud-a"]).ids == []
    finally:
        try:
            if col._client.has_collection(col._remote_collection):
                col._client.drop_collection(col._remote_collection)
        except Exception:
            pass
        backend.close()
