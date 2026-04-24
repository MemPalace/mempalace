"""Tests for mempalace.palace_graph — graph traversal layer.

All ChromaDB access is mocked — no real database needed.
"""

from unittest.mock import MagicMock, patch


def _make_fake_collection(metadatas, ids=None):
    """Create a mock collection that returns the given metadata in batches."""
    if ids is None:
        ids = [f"id_{i}" for i in range(len(metadatas))]

    col = MagicMock()
    col.count.return_value = len(metadatas)

    def fake_get(limit=1000, offset=0, include=None):
        batch_meta = metadatas[offset : offset + limit]
        batch_ids = ids[offset : offset + limit]
        return {"ids": batch_ids, "metadatas": batch_meta}

    col.get.side_effect = fake_get
    return col


# Patch chromadb at import time so palace_graph can be imported
with patch.dict("sys.modules", {"chromadb": MagicMock()}):
    from mempalace.palace_graph import (
        _fuzzy_match,
        build_graph,
        find_tunnels,
        graph_stats,
        invalidate_graph_cache,
        traverse,
    )


# --- build_graph ---


class TestBuildGraph:
    def setup_method(self):
        invalidate_graph_cache()

    def test_empty_collection(self):
        col = _make_fake_collection([])
        nodes, edges = build_graph(col=col)
        assert nodes == {}
        assert edges == []

    def test_falsy_collection(self):
        """When col is explicitly falsy, build_graph returns empty."""
        nodes, edges = build_graph(col=0)
        assert nodes == {}
        assert edges == []

    def test_single_wing_no_edges(self):
        col = _make_fake_collection(
            [
                {"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"},
                {"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-02"},
            ]
        )
        nodes, edges = build_graph(col=col)
        assert "auth" in nodes
        assert nodes["auth"]["count"] == 2
        assert edges == []

    def test_multi_wing_creates_edges(self):
        col = _make_fake_collection(
            [
                {
                    "room": "chromadb",
                    "wing": "wing_code",
                    "hall": "databases",
                    "date": "2026-01-01",
                },
                {
                    "room": "chromadb",
                    "wing": "wing_project",
                    "hall": "databases",
                    "date": "2026-01-02",
                },
            ]
        )
        nodes, edges = build_graph(col=col)
        assert "chromadb" in nodes
        assert len(edges) == 1
        assert edges[0]["wing_a"] == "wing_code"
        assert edges[0]["wing_b"] == "wing_project"
        assert edges[0]["hall"] == "databases"

    def test_general_room_excluded(self):
        col = _make_fake_collection(
            [
                {"room": "general", "wing": "wing_code", "hall": "misc", "date": ""},
            ]
        )
        nodes, edges = build_graph(col=col)
        assert "general" not in nodes

    def test_missing_wing_excluded(self):
        col = _make_fake_collection(
            [
                {"room": "orphan", "wing": "", "hall": "misc", "date": ""},
            ]
        )
        nodes, edges = build_graph(col=col)
        assert "orphan" not in nodes

    def test_dates_capped_at_five(self):
        col = _make_fake_collection(
            [
                {"room": "busy", "wing": "w", "hall": "h", "date": f"2026-01-{i:02d}"}
                for i in range(1, 10)
            ]
        )
        nodes, _ = build_graph(col=col)
        assert len(nodes["busy"]["dates"]) <= 5

    def test_cache_returns_same_result(self):
        """Second call within TTL returns cached nodes without re-scanning.

        The cache intentionally ignores col/config args when warm — this is
        correct for the MCP server's single-palace use case. Callers that
        switch collections must call invalidate_graph_cache() first.
        """
        col = _make_fake_collection(
            [{"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"}]
        )
        nodes1, edges1 = build_graph(col=col)
        # Second call with a *different* collection — should still return cached result
        col2 = _make_fake_collection([])
        nodes2, edges2 = build_graph(col=col2)
        assert nodes1 == nodes2
        assert edges1 == edges2

    def test_invalidate_clears_cache(self):
        """invalidate_graph_cache() forces a fresh scan on next call."""
        col = _make_fake_collection(
            [{"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"}]
        )
        build_graph(col=col)
        invalidate_graph_cache()
        col_empty = _make_fake_collection([])
        nodes, edges = build_graph(col=col_empty)
        assert nodes == {}
        assert edges == []


# --- traverse ---


