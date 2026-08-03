import pytest

import mempalace.backends.chroma as chroma_module
from mempalace.backends.base import PalaceRef
from mempalace.backends.chroma import ChromaBackend, ChromaCollection


class FakeRawCollection:
    def __init__(self):
        self.name = "vectors"
        self.metadata = {"hnsw:space": "cosine"}
        self.calls = []

    def add(self, **kwargs):
        self.calls.append(("add", kwargs))

    def upsert(self, **kwargs):
        self.calls.append(("upsert", kwargs))

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        return {
            "ids": [["drawer-1"]],
            "documents": [["document"]],
            "metadatas": [[{"wing": "test"}]],
            "distances": [[0.1]],
        }


def caller_vector_collection():
    wrapped = ChromaCollection(FakeRawCollection())
    wrapped._require_embeddings = True
    return wrapped


def test_caller_vector_mode_rejects_add_without_embeddings():
    collection = caller_vector_collection()

    with pytest.raises(
        ValueError,
        match="requires explicit embeddings",
    ):
        collection.add(
            ids=["drawer-1"],
            documents=["document"],
            metadatas=[{"wing": "test"}],
        )


def test_caller_vector_mode_rejects_upsert_without_embeddings():
    collection = caller_vector_collection()

    with pytest.raises(
        ValueError,
        match="requires explicit embeddings",
    ):
        collection.upsert(
            ids=["drawer-1"],
            documents=["document"],
            metadatas=[{"wing": "test"}],
        )


def test_caller_vector_mode_forwards_explicit_upsert_embeddings():
    collection = caller_vector_collection()

    collection.upsert(
        ids=["drawer-1"],
        documents=["document"],
        metadatas=[{"wing": "test"}],
        embeddings=[[0.1, 0.2, 0.3]],
    )

    call_name, kwargs = collection._collection.calls[-1]

    assert call_name == "upsert"
    assert kwargs["embeddings"] == [[0.1, 0.2, 0.3]]


def test_caller_vector_mode_rejects_document_update_without_embeddings():
    collection = caller_vector_collection()

    with pytest.raises(
        ValueError,
        match="requires explicit embeddings",
    ):
        collection.update(
            ids=["drawer-1"],
            documents=["updated document"],
        )


def test_caller_vector_mode_allows_metadata_only_update():
    collection = caller_vector_collection()

    collection.update(
        ids=["drawer-1"],
        metadatas=[{"wing": "updated"}],
    )

    call_name, kwargs = collection._collection.calls[-1]

    assert call_name == "update"
    assert kwargs["ids"] == ["drawer-1"]
    assert kwargs["metadatas"] == [{"wing": "updated"}]
    assert "documents" not in kwargs
    assert "embeddings" not in kwargs


def test_caller_vector_mode_forwards_explicit_update_embeddings():
    collection = caller_vector_collection()

    collection.update(
        ids=["drawer-1"],
        documents=["updated document"],
        embeddings=[[0.3, 0.2, 0.1]],
    )

    call_name, kwargs = collection._collection.calls[-1]

    assert call_name == "update"
    assert kwargs["documents"] == ["updated document"]
    assert kwargs["embeddings"] == [[0.3, 0.2, 0.1]]


def test_caller_vector_mode_rejects_text_query():
    collection = caller_vector_collection()

    with pytest.raises(
        ValueError,
        match="requires query_embeddings",
    ):
        collection.query(
            query_texts=["query"],
            n_results=1,
        )


def test_caller_vector_mode_forwards_vector_query():
    collection = caller_vector_collection()

    result = collection.query(
        query_embeddings=[[0.1, 0.2, 0.3]],
        n_results=1,
    )

    assert result["ids"] == [["drawer-1"]]

    call_name, kwargs = collection._collection.calls[-1]

    assert call_name == "query"
    assert kwargs["query_embeddings"] == [[0.1, 0.2, 0.3]]
    assert "query_texts" not in kwargs


def test_backend_option_skips_local_embedder_and_marks_wrapper(
    tmp_path,
    monkeypatch,
):
    raw = FakeRawCollection()
    captured = {}

    class FakeClient:
        def get_collection(self, collection_name, **kwargs):
            captured["collection_name"] = collection_name
            captured["kwargs"] = dict(kwargs)
            return raw

    backend = ChromaBackend()

    monkeypatch.setattr(
        backend,
        "_client",
        lambda _palace_path: FakeClient(),
    )

    def fail_if_resolved():
        raise AssertionError("caller-vector mode must not resolve a local embedder")

    monkeypatch.setattr(
        backend,
        "_resolve_embedding_function",
        fail_if_resolved,
    )
    monkeypatch.setattr(
        chroma_module,
        "_pin_hnsw_threads",
        lambda _collection: None,
    )

    palace_path = str(tmp_path)

    collection = backend.get_collection(
        palace=PalaceRef(
            id=palace_path,
            local_path=palace_path,
        ),
        collection_name="vectors",
        create=False,
        options={"caller_vectors": True},
    )

    assert collection._require_embeddings is True
    assert captured["collection_name"] == "vectors"
    assert "embedding_function" not in captured["kwargs"]


def test_backend_caller_vector_creation_keeps_hnsw_metadata(
    tmp_path,
    monkeypatch,
):
    raw = FakeRawCollection()
    captured = {}

    class FakeNotFoundError(Exception):
        pass

    class FakeClient:
        def get_collection(self, collection_name, **kwargs):
            del collection_name, kwargs
            raise FakeNotFoundError("missing")

        def create_collection(self, collection_name, **kwargs):
            captured["collection_name"] = collection_name
            captured["kwargs"] = dict(kwargs)
            return raw

    backend = ChromaBackend()

    monkeypatch.setattr(
        chroma_module,
        "_ChromaNotFoundError",
        FakeNotFoundError,
    )
    monkeypatch.setattr(
        backend,
        "_client",
        lambda _palace_path: FakeClient(),
    )

    def fail_if_resolved():
        raise AssertionError("caller-vector mode must not resolve a local embedder")

    monkeypatch.setattr(
        backend,
        "_resolve_embedding_function",
        fail_if_resolved,
    )
    monkeypatch.setattr(
        chroma_module,
        "_pin_hnsw_threads",
        lambda _collection: None,
    )

    palace_path = str(tmp_path / "palace")

    collection = backend.get_collection(
        palace=PalaceRef(
            id=palace_path,
            local_path=palace_path,
        ),
        collection_name="vectors",
        create=True,
        options={"caller_vectors": True},
    )

    kwargs = captured["kwargs"]

    assert collection._require_embeddings is True
    assert captured["collection_name"] == "vectors"
    assert "embedding_function" not in kwargs
    assert kwargs["metadata"]["hnsw:space"] == "cosine"
    assert kwargs["metadata"]["hnsw:batch_size"] == 100
    assert kwargs["metadata"]["hnsw:sync_threshold"] == 1000
