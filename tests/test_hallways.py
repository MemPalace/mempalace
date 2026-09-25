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
    """Redirect both the hallway-file resolver and the legacy-file check at the
    tmp_path so existing tests stay in the configured-path branch and don't
    accidentally trip the new legacy-file warning branch in _load_hallways.
    Mirrors the analogous helper in ``tests/test_palace_graph_tunnels.py``.
    """
    hallway_file = tmp_path / "hallways.json"
    monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **kw: str(hallway_file))
    monkeypatch.setattr(
        hallways_mod,
        "_legacy_hallway_file",
        lambda: str(tmp_path / "legacy-hallways.json"),
    )
    return hallway_file


def _fake_collection(drawers):
    """Build a MagicMock collection over ``drawers`` that supports the paginated
    fetch (``count()`` + ``get(limit=, offset=)``) that compute_hallways_for_wing
    uses to stay under SQLite's variable limit (#1619)."""
    col = MagicMock()
    metas = [d for d in drawers]
    col.count.return_value = len(metas)

    def _get(limit=None, offset=0, include=None, where=None, ids=None, **kwargs):
        page = metas[offset : offset + limit] if limit is not None else metas
        return {
            "ids": [f"drawer_{i}" for i in range(offset, offset + len(page))],
            "metadatas": page,
        }

    col.get.side_effect = _get
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
    def test_explicit_config_scopes_persistence_to_selected_palace(self, tmp_path):
        from mempalace.config import MempalaceConfig

        default_cfg = MempalaceConfig(palace_path=tmp_path / "default" / "palace")
        selected_cfg = MempalaceConfig(palace_path=tmp_path / "selected" / "palace")
        col = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "letters", "entities": "Aya;Lumi"},
            ]
        )

        created = hallways_mod.compute_hallways_for_wing("wing_aya", col=col, config=selected_cfg)

        assert len(created) == 1
        assert hallways_mod.list_hallways(config=selected_cfg) == created
        assert hallways_mod.list_hallways(config=default_cfg) == []

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

    def test_list_hallways_file_order_by_default(self, tmp_path, monkeypatch):
        """The miner calls ``list_hallways(config=...)`` with no ``sort`` and
        relies on store (file) order so ``entity_tunnels_for_wing``'s
        first-seen display map picks the first-seen raw-wing casing, not the
        highest-count one. Seed two records for the *same* normalized wing
        (``winga``) with different co-occurrence counts AND different raw
        casings, written to the store in a known file order where the
        LOWEST-count record is first. ``list_hallways()`` (no ``sort``) must
        preserve that file order — the highest-count record must NOT jump to
        the front. This is the pre-PR contract the unconditional-sort
        regression at #2429 broke.
        """
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallways_mod._save_hallways(
            [
                # file order: LOW count, lowercase casing — first in the store
                {
                    "id": "h_low",
                    "wing": "winga",
                    "entity_a": "E",
                    "entity_b": "F",
                    "co_occurrence_count": 1,
                    "rooms": ["room1"],
                },
                # HIGHER count, different raw casing — second in the store
                {
                    "id": "h_high",
                    "wing": "WingA",
                    "entity_a": "E",
                    "entity_b": "G",
                    "co_occurrence_count": 99,
                    "rooms": ["room2"],
                },
            ]
        )
        result = hallways_mod.list_hallways()
        assert [r["id"] for r in result] == ["h_low", "h_high"], (
            "list_hallways() with no sort must return store (file) order, not strongest-first"
        )

    def test_list_hallways_sort_flag_strongest_first(self, tmp_path, monkeypatch):
        """``sort=True`` is the explicit strongest-first path the MCP read
        tool (``tool_list_hallways``) uses before it applies its limit
        (#2327). Same mixed-casing seed as the file-order test; the
        highest-co-occurrence record must come first.
        """
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallways_mod._save_hallways(
            [
                {
                    "id": "h_low",
                    "wing": "winga",
                    "entity_a": "E",
                    "entity_b": "F",
                    "co_occurrence_count": 1,
                    "rooms": ["room1"],
                },
                {
                    "id": "h_high",
                    "wing": "WingA",
                    "entity_a": "E",
                    "entity_b": "G",
                    "co_occurrence_count": 99,
                    "rooms": ["room2"],
                },
            ]
        )
        result = hallways_mod.list_hallways(sort=True)
        assert [r["id"] for r in result] == ["h_high", "h_low"], (
            "list_hallways(sort=True) must return strongest-first"
        )

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

    def test_delete_hallway_uses_selected_palace_config(self, tmp_path):
        from mempalace.config import MempalaceConfig

        default_cfg = MempalaceConfig(palace_path=tmp_path / "default" / "palace")
        selected_cfg = MempalaceConfig(palace_path=tmp_path / "selected" / "palace")
        record = {"id": "h1", "wing": "wing_aya"}
        hallways_mod._save_hallways([record], default_cfg)
        hallways_mod._save_hallways([record], selected_cfg)

        assert hallways_mod.delete_hallway("h1", config=selected_cfg) is True
        assert hallways_mod.list_hallways(config=selected_cfg) == []
        assert hallways_mod.list_hallways(config=default_cfg) == [record]


