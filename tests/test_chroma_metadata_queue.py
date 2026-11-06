"""Reproduce persisted lag with Chroma's real queue producer and consumer."""

import os
import sqlite3
from uuid import UUID

import chromadb
import numpy as np
import pytest
from _mcp_server_helpers import _patch_mcp_server
from chromadb.config import Settings, System
from chromadb.db.impl.sqlite import SqliteDB
from chromadb.segment.impl.metadata.sqlite import SqliteMetadataSegment
from chromadb.types import Operation, ScalarEncoding

from mempalace.backends import _inproc_sqlite


@pytest.mark.parametrize("operation", ["add", "update", "delete"])
def test_list_requires_metadata_queue_catchup(monkeypatch, config, palace_path, kg, operation):
    from mempalace import mcp_server as mcp
    from mempalace.backends.chroma import sqlite_drawer_rows, sqlite_list_id_metadata

    client = chromadb.PersistentClient(
        path=palace_path, settings=Settings(anonymized_telemetry=False)
    )
    db = os.path.join(palace_path, "chroma.sqlite3")
    system = None
    metadata_segment = None
    try:
        col = client.create_collection(config.collection_name, embedding_function=None)
        col.add(
            ids=["original"],
            documents=["old document"],
            embeddings=[[1.0, 0.0]],
            metadatas=[{"wing": "alpha", "room": "notes"}],
        )
        collection_id = col.id
        client.close()
        with sqlite3.connect(db) as raw:
            segment_id = raw.execute(
                "SELECT id FROM segments WHERE collection=? AND scope='METADATA'",
                (str(collection_id),),
            ).fetchone()[0]
        # Load Chroma's real queue producer/metadata consumer without HNSW.
        # Leave the consumer unstarted while the producer commits, reproducing
        # a persisted queue that a later metadata-segment startup must recover.
        system = System(
            Settings(is_persistent=True, persist_directory=palace_path, anonymized_telemetry=False)
        )
        producer = system.instance(SqliteDB)
        system.start()
        metadata_segment = SqliteMetadataSegment(
            system, {"id": UUID(segment_id), "collection": collection_id}
        )
        if operation == "add":
            record = {
                "id": "queued",
                "embedding": np.array([0.0, 1.0], dtype=np.float32),
                "encoding": ScalarEncoding.FLOAT32,
                "metadata": {
                    "wing": "bravo",
                    "room": "notes",
                    "chroma:document": "queued document",
                },
                "operation": Operation.ADD,
            }
        elif operation == "update":
            record = {
                "id": "original",
                "embedding": None,
                "encoding": None,
                "metadata": {"wing": "bravo", "room": "notes"},
                "operation": Operation.UPDATE,
            }
        else:
            record = {
                "id": "original",
                "embedding": None,
                "encoding": None,
                "metadata": None,
                "operation": Operation.DELETE,
            }
        producer.submit_embeddings(collection_id, [record])
        with sqlite3.connect(db) as raw:
            pending = raw.execute(
                "SELECT (SELECT max(seq_id) FROM embeddings_queue), seq_id FROM max_seq_id WHERE segment_id=?",
                (str(metadata_segment._id),),
            ).fetchone()
        consumed = pending[1] if isinstance(pending[1], int) else int.from_bytes(pending[1], "big")
        assert pending[0] > consumed
        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: True)
        monkeypatch.setattr(
            mcp,
            "_get_collection",
            lambda *a, **k: pytest.fail("Fast list unexpectedly requested queue recovery"),
        )
        direct_before = sqlite_list_id_metadata(palace_path, config.collection_name)
        singular_before = sqlite_drawer_rows(
            palace_path, config.collection_name, drawer_id="original"
        )
        assert singular_before is None, "Singular reader should detect same pending queue"
        from mempalace.backends.chroma import ChromaCollection
        from mempalace.hallways import _wing_metadata_from_sqlite

        stream_before = _wing_metadata_from_sqlite(
            ChromaCollection(col, palace_path=palace_path), "alpha"
        )
        assert direct_before is None, "Pending queue was accepted as current list"
        assert stream_before is None, "Pending queue was accepted for Hallway formation"
        metadata_segment.start()
        public_after = mcp.tool_list_drawers()
        assert public_after["total"] == (
            2 if operation == "add" else 0 if operation == "delete" else 1
        )
        if operation == "update":
            assert public_after["drawers"][0]["wing"] == "bravo"
    finally:
        if metadata_segment is not None:
            metadata_segment.stop()
        if system is not None:
            system.stop()
        client.close()
        _inproc_sqlite.release(db)
