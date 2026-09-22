"""Tests for the bulk drawer tools (mempalace_get_drawers /
mempalace_delete_drawers) added by #2558.

The bulk tools sit alongside the singular get/delete tools (option B in the
issue) so the existing schemas and responses are untouched. Their response
shape is uniform — always a ``results`` list, even for one id — and partial
failure is reported per item: a missing id never fails the batch.
"""

import json

import pytest

from mempalace import mcp_server
from mempalace import service


def _patch_mcp_server(monkeypatch, config, kg):
    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda: kg)


# ── Registration ──────────────────────────────────────────────────────────


def test_bulk_tools_registered_in_tools():
    for name in ("mempalace_get_drawers", "mempalace_delete_drawers"):
        assert name in mcp_server.TOOLS
        schema = mcp_server.TOOLS[name]["input_schema"]
        assert schema["type"] == "object"
        prop = schema["properties"]["drawer_ids"]
        assert prop["type"] == "array"
        assert prop["items"] == {"type": "string"}
        assert schema["required"] == ["drawer_ids"]


def test_bulk_tools_classified_in_service():
    assert service.classify_tool("mempalace_get_drawers") == "read"
    assert service.classify_tool("mempalace_delete_drawers") == "write"


def test_bulk_delete_is_mutating_and_vector_gated():
    # Peer-writer + read-only refusal both key off _MUTATING_TOOLS.
    assert "mempalace_delete_drawers" in mcp_server._MUTATING_TOOLS
    # The diverged-HNSW gate covers every vector write; bulk delete reaches
    # the chroma vector segment.
    assert "mempalace_delete_drawers" in mcp_server._VECTOR_WRITE_TOOLS
    # ...and it stays a subset of the mutating set.
    assert mcp_server._VECTOR_WRITE_TOOLS <= mcp_server._MUTATING_TOOLS


# ── Input validation (no palace access required) ──────────────────────────


class TestInputValidation:
    @pytest.mark.parametrize("tool", [mcp_server.tool_get_drawers, mcp_server.tool_delete_drawers])
    def test_rejects_non_list(self, tool):
        result = tool("not-a-list")
        assert "error" in result
        assert "results" not in result

    @pytest.mark.parametrize("tool", [mcp_server.tool_get_drawers, mcp_server.tool_delete_drawers])
    def test_rejects_empty_list(self, tool):
        assert "error" in tool([])

    @pytest.mark.parametrize("tool", [mcp_server.tool_get_drawers, mcp_server.tool_delete_drawers])
    def test_rejects_oversized_list(self, tool):
        ids = [f"drawer_w_r_{i:05d}" for i in range(501)]
        result = tool(ids)
        assert "error" in result
        assert "500" in result["error"]


# ── Bulk get ──────────────────────────────────────────────────────────────


