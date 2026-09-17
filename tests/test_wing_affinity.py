"""Tests for wing_affinity.py and additive cross-wing search expansion.

Expansion contract (see #2341 review): the unfiltered baseline search is
always preserved. When the baseline is thin — fewer hits than requested,
or a weak top hit — the most structurally relevant wings get their own
scoped queries and their hits merge, dedupe, and rerank into the
baseline. Expansion can never remove a baseline hit.
"""

import json
import os
from unittest.mock import MagicMock, patch


import mempalace.searcher as searcher
from mempalace.palace import get_collection
from mempalace.searcher import search_memories
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


# ── Integration: additive expansion over a real palace ─────────────────
#
# Wings "alpha" and "beta" share the room name "handoff_protocols" — a
# passive same-room connection — so a query mentioning "handoff
# protocols" scores them structurally. Wing "delta" holds the drawer that
# is the best semantic match for the query. The drawers in alpha/beta are
# about gardening/cooking — deliberately far from the query's zebra/quasar
# vocabulary — so the baseline top-n contains no alpha/beta drawer.

_QUERY = "handoff protocols zebra quasar"


def _seed_expansion_palace(palace_path, wing_names=("alpha", "beta"), collection_name=None):
    """Seed a palace where scored wings differ from the best-hit wing."""
    col = get_collection(palace_path, collection_name=collection_name, create=True)
    ids, docs, metas = [], [], []
    for i, wing in enumerate(wing_names):
        for j in range(4):
            ids.append(f"{wing}_{j}")
            docs.append(
                [
                    "Compost ratios for raised beds: greens to browns, moisture checks.",
                    "Sourdough starter feeding schedule and hydration notes.",
                    "Tomato trellis pruning calendar for the season.",
                    "Cold frame ventilation habits for spring seedlings.",
                ][j]
            )
            metas.append(
                {
                    "wing": wing,
                    "room": "handoff_protocols",
                    "source_file": f"{wing}_{j}.md",
                    "filed_at": "2026-01-01T00:00:00",
                }
            )
    ids.append("delta_zebra")
    docs.append("Zebra quasar ledger calibration: the only drawer about the query terms.")
    metas.append(
        {
            "wing": "delta",
            "room": "ledger_ops",
            "source_file": "zebra.md",
            "filed_at": "2026-01-02T00:00:00",
        }
    )
    col.upsert(ids=ids, documents=docs, metadatas=metas)
    return col


def _always_thin(monkeypatch):
    """Force the thin-baseline gate open regardless of hit quality."""
    monkeypatch.setattr(searcher, "_THIN_TOP_EFFECTIVE_DISTANCE", -1.0)


def _result_ids(result):
    return {h.get("drawer_id") for h in result.get("results", [])}


