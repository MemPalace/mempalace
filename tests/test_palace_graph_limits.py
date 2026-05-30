"""Ranking and limit tests for mempalace.palace_graph."""

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


def _room_entries(room, wings, count, hall="shared"):
    wings = list(wings)
    return [
        {
            "room": room,
            "wing": wings[i % len(wings)],
            "hall": hall,
            "date": f"2026-01-{(i % 28) + 1:02d}",
        }
        for i in range(count)
    ]


with patch.dict("sys.modules", {"chromadb": MagicMock()}):
    from mempalace.palace_graph import find_tunnels, graph_stats


class TestFindTunnelRanking:
    def test_find_tunnels_sorted_by_descending_count(self):
        metadatas = []
        metadatas += _room_entries("beta", ["wing_code", "wing_project"], 5, hall="db")
        metadatas += _room_entries("alpha", ["wing_code", "wing_ops"], 3, hall="security")
        metadatas += _room_entries("gamma", ["wing_project", "wing_ops"], 2, hall="infra")
        metadatas += _room_entries("solo", ["wing_code"], 6, hall="misc")

        tunnels = find_tunnels(col=_make_fake_collection(metadatas))

        assert [t["room"] for t in tunnels[:3]] == ["beta", "alpha", "gamma"]
        assert [t["count"] for t in tunnels[:3]] == [5, 3, 2]

    def test_find_tunnels_caps_results_at_fifty(self):
        metadatas = []
        for i in range(55):
            metadatas.extend(
                _room_entries(
                    f"room_{i}",
                    [f"wing_a_{i}", f"wing_b_{i}"],
                    2,
                    hall="shared",
                )
            )

        tunnels = find_tunnels(col=_make_fake_collection(metadatas))

        assert len(tunnels) == 50
        assert all(len(t["wings"]) >= 2 for t in tunnels)


class TestGraphStatsDetails:
    def test_graph_stats_counts_unique_rooms_per_wing(self):
        metadatas = []
        metadatas += _room_entries("auth", ["wing_code", "wing_project"], 4, hall="security")
        metadatas += _room_entries("deploy", ["wing_code", "wing_ops"], 3, hall="infra")
        metadatas += _room_entries("roadmap", ["wing_project"], 2, hall="planning")

        stats = graph_stats(col=_make_fake_collection(metadatas))

        assert stats["rooms_per_wing"]["wing_code"] == 2
        assert stats["rooms_per_wing"]["wing_project"] == 2
        assert stats["rooms_per_wing"]["wing_ops"] == 1
        assert stats["tunnel_rooms"] == 2

    def test_graph_stats_top_tunnels_only_lists_multi_wing_rooms_and_caps_at_ten(self):
        metadatas = []
        for i in range(11):
            metadatas.extend(
                _room_entries(
                    f"tunnel_{i}",
                    ["wing_code", f"wing_{i}"],
                    2,
                    hall="shared",
                )
            )
        metadatas.extend(_room_entries("solo", ["wing_code"], 20, hall="misc"))

        stats = graph_stats(col=_make_fake_collection(metadatas))
        top_rooms = [t["room"] for t in stats["top_tunnels"]]

        assert len(stats["top_tunnels"]) == 10
        assert "solo" not in top_rooms
        assert all(len(t["wings"]) >= 2 for t in stats["top_tunnels"])
