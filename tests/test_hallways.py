"""Tests for the within-wing hallway primitive.

Hallways are bridges INSIDE a wing that connect entities (people,
projects, concepts, interests) to each other, materialized from
drawer-level co-occurrence. Two entities are linked by a hallway when
they appear together in enough drawers across the wing.

This file is RED-first. The corresponding implementation lives in
``mempalace/hallways.py`` and is written to make these tests pass.
"""

from unittest.mock import MagicMock, patch


# Mock chromadb at import time so the hallways module can be loaded even
# in environments where chromadb isn't installed. Mirrors the pattern in
# ``tests/test_palace_graph_tunnels.py``.
with patch.dict("sys.modules", {"chromadb": MagicMock()}):
    from mempalace import hallways as hallways_mod


def _use_tmp_hallway_file(monkeypatch, tmp_path):
    """Redirect hallway persistence to a per-test JSON file."""
    hallway_file = tmp_path / "hallways.json"
    monkeypatch.setattr(hallways_mod, "_HALLWAY_FILE", str(hallway_file))
    return hallway_file


def _fake_collection(drawers):
    """Build a MagicMock collection whose .get() returns the given drawer set."""
    col = MagicMock()
    metadatas = [d for d in drawers]
    ids = [f"drawer_{i}" for i in range(len(drawers))]
    col.get.return_value = {"ids": ids, "metadatas": metadatas}
    return col


# ─────────────────────────────────────────────────────────────────────────────
# Storage primitives — _load_hallways / _save_hallways
# ─────────────────────────────────────────────────────────────────────────────


class TestHallwayStorage:
    def test_load_hallways_missing_file_returns_empty_list(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        assert hallways_mod._load_hallways() == []

    def test_load_hallways_corrupt_file_returns_empty_list(self, tmp_path, monkeypatch):
        hallway_file = _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallway_file.write_text("{not valid json", encoding="utf-8")
        assert hallways_mod._load_hallways() == []

    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        sample = [
            {
                "id": "hallway_wing_aya_aya_lumi_abc12345",
                "wing": "wing_aya",
                "entity_a": "Aya",
                "entity_b": "Lumi",
                "co_occurrence_count": 47,
                "rooms": ["diary", "letters"],
                "label": "Aya ↔ Lumi (co-occur in 47 drawers across 2 rooms)",
            }
        ]
        hallways_mod._save_hallways(sample)
        assert hallways_mod._load_hallways() == sample


# ─────────────────────────────────────────────────────────────────────────────
# compute_hallways_for_wing — entity-pair co-occurrence algorithm
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeHallways:
    def test_returns_empty_for_unknown_wing(self, tmp_path, monkeypatch):
        """Wing with no drawers → no hallways, no crash."""
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection([])
        result = hallways_mod.compute_hallways_for_wing("wing_nonexistent", col=col)
        assert result == []

    def test_returns_empty_when_no_drawer_has_two_entities(self, tmp_path, monkeypatch):
        """A drawer must mention >= 2 entities to contribute a pair."""
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya"},  # only one
                {"wing": "wing_aya", "room": "diary", "entities": ""},  # none
            ]
        )
        result = hallways_mod.compute_hallways_for_wing("wing_aya", col=col)
        assert result == []

    def test_creates_hallway_for_entity_pair_when_threshold_met(self, tmp_path, monkeypatch):
        """Two entities co-occurring in >= min_count drawers → one hallway record.

        With min_count=2, Aya↔Lumi appear together in 3 drawers; that's a hallway.
        """
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi;Ever"},
                {"wing": "wing_aya", "room": "letters", "entities": "Aya;Lumi"},
            ]
        )
        result = hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=2)
        # Find the Aya↔Lumi hallway (other pairs like Aya↔Ever might also be present)
        aya_lumi = [h for h in result if {h["entity_a"], h["entity_b"]} == {"Aya", "Lumi"}]
        assert len(aya_lumi) == 1
        hallway = aya_lumi[0]
        assert hallway["wing"] == "wing_aya"
        assert hallway["co_occurrence_count"] == 3
        assert set(hallway["rooms"]) == {"diary", "letters"}

    def test_connects_person_to_concept(self, tmp_path, monkeypatch):
        """Entities aren't only people — projects/concepts/interests count too.

        The entity tag treats 'consciousness' the same as 'Aya'; both are
        just tokens in the drawer's entities field. So Aya↔consciousness is
        a valid hallway when they co-occur enough.
        """
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;consciousness"},
                {"wing": "wing_aya", "room": "research", "entities": "Aya;consciousness"},
                {"wing": "wing_aya", "room": "ideas", "entities": "Aya;consciousness;Lumi"},
            ]
        )
        result = hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=2)
        aya_consciousness = [
            h for h in result if {h["entity_a"], h["entity_b"]} == {"Aya", "consciousness"}
        ]
        assert len(aya_consciousness) == 1
        assert aya_consciousness[0]["co_occurrence_count"] == 3
        assert set(aya_consciousness[0]["rooms"]) == {"diary", "research", "ideas"}

    def test_respects_min_count_threshold(self, tmp_path, monkeypatch):
        """min_count=3 filters out pairs that only co-occur twice."""
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "letters", "entities": "Aya;Lumi"},
            ]
        )
        result = hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=3)
        assert result == []

    def test_creates_deterministic_id_per_entity_pair(self, tmp_path, monkeypatch):
        """Same wing + same entity pair → same hallway id (idempotent re-runs)."""
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "letters", "entities": "Aya;Lumi"},
            ]
        )
        first = hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=2)
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        second = hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=2)
        # Find the Aya↔Lumi record in both runs; ids must match.
        f_id = next(h["id"] for h in first if {h["entity_a"], h["entity_b"]} == {"Aya", "Lumi"})
        s_id = next(h["id"] for h in second if {h["entity_a"], h["entity_b"]} == {"Aya", "Lumi"})
        assert f_id == s_id
        assert f_id.startswith("hallway_")

    def test_entity_pair_is_symmetric(self, tmp_path, monkeypatch):
        """Drawer says 'Aya;Lumi'; another says 'Lumi;Aya' — same hallway."""
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "letters", "entities": "Lumi;Aya"},
            ]
        )
        result = hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=2)
        aya_lumi = [h for h in result if {h["entity_a"], h["entity_b"]} == {"Aya", "Lumi"}]
        # Symmetry: the two drawers count as 2 co-occurrences, not 0 (no
        # double-bookkeeping despite the swapped order).
        assert len(aya_lumi) == 1
        assert aya_lumi[0]["co_occurrence_count"] == 2

    def test_persists_to_json(self, tmp_path, monkeypatch):
        """After compute, _load_hallways() returns the new records."""
        hallway_file = _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "letters", "entities": "Aya;Lumi"},
            ]
        )
        hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=2)
        assert hallway_file.exists()
        loaded = hallways_mod._load_hallways()
        assert any({h["entity_a"], h["entity_b"]} == {"Aya", "Lumi"} for h in loaded)

    def test_tracks_rooms_across_co_occurrences(self, tmp_path, monkeypatch):
        """A hallway records the set of rooms where its entities co-occurred."""
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "letters", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
            ]
        )
        result = hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=2)
        h = next(h for h in result if {h["entity_a"], h["entity_b"]} == {"Aya", "Lumi"})
        assert set(h["rooms"]) == {"diary", "letters"}
        assert h["co_occurrence_count"] == 3  # 3 drawers, not 3 rooms

    def test_skips_sentinel_drawers(self, tmp_path, monkeypatch):
        """Sentinels exist for file_already_mined() bookkeeping. Skip them."""
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {
                    "wing": "wing_aya",
                    "room": "documents",
                    "entities": "Aya;Lumi",
                    "is_sentinel": True,
                },
                {
                    "wing": "wing_aya",
                    "room": "documents",
                    "entities": "Aya;Lumi",
                    "is_sentinel": True,
                },
            ]
        )
        result = hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=2)
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# Query API — list_hallways, delete_hallway
# ─────────────────────────────────────────────────────────────────────────────


