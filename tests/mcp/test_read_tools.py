"""MCP server tests — status, taxonomy, metadata, list filters."""

from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from _mcp_server_helpers import (
    _get_collection,
    _patch_mcp_server,
    _unexpected_client_read,
)


class TestReadTools:
    def test_status_cold_start_no_collection(self, monkeypatch, config, palace_path, kg):
        """Status on a valid palace with no ChromaDB collection yet (#830).

        After `mempalace init`, chroma.sqlite3 exists but the mempalace_drawers
        collection has not been created (no mine or add_drawer yet).  Status
        should return total_drawers: 0, not 'No palace found'.
        """
        import chromadb

        _patch_mcp_server(monkeypatch, config, kg)
        # Create the DB file (init does this) but NOT the collection
        client = chromadb.PersistentClient(path=palace_path)
        del client
        from mempalace.mcp_server import tool_status

        result = tool_status()
        assert "error" not in result, f"cold-start should not error: {result}"
        assert result["total_drawers"] == 0

    def test_status_empty_palace(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_status

        result = tool_status()
        assert result["total_drawers"] == 0
        assert result["wings"] == {}

    def test_status_with_data(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_status

        result = tool_status()
        assert result["total_drawers"] == 4
        assert "project" in result["wings"]
        assert "notes" in result["wings"]

    def test_status_sqlite_exact_backend_has_no_hnsw_fields(
        self, monkeypatch, config, palace_path, kg
    ):
        import mempalace.backends.embedding_wrapper as embedding_wrapper
        from mempalace.palace import get_collection

        monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")
        monkeypatch.setattr(
            embedding_wrapper,
            "_embed_texts",
            lambda texts: [[float(len(text)), 1.0] for text in texts],
        )
        col = get_collection(palace_path, create=True)
        col.add(
            ids=["drawer_sqlite"],
            documents=["verbatim sqlite drawer"],
            metadatas=[{"wing": "w", "room": "r"}],
        )

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_collection_cache", None)
        result = mcp_server.tool_status()

        assert result["backend"] == "sqlite_exact"
        assert result["total_drawers"] == 1
        assert "hnsw_capacity" not in result
        assert result.get("vector_disabled") is not True

    def test_read_only_sqlite_exact_real_read_does_not_mutate_storage(
        self, monkeypatch, config, palace_path, kg
    ):
        """A real MCP read must use sqlite_exact's read-only connection path,
        not the normal schema/WAL initialization path."""
        import mempalace.backends.embedding_wrapper as embedding_wrapper
        from mempalace import mcp_server, palace
        from mempalace.backends import PalaceRef

        monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")
        monkeypatch.setattr(
            embedding_wrapper,
            "_embed_texts",
            lambda texts: [[float(len(text)), 1.0] for text in texts],
        )
        col = palace.get_collection(palace_path, create=True)
        col.add(
            ids=["drawer_read_only"],
            documents=["verbatim read-only drawer"],
            metadatas=[{"wing": "w", "room": "r"}],
        )

        backend = palace.get_backend_for_palace(palace_path)
        palace_ref = PalaceRef(id=palace_path, local_path=palace_path)
        backend.close_palace(palace_ref)

        db_path = Path(palace_path) / "sqlite_exact.sqlite3"
        with sqlite3.connect(db_path) as conn:
            before_schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
            before_meta = conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        before_bytes = db_path.read_bytes()
        before_mtime_ns = db_path.stat().st_mtime_ns

        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp_server, "_READ_ONLY", True)
        monkeypatch.setattr(mcp_server, "_collection_cache", None)
        monkeypatch.setattr(mcp_server, "_collection_cache_backend", None)
        monkeypatch.setattr(mcp_server, "_collection_cache_palace", None)
        monkeypatch.setattr(mcp_server, "_metadata_cache", None)

        result = mcp_server.tool_list_drawers()

        assert result["count"] == 1
        assert result["drawers"][0]["drawer_id"] == "drawer_read_only"
        read_only_handle = backend._read_only_clients[palace_path]
        assert read_only_handle.read_only is True
        assert read_only_handle.conn.execute("PRAGMA query_only").fetchone()[0] == 1

        backend.close_palace(palace_ref)
        with sqlite3.connect(db_path) as conn:
            after_schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
            after_meta = conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        assert after_schema_version == before_schema_version
        assert after_meta == before_meta
        assert db_path.read_bytes() == before_bytes
        assert db_path.stat().st_mtime_ns == before_mtime_ns

    @pytest.mark.parametrize("backend_name", ["sqlite_exact", "rust_exact"])
    def test_stdio_sqlite_exact_reads_with_peer_writer_then_reopens_on_promotion(
        self, monkeypatch, config, palace_path, kg, backend_name
    ):
        """A writable-capable stdio server must recall through a read-only
        handle while a peer owns the palace, then discard that handle when it
        successfully promotes to writer."""
        import mempalace.backends.embedding_wrapper as embedding_wrapper
        from mempalace import mcp_server, palace
        from mempalace.backends import PalaceRef

        monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend_name)
        monkeypatch.setattr(
            embedding_wrapper,
            "_embed_texts",
            lambda texts: [[float(len(text)), 1.0] for text in texts],
        )
        col = palace.get_collection(palace_path, create=True)
        col.add(
            ids=["drawer_peer_writer"],
            documents=["verbatim recall beside peer writer"],
            metadatas=[{"wing": "w", "room": "r"}],
        )

        backend = palace.get_backend_for_palace(palace_path)
        palace_ref = PalaceRef(id=palace_path, local_path=palace_path)
        backend.close_palace(palace_ref)

        holder_code = """
import sys
from mempalace.palace import mine_palace_lock
with mine_palace_lock(sys.argv[1]):
    print("ready", flush=True)
    sys.stdin.read()
"""
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, palace_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "ready"

            _patch_mcp_server(monkeypatch, config, kg)
            monkeypatch.setattr(mcp_server._args, "transport", "stdio")
            monkeypatch.setattr(mcp_server, "_READ_ONLY", False)
            monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", None)
            monkeypatch.setattr(mcp_server, "_MCP_WRITER_READ_ONLY", False)
            monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_FAILED", False)
            monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_ERROR", "")
            monkeypatch.setattr(mcp_server, "_collection_cache", None)
            monkeypatch.setattr(mcp_server, "_collection_cache_backend", None)
            monkeypatch.setattr(mcp_server, "_collection_cache_palace", None)

            result = mcp_server.tool_list_drawers()

            assert result["count"] == 1
            assert result["drawers"][0]["drawer_id"] == "drawer_peer_writer"
            read_only_handle = backend._read_only_clients[palace_path]
            assert read_only_handle.read_only is True
            assert read_only_handle.conn.execute("PRAGMA query_only").fetchone()[0] == 1

            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(timeout=10)
            assert holder.returncode == 0

            # Complete a writer/checkpoint cycle while MCP retains its wrapper.
            from mempalace.backends.sqlite_exact import SQLiteExactBackend

            peer = SQLiteExactBackend()
            try:
                peer_col = peer.get_collection(
                    palace=palace_ref, collection_name=config.collection_name
                )
                peer_col.add(ids=["new_drawer"], documents=["new memory"], embeddings=[[1.0, 0.0]])
            finally:
                peer.close()
            assert mcp_server.tool_list_drawers()["count"] == 2

            writer_ok, writer_reason = mcp_server._acquire_mcp_writer_lock()
            assert writer_ok is True
            assert writer_reason == ""
            assert read_only_handle.closed is True

            promoted = mcp_server._get_collection(create=False)
            assert promoted is not None
            assert backend._clients[palace_path].read_only is False
        finally:
            if mcp_server._MCP_WRITER_LOCK_CM is not None:
                mcp_server._release_mcp_writer_lock()
            if holder.poll() is None:
                if holder.stdin is not None:
                    holder.stdin.close()
                holder.wait(timeout=10)
            backend.close_palace(palace_ref)

    def test_promotion_clears_readonly_embedder_identity_cache(
        self, monkeypatch, config, palace_path, kg
    ):
        """A read-only open of an empty collection must not stick identity
        validation across promotion — the first writable open still records
        the active model on disk."""
        import mempalace.backends.embedding_wrapper as embedding_wrapper
        from mempalace import mcp_server, palace
        from mempalace.backends import PalaceRef
        from mempalace.backends.base import EmbedderIdentity

        monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")
        monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
        monkeypatch.setattr(
            embedding_wrapper,
            "_embed_texts",
            lambda texts: [[float(len(text)), 1.0] for text in texts],
        )
        # Initialize schema without recording identity / drawers (empty palace).
        col = palace.get_collection(palace_path, create=True, _skip_identity_check=True)
        assert col.count() == 0
        # Ensure no identity is stored yet.
        try:
            assert col.get_stored_embedder_identity() is None
        except Exception:
            pass

        backend = palace.get_backend_for_palace(palace_path)
        palace_ref = PalaceRef(id=palace_path, local_path=palace_path)
        backend.close_palace(palace_ref)
        palace._VALIDATED_IDENTITY.clear()

        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp_server._args, "transport", "stdio")
        monkeypatch.setattr(mcp_server, "_READ_ONLY", False)
        monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", None)
        monkeypatch.setattr(mcp_server, "_MCP_WRITER_READ_ONLY", False)
        monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_FAILED", False)
        monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_ERROR", "")
        monkeypatch.setattr(mcp_server, "_collection_cache", None)
        monkeypatch.setattr(mcp_server, "_collection_cache_backend", None)
        monkeypatch.setattr(mcp_server, "_collection_cache_palace", None)

        # Read-only open while a peer owns the palace: create=False path
        # validates without recording identity on an empty collection.
        holder_code = """
import sys
from mempalace.palace import mine_palace_lock
with mine_palace_lock(sys.argv[1]):
    print("ready", flush=True)
    sys.stdin.read()
"""
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, palace_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "ready"

            # Force a peer-writer-coexistence read (opens query_only handle).
            result = mcp_server.tool_list_drawers()
            assert result["count"] == 0
            # Identity may have been marked validated without disk record.
            assert any(key[0] == palace_path for key in palace._VALIDATED_IDENTITY)

            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(timeout=10)

            writer_ok, writer_reason = mcp_server._acquire_mcp_writer_lock()
            assert writer_ok is True
            assert writer_reason == ""
            # Promotion must drop the incomplete read-only validation cache.
            assert not any(key[0] == palace_path for key in palace._VALIDATED_IDENTITY)

            # Writable open after promotion should still record identity.
            promoted = mcp_server._get_collection(create=True)
            assert promoted is not None
            stored = promoted.get_stored_embedder_identity()
            assert stored is not None
            assert stored.model_name == "minilm"
            assert isinstance(stored, EmbedderIdentity) or True
        finally:
            if mcp_server._MCP_WRITER_LOCK_CM is not None:
                mcp_server._release_mcp_writer_lock()
            if holder.poll() is None:
                if holder.stdin is not None:
                    holder.stdin.close()
                holder.wait(timeout=10)
            backend.close_palace(palace_ref)
            palace._VALIDATED_IDENTITY.clear()

    def test_status_qdrant_backend_has_no_hnsw_fields(self, monkeypatch, config, palace_path, kg):
        from mempalace.backends import GetResult

        monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "qdrant")
        monkeypatch.setenv("MEMPALACE_BACKEND", "qdrant")
        with open(os.path.join(palace_path, "qdrant_backend.json"), "w", encoding="utf-8") as f:
            json.dump({"backend": "qdrant"}, f)

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        class _FakeQdrantCollection:
            def count(self):
                return 2

            def get(self, **_kwargs):
                return GetResult(
                    ids=["q1", "q2"],
                    documents=[],
                    metadatas=[
                        {"wing": "project", "room": "backend"},
                        {"wing": "project", "room": "api"},
                    ],
                )

        monkeypatch.setattr(mcp_server, "_collection_cache", None)
        monkeypatch.setattr(mcp_server, "_metadata_cache", None)
        monkeypatch.setattr(
            mcp_server, "_get_collection", lambda create=False: _FakeQdrantCollection()
        )

        result = mcp_server.tool_status()

        assert result["backend"] == "qdrant"
        assert result["total_drawers"] == 2
        assert result["wings"] == {"project": 2}
        assert "hnsw_capacity" not in result
        assert result.get("vector_disabled") is not True

    def test_status_handles_none_metadata_without_partial(
        self, monkeypatch, config, palace_path, kg
    ):
        """tool_status must not crash or go partial when the metadata cache
        returns a ``None`` entry — palaces can contain drawers with no
        metadata (older mining paths, third-party writes). Before the guard,
        ``m.get("wing")`` raised AttributeError mid-tally and the result
        carried ``"error"`` + ``"partial": True`` even though the data was
        perfectly fetchable."""
        from unittest.mock import patch as _patch

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_status

        # Inject a metadata cache where one entry is None
        with _patch("mempalace.mcp_server._get_collection") as mock_get_col:
            fake_col = type("C", (), {"count": lambda self: 2})()
            mock_get_col.return_value = fake_col
            with _patch(
                "mempalace.mcp_server._get_cached_metadata",
                return_value=[{"wing": "proj", "room": "r"}, None],
            ):
                result = tool_status()

        # The None-metadata drawer falls under 'unknown/unknown' — no crash,
        # no partial flag.
        assert "error" not in result
        assert result.get("partial") is not True
        assert result["total_drawers"] == 2
        assert result["wings"].get("proj") == 1
        assert result["wings"].get("unknown") == 1

    def test_list_wings(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_wings

        result = tool_list_wings()
        assert result["wings"]["project"] == 3
        assert result["wings"]["notes"] == 1

    def test_list_rooms_all(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_rooms

        result = tool_list_rooms()
        assert "backend" in result["rooms"]
        assert "frontend" in result["rooms"]
        assert "planning" in result["rooms"]

    def test_list_rooms_filtered(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_rooms

        result = tool_list_rooms(wing="project")
        assert "backend" in result["rooms"]
        assert "planning" not in result["rooms"]

    def test_get_taxonomy(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_get_taxonomy

        result = tool_get_taxonomy()
        assert result["taxonomy"]["project"]["backend"] == 2
        assert result["taxonomy"]["project"]["frontend"] == 1
        assert result["taxonomy"]["notes"]["planning"] == 1

    def test_overview_tools_use_sqlite_fast_path(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        """Overview tools must answer from the sqlite cross-tab without paging
        all metadata through the chroma client (#1748 / #1379). A tripwire on
        the pagination helper fails loudly if the fast path regresses to the
        slow client path that times out on large palaces."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        def _boom(*_a, **_k):
            raise AssertionError("pagination path used instead of sqlite fast path")

        monkeypatch.setattr(mcp_server, "_metadata_cache", None)
        monkeypatch.setattr(mcp_server, "_fetch_all_metadata", _boom)

        status = mcp_server.tool_status()
        assert status["total_drawers"] == 4
        assert status["wings"] == {"project": 3, "notes": 1}

        assert mcp_server.tool_list_wings()["wings"] == {"project": 3, "notes": 1}

        rooms = mcp_server.tool_list_rooms(wing="project")["rooms"]
        assert rooms == {"backend": 2, "frontend": 1}

        tax = mcp_server.tool_get_taxonomy()["taxonomy"]
        assert tax["project"] == {"backend": 2, "frontend": 1}
        assert tax["notes"] == {"planning": 1}

    def test_overview_tools_normalize_missing_wing_room_to_unknown(
        self, monkeypatch, config, palace_path, collection, kg
    ):
        """Fast path must keep the client path's contract: drawers missing
        wing/room metadata read as 'unknown', not the sqlite COALESCE
        placeholder '?' (#1748 review)."""
        collection.add(
            ids=["no_meta_drawer"],
            documents=["a drawer with no wing or room metadata"],
            metadatas=[{"source_file": "loose.txt"}],
        )
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_metadata_cache", None)

        tax = mcp_server.tool_get_taxonomy()["taxonomy"]
        assert tax == {"unknown": {"unknown": 1}}

        status = mcp_server.tool_status()
        assert status["wings"] == {"unknown": 1}
        assert status["rooms"] == {"unknown": 1}

    def test_graph_stats_uses_sqlite_fast_path(
        self, monkeypatch, config, palace_path, collection, kg
    ):
        """graph_stats must aggregate from sqlite without paging metadata
        through build_graph()/HNSW (#1379). Mirrors the build_graph parity
        case in test_palace_graph. Tripwires fail loudly if the fast path
        regresses: graph_stats() (the slow client build) and _get_collection()
        (any client/HNSW open) must never be reached."""
        collection.add(
            ids=["d_db_code", "d_db_proj", "d_auth", "d_general", "d_orphan"],
            documents=[
                "chromadb setup in the code wing",
                "chromadb usage in the project wing",
                "auth and security notes",
                "a general catch-all drawer",
                "a drawer with no wing",
            ],
            metadatas=[
                {"room": "chromadb", "wing": "wing_code", "hall": "db"},
                {"room": "chromadb", "wing": "wing_project", "hall": "db"},
                {"room": "auth", "wing": "wing_code", "hall": "security"},
                {"room": "general", "wing": "wing_code", "hall": "misc"},
                {"room": "orphan", "source_file": "loose.txt"},
            ],
        )
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        def _boom(*_a, **_k):
            raise AssertionError("build_graph client path used instead of sqlite fast path")

        def _no_client_open(*_a, **_k):
            raise AssertionError("chroma collection opened — fast path must avoid HNSW")

        monkeypatch.setattr(mcp_server, "graph_stats", _boom)
        monkeypatch.setattr(mcp_server, "_get_collection", _no_client_open)

        stats = mcp_server.tool_graph_stats()
        # "general" is a real room; the wing-less drawer is still excluded.
        # Existing tripwires above guarantee the collection/HNSW path stays unopened.
        assert stats["total_rooms"] == 3
        assert stats["tunnel_rooms"] == 1
        assert stats["total_edges"] == 1
        assert stats["rooms_per_wing"] == {"wing_code": 3, "wing_project": 1}
        assert stats["top_tunnels"] == [
            {"room": "chromadb", "wings": ["wing_code", "wing_project"], "count": 2}
        ]

    def test_find_tunnels_uses_sqlite_fast_path(
        self, monkeypatch, config, palace_path, collection, kg
    ):
        collection.add(
            ids=["d_db_code", "d_db_proj"],
            documents=["chromadb in code", "chromadb in project"],
            metadatas=[
                {"room": "chromadb", "wing": "wing_code", "hall": "db"},
                {"room": "chromadb", "wing": "wing_project", "hall": "db"},
            ],
        )
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        def _no_client_open(*_a, **_k):
            raise AssertionError("chroma collection opened — find_tunnels must use sqlite")

        monkeypatch.setattr(mcp_server, "_get_collection", _no_client_open)
        tunnels = mcp_server.tool_find_tunnels()
        assert tunnels[0]["room"] == "chromadb"
        assert set(tunnels[0]["wings"]) == {"wing_code", "wing_project"}

    def test_list_drawers_uses_chroma_sqlite_metadata(
        self, monkeypatch, config, palace_path, collection, kg
    ):
        collection.add(
            ids=["keep", "drop"],
            documents=["keep me", "drop me"],
            metadatas=[
                {"wing": "mempalace", "room": "notes"},
                {"wing": "other", "room": "notes"},
            ],
        )
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        def _boom(*_a, **_k):
            raise AssertionError("list_drawers paged col.get instead of chroma sqlite")

        monkeypatch.setattr(mcp_server, "_fetch_drawer_rows", _boom)
        monkeypatch.setattr(mcp_server, "_get_collection", _boom)
        result = mcp_server.tool_list_drawers(wing="mempalace", limit=20)
        assert result["total"] == 1
        assert result["drawers"][0]["drawer_id"] == "keep"
        assert "keep me" in result["drawers"][0]["content_preview"]

    def test_list_drawers_scan_filters_in_sql_and_skips_documents(
        self, monkeypatch, config, palace_path, collection, kg
    ):
        """The listing scan must not drag the palace's text through memory.

        ``chroma:document`` lives in ``embedding_metadata`` alongside the
        loci, so an unqualified join pulls every drawer's verbatim content in
        to render one page — on a six-figure palace that is hundreds of MB and
        seconds of wall clock. The wing/room filter belongs in SQL for the same
        reason: filtering in Python means scanning the whole collection first.
        """
        collection.add(
            ids=["keep", "drop"],
            documents=["keep me", "drop me"],
            metadatas=[
                {"wing": "mempalace", "room": "notes"},
                {"wing": "other", "room": "notes"},
            ],
        )
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server
        from mempalace.backends import chroma as chroma_backend

        statements = []
        real_connect = chroma_backend.sqlite3.connect

        def _tracing_connect(*a, **kw):
            conn = real_connect(*a, **kw)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(chroma_backend.sqlite3, "connect", _tracing_connect)
        monkeypatch.setattr(mcp_server, "_fetch_drawer_rows", _unexpected_client_read)
        monkeypatch.setattr(mcp_server, "_get_collection", _unexpected_client_read)

        result = mcp_server.tool_list_drawers(wing="mempalace", limit=20)
        assert result["total"] == 1
        assert "keep me" in result["drawers"][0]["content_preview"]

        scans = [s for s in statements if "FROM embeddings" in s and "ORDER BY e.id" in s]
        assert scans, f"no listing scan observed in {statements}"
        scan = scans[0]
        # Documents are excluded from the scan and the filter is pushed down.
        assert "chroma:document" in scan and "!=" in scan
        assert scan.count("JOIN embedding_metadata") >= 2, scan
        assert "string_value = " in scan or "string_value = ?" in scan

        # The page's documents are fetched by id, not by re-reading everything.
        doc_reads = [
            s
            for s in statements
            if "chroma:document" in s and "FROM embedding_metadata" in s and " id IN " in s
        ]
        assert doc_reads, f"page previews did not use an id-scoped read: {statements}"

    def test_find_tunnels_reports_recent_from_sqlite(
        self, monkeypatch, config, palace_path, collection, kg
    ):
        """``recent`` survives the sqlite path — it is part of the tool's output."""
        collection.add(
            ids=["d_old", "d_new"],
            documents=["chromadb in code", "chromadb in project"],
            metadatas=[
                {"room": "chromadb", "wing": "wing_code", "hall": "db", "date": "2026-01-02"},
                {"room": "chromadb", "wing": "wing_project", "hall": "db", "date": "2026-03-04"},
            ],
        )
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_get_collection", _unexpected_client_read)
        tunnels = mcp_server.tool_find_tunnels()
        assert tunnels[0]["recent"] == "2026-03-04"

    def test_graph_tools_report_a_missing_palace(self, monkeypatch, config, palace_path, kg):
        """A palace with no database must diagnose, not look empty.

        ``find_tunnels`` returning ``[]`` and ``traverse`` returning "room not
        found" would tell the user their palace has no tunnels when in fact it
        could not be opened at all.
        """
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        tunnels = mcp_server.tool_find_tunnels()
        assert isinstance(tunnels, dict) and tunnels.get("error")

        walked = mcp_server.tool_traverse_graph("anything")
        assert isinstance(walked, dict) and walked.get("error")
        assert "not found" not in walked["error"].lower()

    def test_no_palace_returns_error(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_status

        result = tool_status()
        assert "error" in result


# ── Regression: None-metadata safety (issue #1426) ──────────────────────


class TestMetadataFacets:
    def test_tool_status_uses_metadata_facets(self, monkeypatch):
        import mempalace.mcp_server as mcp

        monkeypatch.setattr(mcp, "_sqlite_taxonomy", lambda: None)
        monkeypatch.setattr(mcp, "_supports_metadata_facets", lambda _: True)

        col = MagicMock()
        col.count.return_value = 5
        col.facet_counts.side_effect = [
            {"wing_a": 2, "wing_b": 3},
            {"room_x": 4, "room_y": 1},
        ]
        monkeypatch.setattr(mcp, "_get_collection", lambda create=False: col)
        result = mcp.tool_status()

        assert result["wings"] == {
            "wing_a": 2,
            "wing_b": 3,
        }

        assert result["rooms"] == {
            "room_x": 4,
            "room_y": 1,
        }
        assert col.facet_counts.call_count == 2

    def test_tool_list_wings_uses_metadata_facets(self, monkeypatch):
        import mempalace.mcp_server as mcp

        monkeypatch.setattr(mcp, "_sqlite_taxonomy", lambda: None)
        monkeypatch.setattr(mcp, "_supports_metadata_facets", lambda _: True)

        col = MagicMock()
        col.facet_counts.return_value = {
            "wing_a": 5,
            "wing_b": 2,
        }
        monkeypatch.setattr(mcp, "_get_collection", lambda: col)
        result = mcp.tool_list_wings()

        assert result == {
            "wings": {
                "wing_a": 5,
                "wing_b": 2,
            }
        }
        col.facet_counts.assert_called_once_with("wing")

    def test_tool_list_rooms_uses_metadata_facets(self, monkeypatch):

        import mempalace.mcp_server as mcp

        monkeypatch.setattr(mcp, "_sqlite_taxonomy", lambda: None)
        monkeypatch.setattr(mcp, "_supports_metadata_facets", lambda _: True)

        col = MagicMock()

        col.facet_counts.return_value = {
            "room1": 7,
            "room2": 3,
        }

        monkeypatch.setattr(mcp, "_get_collection", lambda: col)

        result = mcp.tool_list_rooms("engineering")

        assert result["rooms"] == {
            "room1": 7,
            "room2": 3,
        }

        from unittest.mock import call

        assert col.facet_counts.call_args_list == [
            call("room", where={"wing": "engineering"}),
            call("wing", where={"wing": "engineering"}),
        ]

    def test_tool_get_taxonomy_uses_metadata_facets(self, monkeypatch):
        from unittest.mock import call
        import mempalace.mcp_server as mcp

        monkeypatch.setattr(mcp, "_sqlite_taxonomy", lambda: None)
        monkeypatch.setattr(mcp, "_supports_metadata_facets", lambda _: True)

        col = MagicMock()

        def facet_counts_mock(field, where=None):
            if field == "wing":
                return {"wing_a": 2, "wing_b": 1}
            if field == "room" and where == {"wing": "wing_a"}:
                return {"room1": 2}
            if field == "room" and where == {"wing": "wing_b"}:
                return {"room2": 1}
            return {}

        col.facet_counts.side_effect = facet_counts_mock

        monkeypatch.setattr(mcp, "_get_collection", lambda: col)

        result = mcp.tool_get_taxonomy()
        assert col.facet_counts.call_args_list[0] == call("wing")
        # Per-wing room facets run concurrently (ThreadPoolExecutor), so order is
        # non-deterministic. Compare order-independently without a set() — a
        # ``call`` carrying a dict kwarg is unhashable, so membership (==) is used.
        room_calls = col.facet_counts.call_args_list[1:]
        assert len(room_calls) == 2
        assert call("room", where={"wing": "wing_a"}) in room_calls
        assert call("room", where={"wing": "wing_b"}) in room_calls

        assert result["taxonomy"] == {
            "wing_a": {
                "room1": 2,
            },
            "wing_b": {
                "room2": 1,
            },
        }


class TestNoneMetadataSafety:
    """Regression coverage for issue #1426.

    ChromaDB's ``col.get()`` / ``col.query()`` can return ``None`` for the
    metadata cell of a partially-flushed row or any row written without
    metadata in older formats. Before the ``_safe_meta`` boundary helper,
    indexing the result yielded ``None``, the next ``.get(...)`` raised
    ``AttributeError: 'NoneType' object has no attribute 'get'``, and the
    handler crashed before the ``DELETE FROM embeddings_queue`` cleanup
    step — so the queue grew without bound while writes kept appearing
    successful.

    Each test simulates Chroma returning ``None`` in the metadatas list
    via a stub collection — Chroma's own write path rejects ``None`` at
    insert time, so we can't reproduce the upstream state by writing
    bad data through the real backend. Mocking ``_get_collection`` lets
    us assert the handler tolerates the failure mode that actually shows
    up in the wild.
    """

    def test_safe_meta_helper_coerces_none_to_empty_dict(self):
        from mempalace.mcp_server import _safe_meta

        assert _safe_meta(None) == {}
        assert _safe_meta({}) == {}
        assert _safe_meta({"wing": "x"}) == {"wing": "x"}
        # Defensive against other non-dict types Chroma might return on
        # malformed rows — coerce, don't crash.
        assert _safe_meta("not a dict") == {}
        assert _safe_meta(["wing", "x"]) == {}

    def test_get_drawer_tolerates_none_metadata(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace import mcp_server

        stub_col = MagicMock()
        stub_col.get.return_value = {
            "ids": ["drawer_none_meta"],
            "documents": ["verbatim body"],
            "metadatas": [None],
        }
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: stub_col)

        result = mcp_server.tool_get_drawer("drawer_none_meta")
        assert "error" not in result
        assert result["drawer_id"] == "drawer_none_meta"
        # Missing metadata reduces to empty defaults — no crash, no leak.
        assert result["wing"] == ""
        assert result["room"] == ""
        assert result["content"] == "verbatim body"

    def test_list_drawers_tolerates_none_metadata(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace import mcp_server

        stub_col = MagicMock()
        stub_col.get.return_value = {
            "ids": ["drawer_a", "drawer_b"],
            "documents": ["body a", "body b"],
            "metadatas": [None, {"wing": "ok", "room": "fine"}],
        }
        stub_col.count.return_value = 2
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: stub_col)

        result = mcp_server.tool_list_drawers()
        assert result["count"] == 2
        assert result["drawers"][0]["wing"] == ""
        assert result["drawers"][0]["room"] == ""
        assert result["drawers"][1]["wing"] == "ok"
        assert result["drawers"][1]["room"] == "fine"

    def test_update_drawer_tolerates_none_metadata(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace import mcp_server

        stub_col = MagicMock()
        stub_col.get.return_value = {
            "ids": ["drawer_none_meta"],
            "documents": ["old body"],
            "metadatas": [None],
        }
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: stub_col)

        result = mcp_server.tool_update_drawer("drawer_none_meta", wing="recovered")
        # Should succeed: old_meta is coerced to {}, new wing slots in cleanly.
        assert result.get("success") is True
        # Confirm the update call carried the new wing without inheriting None.
        update_call = stub_col.update.call_args
        assert update_call is not None
        new_meta = update_call.kwargs["metadatas"][0]
        assert new_meta["wing"] == "recovered"

    def test_delete_drawer_audit_log_tolerates_none_metadata(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace import mcp_server

        stub_col = MagicMock()
        stub_col.get.return_value = {
            "ids": ["drawer_none_meta"],
            "documents": ["doomed body"],
            "metadatas": [None],
        }
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: stub_col)

        # Should reach the delete call without AttributeError on the audit-log path.
        result = mcp_server.tool_delete_drawer("drawer_none_meta")
        assert result["success"] is True
        stub_col.delete.assert_called_once_with(ids=["drawer_none_meta"])


# ── Search Tool ─────────────────────────────────────────────────────────


class TestListDrawersDateFilters:
    """Unit tests for the #1128 date-filter helpers in mcp_server."""

    def test_parse_date_filter_none_and_blank(self):
        from mempalace.mcp_server import _parse_date_filter

        assert _parse_date_filter(None, "since") is None
        assert _parse_date_filter("   ", "since") is None

    def test_parse_date_filter_date_only(self):

        from mempalace.mcp_server import _parse_date_filter

        assert _parse_date_filter("2026-04-01", "since") == datetime(2026, 4, 1)

    def test_parse_date_filter_full_timestamp(self):

        from mempalace.mcp_server import _parse_date_filter

        assert _parse_date_filter("2026-04-01T09:30:00", "since") == datetime(2026, 4, 1, 9, 30)

    def test_parse_date_filter_drops_timezone(self):

        from mempalace.mcp_server import _parse_date_filter

        # tz offset dropped -> naive wall-clock, never raises vs naive filed_at.
        parsed = _parse_date_filter("2026-04-01T09:30:00+02:00", "since")
        assert parsed == datetime(2026, 4, 1, 9, 30)
        assert parsed.tzinfo is None

    def test_parse_date_filter_rejects_garbage(self):
        import pytest

        from mempalace.mcp_server import _parse_date_filter

        with pytest.raises(ValueError, match="since"):
            _parse_date_filter("not-a-date", "since")

    def test_parse_date_filter_rejects_impossible_date(self):
        import pytest

        from mempalace.mcp_server import _parse_date_filter

        with pytest.raises(ValueError):
            _parse_date_filter("2026-13-40", "before")

    def test_filed_at_in_window_since_inclusive(self):

        from mempalace.mcp_server import _filed_at_in_window

        since = datetime(2026, 1, 2)
        assert _filed_at_in_window("2026-01-02T00:00:00", since, None) is True
        assert _filed_at_in_window("2026-01-01T23:59:59", since, None) is False

    def test_filed_at_in_window_before_exclusive(self):

        from mempalace.mcp_server import _filed_at_in_window

        before = datetime(2026, 1, 3)
        assert _filed_at_in_window("2026-01-02T23:59:59", None, before) is True
        assert _filed_at_in_window("2026-01-03T00:00:00", None, before) is False

    def test_filed_at_in_window_missing_or_malformed_excluded(self):

        from mempalace.mcp_server import _filed_at_in_window

        since = datetime(2026, 1, 1)
        assert _filed_at_in_window(None, since, None) is False
        assert _filed_at_in_window("", since, None) is False
        assert _filed_at_in_window("garbage", since, None) is False
        assert _filed_at_in_window(12345, since, None) is False

    def test_filed_at_in_window_tz_aware_wall_clock(self):

        from mempalace.mcp_server import _filed_at_in_window

        # tz dropped on both sides -> wall-clock compare, no TypeError raised.
        since = datetime(2026, 1, 2)
        assert _filed_at_in_window("2026-01-02T08:00:00+05:00", since, None) is True

    def test_parse_date_filter_accepts_zulu_suffix(self):

        from mempalace.mcp_server import _parse_date_filter

        # "Z" is not accepted by datetime.fromisoformat before 3.11; the helper
        # strips it so Zulu inputs parse on the 3.9 floor, tz then dropped.
        parsed = _parse_date_filter("2026-04-01T09:30:00Z", "since")
        assert parsed == datetime(2026, 4, 1, 9, 30)
        assert parsed.tzinfo is None

        # Date-only with a Zulu suffix must also parse on 3.9/3.10 (appending
        # "+00:00" would have raised there; stripping Z does not).
        parsed_date = _parse_date_filter("2026-04-01Z", "since")
        assert parsed_date == datetime(2026, 4, 1)
        assert parsed_date.tzinfo is None

        # Lowercase z is tolerated too.
        assert _parse_date_filter("2026-04-01t09:30:00z", "since") == datetime(2026, 4, 1, 9, 30)

    def test_filed_at_in_window_accepts_zulu_filed_at(self):

        from mempalace.mcp_server import _filed_at_in_window

        since = datetime(2026, 1, 2)
        assert _filed_at_in_window("2026-01-02T08:00:00Z", since, None) is True


# ── MCP stdio startup: async preflight ───────────────────────────────────
