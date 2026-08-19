from __future__ import annotations

import chromadb
import pytest

from mempalace.backends.base import PalaceRef
from mempalace.backends.chroma import (
    ChromaBackend,
    ChromaCollection,
    _caller_vector_schema,
    _collection_has_sync_threshold_metadata,
    _read_sync_threshold,
)


class FakeRawCollection:
    def __init__(self):
        self.name = "vectors"
        self.metadata = {
            "hnsw:space": "cosine",
        }
        self.calls = []

    def add(self, **kwargs):
        self.calls.append(
            (
                "add",
                kwargs,
            )
        )

    def upsert(self, **kwargs):
        self.calls.append(
            (
                "upsert",
                kwargs,
            )
        )

    def update(self, **kwargs):
        self.calls.append(
            (
                "update",
                kwargs,
            )
        )

    def query(self, **kwargs):
        self.calls.append(
            (
                "query",
                kwargs,
            )
        )

        return {
            "ids": [
                [
                    "drawer-1",
                ]
            ],
            "documents": [
                [
                    "document",
                ]
            ],
            "metadatas": [
                [
                    {
                        "wing": "test",
                    }
                ]
            ],
            "distances": [
                [
                    0.1,
                ]
            ],
        }


def caller_vector_collection():
    wrapped = ChromaCollection(FakeRawCollection())
    wrapped._require_embeddings = True
    return wrapped


def vector_config(
    raw_collection,
):
    values = raw_collection.schema.keys.get("#embedding")

    assert values is not None
    assert values.float_list is not None
    assert values.float_list.vector_index is not None

    return values.float_list.vector_index.config


def close_client(
    client,
) -> None:
    close = getattr(
        client,
        "close",
        None,
    )

    if callable(close):
        close()


def assert_raw_document_write_refused(
    raw_collection,
) -> None:
    with pytest.raises(
        Exception,
        match=r"(?i)embedding function",
    ):
        raw_collection.add(
            ids=[
                "raw-document-only",
            ],
            documents=[
                "must not be embedded automatically",
            ],
        )


def test_caller_vector_mode_rejects_add_without_embeddings():
    collection = caller_vector_collection()

    with pytest.raises(
        ValueError,
        match="requires explicit embeddings",
    ):
        collection.add(
            ids=[
                "drawer-1",
            ],
            documents=[
                "document",
            ],
            metadatas=[
                {
                    "wing": "test",
                }
            ],
        )


def test_caller_vector_mode_rejects_upsert_without_embeddings():
    collection = caller_vector_collection()

    with pytest.raises(
        ValueError,
        match="requires explicit embeddings",
    ):
        collection.upsert(
            ids=[
                "drawer-1",
            ],
            documents=[
                "document",
            ],
            metadatas=[
                {
                    "wing": "test",
                }
            ],
        )


def test_caller_vector_mode_forwards_explicit_upsert_embeddings():
    collection = caller_vector_collection()

    collection.upsert(
        ids=[
            "drawer-1",
        ],
        documents=[
            "document",
        ],
        metadatas=[
            {
                "wing": "test",
            }
        ],
        embeddings=[
            [
                0.1,
                0.2,
                0.3,
            ]
        ],
    )

    call_name, kwargs = collection._collection.calls[-1]

    assert call_name == "upsert"
    assert kwargs["embeddings"] == [
        [
            0.1,
            0.2,
            0.3,
        ]
    ]


def test_caller_vector_mode_rejects_document_update_without_embeddings():
    collection = caller_vector_collection()

    with pytest.raises(
        ValueError,
        match="requires explicit embeddings",
    ):
        collection.update(
            ids=[
                "drawer-1",
            ],
            documents=[
                "updated document",
            ],
        )


def test_caller_vector_mode_allows_metadata_only_update():
    collection = caller_vector_collection()

    collection.update(
        ids=[
            "drawer-1",
        ],
        metadatas=[
            {
                "wing": "updated",
            }
        ],
    )

    call_name, kwargs = collection._collection.calls[-1]

    assert call_name == "update"
    assert kwargs["ids"] == [
        "drawer-1",
    ]
    assert kwargs["metadatas"] == [
        {
            "wing": "updated",
        }
    ]
    assert "documents" not in kwargs
    assert "embeddings" not in kwargs


def test_caller_vector_mode_forwards_explicit_update_embeddings():
    collection = caller_vector_collection()

    collection.update(
        ids=[
            "drawer-1",
        ],
        documents=[
            "updated document",
        ],
        embeddings=[
            [
                0.3,
                0.2,
                0.1,
            ]
        ],
    )

    call_name, kwargs = collection._collection.calls[-1]

    assert call_name == "update"
    assert kwargs["documents"] == [
        "updated document",
    ]
    assert kwargs["embeddings"] == [
        [
            0.3,
            0.2,
            0.1,
        ]
    ]


def test_caller_vector_mode_rejects_text_query():
    collection = caller_vector_collection()

    with pytest.raises(
        ValueError,
        match="requires query_embeddings",
    ):
        collection.query(
            query_texts=[
                "query",
            ],
            n_results=1,
        )


