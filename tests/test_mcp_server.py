"""
test_mcp_server.py — Tests for the MCP server tool handlers and dispatch.

Tests each tool handler directly (unit-level) and the handle_request
dispatch layer (integration-level). Uses isolated palace + KG fixtures
via monkeypatch to avoid touching real data.
"""

from datetime import datetime
import json
import os
import sys
from unittest.mock import MagicMock

import pytest


def _patch_mcp_server(monkeypatch, config, kg):
    """Patch the mcp_server module globals to use test fixtures."""
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda: kg)


def _get_collection(palace_path, create=False):
    """Helper to get collection from test palace.

    Returns (client, collection) so callers can clean up the client
    when they are done.
    """
    import chromadb

    client = chromadb.PersistentClient(path=palace_path)
    if create:
        return (
            client,
            client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"}),
        )
    return client, client.get_collection("mempalace_drawers")


# ── Protocol Layer ──────────────────────────────────────────────────────


class TestHandleRequest:
    def test_initialize(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "initialize", "id": 1, "params": {}})
        assert resp["result"]["serverInfo"]["name"] == "mempalace"
        assert resp["id"] == 1

    def test_initialize_negotiates_client_version(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request(
            {
                "method": "initialize",
                "id": 1,
                "params": {"protocolVersion": "2025-11-25"},
            }
        )
        assert resp["result"]["protocolVersion"] == "2025-11-25"

    def test_initialize_negotiates_older_supported_version(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request(
            {
                "method": "initialize",
                "id": 1,
                "params": {"protocolVersion": "2025-03-26"},
            }
        )
        assert resp["result"]["protocolVersion"] == "2025-03-26"

    def test_initialize_unknown_version_falls_back_to_latest(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request(
            {
                "method": "initialize",
                "id": 1,
                "params": {"protocolVersion": "9999-12-31"},
            }
        )
        from mempalace.mcp_server import SUPPORTED_PROTOCOL_VERSIONS

        assert resp["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[0]

    def test_initialize_missing_version_uses_oldest(self):
        from mempalace.mcp_server import handle_request, SUPPORTED_PROTOCOL_VERSIONS

        resp = handle_request({"method": "initialize", "id": 1, "params": {}})
        assert resp["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[-1]

    def test_notifications_initialized_returns_none(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "notifications/initialized", "id": None, "params": {}})
        assert resp is None

    def test_ping_returns_empty_result(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "ping", "id": 11, "params": {}})
        assert resp["id"] == 11
        assert resp["result"] == {}

    def test_tools_list(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "tools/list", "id": 2, "params": {}})
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        assert "mempalace_status" in names
        assert "mempalace_search" in names
        assert "mempalace_add_drawer" in names
        assert "mempalace_kg_add" in names

    def test_null_arguments_does_not_hang(self, monkeypatch, config, palace_path, seeded_kg):
        """Sending arguments: null should return a result, not hang (#394)."""
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import handle_request

        _client, _col = _get_collection(palace_path, create=True)
        del _client
        resp = handle_request(
            {
                "method": "tools/call",
                "id": 10,
                "params": {"name": "mempalace_status", "arguments": None},
            }
        )
        assert "error" not in resp
        assert resp["result"] is not None

    def test_unknown_tool(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request(
            {
                "method": "tools/call",
                "id": 3,
                "params": {"name": "nonexistent_tool", "arguments": {}},
            }
        )
        assert resp["error"]["code"] == -32601

    def test_tools_call_missing_params(self):
        from mempalace.mcp_server import handle_request

        for bad_params in [None, {}, {"arguments": {}}]:
            resp = handle_request(
                {
                    "method": "tools/call",
                    "id": 15,
                    "params": bad_params,
                }
            )
            assert resp["error"]["code"] == -32602
            assert "Invalid params" in resp["error"]["message"]

    def test_unknown_method(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "unknown/method", "id": 4, "params": {}})
        assert resp["error"]["code"] == -32601

    def test_any_notification_returns_none(self):
        """All notifications/* methods should return None (no response)."""
        from mempalace.mcp_server import handle_request

        for method in [
            "notifications/initialized",
            "notifications/cancelled",
            "notifications/progress",
            "notifications/roots/list_changed",
        ]:
            resp = handle_request({"method": method, "params": {}})
            assert resp is None, f"{method} should return None"

    def test_unknown_method_no_id_returns_none(self):
        """Messages without id (notifications) must never get a response."""
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "unknown/thing", "params": {}})
        assert resp is None

    def test_malformed_method_none(self):
        """method=None or missing should not crash."""
        from mempalace.mcp_server import handle_request

        # Explicit None
        resp = handle_request({"method": None, "params": {}})
        assert resp is None  # no id → no response

        # Missing method entirely
        resp = handle_request({"params": {}})
        assert resp is None

        # method=None with id → should return error, not crash
        resp = handle_request({"method": None, "id": 99, "params": {}})
        assert resp["error"]["code"] == -32601

    @pytest.mark.parametrize("payload", [None, [], "plain", 42, True])
    def test_handle_request_invalid_payload_returns_jsonrpc_error(self, payload):
        from mempalace.mcp_server import handle_request

        resp = handle_request(payload)
        assert resp == {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }

    def test_tools_call_dispatches(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import handle_request

        # Create a collection so status works
        _client, _col = _get_collection(palace_path, create=True)
        del _client

        resp = handle_request(
            {
                "method": "tools/call",
                "id": 5,
                "params": {"name": "mempalace_status", "arguments": {}},
            }
        )
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert "total_drawers" in content


# ── Read Tools ──────────────────────────────────────────────────────────


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

    def test_no_palace_returns_error(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_status

        result = tool_status()
        assert "error" in result


# ── Search Tool ─────────────────────────────────────────────────────────


class TestSearchTool:
    def test_search_basic(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="JWT authentication tokens")
        assert "results" in result
        assert len(result["results"]) > 0
        # Top result should be the auth drawer
        top = result["results"][0]
        assert "JWT" in top["text"] or "authentication" in top["text"].lower()

    def test_search_with_wing_filter(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="planning", wing="notes")
        assert all(r["wing"] == "notes" for r in result["results"])

    def test_search_with_room_filter(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="database", room="backend")
        assert all(r["room"] == "backend" for r in result["results"])

    def test_search_min_similarity_backwards_compat(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        """Old min_similarity param still works via backwards-compat shim."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        # Old name should work
        result = tool_search(query="JWT", min_similarity=1.5)
        assert "results" in result

        # Old name takes precedence when both provided
        result_strict = tool_search(query="JWT", max_distance=999.0, min_similarity=0.01)
        result_loose = tool_search(query="JWT", max_distance=0.01, min_similarity=999.0)
        assert len(result_strict["results"]) <= len(result_loose["results"])

    def test_list_rooms_rejects_invalid_wing(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_get_collection", lambda: pytest.fail())

        result = mcp_server.tool_list_rooms(wing="../etc/passwd")
        assert "error" in result

    def test_search_rejects_invalid_room(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "search_memories", lambda: pytest.fail())

        result = mcp_server.tool_search(query="JWT", room="../backend")
        assert "error" in result

    def test_list_drawers_rejects_invalid_wing(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_get_collection", lambda: pytest.fail())

        result = mcp_server.tool_list_drawers(wing="../notes")
        assert "error" in result

    def test_find_tunnels_rejects_invalid_wing(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_get_collection", lambda: pytest.fail())

        result = mcp_server.tool_find_tunnels(wing_a="../project")
        assert "error" in result

    def test_wal_redacts_sensitive_fields(self, monkeypatch, config, kg, tmp_path):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        wal_file = tmp_path / "write_log.jsonl"
        monkeypatch.setattr(mcp_server, "_WAL_FILE", wal_file)

        mcp_server._wal_log(
            "test",
            {"content": "secret note", "query": "private search", "safe": "ok"},
        )

        entry = json.loads(wal_file.read_text().strip())
        assert entry["params"]["content"].startswith("[REDACTED")
        assert entry["params"]["query"].startswith("[REDACTED")
        assert entry["params"]["safe"] == "ok"


# ── Write Tools ─────────────────────────────────────────────────────────


class TestWriteTools:
    def test_add_drawer(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        result = tool_add_drawer(
            wing="test_wing",
            room="test_room",
            content="This is a test memory about Python decorators and metaclasses.",
        )
        assert result["success"] is True
        assert result["wing"] == "test_wing"
        assert result["room"] == "test_room"
        assert result["drawer_id"].startswith("drawer_test_wing_test_room_")

    def test_add_drawer_duplicate_detection(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        content = "This is a unique test memory about Rust ownership and borrowing."
        result1 = tool_add_drawer(wing="w", room="r", content=content)
        assert result1["success"] is True

        result2 = tool_add_drawer(wing="w", room="r", content=content)
        assert result2["success"] is True
        assert result2["reason"] == "already_exists"

    def test_add_drawer_fails_when_readback_misses(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        class _FakeGetResult:
            ids = []

        class _FakeCol:
            def get(self, **kwargs):
                return _FakeGetResult()

            def upsert(self, **kwargs):
                return None

        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: _FakeCol())

        result = mcp_server.tool_add_drawer("w", "r", "content")
        assert result["success"] is False
        assert "not readable" in result["error"]

    def test_add_drawer_shared_header_no_collision(self, monkeypatch, config, palace_path, kg):
        """Documents sharing a >100-char header must get distinct IDs (full-content hash)."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        header = "# ACME Corp Knowledge Base\n**Project:** Alpha | **Team:** Backend | **Status:** Active\n\n"
        doc1 = (
            header
            + "Decision: Use PostgreSQL for primary storage. Rationale: ACID compliance required."
        )
        doc2 = header + "Decision: Use Redis for session caching. Rationale: sub-ms latency needed."

        result1 = tool_add_drawer(wing="work", room="decisions", content=doc1)
        result2 = tool_add_drawer(wing="work", room="decisions", content=doc2)

        assert result1["success"] is True
        assert result2["success"] is True
        assert (
            result1["drawer_id"] != result2["drawer_id"]
        ), "Documents with shared header but different content must have distinct drawer IDs"

    def test_delete_drawer(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_delete_drawer

        result = tool_delete_drawer("drawer_proj_backend_aaa")
        assert result["success"] is True
        assert seeded_collection.count() == 3

    def test_delete_drawer_not_found(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_delete_drawer

        result = tool_delete_drawer("nonexistent_drawer")
        assert result["success"] is False

    def test_check_duplicate_handles_none_metadata(self, monkeypatch, config, kg):
        """tool_check_duplicate must tolerate None entries in the result lists
        that ChromaDB 1.5.x returns for partially-flushed rows.

        Previously ``meta = results["metadatas"][0][i]`` was unguarded and
        raised ``AttributeError: 'NoneType' object has no attribute 'get'``
        the moment the first matching drawer came back with None metadata —
        surfacing to the MCP client as the uninformative
        ``"Duplicate check failed"`` because the broad ``except Exception``
        wrapper swallows the real cause.
        """
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        mock_col = MagicMock()
        mock_col.query.return_value = {
            "ids": [["d1", "d2"]],
            "distances": [[0.05, 0.05]],
            "metadatas": [[{"wing": "w", "room": "r"}, None]],
            "documents": [["first doc", None]],
        }
        monkeypatch.setattr(mcp_server, "_get_collection", lambda: mock_col)

        result = mcp_server.tool_check_duplicate("any content", threshold=0.5)

        # Both entries land in matches (above threshold), None ones rendered
        # with sentinel values rather than crashing the whole response.
        assert result.get("is_duplicate") is True
        assert len(result["matches"]) == 2
        # The None-metadata entry falls back to sentinels.
        none_entry = result["matches"][1]
        assert none_entry["wing"] == "?"
        assert none_entry["room"] == "?"
        assert none_entry["content"] == ""

    def test_check_duplicate(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_check_duplicate

        # Exact match text from seeded_collection should be flagged
        result = tool_check_duplicate(
            "The authentication module uses JWT tokens for session management. "
            "Tokens expire after 24 hours. Refresh tokens are stored in HttpOnly cookies.",
            threshold=0.5,
        )
        assert result["is_duplicate"] is True

        # Unrelated content should not be flagged
        result = tool_check_duplicate(
            "Black holes emit Hawking radiation at the event horizon.",
            threshold=0.99,
        )
        assert result["is_duplicate"] is False

    def test_get_drawer(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_get_drawer

        result = tool_get_drawer("drawer_proj_backend_aaa")
        assert result["drawer_id"] == "drawer_proj_backend_aaa"
        assert result["wing"] == "project"
        assert result["room"] == "backend"
        assert "JWT tokens" in result["content"]

    def test_get_drawer_not_found(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_get_drawer

        result = tool_get_drawer("nonexistent_drawer")
        assert "error" in result

    def test_get_drawer_does_not_leak_absolute_source_file_path(
        self, monkeypatch, config, palace_path, collection, kg
    ):
        """tool_get_drawer must not expose the absolute filesystem path
        that the miners write into ``source_file``. Same threat class as
        the palace_path leak in mempalace_status: in nested-agent or
        multi-server MCP topologies the client is a separate trust
        domain, and the directory layout of the host has no documented
        client-side use. Basename is enough for citation."""
        _patch_mcp_server(monkeypatch, config, kg)

        secret_dir = "/private/home/alice/secret-research/2026"
        absolute_source = f"{secret_dir}/notes.md"
        collection.add(
            ids=["drawer_leak_probe"],
            documents=["verbatim drawer body for leak probe"],
            metadatas=[
                {
                    "wing": "research",
                    "room": "notes",
                    "source_file": absolute_source,
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-03T00:00:00",
                }
            ],
        )

        from mempalace.mcp_server import tool_get_drawer

        result = tool_get_drawer("drawer_leak_probe")
        assert result["drawer_id"] == "drawer_leak_probe"
        assert result["metadata"]["source_file"] == "notes.md"
        # Defense-in-depth: no field anywhere in the response should
        # contain the absolute path or its parent directory.
        serialized = json.dumps(result)
        assert absolute_source not in serialized
        assert secret_dir not in serialized

    def test_list_drawers(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        result = tool_list_drawers()
        assert result["count"] == 4
        assert len(result["drawers"]) == 4

    def test_list_drawers_with_wing_filter(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        result = tool_list_drawers(wing="project")
        assert result["count"] == 3
        assert all(d["wing"] == "project" for d in result["drawers"])

    def test_list_drawers_with_room_filter(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        result = tool_list_drawers(wing="project", room="backend")
        assert result["count"] == 2
        assert all(d["room"] == "backend" for d in result["drawers"])

    def test_list_drawers_pagination(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        result = tool_list_drawers(limit=2, offset=0)
        assert result["count"] == 2
        assert result["limit"] == 2
        assert result["offset"] == 0

    def test_list_drawers_negative_offset_clamped(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        result = tool_list_drawers(offset=-5)
        assert result["offset"] == 0

    def test_update_drawer_content(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_update_drawer, tool_get_drawer

        result = tool_update_drawer(
            "drawer_proj_backend_aaa", content="Updated content about auth."
        )
        assert result["success"] is True

        fetched = tool_get_drawer("drawer_proj_backend_aaa")
        assert fetched["content"] == "Updated content about auth."

    def test_update_drawer_wing_and_room(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_update_drawer

        result = tool_update_drawer("drawer_proj_backend_aaa", wing="new_wing", room="new_room")
        assert result["success"] is True
        assert result["wing"] == "new_wing"
        assert result["room"] == "new_room"

    def test_update_drawer_not_found(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_update_drawer

        result = tool_update_drawer("nonexistent_drawer", content="hello")
        assert result["success"] is False

    def test_update_drawer_noop(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_update_drawer

        result = tool_update_drawer("drawer_proj_backend_aaa")
        assert result["success"] is True
        assert result.get("noop") is True


# ── KG Tools ────────────────────────────────────────────────────────────


class TestKGTools:
    def test_kg_add(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        result = tool_kg_add(
            subject="Alice",
            predicate="likes",
            object="coffee",
            valid_from="2025-01-01",
        )
        assert result["success"] is True

    def test_kg_query(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_query

        result = tool_kg_query(entity="Max")
        assert result["count"] > 0

    def test_kg_invalidate(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_invalidate

        result = tool_kg_invalidate(
            subject="Max",
            predicate="does",
            object="chess",
            ended="2026-03-01",
        )
        assert result["success"] is True
        # Regression #1314: response must echo the actual ended date,
        # not silently drop it and return the literal string "today".
        assert result["ended"] == "2026-03-01"

    def test_kg_add_forwards_valid_to(self, monkeypatch, config, palace_path, kg):
        """Regression #1314 case 1: valid_to must round-trip through kg_add."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        result = tool_kg_add(
            subject="_test_temporal",
            predicate="had_value",
            object="probe",
            valid_from="2026-01-01",
            valid_to="2026-04-28",
        )
        assert result["success"] is True

        facts = kg.query_entity("_test_temporal")
        assert len(facts) == 1
        assert facts[0]["valid_from"] == "2026-01-01"
        assert facts[0]["valid_to"] == "2026-04-28"
        # An already-ended fact must not be reported as still current.
        assert facts[0]["current"] is False

    def test_kg_add_forwards_source_provenance(self, monkeypatch, config, palace_path, kg):
        """Regression #1314 case 3: source_file / source_drawer_id reach storage."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        result = tool_kg_add(
            subject="operating-verb",
            predicate="candidate",
            object="husbandry",
            valid_from="2026-04-28",
            source_closet="closet-42",
            source_file="docs/decisions.md",
            source_drawer_id="drawer_abc123",
        )
        assert result["success"] is True

        triple_id = result["triple_id"]
        # Read raw row to verify all provenance columns persisted.
        with kg._lock:
            row = (
                kg._conn()
                .execute(
                    "SELECT source_closet, source_file, source_drawer_id FROM triples WHERE id = ?",
                    (triple_id,),
                )
                .fetchone()
            )
        assert row is not None
        assert row["source_closet"] == "closet-42"
        assert row["source_file"] == "docs/decisions.md"
        assert row["source_drawer_id"] == "drawer_abc123"

    def test_kg_invalidate_returns_actual_ended_date(
        self, monkeypatch, config, palace_path, seeded_kg
    ):
        """Regression #1314 case 2: response reports the resolved date, not 'today'."""
        from datetime import date as _date

        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_invalidate

        # Caller-supplied date round-trips into the response.
        explicit = tool_kg_invalidate(
            subject="Max",
            predicate="does",
            object="swimming",
            ended="2026-04-28",
        )
        assert explicit["ended"] == "2026-04-28"

        # Caller-omitted date resolves to today's ISO date — never the
        # literal string "today" the buggy implementation used to return.
        implicit = tool_kg_invalidate(
            subject="Max",
            predicate="loves",
            object="Chess",
        )
        assert implicit["ended"] != "today"
        assert implicit["ended"] == _date.today().isoformat()

    def test_kg_timeline(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_timeline

        result = tool_kg_timeline(entity="Alice")
        assert result["count"] > 0

    def test_kg_stats(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_stats

        result = tool_kg_stats()
        assert result["entities"] >= 4

    # --- Date validation at the MCP boundary (issue #1164) ---

    def test_kg_add_rejects_invalid_valid_from(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        result = tool_kg_add(
            subject="Alice",
            predicate="likes",
            object="coffee",
            valid_from="Jan 2025",
        )
        assert result["success"] is False
        assert "valid_from" in result["error"]
        assert "ISO-8601" in result["error"]

    def test_kg_query_rejects_invalid_as_of(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_query

        result = tool_kg_query(entity="Max", as_of="March 2026")
        assert "error" in result
        assert "as_of" in result["error"]

    def test_kg_invalidate_rejects_invalid_ended(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_invalidate

        result = tool_kg_invalidate(
            subject="Max",
            predicate="does",
            object="chess",
            ended="yesterday",
        )
        assert result["success"] is False
        assert "ended" in result["error"]

    def test_kg_query_rejects_partial_iso_dates(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_query

        # Partial ISO dates are rejected: KG queries compare TEXT dates
        # lexicographically, so "2026-01-01" <= "2026" is False, which
        # silently excludes facts. Reject at the boundary — only YYYY-MM-DD
        # produces correct results.
        for value in ("2026", "2026-03"):
            result = tool_kg_query(entity="Max", as_of=value)
            assert "error" in result, f"accepted partial date {value!r}: {result}"

        # Full ISO-8601 dates still pass.
        result = tool_kg_query(entity="Max", as_of="2026-03-15")
        assert "error" not in result, f"rejected valid date: {result}"


# ── Diary Tools ─────────────────────────────────────────────────────────


class TestDiaryTools:
    def test_diary_write_and_read(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_diary_write, tool_diary_read

        w = tool_diary_write(
            agent_name="TestAgent",
            entry="Today we discussed authentication patterns.",
            topic="architecture",
        )
        assert w["success"] is True
        # agent_name is normalized to lowercase on write (#1243).
        assert w["agent"] == "testagent"

        r = tool_diary_read(agent_name="TestAgent")
        assert r["total"] == 1
        assert r["entries"][0]["topic"] == "architecture"
        assert "authentication" in r["entries"][0]["content"]

    def test_diary_read_empty(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_diary_read

        r = tool_diary_read(agent_name="Nobody")
        assert r["entries"] == []

    def test_diary_write_same_second_shared_prefix_no_collision(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client

        from mempalace import mcp_server

        class FrozenDateTime:
            calls = [
                datetime(2026, 4, 13, 22, 15, 30, 123456),
                datetime(2026, 4, 13, 22, 15, 30, 123457),
            ]
            fallback = datetime(2026, 4, 13, 22, 15, 30, 123457)

            @classmethod
            def now(cls):
                if cls.calls:
                    return cls.calls.pop(0)
                return cls.fallback

        monkeypatch.setattr(mcp_server, "datetime", FrozenDateTime)

        from mempalace.mcp_server import tool_diary_read, tool_diary_write

        entry1 = "A" * 50 + " entry one"
        entry2 = "A" * 50 + " entry two"

        result1 = tool_diary_write(agent_name="TestAgent", entry=entry1, topic="status")
        result2 = tool_diary_write(agent_name="TestAgent", entry=entry2, topic="status")

        assert result1["success"] is True
        assert result2["success"] is True
        assert result1["entry_id"] != result2["entry_id"]

        read_result = tool_diary_read(agent_name="TestAgent")
        contents = [entry["content"] for entry in read_result["entries"]]
        assert read_result["total"] == 2
        assert entry1 in contents
        assert entry2 in contents

    def test_diary_read_empty_wing_spans_all_wings(self, monkeypatch, config, palace_path, kg):
        """diary_read(wing='') must return entries from every wing this agent
        wrote to. Hooks write to project-derived wings (#659); a reader that
        silos by default wing would never see those entries."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_diary_read, tool_diary_write

        w1 = tool_diary_write(
            agent_name="TestAgent",
            entry="default-wing entry",
            topic="general",
        )
        w2 = tool_diary_write(
            agent_name="TestAgent",
            entry="project-wing entry",
            topic="general",
            wing="wing_someproject",
        )
        assert w1["success"] and w2["success"]

        # Empty wing → return both entries
        r = tool_diary_read(agent_name="TestAgent", wing="")
        assert r["total"] == 2
        contents = {e["content"] for e in r["entries"]}
        assert "default-wing entry" in contents
        assert "project-wing entry" in contents

        # Explicit wing → return only that wing's entries
        r_scoped = tool_diary_read(agent_name="TestAgent", wing="wing_someproject")
        assert r_scoped["total"] == 1
        assert r_scoped["entries"][0]["content"] == "project-wing entry"

    def test_diary_read_case_insensitive_agent(self, monkeypatch, config, palace_path, kg):
        """Regression for #1243: diary_read must be case-insensitive over
        agent_name. Writing as "Claude" and reading as "claude" (or vice
        versa) must surface the same entries — sanitize_name preserved
        case, which silently dropped reads when the agent name's casing
        differed from the write."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_diary_read, tool_diary_write

        # Write as "Claude" → read as "claude" should match.
        w1 = tool_diary_write(
            agent_name="Claude",
            entry="entry written as Claude",
            topic="general",
        )
        assert w1["success"]

        r1 = tool_diary_read(agent_name="claude")
        assert "entries" in r1, r1
        contents1 = {e["content"] for e in r1["entries"]}
        assert "entry written as Claude" in contents1

        # Write as "CLAUDE" → read as "Claude" should also match the
        # same agent. After normalization both writes target the same
        # lowercase agent identity, so both entries are returned.
        w2 = tool_diary_write(
            agent_name="CLAUDE",
            entry="entry written as CLAUDE",
            topic="general",
        )
        assert w2["success"]

        r2 = tool_diary_read(agent_name="Claude")
        contents2 = {e["content"] for e in r2["entries"]}
        assert "entry written as Claude" in contents2
        assert "entry written as CLAUDE" in contents2

        # The stored agent metadata is the lowercase form, and the
        # default wing is derived from that lowercase form too.
        assert w1["agent"] == "claude"
        assert w2["agent"] == "claude"


# ── Cache Invalidation (inode/mtime) ──────────────────────────────────


class TestCacheInvalidation:
    """Tests for _get_collection inode/mtime cache invalidation logic."""

    def test_mtime_change_invalidates_cache(self, monkeypatch, config, palace_path, kg):
        """When mtime changes, the cached collection should be replaced."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        # Create a real collection so _get_collection succeeds
        _client, _col = _get_collection(palace_path, create=True)
        del _client

        # Prime the cache
        col1 = mcp_server._get_collection()
        assert col1 is not None

        # Simulate an external write changing the mtime
        old_mtime = mcp_server._palace_db_mtime
        monkeypatch.setattr(mcp_server, "_palace_db_mtime", old_mtime - 10.0)

        # _get_collection should detect the mtime drift and reconnect
        col2 = mcp_server._get_collection()
        assert col2 is not None

    def test_inode_change_invalidates_cache(self, monkeypatch, config, palace_path, kg):
        """When inode changes (file replaced), the cached collection should be replaced."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        _client, _col = _get_collection(palace_path, create=True)
        del _client

        # Prime the cache
        col1 = mcp_server._get_collection()
        assert col1 is not None

        # Simulate a rebuild that changes the inode
        monkeypatch.setattr(mcp_server, "_palace_db_inode", 99999)

        col2 = mcp_server._get_collection()
        assert col2 is not None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows holds chroma.sqlite3 open while the client is cached, blocking os.remove",
    )
    def test_missing_db_invalidates_cache(self, monkeypatch, config, palace_path, kg):
        """When chroma.sqlite3 disappears, a cached collection should be invalidated."""
        _patch_mcp_server(monkeypatch, config, kg)
        import os
        from mempalace import mcp_server

        _client, _col = _get_collection(palace_path, create=True)
        del _client

        # Prime the cache
        col1 = mcp_server._get_collection()
        assert col1 is not None
        assert mcp_server._collection_cache is not None

        # Delete the DB file to simulate a rebuild in progress
        db_file = os.path.join(palace_path, "chroma.sqlite3")
        if os.path.isfile(db_file):
            os.remove(db_file)

        # Cache should be invalidated; _get_collection returns None
        # because the backend can't open a missing DB without create=True
        mcp_server._get_collection()
        # The key assertion: the old cached collection was dropped
        assert mcp_server._palace_db_inode == 0
        assert mcp_server._palace_db_mtime == 0.0

    def test_reconnect_reports_failure_when_no_palace(self, monkeypatch, config, kg):
        """tool_reconnect should report failure when no collection is available."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        # Make _get_collection always return None
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: None)

        result = mcp_server.tool_reconnect()
        assert result["success"] is False
        assert "No palace found" in result["message"]
        assert result["drawers"] == 0

    def test_reconnect_reports_success(self, monkeypatch, config, palace_path, kg):
        """tool_reconnect should report success with drawer count."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        result = mcp_server.tool_reconnect()
        assert result["success"] is True
        assert "Reconnected" in result["message"]
        assert isinstance(result["drawers"], int)

    def test_reconnect_closes_shared_backend(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from unittest.mock import MagicMock

        from mempalace import mcp_server, palace

        close_palace = MagicMock()
        monkeypatch.setattr(palace._DEFAULT_BACKEND, "close_palace", close_palace)

        class _FakeCol:
            def count(self):
                return 7

        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: _FakeCol())

        result = mcp_server.tool_reconnect()
        assert result["success"] is True
        close_palace.assert_called_once_with(config.palace_path)

    def test_get_collection_create_true_avoids_get_or_create_on_reopen(
        self, monkeypatch, config, palace_path, kg
    ):
        """Regression for the MCP-server half of #1262.

        ChromaDB 1.5.x's Rust bindings SIGSEGV when
        ``client.get_or_create_collection`` is called with metadata that
        differs from the collection's stored metadata. The Stop hook
        path (``tool_diary_write`` -> ``_get_collection(create=True)``)
        was reaching that codepath on every session-end; #1262 fixed
        the equivalent crash class in ``ChromaBackend`` but left this
        site untouched. ``_get_collection(create=True)`` must call
        ``client.get_collection`` first and only fall back to
        ``client.create_collection`` when the collection does not yet
        exist on disk.
        """
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        col1 = mcp_server._get_collection(create=True)
        assert col1 is not None

        client = mcp_server._client_cache
        assert client is not None

        # Patch at the class level — chromadb's mtime-change detection
        # may rebuild the client between calls, so an instance-level
        # spy would not survive.
        client_cls = type(client)
        calls: list[tuple] = []

        def _spy(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError(
                "get_or_create_collection must not be called on reopen "
                "(SIGSEGV path on metadata mismatch)"
            )

        monkeypatch.setattr(client_cls, "get_or_create_collection", _spy)
        mcp_server._collection_cache = None

        col2 = mcp_server._get_collection(create=True)
        assert col2 is not None
        assert calls == [], f"get_or_create_collection was called: {calls}"

    def test_get_collection_passes_embedding_function(self, monkeypatch, config, palace_path, kg):
        """Regression for #1299.

        ``mcp_server._get_collection`` must pass ``embedding_function=`` into
        both ``client.get_collection`` and ``client.create_collection``,
        mirroring ``ChromaBackend.get_collection``. Without it, ChromaDB 1.x
        falls back to its built-in ``DefaultEmbeddingFunction`` (whose lazy
        ONNX provider selection has SIGSEGV'd on python 3.14 + Apple Silicon),
        and writers/readers can disagree with the miner about which EF is
        bound to the collection. The miner / Stop hook ingest path routes
        through ``ChromaBackend.get_collection`` which does this correctly;
        the MCP server must match.
        """
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        client = mcp_server._get_client()
        client_cls = type(client)
        captured: dict[str, list[dict]] = {"get": [], "create": []}
        real_get = client_cls.get_collection
        real_create = client_cls.create_collection

        def _spy_get(self, name, **kwargs):
            captured["get"].append(dict(kwargs))
            return real_get(self, name, **kwargs)

        def _spy_create(self, name, **kwargs):
            captured["create"].append(dict(kwargs))
            return real_create(self, name, **kwargs)

        monkeypatch.setattr(client_cls, "get_collection", _spy_get)
        monkeypatch.setattr(client_cls, "create_collection", _spy_create)
        mcp_server._collection_cache = None

        col = mcp_server._get_collection(create=True)
        assert col is not None

        all_calls = captured["get"] + captured["create"]
        assert all_calls, "expected get_collection or create_collection to be called"
        for kwargs in all_calls:
            assert (
                "embedding_function" in kwargs
            ), f"missing embedding_function= in chromadb call: {kwargs}"
            assert kwargs["embedding_function"] is not None

        # Same expectation on the create=False (cache-miss) reopen path.
        mcp_server._collection_cache = None
        captured["get"].clear()
        captured["create"].clear()
        col2 = mcp_server._get_collection()
        assert col2 is not None
        assert captured["get"], "expected get_collection on cache-miss reopen"
        for kwargs in captured["get"]:
            assert "embedding_function" in kwargs
            assert kwargs["embedding_function"] is not None

    def test_get_collection_retries_once_on_exception(self, monkeypatch, config, palace_path, kg):
        """Regression: a transient failure inside _get_collection must trigger
        one retry after clearing the client/collection caches, not silently
        return None.

        Before this fix, a stale chromadb handle (e.g. the rust bindings
        invalidating after an out-of-band write) would raise inside the
        single ``try`` block, get swallowed by ``except Exception: return
        None``, and every subsequent tool call would hit the same poisoned
        cache returning None. The retry forces ``_get_client()`` to rebuild
        the client (which re-runs ``quarantine_stale_hnsw`` per #1322), so
        the second attempt heals the common stale-handle case.
        """
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        # Force a cold cache so the first call goes through the open path.
        mcp_server._client_cache = None
        mcp_server._collection_cache = None

        real_get_client = mcp_server._get_client
        attempts = {"count": 0}

        def flaky_get_client():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("simulated transient chromadb failure")
            return real_get_client()

        monkeypatch.setattr(mcp_server, "_get_client", flaky_get_client)

        col = mcp_server._get_collection()

        # Both attempts ran and the second succeeded.
        assert attempts["count"] == 2
        assert col is not None

    def test_get_collection_returns_none_after_two_failures(
        self, monkeypatch, config, palace_path, kg
    ):
        """If both attempts fail, return None (matches the prior contract for
        permanent failures — only the transient case is now self-healing)."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        mcp_server._client_cache = None
        mcp_server._collection_cache = None

        attempts = {"count": 0}

        def always_fails():
            attempts["count"] += 1
            raise RuntimeError("permanent chromadb failure")

        monkeypatch.setattr(mcp_server, "_get_client", always_fails)

        col = mcp_server._get_collection()

        assert attempts["count"] == 2
        assert col is None


class TestKGLazyCache:
    """Lazy per-path KnowledgeGraph cache (issue #1136)."""

    def test_lazy_init_no_import_side_effect(self, tmp_path):
        """Importing mcp_server must not create knowledge_graph.sqlite3.

        Runs in a fresh subprocess with HOME pointed at tmp_path so the
        assertion targets a clean filesystem, independent of conftest's
        session-level HOME patch.
        """
        import subprocess
        import sys

        kg_file = tmp_path / ".mempalace" / "knowledge_graph.sqlite3"
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMPAL")}
        env["HOME"] = str(tmp_path)
        env["USERPROFILE"] = str(tmp_path)
        result = subprocess.run(
            [sys.executable, "-c", "import mempalace.mcp_server"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"import failed: {result.stderr}"
        assert not kg_file.exists(), f"import created sqlite file at {kg_file} as a side effect"

    def test_get_kg_returns_same_instance(self, tmp_path, monkeypatch):
        """Two calls with the same resolved path return the same KG."""
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_kg_by_path", {})
        monkeypatch.setattr(mcp_server, "_palace_flag_given", True)
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))

        kg1 = mcp_server._get_kg()
        kg2 = mcp_server._get_kg()
        assert kg1 is kg2
        assert len(mcp_server._kg_by_path) == 1

    def test_get_kg_different_paths_different_instances(self, tmp_path, monkeypatch):
        """Different palace paths map to different KG instances."""
        from mempalace import mcp_server

        tmp_a = tmp_path / "a"
        tmp_b = tmp_path / "b"
        tmp_a.mkdir()
        tmp_b.mkdir()

        monkeypatch.setattr(mcp_server, "_kg_by_path", {})
        monkeypatch.setattr(mcp_server, "_palace_flag_given", True)

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_a))
        kg_a = mcp_server._get_kg()
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_b))
        kg_b = mcp_server._get_kg()

        assert kg_a is not kg_b
        assert len(mcp_server._kg_by_path) == 2

    def test_multi_tenant_env_switch(self, tmp_path, monkeypatch):
        """The issue #1136 acceptance scenario.

        Rotating MEMPALACE_PALACE_PATH between MCP tool calls must route
        each call to the correct tenant's KG sqlite file.
        """
        from mempalace import mcp_server

        tmp_a = tmp_path / "tenant_a"
        tmp_b = tmp_path / "tenant_b"
        tmp_a.mkdir()
        tmp_b.mkdir()

        monkeypatch.setattr(mcp_server, "_kg_by_path", {})
        monkeypatch.setattr(mcp_server, "_palace_flag_given", True)

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_a))
        add_result = mcp_server.tool_kg_add(
            subject="alice_secret",
            predicate="owns",
            object="repo_a",
        )
        assert add_result.get("success") is True, add_result

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_b))
        query_b = mcp_server.tool_kg_query(entity="alice_secret")
        assert query_b.get("count", 0) == 0, f"tenant B leaked tenant A's fact: {query_b}"

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_a))
        query_a = mcp_server.tool_kg_query(entity="alice_secret")
        assert query_a.get("count", 0) >= 1, f"tenant A lost its own fact: {query_a}"

    def test_cache_thread_safe(self, tmp_path, monkeypatch):
        """Concurrent _get_kg() for the same path yields one instance."""
        import concurrent.futures
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_kg_by_path", {})
        monkeypatch.setattr(mcp_server, "_palace_flag_given", True)
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: mcp_server._get_kg(), range(16)))

        ids = {id(kg) for kg in results}
        assert len(ids) == 1, f"expected 1 unique instance, got {len(ids)}"
        assert len(mcp_server._kg_by_path) == 1

    def test_tool_reconnect_drains_kg_cache(self, monkeypatch):
        """``tool_reconnect`` must close cached KG instances and clear the dict.

        Without this, an external replacement of ``knowledge_graph.sqlite3``
        leaves the server pinned to a stale ``sqlite3.Connection``.
        """
        from mempalace import mcp_server

        class _FakeKG:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        fake_a = _FakeKG()
        fake_b = _FakeKG()
        monkeypatch.setattr(mcp_server, "_kg_by_path", {"/a": fake_a, "/b": fake_b})
        # Bypass real ChromaDB so the test isolates KG-cache behaviour.
        monkeypatch.setattr(mcp_server, "_get_collection", lambda: None)

        mcp_server.tool_reconnect()

        assert fake_a.closed is True
        assert fake_b.closed is True
        assert mcp_server._kg_by_path == {}

    def test_tool_reconnect_swallows_kg_close_errors(self, monkeypatch):
        """A failing ``close()`` on one cached KG must not block cache clearing."""
        from mempalace import mcp_server

        class _BoomKG:
            def close(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(mcp_server, "_kg_by_path", {"/a": _BoomKG()})
        monkeypatch.setattr(mcp_server, "_get_collection", lambda: None)

        mcp_server.tool_reconnect()

        assert mcp_server._kg_by_path == {}

    def test_call_kg_retries_after_concurrent_close(self, monkeypatch):
        """A KG closed mid-handler must trigger a one-shot retry with a fresh
        instance — not surface a -32000 to the MCP client."""
        import sqlite3 as _sqlite3

        from mempalace import mcp_server

        path = "/fake/palace/knowledge_graph.sqlite3"
        monkeypatch.setattr(mcp_server, "_resolve_kg_path", lambda: path)

        class _ClosedKG:
            def query_entity(self, entity, **kwargs):
                raise _sqlite3.ProgrammingError("Cannot operate on a closed database")

        class _FreshKG:
            def query_entity(self, entity, **kwargs):
                return [{"entity": entity}]

        cache = {os.path.abspath(path): _ClosedKG()}
        monkeypatch.setattr(mcp_server, "_kg_by_path", cache)

        # Second _get_kg() call (after the cache eviction) constructs a new
        # KG. Patch the constructor so we don't open a real sqlite file.
        monkeypatch.setattr(mcp_server, "KnowledgeGraph", lambda **_: _FreshKG())

        result = mcp_server._call_kg(lambda kg: kg.query_entity("Alice"))
        assert result == [{"entity": "Alice"}]
        # The closed instance must be evicted; the fresh one must be cached.
        assert isinstance(cache[os.path.abspath(path)], _FreshKG)

    def test_call_kg_does_not_retry_on_other_errors(self, monkeypatch):
        """Non-ProgrammingError exceptions must propagate without retry —
        we don't want the retry guard masking real bugs."""
        from mempalace import mcp_server

        path = "/fake/palace/knowledge_graph.sqlite3"
        monkeypatch.setattr(mcp_server, "_resolve_kg_path", lambda: path)

        calls = {"count": 0}

        class _FailingKG:
            def query_entity(self, entity, **kwargs):
                calls["count"] += 1
                raise ValueError("bad input")

        monkeypatch.setattr(mcp_server, "_kg_by_path", {os.path.abspath(path): _FailingKG()})
        monkeypatch.setattr(mcp_server, "KnowledgeGraph", lambda **_: _FailingKG())

        with pytest.raises(ValueError, match="bad input"):
            mcp_server._call_kg(lambda kg: kg.query_entity("Alice"))
        assert calls["count"] == 1, "non-ProgrammingError must not trigger retry"

    def test_call_kg_gives_up_after_one_retry(self, monkeypatch):
        """If the second attempt also hits a closed DB, give up rather than
        loop forever — a sustained close-stream is a different bug."""
        import sqlite3 as _sqlite3

        from mempalace import mcp_server

        path = "/fake/palace/knowledge_graph.sqlite3"
        monkeypatch.setattr(mcp_server, "_resolve_kg_path", lambda: path)

        calls = {"count": 0}

        class _AlwaysClosedKG:
            def query_entity(self, entity, **kwargs):
                calls["count"] += 1
                raise _sqlite3.ProgrammingError("closed again")

        cache = {}
        monkeypatch.setattr(mcp_server, "_kg_by_path", cache)
        monkeypatch.setattr(mcp_server, "KnowledgeGraph", lambda **_: _AlwaysClosedKG())

        with pytest.raises(_sqlite3.ProgrammingError):
            mcp_server._call_kg(lambda kg: kg.query_entity("Alice"))
        assert calls["count"] == 2, "expected exactly one retry beyond the initial attempt"
