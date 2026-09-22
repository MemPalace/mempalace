"""Tests for the bulk drawer tools (mempalace_get_drawers / mempalace_delete_drawers).

The bulk tools sit alongside the singular get and delete tools, so the
existing schemas and responses are untouched. An accepted call always
returns a ``results`` list, even for one id, and a missing id is an error
slot rather than a failed batch.
"""

import json

import pytest

from mempalace import mcp_server
from mempalace import service


class _CountingCollection:
    """Counts ``get`` calls and forwards everything else to the real collection."""

    def __init__(self, inner):
        self._inner = inner
        self.gets = 0

    def get(self, *args, **kwargs):
        self.gets += 1
        return self._inner.get(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


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

    def test_direct_ids_resolve_in_one_read(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        ids = []
        for i in range(3):
            added = mcp_server.tool_add_drawer(
                wing="test",
                room="bulk_get",
                content=f"direct row {i}",
            )
            ids.append(added["drawer_id"])
        counter = _CountingCollection(mcp_server._get_collection())
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: counter)

        result = mcp_server.tool_get_drawers(ids)

        assert counter.gets == 1
        assert result["errors"] == 0
        assert [item["drawer_id"] for item in result["results"]] == ids

    def test_mixed_ids_resolve_in_two_reads(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        single = mcp_server.tool_add_drawer(wing="test", room="bulk_get", content="one row")
        chunked = mcp_server.tool_add_drawer(wing="test", room="bulk_get", content="q" * 2000)
        assert chunked["chunks"] > 1
        single_payload = mcp_server.tool_get_drawer(single["drawer_id"])
        logical_payload = mcp_server.tool_get_drawer(chunked["drawer_id"])
        chunk_payload = mcp_server.tool_get_drawer(chunked["chunk_ids"][0])
        counter = _CountingCollection(mcp_server._get_collection())
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: counter)

        result = mcp_server.tool_get_drawers(
            [
                single["drawer_id"],
                chunked["drawer_id"],
                chunked["chunk_ids"][0],
                "drawer_missing_none",
            ]
        )

        assert counter.gets == 2
        assert result["errors"] == 1
        assert result["results"][0] == single_payload
        assert result["results"][1] == logical_payload
        assert result["results"][2] == chunk_payload
        assert result["results"][3]["drawer_id"] == "drawer_missing_none"

    def test_payload_error_stays_on_that_item(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        first = mcp_server.tool_add_drawer(wing="test", room="bulk_get", content="alpha content")
        second = mcp_server.tool_add_drawer(wing="test", room="bulk_get", content="beta content")
        real_payload = mcp_server._drawer_payload

        def boom(record):
            if record["content"] == "alpha content":
                raise RuntimeError("bad payload")
            return real_payload(record)

        monkeypatch.setattr(mcp_server, "_drawer_payload", boom)
        result = mcp_server.tool_get_drawers([first["drawer_id"], second["drawer_id"]])

        assert result["count"] == 2
        assert result["errors"] == 1
        assert result["results"][0]["drawer_id"] == first["drawer_id"]
        assert "bad payload" in result["results"][0]["error"]
        assert result["results"][1]["content"] == "beta content"


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

    def test_resolves_many_ids_in_one_read(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        ids = []
        for i in range(3):
            added = mcp_server.tool_add_drawer(
                wing="test",
                room="bulk_del",
                content=f"delete direct {i}",
            )
            ids.append(added["drawer_id"])
        counter = _CountingCollection(mcp_server._get_collection())
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: counter)

        result = mcp_server.tool_delete_drawers(ids)

        assert counter.gets == 1
        assert result["deleted"] == 3
        assert result["errors"] == 0

    def test_chunk_id_deletes_one_row_like_the_singular_tool(
        self, monkeypatch, config, collection, kg
    ):
        """A physical chunk id removes that row, not the rest of the group.

        That is what the singular delete does: resolution hits the chunk
        row directly. The logical handle is what removes the whole group.
        """
        _patch_mcp_server(monkeypatch, config, kg)
        first = mcp_server.tool_add_drawer(wing="test", room="bulk_del", content="a" * 2000)
        second = mcp_server.tool_add_drawer(wing="test", room="bulk_del", content="b" * 2000)
        assert first["chunks"] > 1
        assert second["chunks"] > 1

        singular = mcp_server.tool_delete_drawer(first["chunk_ids"][0])
        bulk = mcp_server.tool_delete_drawers([second["chunk_ids"][0]])

        assert singular["success"] is True
        assert singular["chunks_deleted"] == 1
        assert singular["deleted_ids"] == [first["chunk_ids"][0]]
        assert bulk["deleted"] == 1
        assert bulk["results"][0]["chunks_deleted"] == 1
        assert bulk["results"][0]["deleted_ids"] == [second["chunk_ids"][0]]

        for added in (first, second):
            raw = collection.get(ids=added["chunk_ids"], include=[])
            remaining = raw.get("ids", []) if isinstance(raw, dict) else raw.ids
            assert added["chunk_ids"][0] not in list(remaining)
            assert added["chunk_ids"][1] in list(remaining)

    def test_purges_closets_for_the_deleted_source_only(
        self, monkeypatch, config, palace_path, collection, kg
    ):
        """Bulk delete drops closets for the removed drawer's source file.

        A closet for a different source stays. The count is the number of
        matching closets removed, the same field the singular delete returns.
        """
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.palace import get_closets_collection

        removed = mcp_server.tool_add_drawer(
            wing="test",
            room="bulk_del",
            content="drawer whose source closets should go",
            source_file="auth.py",
        )
        kept = mcp_server.tool_add_drawer(
            wing="test",
            room="bulk_del",
            content="drawer whose source closets should stay",
            source_file="db.py",
        )
        closets = get_closets_collection(palace_path, create=True)
        closets.add(
            ids=["closet_auth", "closet_db"],
            documents=["auth index card", "db index card"],
            metadatas=[{"source_file": "auth.py"}, {"source_file": "db.py"}],
        )

        result = mcp_server.tool_delete_drawers([removed["drawer_id"]])

        assert result["deleted"] == 1
        assert result["results"][0]["closets_deleted"] == 1
        assert "error" not in mcp_server.tool_get_drawer(kept["drawer_id"])

        # Re-acquire: the purge drops the path-keyed collection cache, so
        # the handle taken before the call is stale.
        closets = get_closets_collection(palace_path, create=False)
        assert closets.get(include=[])["ids"] == ["closet_db"]


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