def test_caller_vector_mode_forwards_vector_query():
    collection = caller_vector_collection()

    result = collection.query(
        query_embeddings=[
            [
                0.1,
                0.2,
                0.3,
            ]
        ],
        n_results=1,
    )

    assert result["ids"] == [
        [
            "drawer-1",
        ]
    ]

    call_name, kwargs = collection._collection.calls[-1]

    assert call_name == "query"
    assert kwargs["query_embeddings"] == [
        [
            0.1,
            0.2,
            0.3,
        ]
    ]
    assert "query_texts" not in kwargs


def test_caller_vector_schema_carries_hnsw_options_without_embedder():
    schema = _caller_vector_schema(
        {
            "hnsw_space": "cosine",
            "num_threads": 1,
            "batch_size": 64,
            "sync_threshold": 512,
            "ef_construction": 101,
            "max_neighbors": 17,
        }
    )

    values = schema.keys["#embedding"]
    config = values.float_list.vector_index.config

    assert config.embedding_function is None
    assert config.space == "cosine"
    assert config.hnsw is not None
    assert config.hnsw.num_threads == 1
    assert config.hnsw.batch_size == 64
    assert config.hnsw.sync_threshold == 512
    assert config.hnsw.ef_construction == 101
    assert config.hnsw.max_neighbors == 17


def test_live_caller_vector_collection_is_ef_free_and_survives_reopen(
    tmp_path,
):
    palace_path = str(tmp_path / "palace")
    palace = PalaceRef(
        id=palace_path,
        local_path=palace_path,
    )
    options = {
        "caller_vectors": True,
        "hnsw_space": "cosine",
        "num_threads": 1,
        "batch_size": 64,
        "sync_threshold": 512,
        "ef_construction": 101,
        "max_neighbors": 17,
    }

    backend = ChromaBackend()

    try:
        collection = backend.get_collection(
            palace=palace,
            collection_name=("vectors"),
            create=True,
            options=options,
        )
        raw = collection._collection
        config = vector_config(raw)

        assert (
            getattr(
                raw,
                "_embedding_function",
                None,
            )
            is None
        )
        assert raw.configuration.get("embedding_function") is None
        assert config.embedding_function is None
        assert config.space == "cosine"
        assert config.hnsw is not None
        assert config.hnsw.num_threads == 1
        assert config.hnsw.batch_size == 64
        assert config.hnsw.sync_threshold == 512
        assert config.hnsw.ef_construction == 101
        assert config.hnsw.max_neighbors == 17
        assert collection.distance_metric == "cosine"

        collection.upsert(
            ids=[
                "caller-vector-1",
            ],
            documents=[
                "stored with a caller vector",
            ],
            metadatas=[
                {
                    "wing": "test",
                }
            ],
            embeddings=[
                [
                    1.0,
                    0.0,
                    0.0,
                ]
            ],
        )

        assert_raw_document_write_refused(raw)
    finally:
        backend.close()

    assert (
        _read_sync_threshold(
            palace_path,
            "vectors",
        )
        == 512
    )
    assert _collection_has_sync_threshold_metadata(
        palace_path,
        "vectors",
    )

    reopened_backend = ChromaBackend()

    try:
        reopened = reopened_backend.get_collection(
            palace=palace,
            collection_name=("vectors"),
            create=False,
            options={
                "caller_vectors": True,
            },
        )
        reopened_raw = reopened._collection
        reopened_config = vector_config(reopened_raw)

        assert (
            getattr(
                reopened_raw,
                "_embedding_function",
                None,
            )
            is None
        )
        assert reopened_raw.configuration.get("embedding_function") is None
        assert reopened_config.embedding_function is None
        assert reopened_config.space == "cosine"
        assert reopened_config.hnsw is not None
        assert reopened_config.hnsw.num_threads == 1
        assert reopened_config.hnsw.batch_size == 64
        assert reopened_config.hnsw.sync_threshold == 512
        assert reopened.distance_metric == "cosine"

        result = reopened.query(
            query_embeddings=[
                [
                    1.0,
                    0.0,
                    0.0,
                ]
            ],
            n_results=1,
            include=[
                "documents",
            ],
        )

        assert result["ids"] == [
            [
                "caller-vector-1",
            ]
        ]
        assert result["documents"] == [
            [
                "stored with a caller vector",
            ]
        ]

        assert_raw_document_write_refused(reopened_raw)
    finally:
        reopened_backend.close()


def test_caller_vector_mode_rejects_existing_default_embedder_collection(
    tmp_path,
):
    palace_path = str(tmp_path / "palace")

    raw_client = chromadb.PersistentClient(path=palace_path)

    try:
        raw_client.create_collection("legacy-default")
    finally:
        close_client(raw_client)

    backend = ChromaBackend()

    try:
        with pytest.raises(
            ValueError,
            match=("embedding-function-free"),
        ) as exc_info:
            backend.get_collection(
                palace=PalaceRef(
                    id=palace_path,
                    local_path=(palace_path),
                ),
                collection_name=("legacy-default"),
                create=False,
                options={
                    "caller_vectors": True,
                },
            )

        message = str(exc_info.value)

        assert "configuration" in message or "schema" in message or "client" in message
    finally:
        backend.close()
