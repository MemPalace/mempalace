"""F6: overview caches must invalidate when only the Chroma WAL changes."""

import contextlib
import os
import sqlite3

import chromadb
import pytest

from _mcp_server_helpers import _patch_mcp_server
from mempalace.backends import _inproc_sqlite


@pytest.mark.parametrize("cache", ["taxonomy", "graph"])
def test_overview_observes_wal_commits_with_unchanged_main_file(
    cache, monkeypatch, config, palace_path, kg
):
    """F6: both overview caches must see a commit made outside MCP dispatch."""
    from mempalace import mcp_server
    from mempalace.backends import chroma

    db = os.path.join(palace_path, "chroma.sqlite3")
    with contextlib.closing(sqlite3.connect(db)) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
    client = chromadb.PersistentClient(path=palace_path)
    try:
        collection = client.create_collection(config.collection_name, embedding_function=None)

        def add(index):
            collection.add(
                ids=[str(index)],
                documents=[f"stored drawer {index}"],
                embeddings=[[1.0, 0.0]],
                metadatas=[{"wing": "test", "room": "notes", "hall": "test-hall"}],
            )

        add(1)
        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp_server, "_selected_backend_name", lambda: "chroma")
        query_name = (
            "_sqlite_wing_room_counts" if cache == "taxonomy" else "sqlite_room_wing_hall_counts"
        )
        actual_query = getattr(chroma, query_name)
        calls = []

        def counted_query(*args, **kwargs):
            calls.append(1)
            return actual_query(*args, **kwargs)

        monkeypatch.setattr(chroma, query_name, counted_query)
        read = (
            mcp_server._sqlite_taxonomy
            if cache == "taxonomy"
            else mcp_server._chroma_room_wing_hall_counts
        )

        def count(result):
            return result[0] if cache == "taxonomy" else sum(row[3] for row in result)

        assert count(read()) == 1
        assert count(read()) == 1
        assert len(calls) == 1, "An unwritten WAL palace should still reuse its cache"
        stamp = os.stat(db)
        add(2)
        current = os.stat(db)
        assert (stamp.st_ino, stamp.st_mtime_ns, stamp.st_size) == (
            current.st_ino,
            current.st_mtime_ns,
            current.st_size,
        ), "Fixture must exercise a commit confined to the WAL"
        assert count(read()) == 2, "A new WAL commit must invalidate the overview cache"
        assert len(calls) == 2
        assert count(read()) == 2
        assert len(calls) == 2
    finally:
        client.close()
        _inproc_sqlite.release(db)
