"""MCP server tests — supersedes linkage between drawers.

A handoff checkpoint that states "for next session" goes stale the moment
the work closes in a later session, and nothing linked the closure back to
the handoff (2026-11-03 D5 incident: a wing-scoped "most recent checkpoint"
read served the stale handoff because the closure lived in another wing and
no edge connected the two). ``supersedes`` on add_drawer/checkpoint files
the new drawer with ``supersedes`` metadata and stamps every physical row
of the target with ``superseded_by`` — the stale record self-identifies on
any read that returns metadata.
"""

from _mcp_server_helpers import (
    _get_collection,
    _patch_mcp_server,
)


class TestSupersedes:
    def test_add_drawer_stamps_target_and_carries_link(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        old = tool_add_drawer(wing="w", room="handoffs", content="handoff: do X next session")
        assert old["success"] is True

        new = tool_add_drawer(
            wing="w",
            room="handoffs",
            content="closure: X done, verified",
            supersedes=old["drawer_id"],
        )
        assert new["success"] is True, new
        assert new["supersedes"] == [old["drawer_id"]]

        old_rows = col.get(ids=[old["drawer_id"]], include=["metadatas"])
        new_rows = col.get(ids=[new["drawer_id"]], include=["metadatas"])
        # The stamp must MERGE with the target's existing metadata (chroma
        # update semantics), not replace it — wing/room/filed_at survive.
        assert old_rows["metadatas"][0]["superseded_by"] == new["drawer_id"]
        assert old_rows["metadatas"][0]["wing"] == "w"
        assert old_rows["metadatas"][0]["room"] == "handoffs"
        assert old_rows["metadatas"][0]["filed_at"]
        assert new_rows["metadatas"][0]["supersedes"] == old["drawer_id"]

    def test_supersedes_chunked_target_stamps_all_chunks(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        big = "stale handoff body. " * 400  # exceeds the default chunk size
        old = tool_add_drawer(wing="w", room="handoffs", content=big)
        assert old["success"] is True
        assert old["chunks"] > 1, "premise: the fixture content actually chunks"

        new = tool_add_drawer(
            wing="w", room="handoffs", content="closure", supersedes=old["drawer_id"]
        )
        assert new["success"] is True
        chunks = col.get(where={"parent_drawer_id": old["drawer_id"]}, include=["metadatas"])
        assert chunks["ids"], "chunked target resolved no rows"
        assert all(m["superseded_by"] == new["drawer_id"] for m in chunks["metadatas"])

    def test_supersedes_unknown_target_reports_without_failing(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        res = tool_add_drawer(
            wing="w", room="r", content="closure", supersedes="drawer_does_not_exist"
        )
        assert res["success"] is True
        assert res["supersedes_unresolved"] == ["drawer_does_not_exist"]

    def test_supersedes_stamps_on_already_exists_refile(self, monkeypatch, config, palace_path, kg):
        """A re-filed identical closure (retry after a timeout, a replayed
        batch) hits add_drawer idempotency and returns already_exists — the
        stamp must still land, or a retry silently drops the linkage."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        old = tool_add_drawer(wing="w", room="handoffs", content="old handoff")
        assert old["success"] is True

        first = tool_add_drawer(
            wing="w", room="handoffs", content="closure", supersedes=old["drawer_id"]
        )
        assert first["success"] is True

        second = tool_add_drawer(
            wing="w", room="handoffs", content="closure", supersedes=old["drawer_id"]
        )
        assert second["success"] is True
        assert second["reason"] == "already_exists"
        assert second["supersedes"] == [old["drawer_id"]]

        old_rows = col.get(ids=[old["drawer_id"]], include=["metadatas"])
        assert old_rows["metadatas"][0]["superseded_by"] == first["drawer_id"]

    def test_search_results_carry_superseded_by(self, monkeypatch, config, palace_path, kg):
        """Search is the read path that surfaces stale handoffs — the
        supersession stamp must ride the hit, not just get_drawers."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer, tool_search

        old = tool_add_drawer(wing="w", room="handoffs", content="handoff: do X next session")
        new = tool_add_drawer(
            wing="w",
            room="closures",
            content="closure: X done and verified",
            supersedes=old["drawer_id"],
        )
        assert new["success"] is True

        hits = tool_search("handoff do X next session", wing="w", limit=5)
        rows = hits.get("results", hits.get("matches", []))
        assert rows, "search returned no hits for the filed handoff"
        by_id = {h["drawer_id"]: h for h in rows}
        hit = by_id[old["drawer_id"]]
        assert hit["superseded_by"] == new["drawer_id"]

    def test_supersedes_bad_shape_rejected(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        res = tool_add_drawer(wing="w", room="r", content="x", supersedes=123)
        assert res["success"] is False
        assert "supersedes" in res["error"]

    def test_checkpoint_item_passes_supersedes_through(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer, tool_checkpoint

        old = tool_add_drawer(wing="w", room="handoffs", content="old handoff")
        out = tool_checkpoint(
            items=[
                {
                    "wing": "w",
                    "room": "handoffs",
                    "content": "new session checkpoint",
                    "supersedes": old["drawer_id"],
                }
            ]
        )
        assert out["errors"] == []
        assert len(out["added"]) == 1
        new_id = out["added"][0]["drawer_id"]
        old_rows = col.get(ids=[old["drawer_id"]], include=["metadatas"])
        assert old_rows["metadatas"][0]["superseded_by"] == new_id

    def test_checkpoint_item_rejects_malformed_supersedes(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_checkpoint

        out = tool_checkpoint(
            items=[
                {"wing": "w", "room": "r", "content": "x", "supersedes": 7},
                {"wing": "w", "room": "r", "content": "y"},
            ]
        )
        assert len(out["errors"]) == 1
        assert "supersedes" in out["errors"][0]["error"]
        assert len(out["added"]) == 1, "the well-formed item must still file"
