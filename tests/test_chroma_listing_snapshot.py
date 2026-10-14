"""Read recovered listing metadata and page previews from one SQLite snapshot."""

import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import chromadb
import pytest
from chromadb.config import Settings, System
from chromadb.db.impl.sqlite import SqliteDB
from chromadb.segment.impl.metadata.sqlite import SqliteMetadataSegment
from chromadb.types import Operation

from mempalace.backends import chroma as cb
from mempalace.backends._inproc_sqlite import release
from test_chroma_metadata_contracts import synthetic_db
from _mcp_server_helpers import _patch_mcp_server


def test_listing_hydrates_only_selected_logical_page(monkeypatch, config, palace_path, kg):
    from mempalace import mcp_server as mcp

    client = chromadb.PersistentClient(
        path=palace_path, settings=Settings(anonymized_telemetry=False)
    )
    try:
        col = client.create_collection(config.collection_name, embedding_function=None)
        col.add(
            ids=["a_chunk_1", "a_chunk_0", "z"],
            documents=["second", "first ", "not on first page"],
            metadatas=[
                {"wing": "alpha", "parent_drawer_id": "a", "chunk_index": 1},
                {"wing": "alpha", "parent_drawer_id": "a", "chunk_index": 0},
                {"wing": "alpha"},
            ],
            embeddings=[[1.0, 0.0]] * 3,
        )
        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: True)
        monkeypatch.setattr(
            mcp, "_get_collection", lambda *a, **kw: pytest.fail("ready list opened SDK")
        )
        original = cb.sqlite_documents_for_ids
        fetched = []

        def documents(*args, **kwargs):
            fetched.append(list(args[2]))
            assert kwargs["_connection"].in_transaction
            return original(*args, **kwargs)

        monkeypatch.setattr(cb, "sqlite_documents_for_ids", documents)
        page = mcp.tool_list_drawers(wing="alpha", limit=1)
        assert page["total"] == 2
        assert page["drawers"][0]["content_preview"] == "first second"
        assert fetched == [["a_chunk_0", "a_chunk_1"]]
        second = mcp.tool_list_drawers(wing="alpha", limit=1, offset=1)
        assert second["drawers"][0]["content_preview"] == "not on first page"
        assert fetched[-1] == ["z"]
        assert mcp.tool_list_drawers(offset=10)["drawers"] == []
    finally:
        client.close()
        release(str(Path(palace_path) / "chroma.sqlite3"))


def test_preview_failure_retries_listing_through_collection(monkeypatch, config, palace_path, kg):
    from mempalace import mcp_server as mcp

    client = chromadb.PersistentClient(
        path=palace_path, settings=Settings(anonymized_telemetry=False)
    )
    try:
        col = client.create_collection(config.collection_name, embedding_function=None)
        col.add(
            ids=["doc"],
            documents=["complete preview"],
            metadatas=[{"wing": "alpha"}],
            embeddings=[[1.0, 0.0]],
        )
        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: True)
        monkeypatch.setattr(mcp, "_get_collection", lambda *a, **kw: col)
        monkeypatch.setattr(cb, "sqlite_documents_for_ids", lambda *a, **kw: None)
        page = mcp.tool_list_drawers()
        assert page["drawers"][0]["content_preview"] == "complete preview"
    finally:
        client.close()
        release(str(Path(palace_path) / "chroma.sqlite3"))