class TestGetDrawers:
    def test_fetches_multiple_drawers_in_input_order(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)

        filed = [
            mcp_server.tool_add_drawer(
                wing="test",
                room="bulk_get",
                content=f"bulk fetch item {i} — distinct content",
            )
            for i in range(3)
        ]
        ids = [r["drawer_id"] for r in filed]

        result = mcp_server.tool_get_drawers(ids)

        assert "error" not in result
        assert result["count"] == 3
        assert result["errors"] == 0
        assert [r["drawer_id"] for r in result["results"]] == ids
        for r in result["results"]:
            assert "error" not in r
            assert r["content"]
            assert r["wing"] == "test"
            assert r["room"] == "bulk_get"

    def test_each_item_matches_the_singular_tool(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        added = mcp_server.tool_add_drawer(
            wing="test",
            room="bulk_get",
            content="content that must be verbatim",
        )
        drawer_id = added["drawer_id"]

        single = mcp_server.tool_get_drawer(drawer_id)
        bulk = mcp_server.tool_get_drawers([drawer_id])

        # Uniform shape: always a list, even for one id.
        assert bulk["count"] == 1
        assert bulk["results"][0] == single

    def test_resolves_chunk_groups_like_singular_tool(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        # Content well over the default 800-char chunk size splits into a
        # logical group of _chunk_NNNNNN rows.
        content = "z" * 2000
        added = mcp_server.tool_add_drawer(wing="test", room="bulk_get", content=content)
        assert added["chunks"] > 1

        # The logical group handle resolves to the reassembled drawer — the
        # same payload the singular tool returns for it.
        via_handle = mcp_server.tool_get_drawers([added["drawer_id"]])
        assert via_handle["errors"] == 0
        payload = via_handle["results"][0]
        assert payload["drawer_id"] == added["drawer_id"]
        assert payload["content"] == content
        assert payload["chunks"] == added["chunks"]
        assert payload == mcp_server.tool_get_drawer(added["drawer_id"])

        # A physical chunk id resolves exactly the way the singular tool
        # resolves it (the chunk row itself — reassembly is only via the
        # group handle), so the bulk path never diverges from the singular
        # one.
        chunk_id = added["chunk_ids"][0]
        bulk = mcp_server.tool_get_drawers([chunk_id])
        assert bulk["results"][0] == mcp_server.tool_get_drawer(chunk_id)

    def test_missing_ids_reported_per_item_not_batch(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        added = mcp_server.tool_add_drawer(
            wing="test",
            room="bulk_get",
            content="one real drawer",
        )

        result = mcp_server.tool_get_drawers([added["drawer_id"], "drawer_missing_none"])

        assert result["count"] == 2
        assert result["errors"] == 1
        assert "error" not in result["results"][0]
        assert result["results"][1]["drawer_id"] == "drawer_missing_none"
        assert "not found" in result["results"][1]["error"].lower()

    def test_all_missing_ids_do_not_fail(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        result = mcp_server.tool_get_drawers(["drawer_a", "drawer_b"])
        assert result["count"] == 2
        assert result["errors"] == 2
        assert all("error" in r for r in result["results"])


# ── Bulk delete ───────────────────────────────────────────────────────────


class TestDeleteDrawers:
    def test_deletes_multiple_drawers_in_input_order(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)

        filed = [
            mcp_server.tool_add_drawer(
                wing="test",
                room="bulk_del",
                content=f"bulk delete item {i} — distinct content",
            )
            for i in range(3)
        ]
        ids = [r["drawer_id"] for r in filed]

        result = mcp_server.tool_delete_drawers(ids)

        assert "error" not in result
        assert result["count"] == 3
        assert result["deleted"] == 3
        assert result["errors"] == 0
        assert [r["drawer_id"] for r in result["results"]] == ids
        for r in result["results"]:
            assert "error" not in r
            assert r["chunks_deleted"] >= 1

        # Every drawer is actually gone.
        gone = mcp_server.tool_get_drawers(ids)
        assert gone["errors"] == 3

    def test_deletes_chunk_groups_whole(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        content = "y" * 2000
        added = mcp_server.tool_add_drawer(wing="test", room="bulk_del", content=content)
        assert added["chunks"] > 1

        result = mcp_server.tool_delete_drawers([added["drawer_id"]])
        assert result["deleted"] == 1
        assert result["results"][0]["chunks_deleted"] == added["chunks"]

        # The logical group is gone and no chunk row survived.
        gone = mcp_server.tool_get_drawer(added["drawer_id"])
        assert "error" in gone
        raw = collection.get(ids=added["chunk_ids"], include=[])
        remaining = raw.get("ids", []) if isinstance(raw, dict) else raw.ids
        assert list(remaining) == []

    def test_mixed_present_and_missing_reports_per_item(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        added = mcp_server.tool_add_drawer(
            wing="test",
            room="bulk_del",
            content="one real drawer",
        )

        result = mcp_server.tool_delete_drawers([added["drawer_id"], "drawer_missing_none"])

        assert result["count"] == 2
        assert result["deleted"] == 1
        assert result["errors"] == 1
        assert "error" not in result["results"][0]
        assert result["results"][1]["drawer_id"] == "drawer_missing_none"
        assert "not found" in result["results"][1]["error"].lower()

    def test_oversized_input_fails_before_any_delete(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        added = mcp_server.tool_add_drawer(
            wing="test",
            room="bulk_del",
            content="must survive the oversized call",
        )
        ids = [added["drawer_id"]] + [f"drawer_pad_{i:05d}" for i in range(501)]

        result = mcp_server.tool_delete_drawers(ids)
        assert "error" in result
        assert "500" in result["error"]

        # Nothing was deleted: validation rejects the whole call.
        assert "error" not in mcp_server.tool_get_drawer(added["drawer_id"])


# ── Protocol dispatch (tools/call with an array argument) ─────────────────


def _dispatch(server, name, arguments, req_id=1):
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    return response, payload


class TestProtocolDispatch:
    def test_bulk_get_dispatch_with_array(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        added = mcp_server.tool_add_drawer(
            wing="test",
            room="bulk_proto",
            content="filed for protocol dispatch",
        )

        response, payload = _dispatch(
            mcp_server, "mempalace_get_drawers", {"drawer_ids": [added["drawer_id"]]}
        )
        assert "error" not in response
        assert payload["count"] == 1
        assert payload["results"][0]["drawer_id"] == added["drawer_id"]

    def test_bulk_delete_dispatch_with_array(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        added = mcp_server.tool_add_drawer(
            wing="test",
            room="bulk_proto",
            content="filed for protocol dispatch",
        )

        response, payload = _dispatch(
            mcp_server, "mempalace_delete_drawers", {"drawer_ids": [added["drawer_id"]]}
        )
        assert "error" not in response
        assert payload["deleted"] == 1
        assert payload["errors"] == 0

        gone = mcp_server.tool_get_drawer(added["drawer_id"])
        assert "error" in gone