# ─────────────────────────────────────────────────────────────────────────────
# L7 dynamics integration — hallway records carry strength/stability/etc
# ─────────────────────────────────────────────────────────────────────────────


class TestHallwayDynamicsIntegration:
    """Hallway records produced by ``compute_hallways_for_wing`` must carry
    the L7 dynamics fields (strength, stability, last_activated, access_count)
    so the living-connection math in ``mempalace.dynamics`` can operate on
    them. Plus: recomputing the same wing must PRESERVE accumulated dynamics
    rather than reset them — otherwise every mine wipes the connection
    weights and L7 is undermined."""

    def test_new_hallway_record_carries_all_dynamics_fields(self, tmp_path, monkeypatch):
        from mempalace.dynamics import DEFAULT_STABILITY, DEFAULT_STRENGTH

        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "letters", "entities": "Aya;Lumi"},
            ]
        )
        created = hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=2)
        assert created, "expected at least one hallway record"
        for h in created:
            assert h["strength"] == DEFAULT_STRENGTH, (
                f"new hallway should carry default strength; got {h}"
            )
            assert h["stability"] == DEFAULT_STABILITY, (
                f"new hallway should carry default stability; got {h}"
            )
            assert h["access_count"] == 0, f"new hallway should start at access_count=0; got {h}"
            assert "last_activated" in h, f"new hallway must carry last_activated; got {h}"
            # last_activated should anchor to created_at so decay starts from
            # creation, not from recompute-time.
            assert h["last_activated"] == h["created_at"]

    def test_recompute_preserves_accumulated_strength(self, tmp_path, monkeypatch):
        """If a hallway has been potentiated through use (strength > default),
        a re-run of compute_hallways_for_wing on the same drawer set must
        NOT reset that strength. Otherwise every mine wipes the connection
        weights — undermining the whole L7 dynamics layer."""
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "letters", "entities": "Aya;Lumi"},
            ]
        )
        first_pass = hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=2)
        assert first_pass, "first pass must create at least one hallway"

        # Simulate user activity: manually bump strength + access_count on
        # the persisted records.
        stored = hallways_mod._load_hallways()
        for h in stored:
            h["strength"] = 2.5
            h["access_count"] = 7
            h["stability"] = 1.8
        hallways_mod._save_hallways(stored)

        # Recompute the same wing — should preserve the bumped values.
        hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=2)

        after = hallways_mod._load_hallways()
        assert after, "after-recompute records must exist"
        for h in after:
            assert h["strength"] == 2.5, (
                f"recompute reset strength — L7 dynamics undermined; got {h}"
            )
            assert h["access_count"] == 7, f"recompute reset access_count; got {h}"
            assert h["stability"] == 1.8, f"recompute reset stability; got {h}"

    def test_recompute_initializes_dynamics_for_brand_new_pairs(self, tmp_path, monkeypatch):
        """When a recompute discovers a NEW entity pair (not in the prior
        wing's hallways), the new record gets default dynamics — not
        inherited from some unrelated previous record."""
        from mempalace.dynamics import DEFAULT_STRENGTH

        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col_a = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "letters", "entities": "Aya;Lumi"},
            ]
        )
        hallways_mod.compute_hallways_for_wing("wing_aya", col=col_a, min_count=2)

        # Bump strength on the Aya↔Lumi pair to verify it's not leaked.
        stored = hallways_mod._load_hallways()
        for h in stored:
            h["strength"] = 3.5
        hallways_mod._save_hallways(stored)

        # Now a recompute with a different entity pair (Ever shows up).
        col_b = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "letters", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Ever"},
                {"wing": "wing_aya", "room": "letters", "entities": "Aya;Ever"},
            ]
        )
        hallways_mod.compute_hallways_for_wing("wing_aya", col=col_b, min_count=2)

        after = hallways_mod._load_hallways()
        aya_ever = [h for h in after if {h["entity_a"], h["entity_b"]} == {"Aya", "Ever"}]
        aya_lumi = [h for h in after if {h["entity_a"], h["entity_b"]} == {"Aya", "Lumi"}]

        assert len(aya_ever) == 1, "Aya↔Ever pair should now exist"
        assert aya_ever[0]["strength"] == DEFAULT_STRENGTH, (
            "new pair should get default strength, not inherit from another pair"
        )
        assert len(aya_lumi) == 1
        assert aya_lumi[0]["strength"] == 3.5, (
            "existing pair's accumulated strength must still be preserved"
        )

    def test_recompute_preserves_dynamics_when_existing_record_has_reversed_entity_order(
        self, tmp_path, monkeypatch
    ):
        """The dynamics-preservation lookup must canonicalize the entity-pair
        key by sorting, matching the symmetric ID generation. Otherwise a
        persisted record with (entity_a='Lumi', entity_b='Aya') would miss
        the lookup when the new computation produces (entity_a='Aya',
        entity_b='Lumi') — silently wiping accumulated dynamics.

        Per PR #1578 review (gemini-code-assist, HIGH priority): existing
        records may not always be stored with sorted entity order
        (manual edits, imports from other sources, legacy schema). The
        lookup must canonicalize the same way ``_hallway_id`` does.
        """
        _use_tmp_hallway_file(monkeypatch, tmp_path)

        # Pre-populate with a record whose entities are stored in REVERSED
        # (non-sorted) order, but bumped to non-default dynamics.
        hallways_mod._save_hallways(
            [
                {
                    "id": hallways_mod._hallway_id("wing_aya", "Aya", "Lumi"),
                    "wing": "wing_aya",
                    "entity_a": "Lumi",  # NOT sorted — Lumi > Aya
                    "entity_b": "Aya",
                    "co_occurrence_count": 5,
                    "rooms": ["diary"],
                    "label": "...",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "created_by": "auto",
                    "strength": 4.2,
                    "stability": 1.9,
                    "last_activated": "2026-05-01T00:00:00+00:00",
                    "access_count": 33,
                }
            ]
        )

        # Recompute — should match the existing record via sorted-key lookup
        # and preserve its dynamics, not initialize defaults.
        col = _fake_collection(
            [
                {"wing": "wing_aya", "room": "diary", "entities": "Aya;Lumi"},
                {"wing": "wing_aya", "room": "letters", "entities": "Aya;Lumi"},
            ]
        )
        hallways_mod.compute_hallways_for_wing("wing_aya", col=col, min_count=2)

        after = hallways_mod._load_hallways()
        assert len(after) == 1
        assert after[0]["strength"] == 4.2, (
            "lookup failed to match the reverse-ordered existing record — "
            "strength got reset to default. Lookup key must be canonicalized "
            "by sorting the entity pair (matching _hallway_id's symmetric ID)."
        )
        assert after[0]["access_count"] == 33
        assert after[0]["stability"] == 1.9


