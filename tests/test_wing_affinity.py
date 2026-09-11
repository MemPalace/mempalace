"""Tests for wing_affinity.py and search wing expansion."""

from unittest.mock import MagicMock, patch


from mempalace.wing_affinity import expand_wings, score_wings


def _fake_tunnels():
    return [
        {"room": "dual_detector_docs", "wings": ["orkid", "past-performance"], "count": 100},
        {"room": "audit_report", "wings": ["orkid", "defi"], "count": 50},
    ]


def _fake_hallways():
    return [
        {
            "id": "hallway_orkid_aya_lumi_abc123",
            "wing": "orkid",
            "entity_a": "Aya",
            "entity_b": "Lumi",
            "co_occurrence_count": 5,
        },
        {
            "id": "hallway_defi_oracle_risk_xyz789",
            "wing": "defi",
            "entity_a": "oracle",
            "entity_b": "risk",
            "co_occurrence_count": 12,
        },
    ]


def _fake_graph_nodes():
    return {
        "dual_detector_docs": {
            "wings": ["orkid", "past-performance"],
            "count": 100,
            "halls": [],
            "dates": [],
        },
        "audit_report": {"wings": ["orkid", "defi"], "count": 50, "halls": [], "dates": []},
        "contracts": {"wings": ["orkid"], "count": 30, "halls": [], "dates": []},
    }


@patch("mempalace.wing_affinity.find_tunnels")
@patch("mempalace.wing_affinity.list_hallways")
@patch("mempalace.wing_affinity.build_graph")
def test_score_wings_tunnels_room_name_overlap(
    mock_build_graph, mock_list_hallways, mock_find_tunnels
):
    mock_find_tunnels.return_value = _fake_tunnels()
    mock_list_hallways.return_value = []
    mock_build_graph.return_value = (_fake_graph_nodes(), [])

    ranked = score_wings("dual detector docs", config=MagicMock())
    wings = [w for w, _ in ranked]

    assert "orkid" in wings
    assert "past-performance" in wings


@patch("mempalace.wing_affinity.find_tunnels")
@patch("mempalace.wing_affinity.list_hallways")
@patch("mempalace.wing_affinity.build_graph")
def test_score_wings_hallway_entity_overlap(
    mock_build_graph, mock_list_hallways, mock_find_tunnels
):
    mock_find_tunnels.return_value = []
    mock_list_hallways.return_value = _fake_hallways()
    mock_build_graph.return_value = ({}, [])

    ranked = score_wings("Aya and Lumi project", config=MagicMock())
    wings = [w for w, _ in ranked]

    assert "orkid" in wings


@patch("mempalace.wing_affinity.find_tunnels")
@patch("mempalace.wing_affinity.list_hallways")
@patch("mempalace.wing_affinity.build_graph")
def test_score_wings_no_match_returns_empty(
    mock_build_graph, mock_list_hallways, mock_find_tunnels
):
    mock_find_tunnels.return_value = []
    mock_list_hallways.return_value = []
    mock_build_graph.return_value = ({}, [])

    ranked = score_wings("completely unrelated topic", config=MagicMock())
    assert ranked == []


@patch("mempalace.wing_affinity.find_tunnels")
@patch("mempalace.wing_affinity.list_hallways")
@patch("mempalace.wing_affinity.build_graph")
def test_expand_wings_respects_max_wings(mock_build_graph, mock_list_hallways, mock_find_tunnels):
    mock_find_tunnels.return_value = _fake_tunnels()
    mock_list_hallways.return_value = []
    mock_build_graph.return_value = (_fake_graph_nodes(), [])

    wings = expand_wings("dual detector docs audit report", config=MagicMock(), max_wings=2)
    assert len(wings) <= 2


def test_build_where_filter_accepts_wings_list():
    from mempalace.searcher import build_where_filter

    f = build_where_filter(wings=["orkid", "past-performance"])
    assert f == {"$or": [{"wing": "orkid"}, {"wing": "past-performance"}]}


def test_build_where_filter_wing_and_wings_prefers_single_wing():
    from mempalace.searcher import build_where_filter

    f = build_where_filter(wing="orkid", wings=["past-performance"])
    assert f == {"wing": "orkid"}


def test_build_where_filter_wings_room_and_source_file():
    from mempalace.searcher import build_where_filter

    f = build_where_filter(wings=["orkid", "past-performance"], room="dual_detector_docs")
    assert "$and" in f
    assert {"room": "dual_detector_docs"} in f["$and"]
    assert {"$or": [{"wing": "orkid"}, {"wing": "past-performance"}]} in f["$and"]


@patch("mempalace.searcher._maybe_expand_wings")
def test_search_memories_passes_expand_wings_to_build_where_filter(mock_expand, tmp_path):
    from mempalace.searcher import search_memories

    mock_expand.return_value = ["orkid", "past-performance"]
    # We don't have a real palace, so expect it to fail early
    with patch("mempalace.searcher._open_search_collection") as mock_open:
        mock_open.return_value = (None, {"error": "no palace"})
        result = search_memories(
            "dual detector",
            palace_path=str(tmp_path),
            expand_wings=True,
        )
        assert result.get("error") == "no palace"
