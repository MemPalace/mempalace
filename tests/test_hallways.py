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