class TestAdditiveExpansion:
    def test_baseline_hit_never_removed_by_expansion(self, tmp_path, monkeypatch):
        """Reviewer regression: wings A/B/C score structurally but the best
        semantic drawer sits in wing D — expansion must not remove D."""
        palace = str(tmp_path / "palace")
        _seed_expansion_palace(palace)
        _always_thin(monkeypatch)

        baseline = search_memories(_QUERY, palace, n_results=3, expand_wings=False)
        expanded = search_memories(_QUERY, palace, n_results=3, expand_wings=True)

        assert "delta_zebra" in _result_ids(baseline), "baseline must find the D hit"
        assert "delta_zebra" in _result_ids(expanded), "expansion must not drop the D hit"
        # Every baseline hit remains a result candidate — expansion is
        # additive only.
        assert _result_ids(baseline) <= _result_ids(expanded)

        info = expanded.get("wing_expansion")
        assert info is not None
        assert info["applied"] is True
        assert info["added"] >= 1
        assert set(info["wings"]) <= {"alpha", "beta"}

    def test_expansion_disabled_flag(self, tmp_path, monkeypatch):
        palace = str(tmp_path / "palace")
        _seed_expansion_palace(palace)
        _always_thin(monkeypatch)

        result = search_memories(_QUERY, palace, n_results=3, expand_wings=False)
        assert "wing_expansion" not in result

    def test_explicit_filters_skip_expansion(self, tmp_path, monkeypatch):
        """A caller that names a wing/room/source_file asked for exactly
        that scope — expansion must not silently widen it."""
        palace = str(tmp_path / "palace")
        _seed_expansion_palace(palace)
        _always_thin(monkeypatch)

        result = search_memories(_QUERY, palace, n_results=3, wing="alpha")
        assert "wing_expansion" not in result
        assert all(h["wing"] == "alpha" for h in result["results"])

    def test_healthy_baseline_skips_expansion(self, tmp_path, monkeypatch):
        """Enough hits at a good top score → expansion never fires."""
        palace = str(tmp_path / "palace")
        _seed_expansion_palace(palace)
        # Threshold so strict no hit ever reads as weak, and len(hits)
        # meets n_results, so neither thin condition holds.
        monkeypatch.setattr(searcher, "_THIN_TOP_EFFECTIVE_DISTANCE", 99.0)

        result = search_memories(_QUERY, palace, n_results=3)
        assert "wing_expansion" not in result

    def test_union_strategy_expands_with_same_scope(self, tmp_path, monkeypatch):
        """candidate_strategy='union' routes wing-scoped expansion through
        the same candidate merge as the baseline."""
        palace = str(tmp_path / "palace")
        _seed_expansion_palace(palace)
        _always_thin(monkeypatch)

        result = search_memories(
            _QUERY, palace, n_results=3, candidate_strategy="union", expand_wings=True
        )
        info = result.get("wing_expansion")
        assert info is not None and info["applied"] is True
        assert set(info["wings"]) <= {"alpha", "beta"}
        assert "delta_zebra" in _result_ids(result)

    def test_vector_disabled_fallback_expands(self, tmp_path):
        """The BM25 fallback applies the same additive expansion scope."""
        palace = str(tmp_path / "palace")
        _seed_expansion_palace(palace)

        # BM25 thin check is count-based: ask for more hits than exist.
        result = search_memories(
            _QUERY,
            palace,
            n_results=50,
            vector_disabled=True,
            expand_wings=True,
        )
        info = result.get("wing_expansion")
        assert info is not None
        assert set(info["wings"]) <= {"alpha", "beta"}
        assert "delta_zebra" in _result_ids(result)

    def test_collection_isolation(self, tmp_path):
        """Affinity scores against the opened collection only: a second
        collection with disjoint wings must not inherit the default
        collection's expansion targets. ``get_collection`` allowlists
        configured names, so the second collection is opened through the
        raw client — the same object ``search_memories`` hands to the
        affinity scorer."""
        import chromadb

        from mempalace.config import MempalaceConfig

        palace = str(tmp_path / "palace")
        _seed_expansion_palace(palace)
        raw = chromadb.PersistentClient(path=palace)
        other = raw.get_or_create_collection("other_drawers")
        ids, docs, metas = [], [], []
        for wing in ("xray", "yankee"):
            for j in range(3):
                ids.append(f"{wing}_{j}")
                docs.append(f"{wing} drawer {j} about entirely different material.")
                metas.append(
                    {
                        "wing": wing,
                        "room": "handoff_protocols",
                        "source_file": f"{wing}_{j}.md",
                        "filed_at": "2026-01-04T00:00:00",
                    }
                )
        other.upsert(ids=ids, documents=docs, metadatas=metas)

        bound = MempalaceConfig(palace_path=palace, collection_name="other_drawers")
        wings_other = expand_wings(_QUERY, col=other, config=bound)
        assert set(wings_other) <= {"xray", "yankee"}
        assert not (set(wings_other) & {"alpha", "beta"})

        # And the sqlite/config path (no explicit col) binds to the
        # configured collection name, not whichever collection was warmed.
        wings_cfg = expand_wings(
            _QUERY, config=MempalaceConfig(palace_path=palace, collection_name="other_drawers")
        )
        assert set(wings_cfg) <= {"xray", "yankee"}

    def test_sequential_palace_isolation(self, tmp_path, monkeypatch):
        """Two palaces searched in one process: the second search must not
        be served the first palace's graph from the warm cache."""
        from mempalace.palace_graph import invalidate_graph_cache

        palace_a = str(tmp_path / "palace_a")
        palace_b = str(tmp_path / "palace_b")
        _seed_expansion_palace(palace_a)
        _seed_expansion_palace(palace_b, wing_names=("omega1", "omega2"))
        _always_thin(monkeypatch)

        first = search_memories(_QUERY, palace_a, n_results=3, expand_wings=True)
        second = search_memories(_QUERY, palace_b, n_results=3, expand_wings=True)
        invalidate_graph_cache()

        first_wings = set((first.get("wing_expansion") or {}).get("wings") or [])
        second_wings = set((second.get("wing_expansion") or {}).get("wings") or [])
        assert first_wings <= {"alpha", "beta"}
        assert second_wings <= {"omega1", "omega2"}

    def test_graph_cache_keyed_by_palace_identity(self, tmp_path):
        """build_graph's warm cache keys on (palace_path, collection_name):
        sequential build_graph calls for two palaces must not collide."""
        from mempalace.config import MempalaceConfig
        from mempalace.palace_graph import build_graph, invalidate_graph_cache

        palace_a = str(tmp_path / "palace_a")
        palace_b = str(tmp_path / "palace_b")
        _seed_expansion_palace(palace_a)
        _seed_expansion_palace(palace_b, wing_names=("omega1", "omega2"))

        invalidate_graph_cache()
        nodes_a, _ = build_graph(config=MempalaceConfig(palace_path=palace_a))
        # Second call against a different palace must not serve A's graph.
        nodes_b, _ = build_graph(config=MempalaceConfig(palace_path=palace_b))
        invalidate_graph_cache()

        wings_a = {w for d in nodes_a.values() for w in d["wings"]}
        wings_b = {w for d in nodes_b.values() for w in d["wings"]}
        assert "alpha" in wings_a
        assert "omega1" not in wings_a
        assert "omega1" in wings_b
        assert "alpha" not in wings_b

    def test_explicit_tunnels_json_not_read(self, tmp_path, monkeypatch):
        """Expansion uses passive same-room connections only — an explicit
        tunnels.json record pointing at an unrelated wing must not pull
        that wing into the expansion set."""
        palace = str(tmp_path / "palace")
        col = _seed_expansion_palace(palace)
        # A quarantine wing that shares no room with the scored wings.
        col.upsert(
            ids=["quar_0"],
            documents=["Quarantined notes on unrelated matters."],
            metadatas=[
                {
                    "wing": "quarantine",
                    "room": "isolated_stuff",
                    "source_file": "quar.md",
                    "filed_at": "2026-01-03T00:00:00",
                }
            ],
        )
        # Explicit tunnel record claiming alpha connects to quarantine.
        tunnels_file = os.path.join(os.path.dirname(palace), "tunnels.json")
        with open(tunnels_file, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "id": "t1",
                        "source": {"wing": "alpha", "room": "handoff_protocols"},
                        "target": {"wing": "quarantine", "room": "isolated_stuff"},
                        "kind": "explicit",
                    }
                ],
                f,
            )
        _always_thin(monkeypatch)

        result = search_memories(_QUERY, palace, n_results=3, expand_wings=True)
        info = result.get("wing_expansion")
        assert info is not None
        assert "quarantine" not in (info["wings"] or [])

    def test_expansion_error_falls_back_to_baseline(self, tmp_path, monkeypatch):
        """A wing-scoped expansion query failing must not lose the
        baseline — the merged result still contains every baseline hit."""
        palace = str(tmp_path / "palace")
        _seed_expansion_palace(palace)
        _always_thin(monkeypatch)

        baseline = search_memories(_QUERY, palace, n_results=3, expand_wings=False)
        with (
            patch("mempalace.wing_affinity.build_graph", side_effect=RuntimeError("graph boom")),
            patch("mempalace.wing_affinity.find_tunnels", side_effect=RuntimeError("tunnels boom")),
        ):
            result = search_memories(_QUERY, palace, n_results=3, expand_wings=True)
        assert "error" not in result
        assert _result_ids(baseline) <= _result_ids(result)