class TestHallwayQuery:
    def test_list_hallways_returns_all_when_no_filter(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallways_mod._save_hallways(
            [
                {"id": "h1", "wing": "wing_aya", "entity_a": "Aya", "entity_b": "Lumi"},
                {"id": "h2", "wing": "wing_lumi", "entity_a": "Lumi", "entity_b": "Ever"},
            ]
        )
        assert len(hallways_mod.list_hallways()) == 2

    def test_list_hallways_filters_by_wing(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallways_mod._save_hallways(
            [
                {"id": "h1", "wing": "wing_aya", "entity_a": "Aya", "entity_b": "Lumi"},
                {"id": "h2", "wing": "wing_lumi", "entity_a": "Lumi", "entity_b": "Ever"},
            ]
        )
        result = hallways_mod.list_hallways(wing="wing_aya")
        assert len(result) == 1
        assert result[0]["id"] == "h1"

    def test_delete_hallway_removes_record(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallways_mod._save_hallways(
            [
                {"id": "h1", "wing": "wing_aya", "entity_a": "Aya", "entity_b": "Lumi"},
                {"id": "h2", "wing": "wing_aya", "entity_a": "Aya", "entity_b": "Ever"},
            ]
        )
        assert hallways_mod.delete_hallway("h1") is True
        remaining = hallways_mod._load_hallways()
        assert len(remaining) == 1
        assert remaining[0]["id"] == "h2"

    def test_delete_hallway_unknown_id_returns_false(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallways_mod._save_hallways([{"id": "h1", "wing": "wing_aya"}])
        assert hallways_mod.delete_hallway("nonexistent") is False
