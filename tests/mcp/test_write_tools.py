"""MCP server tests — drawer write/update/delete and delete-by-source."""

import json
from unittest.mock import MagicMock


from _mcp_server_helpers import (
    _get_collection,
    _patch_mcp_server,
)


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

    def test_add_drawer_returns_failure_when_idempotency_precheck_raises(
        self, monkeypatch, config, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        mock_col = MagicMock()
        mock_col.get.side_effect = RuntimeError("precheck boom")
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: mock_col)

        result = mcp_server.tool_add_drawer("w", "r", "content")

        assert result["success"] is False
        assert "Idempotency check failed before write" in result["error"]
        assert "precheck boom" in result["error"]

    def test_add_drawer_does_not_upsert_when_idempotency_precheck_raises(
        self, monkeypatch, config, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        mock_col = MagicMock()
        mock_col.get.side_effect = RuntimeError("precheck boom")
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: mock_col)

        result = mcp_server.tool_add_drawer("w", "r", "content")

        assert result["success"] is False
        mock_col.upsert.assert_not_called()

    def test_add_drawer_treats_dict_like_precheck_hit_as_already_exists(
        self, monkeypatch, config, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": ["existing-drawer"]}
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: mock_col)

        result = mcp_server.tool_add_drawer("w", "r", "content")

        assert result["success"] is True
        assert result["reason"] == "already_exists"
        mock_col.upsert.assert_not_called()

    def test_get_result_ids_normalizes_none_to_empty_list(self):
        from mempalace import mcp_server

        class DictLikeResult:
            def get(self, key, default=None):
                return None

        assert mcp_server._get_result_ids({"ids": None}) == []
        assert mcp_server._get_result_ids(DictLikeResult()) == []

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
        assert result1["drawer_id"] != result2["drawer_id"], (
            "Documents with shared header but different content must have distinct drawer IDs"
        )

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

    def test_delete_drawer_purges_matching_closets(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        """Deleting a drawer purges its source's closets too, so the AAAK
        index keeps no stale pointer at the now-deleted drawer (#2325)."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_delete_drawer
        from mempalace.palace import get_closets_collection

        closets_col = get_closets_collection(palace_path, create=True)
        closets_col.add(
            ids=["auth_closet_01"],
            documents=["topic: JWT session tokens"],
            metadatas=[{"source_file": "auth.py"}],
        )

        result = tool_delete_drawer("drawer_proj_backend_aaa")
        assert result["success"] is True
        assert result["closets_deleted"] == 1

        # Re-acquire: the staleness reconnect drops chromadb's path-keyed
        # System cache (#2002), so a handle taken before the call is dead now.
        closets_col = get_closets_collection(palace_path, create=False)
        assert closets_col.get(include=[])["ids"] == []

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

    def test_check_duplicate_short_circuits_when_vector_disabled(self, monkeypatch):
        from mempalace import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "hnsw_capacity_status",
            lambda *_args, **_kwargs: {"diverged": True, "message": "capacity mismatch"},
        )

        def fail_get_collection():
            raise AssertionError("_get_collection must not run when vector search is disabled")

        monkeypatch.setattr(mcp_server, "_get_collection", fail_get_collection)
        result = mcp_server.tool_check_duplicate("content")

        assert result["is_duplicate"] is False
        assert result["vector_disabled"] is True
        assert result["vector_disabled_reason"] == "capacity mismatch"

    def test_checkpoint_files_items_and_writes_diary(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_checkpoint

        result = tool_checkpoint(
            items=[
                {"wing": "w", "room": "decisions", "content": "Use PostgreSQL for storage."},
                {"wing": "w", "room": "backend", "content": "Cache sessions in Redis."},
            ],
            diary={"agent_name": "cursor-ide", "wing": "w", "entry": "SESSION|did.stuff|★"},
        )
        assert len(result["added"]) == 2
        assert result["duplicates"] == []
        assert result["errors"] == []
        assert all(a["success"] for a in result["added"])
        assert result["diary"]["success"] is True

    def test_checkpoint_skips_semantic_duplicates(self, monkeypatch, config, kg):
        from mempalace import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "tool_check_duplicate",
            lambda content, threshold=0.9: {
                "is_duplicate": True,
                "matches": [{"id": "x", "similarity": 0.95}],
            },
        )
        called = {"add": False}

        def _fail_add(**_kwargs):
            called["add"] = True
            return {"success": True}

        monkeypatch.setattr(mcp_server, "tool_add_drawer", _fail_add)

        result = mcp_server.tool_checkpoint(
            items=[{"wing": "w", "room": "r", "content": "already known"}]
        )
        assert result["added"] == []
        assert len(result["duplicates"]) == 1
        assert called["add"] is False

    def test_checkpoint_reports_malformed_items(self, monkeypatch, config, kg):
        from mempalace import mcp_server

        monkeypatch.setattr(
            mcp_server, "tool_check_duplicate", lambda *a, **k: {"is_duplicate": False}
        )
        result = mcp_server.tool_checkpoint(items=[{"wing": "w", "room": "r"}, "not-a-dict"])
        assert result["added"] == []
        assert len(result["errors"]) == 2

    def test_checkpoint_rejects_non_string_fields_without_calling_handlers(
        self, monkeypatch, config, kg
    ):
        """A non-string content must be reported, never passed to the
        single-item handlers where it would raise deep in sanitization."""
        from mempalace import mcp_server

        def _explode(*_a, **_k):
            raise AssertionError("handlers must not run for malformed items")

        monkeypatch.setattr(mcp_server, "tool_check_duplicate", _explode)
        monkeypatch.setattr(mcp_server, "tool_add_drawer", _explode)

        result = mcp_server.tool_checkpoint(
            items=[{"wing": "w", "room": "r", "content": {"not": "a string"}}]
        )
        assert result["added"] == []
        assert len(result["errors"]) == 1
        assert "non-empty strings" in result["errors"][0]["error"]

    def test_checkpoint_files_when_dedup_check_errors(self, monkeypatch, config, kg):
        """A dedup error is a genuine index failure (content is already
        validated as a string); we still file rather than drop the memory."""
        from mempalace import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "tool_check_duplicate",
            lambda *a, **k: {"error": "Duplicate check failed"},
        )
        filed = {}

        def _add(**kwargs):
            filed.update(kwargs)
            return {"success": True, "drawer_id": "d1"}

        monkeypatch.setattr(mcp_server, "tool_add_drawer", _add)

        result = mcp_server.tool_checkpoint(
            items=[{"wing": "w", "room": "r", "content": "keep me"}]
        )
        assert len(result["added"]) == 1
        assert filed["content"] == "keep me"

    def test_checkpoint_reports_malformed_diary(self, monkeypatch, config, kg):
        from mempalace import mcp_server

        monkeypatch.setattr(
            mcp_server, "tool_check_duplicate", lambda *a, **k: {"is_duplicate": False}
        )

        def _fail_diary(*_a, **_k):
            raise AssertionError("diary_write must not run for malformed diary")

        monkeypatch.setattr(mcp_server, "tool_diary_write", _fail_diary)

        result = mcp_server.tool_checkpoint(items=[], diary={"agent_name": "x"})
        assert "diary" not in result
        assert any("diary entry" in e.get("error", "") for e in result["errors"])

    def test_checkpoint_registered_in_tools(self):
        from mempalace import mcp_server

        assert "mempalace_checkpoint" in mcp_server.TOOLS
        assert mcp_server.TOOLS["mempalace_checkpoint"]["handler"] is mcp_server.tool_checkpoint

    def test_checkpoint_added_by_defaults_to_diary_agent(
        self, monkeypatch, config, palace_path, kg
    ):
        """#2023: with no explicit ``added_by``, each filed drawer is attributed
        to the diary ``agent_name`` (verbatim case) rather than the generic
        ``checkpoint`` label, so the filing agent survives in provenance."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        _client.close()  # release file handles; a bare del leaks them on Windows (#1128)
        from mempalace.mcp_server import tool_checkpoint

        result = tool_checkpoint(
            items=[{"wing": "w", "room": "decisions", "content": "Use PostgreSQL for storage."}],
            diary={"agent_name": "DeepSeek", "wing": "w", "entry": "SESSION|did.stuff|star"},
        )
        assert len(result["added"]) == 1

        client, col = _get_collection(palace_path)
        try:
            metas = col.get(include=["metadatas"])["metadatas"]
        finally:
            client.close()
        drawers = [m for m in metas if m.get("room") == "decisions"]
        assert len(drawers) == 1
        # Verbatim case, not the lowercased diary-index form of agent_name.
        assert drawers[0]["added_by"] == "DeepSeek"

    def test_checkpoint_explicit_added_by_overrides_diary(self, monkeypatch):
        """An explicit ``added_by`` wins over the diary ``agent_name`` fallback."""
        from mempalace import mcp_server

        monkeypatch.setattr(
            mcp_server, "tool_check_duplicate", lambda *a, **k: {"is_duplicate": False}
        )
        monkeypatch.setattr(mcp_server, "tool_diary_write", lambda **k: {"success": True})
        filed = {}

        def _add(**kwargs):
            filed.update(kwargs)
            return {"success": True, "drawer_id": "d1"}

        monkeypatch.setattr(mcp_server, "tool_add_drawer", _add)

        mcp_server.tool_checkpoint(
            items=[{"wing": "w", "room": "r", "content": "keep me"}],
            diary={"agent_name": "deepseek", "entry": "SESSION|x|star"},
            added_by="alice",
        )
        assert filed["added_by"] == "alice"

    def test_checkpoint_added_by_falls_back_to_checkpoint_label(self, monkeypatch):
        """Neither an explicit ``added_by`` nor a diary ``agent_name`` -> the
        drawer keeps the legacy ``checkpoint`` attribution (backward compatible)."""
        from mempalace import mcp_server

        monkeypatch.setattr(
            mcp_server, "tool_check_duplicate", lambda *a, **k: {"is_duplicate": False}
        )
        monkeypatch.setattr(mcp_server, "tool_diary_write", lambda **k: {"success": True})
        seen = []

        def _add(**kwargs):
            seen.append(kwargs["added_by"])
            return {"success": True, "drawer_id": "d1"}

        monkeypatch.setattr(mcp_server, "tool_add_drawer", _add)

        # No diary block at all.
        mcp_server.tool_checkpoint(items=[{"wing": "w", "room": "r", "content": "a"}])
        # Diary present but without an ``agent_name``.
        mcp_server.tool_checkpoint(
            items=[{"wing": "w", "room": "r", "content": "b"}],
            diary={"entry": "SESSION|y|star"},
        )
        assert seen == ["checkpoint", "checkpoint"]

    def test_checkpoint_added_by_accepted_via_dispatch(self, monkeypatch):
        """#2023: ``added_by`` passes the tools/call schema whitelist (the
        reporter's HTTP MCP transport reuses this dispatcher) and the real
        handler forwards it, for both the explicit value and the diary fallback."""
        from mempalace import mcp_server

        monkeypatch.setattr(
            mcp_server, "tool_check_duplicate", lambda *a, **k: {"is_duplicate": False}
        )
        monkeypatch.setattr(mcp_server, "tool_diary_write", lambda **k: {"success": True})
        filed = {}

        def _add(**kwargs):
            filed.update(kwargs)
            return {"success": True, "drawer_id": "d1"}

        monkeypatch.setattr(mcp_server, "tool_add_drawer", _add)

        resp = mcp_server.handle_request(
            {
                "method": "tools/call",
                "id": 1,
                "params": {
                    "name": "mempalace_checkpoint",
                    "arguments": {
                        "items": [{"wing": "w", "room": "r", "content": "hi"}],
                        "added_by": "alice",
                    },
                },
            }
        )
        assert "error" not in resp
        assert filed["added_by"] == "alice"

        filed.clear()
        resp2 = mcp_server.handle_request(
            {
                "method": "tools/call",
                "id": 2,
                "params": {
                    "name": "mempalace_checkpoint",
                    "arguments": {
                        "items": [{"wing": "w", "room": "r", "content": "yo"}],
                        "diary": {"agent_name": "DeepSeek", "entry": "SESSION|z|star"},
                    },
                },
            }
        )
        assert "error" not in resp2
        assert filed["added_by"] == "DeepSeek"

    def test_checkpoint_schema_exposes_added_by(self):
        """``added_by`` is declared in the checkpoint tool schema so the
        dispatch whitelist admits it instead of rejecting it as unknown."""
        from mempalace import mcp_server

        props = mcp_server.TOOLS["mempalace_checkpoint"]["input_schema"]["properties"]
        assert "added_by" in props
        assert props["added_by"]["type"] == "string"

    def test_checkpoint_blank_or_invalid_added_by_defers_to_diary(self, monkeypatch):
        """A blank, whitespace-only, non-string, or None explicit ``added_by``
        counts as unspecified, so it defers to the diary ``agent_name`` rather
        than masking it; with no usable diary name it falls to ``checkpoint``."""
        from mempalace import mcp_server

        monkeypatch.setattr(
            mcp_server, "tool_check_duplicate", lambda *a, **k: {"is_duplicate": False}
        )
        monkeypatch.setattr(mcp_server, "tool_diary_write", lambda **k: {"success": True})
        seen = []

        def _add(**kwargs):
            seen.append(kwargs["added_by"])
            return {"success": True, "drawer_id": "d1"}

        monkeypatch.setattr(mcp_server, "tool_add_drawer", _add)

        diary = {"agent_name": "deepseek", "entry": "SESSION|x|star"}
        for bad in ("", "   ", 123, None):
            mcp_server.tool_checkpoint(
                items=[{"wing": "w", "room": "r", "content": f"c{bad!r}"}],
                diary=diary,
                added_by=bad,
            )
        # Every unusable explicit value defers to the diary agent.
        assert seen == ["deepseek", "deepseek", "deepseek", "deepseek"]

        # Blank explicit AND a blank diary name -> the legacy label.
        seen.clear()
        mcp_server.tool_checkpoint(
            items=[{"wing": "w", "room": "r", "content": "z"}],
            diary={"agent_name": "   ", "entry": "SESSION|y|star"},
            added_by="",
        )
        assert seen == ["checkpoint"]

    def test_checkpoint_added_by_uniform_across_items(self, monkeypatch):
        """All items in one checkpoint share a single resolved author (a
        checkpoint is one agent's session save; attribution is resolved once)."""
        from mempalace import mcp_server

        monkeypatch.setattr(
            mcp_server, "tool_check_duplicate", lambda *a, **k: {"is_duplicate": False}
        )
        monkeypatch.setattr(mcp_server, "tool_diary_write", lambda **k: {"success": True})
        seen = []

        def _add(**kwargs):
            seen.append(kwargs["added_by"])
            return {"success": True, "drawer_id": kwargs["content"]}

        monkeypatch.setattr(mcp_server, "tool_add_drawer", _add)

        mcp_server.tool_checkpoint(
            items=[
                {"wing": "w", "room": "r", "content": "one"},
                {"wing": "w", "room": "r", "content": "two"},
            ],
            diary={"agent_name": "DeepSeek", "entry": "SESSION|q|star"},
        )
        assert seen == ["DeepSeek", "DeepSeek"]

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

    def test_list_drawers_since_filter_inclusive(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        # seeded filed_at values: 2026-01-01..2026-01-04; since is inclusive.
        result = tool_list_drawers(since="2026-01-03")
        assert result["total"] == 2
        assert result["count"] == 2
        filed = sorted(d["metadata"]["filed_at"] for d in result["drawers"])
        assert filed == ["2026-01-03T00:00:00", "2026-01-04T00:00:00"]

    def test_list_drawers_before_filter_exclusive(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        # before is exclusive: 2026-01-03 keeps only 01 and 02.
        result = tool_list_drawers(before="2026-01-03")
        assert result["total"] == 2
        filed = sorted(d["metadata"]["filed_at"] for d in result["drawers"])
        assert filed == ["2026-01-01T00:00:00", "2026-01-02T00:00:00"]

    def test_list_drawers_since_and_before_window(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        # [since, before): 02 and 03 kept, 01 below, 04 at/above the bound.
        result = tool_list_drawers(since="2026-01-02", before="2026-01-04")
        assert result["total"] == 2
        filed = sorted(d["metadata"]["filed_at"] for d in result["drawers"])
        assert filed == ["2026-01-02T00:00:00", "2026-01-03T00:00:00"]

    def test_list_drawers_date_window_single_day(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        # since inclusive + before exclusive isolates exactly 2026-01-02.
        result = tool_list_drawers(since="2026-01-02", before="2026-01-03")
        assert result["total"] == 1
        assert result["drawers"][0]["metadata"]["filed_at"] == "2026-01-02T00:00:00"

    def test_list_drawers_date_filter_combines_with_wing(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        # project wing = 01,02,03; since 2026-01-02 narrows to 02,03.
        result = tool_list_drawers(wing="project", since="2026-01-02")
        assert result["total"] == 2
        assert all(d["wing"] == "project" for d in result["drawers"])

    def test_list_drawers_no_date_filter_unchanged(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        # Omitting since/before leaves the full set (regression guard).
        assert tool_list_drawers()["total"] == 4

    def test_list_drawers_rejects_invalid_since(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        result = tool_list_drawers(since="not-a-date")
        assert "error" in result
        assert "since" in result["error"]

    def test_list_drawers_rejects_invalid_before(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        result = tool_list_drawers(before="2026-99-99")
        assert "error" in result
        assert "before" in result["error"]

    def test_list_drawers_rejects_inverted_window(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        # since must be earlier than before; inverted bounds are a clear error,
        # not a silently empty result.
        result = tool_list_drawers(since="2026-06-01", before="2026-01-01")
        assert "error" in result
        assert "since" in result["error"]
        assert "before" in result["error"]

    def test_list_drawers_excludes_undated_drawer_when_filtered(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        # A drawer with no filed_at is present unfiltered but excluded once a
        # date bound is active (its age cannot be confirmed in-window).
        seeded_collection.add(
            ids=["drawer_no_filed_at"],
            documents=["A drawer without a filed_at timestamp."],
            metadatas=[{"wing": "project", "room": "backend"}],
        )
        assert tool_list_drawers()["total"] == 5
        filtered = tool_list_drawers(since="2026-01-01")
        ids = [d["drawer_id"] for d in filtered["drawers"]]
        assert "drawer_no_filed_at" not in ids
        assert filtered["total"] == 4

    def test_list_drawers_date_filter_paginates_on_filtered_total(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_list_drawers

        # window [01-01, 01-04) keeps 01, 02, 03; pagination runs on that
        # filtered total, not the grand total of 4.
        page1 = tool_list_drawers(since="2026-01-01", before="2026-01-04", limit=2, offset=0)
        page2 = tool_list_drawers(since="2026-01-01", before="2026-01-04", limit=2, offset=2)
        assert page1["total"] == 3
        assert page1["count"] == 2
        assert page2["total"] == 3
        assert page2["count"] == 1

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

    def test_update_drawer_content_purges_matching_closets(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        """Correcting a drawer's content purges its source's closets, which
        otherwise keep quoting the pre-correction text indefinitely (#2325)."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_update_drawer
        from mempalace.palace import get_closets_collection

        closets_col = get_closets_collection(palace_path, create=True)
        closets_col.add(
            ids=["auth_closet_01"],
            documents=["topic: JWT session tokens"],
            metadatas=[{"source_file": "auth.py"}],
        )

        result = tool_update_drawer("drawer_proj_backend_aaa", content="[RETRACTED]")
        assert result["success"] is True
        assert result["closets_deleted"] == 1

        closets_col = get_closets_collection(palace_path, create=False)
        assert closets_col.get(include=[])["ids"] == []

    def test_update_drawer_wing_and_room_does_not_purge_closets(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        """A wing/room move alone leaves the quoted text correct, so it must
        not purge closets the way a content edit does (#2325)."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_update_drawer
        from mempalace.palace import get_closets_collection

        closets_col = get_closets_collection(palace_path, create=True)
        closets_col.add(
            ids=["auth_closet_01"],
            documents=["topic: JWT session tokens"],
            metadatas=[{"source_file": "auth.py"}],
        )

        result = tool_update_drawer("drawer_proj_backend_aaa", wing="new_wing", room="new_room")
        assert result["success"] is True
        assert result["closets_deleted"] == 0

        closets_col = get_closets_collection(palace_path, create=False)
        assert len(closets_col.get(include=[])["ids"]) == 1

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

    def test_tool_create_tunnel_preserves_hyphenated_wings(self, monkeypatch, tmp_path):
        """Regression for #1504: ``tool_create_tunnel`` stores the wing slug
        verbatim, and both hyphen and underscore queries find the result."""
        from mempalace import mcp_server, palace_graph

        tunnel_file = tmp_path / "tunnels.json"
        monkeypatch.setattr(palace_graph, "_get_tunnel_file", lambda *a, **kw: str(tunnel_file))
        monkeypatch.setattr(
            palace_graph,
            "_legacy_tunnel_file",
            lambda: str(tmp_path / "legacy-tunnels.json"),
        )
        monkeypatch.setattr(palace_graph, "_get_collection", lambda *a, **kw: None)

        t = mcp_server.tool_create_tunnel(
            source_wing="other-wing",
            source_room="r1",
            target_wing="my-wing",
            target_room="r2",
            label="hyphen preservation",
        )

        assert t["source"]["wing"] == "other-wing"
        assert t["target"]["wing"] == "my-wing"
        assert len(mcp_server.tool_list_tunnels(wing="my-wing")) == 1
        assert len(mcp_server.tool_list_tunnels(wing="my_wing")) == 1

    def test_tool_create_tunnel_surfaces_value_error(self, monkeypatch):
        """Regression for #1473: a ValueError from create_tunnel (e.g. a
        missing room) must be returned to the caller as a clear error,
        not escape and get wrapped as the opaque 'Internal tool error'."""
        from mempalace import mcp_server

        msg = "Target room 'does-not-exist-probe' does not exist in wing 'wing_minerva'"

        def _raise(*args, **kwargs):
            raise ValueError(msg)

        monkeypatch.setattr(mcp_server, "create_tunnel", _raise)

        result = mcp_server.tool_create_tunnel(
            source_wing="wing_minerva",
            source_room="fx-invariants",
            target_wing="wing_minerva",
            target_room="does-not-exist-probe",
        )

        assert result == {"error": msg}

    # ── hallway MCP tools (mirror the tunnel pattern) ──

    def _seed_hallways(self, monkeypatch, tmp_path):
        """Point hallways resolvers at a tmp file and seed two records."""
        from mempalace import hallways

        hallway_file = tmp_path / "hallways.json"
        monkeypatch.setattr(hallways, "_get_hallway_file", lambda *a, **kw: str(hallway_file))
        monkeypatch.setattr(
            hallways,
            "_legacy_hallway_file",
            lambda: str(tmp_path / "legacy-hallways.json"),
        )
        seeded = [
            {
                "id": "hallway_wing_a_X_Y_aaaa",
                "wing": "wing_a",
                "entity_a": "X",
                "entity_b": "Y",
                "co_occurrence_count": 3,
                "rooms": ["room1"],
            },
            {
                "id": "hallway_wing_b_X_Z_bbbb",
                "wing": "wing_b",
                "entity_a": "X",
                "entity_b": "Z",
                "co_occurrence_count": 1,
                "rooms": ["room2"],
            },
        ]
        hallways._save_hallways(seeded)
        return seeded

    def test_tool_list_hallways_returns_all_without_filter(self, monkeypatch, tmp_path):
        """tool_list_hallways with no wing returns every record."""
        from mempalace import mcp_server

        seeded = self._seed_hallways(monkeypatch, tmp_path)
        result = mcp_server.tool_list_hallways()
        assert isinstance(result, list)
        assert len(result) == len(seeded)
        ids = {h["id"] for h in result}
        assert ids == {h["id"] for h in seeded}

    def test_tool_list_hallways_filters_by_wing(self, monkeypatch, tmp_path):
        """tool_list_hallways with wing returns only that wing's records."""
        from mempalace import mcp_server

        self._seed_hallways(monkeypatch, tmp_path)
        result = mcp_server.tool_list_hallways(wing="wing_a")
        assert len(result) == 1
        assert result[0]["wing"] == "wing_a"

    def test_tool_list_hallways_rejects_invalid_wing_name(self, monkeypatch, tmp_path):
        """Invalid wing names go through _sanitize_optional_name and return a
        structured error rather than crashing — mirrors tool_list_tunnels."""
        from mempalace import mcp_server

        self._seed_hallways(monkeypatch, tmp_path)
        # Forward-slash is not a valid name character per sanitize_name.
        result = mcp_server.tool_list_hallways(wing="wing/with/slashes")
        assert isinstance(result, dict)
        assert "error" in result

    def test_tool_delete_hallway_removes_existing_record(self, monkeypatch, tmp_path):
        """tool_delete_hallway removes the record and returns {deleted: True}."""
        from mempalace import mcp_server

        seeded = self._seed_hallways(monkeypatch, tmp_path)
        target_id = seeded[0]["id"]
        result = mcp_server.tool_delete_hallway(hallway_id=target_id)
        assert result == {"deleted": True}
        remaining = mcp_server.tool_list_hallways()
        assert target_id not in {h["id"] for h in remaining}

    def test_tool_delete_hallway_unknown_id_returns_false(self, monkeypatch, tmp_path):
        """Deleting an ID that doesn't exist returns {deleted: False} without error."""
        from mempalace import mcp_server

        self._seed_hallways(monkeypatch, tmp_path)
        result = mcp_server.tool_delete_hallway(hallway_id="hallway_does_not_exist")
        assert result == {"deleted": False}

    def test_tool_delete_hallway_requires_string_id(self):
        """Missing or non-string hallway_id surfaces a structured error."""
        from mempalace import mcp_server

        assert mcp_server.tool_delete_hallway(hallway_id="") == {"error": "hallway_id is required"}
        assert mcp_server.tool_delete_hallway(hallway_id=None) == {
            "error": "hallway_id is required"
        }

    def test_hallway_tools_registered_in_tools_registry(self):
        """Both new tools must appear in the public TOOLS registry so MCP clients can dispatch them."""
        from mempalace import mcp_server

        assert "mempalace_list_hallways" in mcp_server.TOOLS
        assert "mempalace_delete_hallway" in mcp_server.TOOLS
        assert (
            mcp_server.TOOLS["mempalace_list_hallways"]["handler"] is mcp_server.tool_list_hallways
        )
        assert (
            mcp_server.TOOLS["mempalace_delete_hallway"]["handler"]
            is mcp_server.tool_delete_hallway
        )

    def test_add_drawer_normal_content_single_drawer(self, monkeypatch, config, palace_path, kg):
        """Regression catch: content below CHUNK_SIZE produces exactly
        one drawer with ``chunks == 1``. Pre-#1539 contract preserved."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        result = tool_add_drawer(wing="w", room="r", content="Short content well under chunk_size.")
        assert result["success"] is True
        assert result["chunks"] == 1
        assert "chunk_ids" not in result
        _client2, col = _get_collection(palace_path)
        del _client2
        assert col.count() == 1
        assert col.get()["ids"] == [result["drawer_id"]]

    def test_add_drawer_oversized_content_chunked(self, monkeypatch, config, palace_path, kg):
        """Regression for #1539: content far above chunk_size must be
        sliced into bounded per-chunk drawers, each linked by a
        ``parent_drawer_id`` metadata field. No stored document may
        exceed the configured chunk_size."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        oversized = "X" * 10000
        result = tool_add_drawer(wing="w", room="r", content=oversized)
        assert result["success"] is True
        assert result["chunks"] > 1
        assert "chunk_ids" in result and len(result["chunk_ids"]) == result["chunks"]

        _client2, col = _get_collection(palace_path)
        del _client2
        stored = col.get()
        max_doc = max(len(d) for d in stored["documents"])
        assert max_doc <= config.chunk_size, (
            f"no stored document may exceed chunk_size={config.chunk_size}; got max={max_doc}"
        )
        # Chroma does not guarantee insertion order on a bare ``get()``;
        # sort by ``chunk_index`` before joining so the verbatim check
        # is deterministic.
        ordered = sorted(
            zip(stored["metadatas"], stored["documents"]),
            key=lambda pair: pair[0]["chunk_index"],
        )
        assert "".join(doc for _meta, doc in ordered) == oversized
        parent_ids = {m.get("parent_drawer_id") for m in stored["metadatas"]}
        assert parent_ids == {result["drawer_id"]}, (
            f"all chunks must share one parent_drawer_id; got {parent_ids}"
        )

    def test_add_drawer_oversized_idempotency_skips_duplicate_chunk_writes(
        self, monkeypatch, config, palace_path, kg
    ):
        """Re-calling with identical oversized content must not duplicate
        any drawer. Idempotency on the chunked path probes the last
        chunk id (its presence implies the whole batch committed) and
        also the legacy logical drawer_id so a pre-#1539 single-row
        write under the same logical id does not get co-resident chunk
        siblings on the next call."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        oversized = "Y" * 5000
        r1 = tool_add_drawer(wing="w", room="r", content=oversized)
        assert r1["success"] is True and r1["chunks"] > 1
        r2 = tool_add_drawer(wing="w", room="r", content=oversized)
        assert r2["success"] is True
        assert r2.get("reason") == "already_exists"

        _client2, col = _get_collection(palace_path)
        del _client2
        assert col.count() == r1["chunks"]
        # The probe must succeed against the last chunk id (atomicity
        # signal), and no row must be stored under the logical id.
        last_chunk = r1["chunk_ids"][-1]
        assert col.get(ids=[last_chunk])["ids"] == [last_chunk]
        assert col.get(ids=[r1["drawer_id"]])["ids"] == []

    def test_add_drawer_chunk_metadata_carries_parent_link(
        self, monkeypatch, config, palace_path, kg
    ):
        """Every chunk produced from oversized content must carry both
        ``chunk_index`` (0..N-1) and ``parent_drawer_id`` matching the
        logical group handle returned to the caller."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        result = tool_add_drawer(wing="w", room="r", content="Q" * 3500)
        assert result["success"] is True and result["chunks"] > 1

        _client2, col = _get_collection(palace_path)
        del _client2
        stored = col.get()
        indices = sorted(m["chunk_index"] for m in stored["metadatas"])
        assert indices == list(range(len(indices)))
        for meta in stored["metadatas"]:
            assert meta.get("parent_drawer_id") == result["drawer_id"]

    def test_add_drawer_boundary_exact_chunk_size_stays_single(
        self, monkeypatch, config, palace_path, kg
    ):
        """The ``<= chunk_size`` predicate must include the boundary:
        content of exactly chunk_size chars stays a single drawer, not
        an off-by-one chunked write."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        boundary = "Z" * config.chunk_size
        result = tool_add_drawer(wing="w", room="r", content=boundary)
        assert result["success"] is True
        assert result["chunks"] == 1
        assert "chunk_ids" not in result


def test_add_drawer_chunked_logical_id_fetches_deletes_and_lists_as_one(
    monkeypatch, config, palace_path, kg
):
    """Chunk rows are internal storage; MCP tools operate on the logical id."""
    _patch_mcp_server(monkeypatch, config, kg)
    _client, _col = _get_collection(palace_path, create=True)
    del _client

    from mempalace.mcp_server import (
        tool_add_drawer,
        tool_delete_drawer,
        tool_get_drawer,
        tool_list_drawers,
    )

    result = tool_add_drawer(wing="w", room="r", content="P" * 4000)

    assert result["success"] is True
    assert result["chunks"] > 1

    logical_id = result["drawer_id"]

    fetched = tool_get_drawer(logical_id)
    assert fetched["drawer_id"] == logical_id
    assert fetched["content"] == "P" * 4000
    assert fetched["chunks"] == result["chunks"]
    assert fetched["chunk_ids"] == result["chunk_ids"]

    listed = tool_list_drawers(wing="w", room="r")
    assert listed["total"] == 1
    assert listed["count"] == 1
    assert listed["drawers"][0]["drawer_id"] == logical_id
    assert listed["drawers"][0]["chunks"] == result["chunks"]

    deleted = tool_delete_drawer(logical_id)
    assert deleted["success"] is True
    assert deleted["chunks_deleted"] == result["chunks"]

    missing = tool_get_drawer(logical_id)
    assert "error" in missing
    assert "not found" in missing["error"].lower()


def test_update_drawer_chunked_logical_id_rewrites_group(monkeypatch, config, palace_path, kg):
    """Updating the returned logical id rewrites the underlying chunk group."""
    _patch_mcp_server(monkeypatch, config, kg)
    _client, _col = _get_collection(palace_path, create=True)
    del _client

    from mempalace.mcp_server import (
        tool_add_drawer,
        tool_get_drawer,
        tool_list_drawers,
        tool_update_drawer,
    )

    result = tool_add_drawer(wing="old", room="old_room", content="A" * 2600)
    assert result["success"] is True
    assert result["chunks"] > 1

    logical_id = result["drawer_id"]

    updated = tool_update_drawer(
        logical_id,
        content="B" * 1800,
        wing="new",
        room="new_room",
    )

    assert updated["success"] is True
    assert updated["drawer_id"] == logical_id

    fetched = tool_get_drawer(logical_id)
    assert fetched["drawer_id"] == logical_id
    assert fetched["content"] == "B" * 1800
    assert fetched["wing"] == "new"
    assert fetched["room"] == "new_room"

    listed = tool_list_drawers(wing="new", room="new_room")
    assert listed["total"] == 1
    assert listed["drawers"][0]["drawer_id"] == logical_id


class TestDeleteBySource:
    """``tool_delete_by_source`` — bulk cleanup of benchmark/test contamination (#1722)."""

    def _seed(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        # Two drawers from a "benchmark" source, one from real user data.
        tool_add_drawer(
            wing="bench",
            room="general",
            content="ShareGPT yoga retreat conversation noise number one.",
            source_file="results_mempal_hybrid_v4_session_1.jsonl",
        )
        tool_add_drawer(
            wing="bench",
            room="general",
            content="ShareGPT coding job description noise number two.",
            source_file="results_mempal_hybrid_v4_session_1.jsonl",
        )
        tool_add_drawer(
            wing="clients",
            room="webdesign",
            content="GG Sauna Dachdecker real client memory that must survive.",
            source_file="notes/clients.md",
        )

    def _seed_closets(self, palace_path):
        """Seed the AAAK index (closets) directly.

        ``tool_add_drawer`` never builds closets — those are a miner-side
        artifact — so to exercise the closet purge we add them straight to the
        collection, keyed by the same ``source_file`` the drawers use: two for
        the benchmark source, one for the real-client source.
        """
        from mempalace.palace import get_closets_collection

        closets_col = get_closets_collection(palace_path, create=True)
        closets_col.add(
            ids=["bench_closet_01", "bench_closet_02", "client_closet_01"],
            documents=[
                "topic: yoga retreat | coding job",
                "topic: more bench noise",
                "topic: GG Sauna client",
            ],
            metadatas=[
                {"source_file": "results_mempal_hybrid_v4_session_1.jsonl"},
                {"source_file": "results_mempal_hybrid_v4_session_1.jsonl"},
                {"source_file": "notes/clients.md"},
            ],
        )
        return closets_col

    def test_dry_run_reports_count_without_deleting(self, monkeypatch, config, palace_path, kg):
        self._seed(monkeypatch, config, palace_path, kg)
        from mempalace.mcp_server import tool_delete_by_source, tool_status

        result = tool_delete_by_source("results_mempal_hybrid_v4_session_1.jsonl")
        assert result["success"] is True
        assert result["dry_run"] is True
        assert result["match_count"] == 2
        assert {"wing": "bench", "room": "general"} in result["sample"]
        # Nothing removed — all three drawers still present.
        assert tool_status()["total_drawers"] == 3

    def test_dry_run_reports_closet_match_count(self, monkeypatch, config, palace_path, kg):
        """Dry run surfaces the closet blast radius (#1722) without deleting."""
        self._seed(monkeypatch, config, palace_path, kg)
        self._seed_closets(palace_path)
        from mempalace.mcp_server import tool_delete_by_source
        from mempalace.palace import get_closets_collection

        result = tool_delete_by_source("results_mempal_hybrid_v4_session_1.jsonl")
        assert result["dry_run"] is True
        assert result["closet_match_count"] == 2
        # Re-acquire: the staleness reconnect drops chromadb's path-keyed System
        # cache (#2002), so a handle taken before the call is dead by now.
        closets_col = get_closets_collection(palace_path, create=False)
        # Nothing removed — all three closets still present.
        assert len(closets_col.get(include=[])["ids"]) == 3

    def test_commit_deletes_only_matching_source(self, monkeypatch, config, palace_path, kg):
        self._seed(monkeypatch, config, palace_path, kg)
        from mempalace.mcp_server import tool_delete_by_source, tool_status

        result = tool_delete_by_source("results_mempal_hybrid_v4_session_1.jsonl", dry_run=False)
        assert result["success"] is True
        assert result["dry_run"] is False
        assert result["deleted"] == 2
        # Only the real client drawer remains.
        assert tool_status()["total_drawers"] == 1

    def test_commit_purges_matching_closets(self, monkeypatch, config, palace_path, kg):
        """Deleting by source purges the matching closets too, so the AAAK
        index keeps no stale pointers at the now-deleted drawers (#1722)."""
        self._seed(monkeypatch, config, palace_path, kg)
        self._seed_closets(palace_path)
        from mempalace.mcp_server import tool_delete_by_source
        from mempalace.palace import get_closets_collection

        result = tool_delete_by_source("results_mempal_hybrid_v4_session_1.jsonl", dry_run=False)
        assert result["success"] is True
        assert result["deleted"] == 2
        assert result["closets_deleted"] == 2
        # Re-acquire: the staleness reconnect drops chromadb's path-keyed System
        # cache (#2002), so a handle taken before the call is dead by now.
        closets_col = get_closets_collection(palace_path, create=False)
        # The two benchmark closets are gone; the real-client closet survives.
        remaining = closets_col.get(include=["metadatas"])
        sources = {m["source_file"] for m in remaining["metadatas"]}
        assert sources == {"notes/clients.md"}

    def test_no_match_is_idempotent_not_error(self, monkeypatch, config, palace_path, kg):
        self._seed(monkeypatch, config, palace_path, kg)
        from mempalace.mcp_server import tool_delete_by_source, tool_status

        result = tool_delete_by_source("does/not/exist.jsonl", dry_run=False)
        assert result["success"] is True
        assert result["deleted"] == 0
        assert tool_status()["total_drawers"] == 3

    def test_empty_source_file_rejected(self, monkeypatch, config, palace_path, kg):
        self._seed(monkeypatch, config, palace_path, kg)
        from mempalace.mcp_server import tool_delete_by_source

        result = tool_delete_by_source("   ", dry_run=False)
        assert result["success"] is False
        assert "non-empty" in result["error"]

    def test_non_string_source_rejected(self, monkeypatch, config, palace_path, kg):
        """A non-string source_file must return a clean error, not AttributeError."""
        self._seed(monkeypatch, config, palace_path, kg)
        from mempalace.mcp_server import tool_delete_by_source

        result = tool_delete_by_source(123, dry_run=False)
        assert result["success"] is False
        assert "non-empty" in result["error"]

    def test_matches_after_surrogate_normalization(self, monkeypatch, config, palace_path, kg):
        """source_file is stripped of lone surrogates on both ingest and delete,
        so a path that arrived via a cp1252 stdin (#1488) still matches."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import (
            tool_add_drawer,
            tool_delete_by_source,
            tool_status,
        )

        # Lone low surrogate embedded in the path — add_drawer strips it.
        raw_source = "noise\udce9_data.jsonl"
        tool_add_drawer(
            wing="bench",
            room="general",
            content="benchmark noise from a non-ASCII path",
            source_file=raw_source,
        )
        assert tool_status()["total_drawers"] == 1

        # Deleting with the same raw (un-stripped) string must still match.
        result = tool_delete_by_source(raw_source, dry_run=False)
        assert result["success"] is True
        assert result["deleted"] == 1
        assert tool_status()["total_drawers"] == 0

    def test_registered_and_dispatchable(self, monkeypatch, config, palace_path, kg):
        self._seed(monkeypatch, config, palace_path, kg)
        from mempalace.mcp_server import handle_request

        # Listed in tools/list
        listed = handle_request({"method": "tools/list", "id": 1, "params": {}})
        names = {t["name"] for t in listed["result"]["tools"]}
        assert "mempalace_delete_by_source" in names

        # Dispatches and defaults to dry-run (no destructive side effect)
        resp = handle_request(
            {
                "method": "tools/call",
                "id": 2,
                "params": {
                    "name": "mempalace_delete_by_source",
                    "arguments": {"source_file": "results_mempal_hybrid_v4_session_1.jsonl"},
                },
            }
        )
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["dry_run"] is True
        assert content["match_count"] == 2


# ── KG Tools ────────────────────────────────────────────────────────────