# ── entity spellings (audit repair session, 2026-09-20) ──────────────────────


class TestEntitySpellings:
    def test_spelling_key_collapses_paths_and_extensions(self):
        key = hallways_mod.entity_spelling_key
        assert key("main.zig") == key("src/main.zig") == key("/Users/x/p/src/main.zig")
        assert key("mcp_server") == key("mcp_server.py")
        assert key("device.zig") == key("src\\wireguard\\device.zig")
        assert key("ChatStore") != key("ChatStore.send")
        assert key("MemPalace") != key("github.com")

    def test_canonical_entities_keeps_qualified_files_and_short_symbols(self):
        # A file keeps its path (it is what tells two same-named files apart
        # once the record leaves the wing); a symbol keeps its shortest name.
        out = hallways_mod.canonical_entities(
            ["src/main.zig", "RootView.swift", "main.zig", "RootView", "swim.zig"]
        )
        assert out == ["src/main.zig", "RootView", "swim.zig"]

    def test_miner_pairs_canonical_spellings_only(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {
                    "wing": "w",
                    "room": "technical",
                    "entities": "ChatStore;ChatStore.swift;RootView;RootView.swift",
                },
                {"wing": "w", "room": "technical", "entities": "ChatStore.swift;RootView"},
            ]
        )
        created = hallways_mod.compute_hallways_for_wing("w", col=col, min_count=2)
        assert [(h["entity_a"], h["entity_b"]) for h in created] == [("ChatStore", "RootView")]
        assert created[0]["co_occurrence_count"] == 2

    def test_prune_spellings_drops_self_links_and_merges_variants(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        records = [
            {
                "id": "1",
                "wing": "w",
                "entity_a": "main.zig",
                "entity_b": "src/main.zig",
                "co_occurrence_count": 400,
            },
            {
                "id": "2",
                "wing": "w",
                "entity_a": "ChatStore",
                "entity_b": "RootView",
                "co_occurrence_count": 240,
            },
            {
                "id": "3",
                "wing": "w",
                "entity_a": "ChatStore.swift",
                "entity_b": "RootView.swift",
                "co_occurrence_count": 218,
            },
            {
                "id": "4",
                "wing": "w",
                "entity_a": "ChatStore.swift",
                "entity_b": "RootView",
                "co_occurrence_count": 221,
            },
            {
                "id": "5",
                "wing": "w",
                "entity_a": "codec.zig",
                "entity_b": "swim.zig",
                "co_occurrence_count": 195,
            },
            {
                "id": "6",
                "wing": "other",
                "entity_a": "ChatStore.swift",
                "entity_b": "RootView",
                "co_occurrence_count": 3,
            },
        ]
        hallways_mod._save_hallways(records)

        dry = hallways_mod.prune_spelling_hallways(apply=False)
        assert dry["self_links"] == 1
        assert dry["duplicates"] == 2
        assert dry["removed"] == 0
        assert dry["by_wing"] == {"w": 3}
        assert dry["sample"][0] == "main.zig ↔ src/main.zig"
        assert len(hallways_mod.list_hallways()) == 6  # dry run touched nothing

        applied = hallways_mod.prune_spelling_hallways(apply=True)
        assert applied["removed"] == 3
        left = {(h["wing"], h["entity_a"], h["entity_b"]): h for h in hallways_mod.list_hallways()}
        assert set(left) == {
            ("w", "ChatStore", "RootView"),
            ("w", "codec.zig", "swim.zig"),
            ("other", "ChatStore.swift", "RootView"),  # lone record: untouched
        }
        # The survivor keeps the highest count and gets the shortest spellings + a matching id.
        survivor = left[("w", "ChatStore", "RootView")]
        assert survivor["co_occurrence_count"] == 240
        assert survivor["id"] == hallways_mod._hallway_id("w", "ChatStore", "RootView")

    def test_prune_spellings_canonicalizes_reversed_variants(self, tmp_path, monkeypatch):
        """``a ↔ b.py`` and ``b ↔ a.py`` are one association with swapped columns;
        the survivor must still end up under the shortest spelling of each."""
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallways_mod._save_hallways(
            [
                {
                    "id": "1",
                    "wing": "w",
                    "entity_a": "src/store.py",
                    "entity_b": "view",
                    "co_occurrence_count": 50,
                },
                {
                    "id": "2",
                    "wing": "w",
                    "entity_a": "view.py",
                    "entity_b": "store",
                    "co_occurrence_count": 40,
                },
            ]
        )
        applied = hallways_mod.prune_spelling_hallways(apply=True)
        assert applied["removed"] == 1
        (survivor,) = hallways_mod.list_hallways()
        assert (survivor["entity_a"], survivor["entity_b"]) == ("src/store.py", "view")
        assert survivor["co_occurrence_count"] == 50
        assert survivor["id"] == hallways_mod._hallway_id("w", "src/store.py", "view")


class TestSameNamedFilesStayApart:
    """``src/models/user.py`` and ``tests/models/user.py`` are two files."""

    def test_same_file_spelling_requires_a_suffix_path(self):
        from mempalace.hallways import same_file_spelling

        assert same_file_spelling("main.zig", "src/main.zig")
        assert same_file_spelling("b/x.py", "a/b/x.py")
        assert same_file_spelling("mcp_server", "mcp_server.py")
        assert not same_file_spelling("src/models/user.py", "tests/models/user.py")
        assert not same_file_spelling("ChatStore", "RootView")

    def test_canonical_entities_keeps_distinct_files_apart(self):
        from mempalace.hallways import canonical_entities

        assert canonical_entities(["src/models/user.py", "tests/models/user.py"]) == [
            "src/models/user.py",
            "tests/models/user.py",
        ]
        assert canonical_entities(["src/main.zig", "main.zig"]) == ["src/main.zig"]

    def test_hallway_between_two_same_named_files_is_not_a_self_link(self):
        from mempalace.hallways import is_self_link

        assert not is_self_link(
            {"entity_a": "src/models/user.py", "entity_b": "tests/models/user.py"}
        )
        assert is_self_link({"entity_a": "main.zig", "entity_b": "src/main.zig"})

    def test_miner_counts_two_same_named_files_as_two_entities(self, tmp_path, monkeypatch):
        """One drawer naming both files must not count one pair twice."""
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {
                    "wing": "w",
                    "room": "technical",
                    "entities": "src/models/user.py;tests/models/user.py;Router",
                },
                {
                    "wing": "w",
                    "room": "technical",
                    "entities": "src/models/user.py;Router",
                },
            ]
        )
        created = hallways_mod.compute_hallways_for_wing("w", col=col, min_count=1)
        pairs = {(h["entity_a"], h["entity_b"]): h["co_occurrence_count"] for h in created}
        assert pairs == {
            ("Router", "src/models/user.py"): 2,
            ("Router", "tests/models/user.py"): 1,
            ("src/models/user.py", "tests/models/user.py"): 1,
        }

    def test_miner_still_unifies_one_file_spelled_two_ways(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {"wing": "w", "room": "r", "entities": "src/main.zig;Router"},
                {"wing": "w", "room": "r", "entities": "main.zig;Router"},
            ]
        )
        created = hallways_mod.compute_hallways_for_wing("w", col=col, min_count=1)
        assert [(h["entity_a"], h["entity_b"], h["co_occurrence_count"]) for h in created] == [
            ("Router", "src/main.zig", 2)
        ]

    def test_prune_keeps_both_files_hallways(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallways_mod._save_hallways(
            [
                {
                    "id": "1",
                    "wing": "w",
                    "entity_a": "src/models/user.py",
                    "entity_b": "view",
                    "co_occurrence_count": 50,
                },
                {
                    "id": "2",
                    "wing": "w",
                    "entity_a": "tests/models/user.py",
                    "entity_b": "view",
                    "co_occurrence_count": 40,
                },
                {
                    "id": "3",
                    "wing": "w",
                    "entity_a": "src/models/user.py",
                    "entity_b": "view.py",
                    "co_occurrence_count": 30,
                },
            ]
        )
        report = hallways_mod.prune_spelling_hallways(apply=True)
        assert report["removed"] == 1  # only the src/user ↔ view spelling variant
        left = {(h["entity_a"], h["entity_b"]) for h in hallways_mod.list_hallways()}
        assert left == {("src/models/user.py", "view"), ("tests/models/user.py", "view")}


class TestMinerAndPruneAgree:
    """Whatever the miner writes, the prune and the audit find nothing to remove."""

    MESSY = [
        # git diff names every touched file twice, as a/<path> and b/<path>.
        "a/mempalace/cli.py;b/mempalace/cli.py;cli;Router",
        "a/mempalace/cli.py;b/mempalace/cli.py;Router;ChatStore",
        # Two files with one basename, plus a bare name that could be either.
        "src/models/user.py;tests/models/user.py;user.py;Router",
        "src/models/user.py;user.py;ChatStore",
        "tests/models/user.py;Router",
        # One symbol spelled with and without an extension.
        "ChatStore.swift;Router",
        "ChatStore;Router;src/main.zig",
        "main.zig;Router",
    ]

    def test_mined_records_contain_no_spelling_artifacts(self, tmp_path, monkeypatch):
        from mempalace.palace_audit import _analyze_hallways

        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection([{"wing": "w", "room": "r", "entities": e} for e in self.MESSY])
        created = hallways_mod.compute_hallways_for_wing("w", col=col, min_count=1)
        names = {n for h in created for n in (h["entity_a"], h["entity_b"])}
        assert "b/mempalace/cli.py" not in names  # the diff pair is one file
        assert "user.py" not in names  # ambiguous: identifies no single file
        assert {"src/models/user.py", "tests/models/user.py"} <= names
        assert "ChatStore.swift" not in names and "ChatStore" in names

        report = hallways_mod.prune_spelling_hallways(apply=False)
        assert (report["self_links"], report["duplicates"]) == (0, 0)
        audit = _analyze_hallways(hallways_mod.list_hallways())
        assert (audit["self_links"], audit["duplicates"]) == (0, 0)


class TestDiffAliasesAndEmptyRebuilds:
    def test_diff_pairs_collapse_at_any_depth_but_a_lone_prefix_does_not(self):
        from mempalace.hallways import _spelling_clusters, same_file_spelling

        assert same_file_spelling("a/main.py", "b/main.py")  # root-level diff
        assert same_file_spelling("a/src/x.py", "b/src/x.py")
        assert _spelling_clusters(["a/main.py", "b/main.py"]) == [["a/main.py", "b/main.py"]]
        # A lone a/ may be a real directory named "a": not conflated.
        assert not same_file_spelling("a/lib/x.py", "c/lib/x.py")
        assert _spelling_clusters(["a/lib/x.py", "src/lib/x.py"]) == [
            ["a/lib/x.py"],
            ["src/lib/x.py"],
        ]

    def test_root_level_diff_pair_is_never_a_hallway(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        col = _fake_collection(
            [
                {"wing": "w", "room": "r", "entities": "a/main.py;b/main.py;Router"},
                {"wing": "w", "room": "r", "entities": "a/main.py;b/main.py;Router"},
            ]
        )
        created = hallways_mod.compute_hallways_for_wing("w", col=col, min_count=1)
        assert [(h["entity_a"], h["entity_b"]) for h in created] == [("Router", "a/main.py")]
        assert hallways_mod.prune_spelling_hallways()["removed"] == 0

    def test_rebuild_with_no_pairs_replaces_the_wings_stale_records(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallways_mod._save_hallways(
            [
                {"id": "old", "wing": "w", "entity_a": "main.zig", "entity_b": "src/main.zig"},
                {"id": "keep", "wing": "other", "entity_a": "X", "entity_b": "Y"},
            ]
        )
        # Every drawer now names one file under two spellings: no pair left.
        col = _fake_collection(
            [{"wing": "w", "room": "r", "entities": "main.zig;src/main.zig"}] * 3
        )
        assert hallways_mod.compute_hallways_for_wing("w", col=col) == []
        assert [h["id"] for h in hallways_mod.list_hallways()] == ["keep"]

    def test_failed_read_leaves_the_wing_untouched(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallways_mod._save_hallways([{"id": "old", "wing": "w", "entity_a": "A", "entity_b": "B"}])
        col = MagicMock()
        col.get.side_effect = OSError("disk I/O error")
        assert hallways_mod.compute_hallways_for_wing("w", col=col) == []
        assert [h["id"] for h in hallways_mod.list_hallways()] == ["old"]

    def test_prune_never_turns_a_two_file_association_into_a_self_link(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallways_mod._save_hallways(
            [
                {
                    "id": "1",
                    "wing": "w",
                    "entity_a": "src/user.py",
                    "entity_b": "tests/user.py",
                    "co_occurrence_count": 9,
                },
                {
                    "id": "2",
                    "wing": "w",
                    "entity_a": "repo/src/user.py",
                    "entity_b": "tests/user.py",
                    "co_occurrence_count": 4,
                },
            ]
        )
        report = hallways_mod.prune_spelling_hallways(apply=True)
        assert report["removed"] == 1
        (survivor,) = hallways_mod.list_hallways()
        assert {survivor["entity_a"], survivor["entity_b"]} == {"repo/src/user.py", "tests/user.py"}
        assert not hallways_mod.is_self_link(survivor)


class TestAmbiguousRecordsNeverBridgeFiles:
    RECORDS = [
        # The ambiguous bare record comes first and is strongest, so a greedy
        # pairwise grouping would have pulled both files into its group.
        {
            "id": "0",
            "wing": "w",
            "entity_a": "user.py",
            "entity_b": "Account",
            "co_occurrence_count": 50,
        },
        {
            "id": "1",
            "wing": "w",
            "entity_a": "src/user.py",
            "entity_b": "Account",
            "co_occurrence_count": 9,
        },
        {
            "id": "2",
            "wing": "w",
            "entity_a": "tests/user.py",
            "entity_b": "Account",
            "co_occurrence_count": 5,
        },
    ]

    def test_prune_keeps_both_files_associations(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        hallways_mod._save_hallways([dict(r) for r in self.RECORDS])
        report = hallways_mod.prune_spelling_hallways(apply=True)
        assert report["removed"] == 0
        left = {(h["entity_a"], h["entity_b"]) for h in hallways_mod.list_hallways()}
        assert ("src/user.py", "Account") in left and ("tests/user.py", "Account") in left

    def test_audit_agrees_with_the_prune(self):
        from mempalace.palace_audit import _analyze_hallways

        out = _analyze_hallways([dict(r) for r in self.RECORDS])
        assert (out["self_links"], out["duplicates"]) == (0, 0)