def test_pending_queue_cannot_hydrate_old_preview(tmp_path):
    palace = str(tmp_path)
    client = chromadb.PersistentClient(path=palace, settings=Settings(anonymized_telemetry=False))
    system = segment = None
    db = tmp_path / "chroma.sqlite3"
    try:
        col = client.create_collection("review_preview", embedding_function=None)
        col.add(
            ids=["doc"],
            documents=["old document"],
            embeddings=[[1.0, 0.0]],
            metadatas=[{"wing": "alpha"}],
        )
        cid = col.id
        client.close()
        with sqlite3.connect(db) as conn:
            sid = conn.execute(
                "SELECT id FROM segments WHERE collection=? AND scope='METADATA'", (str(cid),)
            ).fetchone()[0]
        system = System(
            Settings(is_persistent=True, persist_directory=palace, anonymized_telemetry=False)
        )
        producer = system.instance(SqliteDB)
        system.start()
        segment = SqliteMetadataSegment(system, {"id": UUID(sid), "collection": cid})
        assert cb.sqlite_list_id_metadata(palace, "review_preview") is not None
        producer.submit_embeddings(
            cid,
            [
                {
                    "id": "doc",
                    "embedding": None,
                    "encoding": None,
                    "metadata": {"chroma:document": "queued replacement"},
                    "operation": Operation.UPDATE,
                }
            ],
        )
        assert cb.sqlite_list_id_metadata(palace, "review_preview") is None
        actual = cb.sqlite_documents_for_ids(palace, "review_preview", ["doc"])
        assert actual is None, {
            "metadata_reader": "refused pending state",
            "preview_reader": actual,
        }
    finally:
        if segment is not None:
            segment.stop()
        if system is not None:
            system.stop()
        client.close()
        release(str(db))


def test_public_listing_does_not_mix_old_metadata_and_new_preview(
    monkeypatch, config, palace_path, kg
):
    from mempalace import mcp_server as mcp

    client = chromadb.PersistentClient(
        path=palace_path, settings=Settings(anonymized_telemetry=False)
    )
    try:
        col = client.create_collection(config.collection_name, embedding_function=None)
        col.add(
            ids=["doc"],
            documents=["old document"],
            metadatas=[{"wing": "alpha", "room": "notes"}],
            embeddings=[[1.0, 0.0]],
        )
        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: True)
        original = cb.sqlite_list_id_metadata

        writer_errors = []

        def write_new_version():
            try:
                cb.ChromaCollection(col, palace_path=palace_path).update(
                    ids=["doc"],
                    documents=["new document"],
                    metadatas=[{"wing": "bravo"}],
                    embeddings=[[0.0, 1.0]],
                )
            except Exception as exc:  # surfaced after the listing returns
                writer_errors.append(exc)

        # The write is a concurrent writer, not a call made from inside the
        # reader's open transaction. On Windows, SQLite locks are per handle,
        # so the snapshot's SHARED lock makes a same-thread write wait on
        # itself until it fails with "database is locked". POSIX locks are
        # per process, so there the write can still land mid-snapshot.
        writer = threading.Thread(target=write_new_version)

        def interleaved(*args, **kwargs):
            result = original(*args, **kwargs)
            writer.start()
            writer.join(timeout=0.5)
            return result

        monkeypatch.setattr(cb, "sqlite_list_id_metadata", interleaved)
        page = mcp.tool_list_drawers()
        writer.join(timeout=120)
        assert not writer.is_alive(), "concurrent writer never finished"
        assert not writer_errors, writer_errors
        assert "drawers" in page, page
        drawer = page["drawers"][0]
        actual = (drawer["wing"], drawer["content_preview"])
        assert actual in [("alpha", "old document"), ("bravo", "new document")], page
    finally:
        client.close()
        release(str(Path(palace_path) / "chroma.sqlite3"))


def test_mixed_case_metadata_survives_both_fast_readers(tmp_path):
    db = synthetic_db(tmp_path)
    with sqlite3.connect(db) as writer:
        writer.execute(
            "INSERT INTO embedding_metadata(id,key,string_value) VALUES(1,'Chroma:note','keep')"
        )
    try:
        listed = cb.sqlite_list_id_metadata(str(tmp_path), "independent_repairs")[1]
        streamed = list(
            cb.ChromaCollection(
                SimpleNamespace(name="independent_repairs"), palace_path=str(tmp_path)
            ).iter_metadata()
        )
        assert listed == streamed, {"list": listed, "stream": streamed}
    finally:
        release(str(db))
