"""MCP server tests — list_drawers page ordering."""

from _mcp_server_helpers import (
    _get_collection,
    _patch_mcp_server,
)


class TestListDrawersOrder:
    def test_list_drawers_newest_first(self, monkeypatch, config, palace_path, kg):
        """list_drawers pages newest-first. The natural row order is
        insertion order, which puts the oldest drawers on page 1 — a
        "read the most recent checkpoint" query then reads old handoffs
        while fresh records sit pages deep. Deterministic timestamps:
        filed_at is patched after filing so the assertion does not depend
        on clock granularity."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer, tool_list_drawers

        early = tool_add_drawer(wing="w", room="r", content="early record")
        late = tool_add_drawer(wing="w", room="r", content="late record")
        assert early["success"] and late["success"]
        col.update(ids=[early["drawer_id"]], metadatas=[{"filed_at": "2026-01-01T00:00:00Z"}])
        col.update(ids=[late["drawer_id"]], metadatas=[{"filed_at": "2026-06-01T00:00:00Z"}])

        listed = tool_list_drawers(wing="w", limit=10)
        ids = [d["drawer_id"] for d in listed["drawers"]]
        assert ids.index(late["drawer_id"]) < ids.index(early["drawer_id"])
