from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from mempalace import mcp_server
from mempalace.backends.base import (
    initialize_last_modified_metadata,
)
from mempalace.backends.chroma import ChromaCollection
from mempalace.backends.embedding_wrapper import (
    EmbeddingCollection,
)


CREATED = "2020-01-01T00:00:00"
MODIFIED = "2020-02-01T00:00:00"


def test_initializer_copies_filed_at_preserves_explicit_and_does_not_mutate():
    original = {
        "wing": "test",
        "filed_at": CREATED,
    }
    explicit = {
        "wing": "test",
        "filed_at": CREATED,
        "last_modified": MODIFIED,
    }
    without_filed_at = {
        "wing": "test",
    }

    result = initialize_last_modified_metadata(
        [
            original,
            explicit,
            without_filed_at,
        ]
    )

    assert result == [
        {
            "wing": "test",
            "filed_at": CREATED,
            "last_modified": CREATED,
        },
        {
            "wing": "test",
            "filed_at": CREATED,
            "last_modified": MODIFIED,
        },
        {
            "wing": "test",
        },
    ]

    assert "last_modified" not in original
    assert explicit["last_modified"] == MODIFIED


def test_initializer_accepts_one_metadata_dict():
    result = initialize_last_modified_metadata(
        {
            "filed_at": CREATED,
        }
    )

    assert result == [
        {
            "filed_at": CREATED,
            "last_modified": CREATED,
        }
    ]


class RawChromaCollection:
    name = "drawers"
    metadata = {
        "hnsw:space": "cosine",
    }

    def __init__(self):
        self.kwargs = None

    def upsert(self, **kwargs):
        self.kwargs = kwargs


def test_chroma_creation_initializes_last_modified():
    raw = RawChromaCollection()
    collection = ChromaCollection(raw)

    collection.upsert(
        ids=["drawer-1"],
        documents=["document"],
        embeddings=[[0.1, 0.2]],
        metadatas=[
            {
                "wing": "test",
                "filed_at": CREATED,
            }
        ],
    )

    assert raw.kwargs is not None
    assert raw.kwargs["metadatas"] == [
        {
            "wing": "test",
            "filed_at": CREATED,
            "last_modified": CREATED,
        }
    ]


class RawExplicitVectorCollection:
    def __init__(self):
        self.kwargs = None

    def upsert(self, **kwargs):
        self.kwargs = kwargs


def test_explicit_vector_creation_initializes_last_modified():
    raw = RawExplicitVectorCollection()
    collection = EmbeddingCollection(raw)

    collection.upsert(
        ids=["drawer-1"],
        documents=["document"],
        embeddings=[[0.1, 0.2]],
        metadatas=[
            {
                "wing": "test",
                "filed_at": CREATED,
            }
        ],
    )

    assert raw.kwargs is not None
    assert raw.kwargs["metadatas"] == [
        {
            "wing": "test",
            "filed_at": CREATED,
            "last_modified": CREATED,
        }
    ]


def test_response_metadata_falls_back_without_mutating_legacy_row():
    stored = {
        "wing": "test",
        "filed_at": CREATED,
        "source_file": "/tmp/example.md",
    }

    result = mcp_server._response_safe_meta(stored)

    assert result["last_modified"] == CREATED
    assert result["source_file"] == "example.md"
    assert "last_modified" not in stored
    assert stored["source_file"] == "/tmp/example.md"


def test_add_drawer_stores_matching_creation_and_modification_time(
    monkeypatch,
):
    collection = MagicMock()

    collection.get.side_effect = [
        {
            "ids": [],
        },
        {
            "ids": ["stored-drawer"],
        },
    ]

    monkeypatch.setattr(
        mcp_server,
        "_get_collection",
        lambda create=False: collection,
    )
    monkeypatch.setattr(
        mcp_server,
        "_wal_log",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        mcp_server,
        "_config",
        SimpleNamespace(
            chunk_size=800,
        ),
    )

    result = mcp_server.tool_add_drawer(
        wing="test",
        room="general",
        content="A short drawer.",
    )

    assert result["success"] is True

    metadata = collection.upsert.call_args.kwargs["metadatas"][0]

    assert metadata["filed_at"]
    assert metadata["last_modified"] == metadata["filed_at"]


def test_update_drawer_stamps_last_modified_and_preserves_filed_at(
    monkeypatch,
):
    collection = MagicMock()

    collection.get.return_value = {
        "ids": ["drawer-1"],
        "documents": ["old content"],
        "metadatas": [
            {
                "wing": "test",
                "room": "general",
                "filed_at": CREATED,
                "last_modified": CREATED,
            }
        ],
    }

    monkeypatch.setattr(
        mcp_server,
        "_get_collection",
        lambda create=False: collection,
    )
    monkeypatch.setattr(
        mcp_server,
        "_wal_log",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        mcp_server,
        "_config",
        SimpleNamespace(
            chunk_size=800,
        ),
    )

    result = mcp_server.tool_update_drawer(
        "drawer-1",
        content="new content",
    )

    assert result["success"] is True
    collection.update.assert_called_once()

    metadata = collection.update.call_args.kwargs["metadatas"][0]

    assert metadata["filed_at"] == CREATED
    assert datetime.fromisoformat(metadata["last_modified"]) > datetime.fromisoformat(CREATED)