class TestTraverse:
    def setup_method(self):
        invalidate_graph_cache()

    def _build_col(self):
        return _make_fake_collection(
            [
                {"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"},
                {"room": "login", "wing": "wing_code", "hall": "security", "date": "2026-01-01"},
                {"room": "deploy", "wing": "wing_ops", "hall": "infra", "date": "2026-01-01"},
            ]
        )

    def test_traverse_known_room(self):
        col = self._build_col()
        result = traverse("auth", col=col)
        assert isinstance(result, list)
        rooms = [r["room"] for r in result]
        assert "auth" in rooms
        # login shares wing_code with auth
        assert "login" in rooms

    def test_traverse_unknown_room(self):
        col = self._build_col()
        result = traverse("nonexistent", col=col)
        assert isinstance(result, dict)
        assert "error" in result
        assert "suggestions" in result

    def test_traverse_max_hops(self):
        col = self._build_col()
        result = traverse("auth", col=col, max_hops=0)
        # Only the start room itself at hop 0
        assert len(result) == 1
        assert result[0]["room"] == "auth"


# --- find_tunnels ---


class TestFindTunnels:
    def setup_method(self):
        invalidate_graph_cache()

    def _build_tunnel_col(self):
        return _make_fake_collection(
            [
                {"room": "chromadb", "wing": "wing_code", "hall": "db", "date": "2026-01-01"},
                {"room": "chromadb", "wing": "wing_project", "hall": "db", "date": "2026-01-02"},
                {"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"},
            ]
        )

    def test_find_all_tunnels(self):
        col = self._build_tunnel_col()
        tunnels = find_tunnels(col=col)
        assert len(tunnels) == 1
        assert tunnels[0]["room"] == "chromadb"

    def test_find_tunnels_with_wing_filter(self):
        col = self._build_tunnel_col()
        tunnels = find_tunnels(wing_a="wing_code", col=col)
        assert len(tunnels) == 1

    def test_find_tunnels_no_match(self):
        col = self._build_tunnel_col()
        tunnels = find_tunnels(wing_a="wing_nonexistent", col=col)
        assert tunnels == []

    def test_find_tunnels_both_wings(self):
        col = self._build_tunnel_col()
        tunnels = find_tunnels(wing_a="wing_code", wing_b="wing_project", col=col)
        assert len(tunnels) == 1
        assert tunnels[0]["room"] == "chromadb"


# --- graph_stats ---


class TestGraphStats:
    def setup_method(self):
        invalidate_graph_cache()

    def test_empty_graph(self):
        col = _make_fake_collection([])
        stats = graph_stats(col=col)
        assert stats["total_rooms"] == 0
        assert stats["tunnel_rooms"] == 0
        assert stats["total_edges"] == 0

    def test_stats_with_data(self):
        col = _make_fake_collection(
            [
                {"room": "chromadb", "wing": "wing_code", "hall": "db", "date": "2026-01-01"},
                {"room": "chromadb", "wing": "wing_project", "hall": "db", "date": "2026-01-02"},
                {"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"},
            ]
        )
        stats = graph_stats(col=col)
        assert stats["total_rooms"] == 2
        assert stats["tunnel_rooms"] == 1
        assert stats["total_edges"] == 1
        assert "wing_code" in stats["rooms_per_wing"]


# --- _fuzzy_match ---


class TestFuzzyMatch:
    def test_exact_substring(self):
        nodes = {"chromadb-setup": {}, "auth-module": {}, "deploy-config": {}}
        result = _fuzzy_match("chromadb", nodes)
        assert "chromadb-setup" in result

    def test_partial_word_match(self):
        nodes = {"chromadb-setup": {}, "auth-module": {}, "deploy-config": {}}
        result = _fuzzy_match("auth", nodes)
        assert "auth-module" in result

    def test_no_match(self):
        nodes = {"chromadb-setup": {}, "auth-module": {}}
        result = _fuzzy_match("zzzzz", nodes)
        assert result == []

    def test_hyphenated_query(self):
        nodes = {"riley-college-apps": {}, "college-prep": {}}
        result = _fuzzy_match("riley-college", nodes)
        assert "riley-college-apps" in result

    def test_max_results(self):
        nodes = {f"room-{i}": {} for i in range(20)}
        result = _fuzzy_match("room", nodes, n=3)
        assert len(result) <= 3
