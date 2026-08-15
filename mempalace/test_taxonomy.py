"""Tests for MemPalace taxonomy canonicalization (SBAI-6290 / SBAI-7040).

Covers: slug normalization, alias resolution, validation, startup assertion,
and tool_add_drawer integration.
"""

import pytest
from mempalace.taxonomy import (
    CANONICAL_WINGS,
    CANONICAL_ROOMS,
    WING_ALIASES,
    ROOM_ALIASES,
    normalize_slug,
    is_canonical_slug,
    resolve_wing,
    resolve_room,
    validate_wing_room,
    is_canonical_wing,
    is_canonical_room,
    assert_canonical_set_nonempty,
)
from mempalace.mcp_server import tool_add_drawer


# ── Slug normalization ───────────────────────────────────────────────────────


class TestNormalizeSlug:
    def test_underscore_to_hyphen(self):
        assert normalize_slug("wing_codex_manager") == "wing-codex-manager"
        assert normalize_slug("model_manager") == "model-manager"

    def test_already_canonical(self):
        assert normalize_slug("codex-manager") == "codex-manager"
        assert normalize_slug("pipeline") == "pipeline"

    def test_case_folding(self):
        assert normalize_slug("BrainMon") == "brainmon"
        assert normalize_slug("BRAINMON") == "brainmon"

    def test_collapse_multiple_hyphens(self):
        assert normalize_slug("wing--manager") == "wing-manager"
        assert normalize_slug("wing___manager") == "wing-manager"

    def test_strip_leading_trailing_hyphens(self):
        assert normalize_slug("-manager-") == "manager"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="slug cannot be empty"):
            normalize_slug("")
        with pytest.raises(ValueError, match="slug cannot be empty"):
            normalize_slug("   ")

    def test_only_hyphens_raises(self):
        with pytest.raises(ValueError, match="normalized to empty string"):
            normalize_slug("___")
        with pytest.raises(ValueError, match="normalized to empty string"):
            normalize_slug("---")

    def test_space_to_hyphen(self):
        assert normalize_slug("wing manager") == "wing-manager"


class TestIsCanonicalSlug:
    def test_canonical(self):
        assert is_canonical_slug("codex-manager") is True
        assert is_canonical_slug("pipeline") is True
        assert is_canonical_slug("wing123") is True

    def test_not_canonical(self):
        assert is_canonical_slug("wing_codex_manager") is False
        assert is_canonical_slug("BrainMon") is False
        assert is_canonical_slug("-manager") is False
        assert is_canonical_slug("manager-") is False


# ── Alias resolution ─────────────────────────────────────────────────────────


class TestResolveWing:
    def test_canonical_wing_passthrough(self):
        assert resolve_wing("pipeline") == "pipeline"
        assert resolve_wing("core") == "core"
        assert resolve_wing("architecture") == "architecture"

    def test_underscore_variant(self):
        assert resolve_wing("wing_codex_manager") == "codex-manager"
        assert resolve_wing("model_manager") == "model-manager"

    def test_hyphen_variant(self):
        assert resolve_wing("wing-codex-manager") == "codex-manager"

    def test_studiobrain_manager_variants(self):
        assert resolve_wing("wing_manager") == "studiobrain-manager"
        assert resolve_wing("wing_manager-codex") == "studiobrain-manager"
        assert resolve_wing("wing_studiobrain-manager-codex") == "studiobrain-manager"
        assert resolve_wing("wing_studiobrain-manager") == "studiobrain-manager"

    def test_accident_wings(self):
        assert resolve_wing("SBAI-2326") == "tickets"
        assert resolve_wing("studiobrain-docs") == "architecture"
        assert resolve_wing("wing_claudeqa") == "pipeline"
        assert resolve_wing("wing_qwen-agent") == "pipeline"
        assert resolve_wing("charactercrew") == "app"

    def test_unknown_wing_raises(self):
        with pytest.raises(ValueError, match="Unknown wing"):
            resolve_wing("nonexistent-wing-xyz")


class TestResolveRoom:
    def test_case_variant(self):
        wing, room = resolve_room("BrainMon")
        assert wing == "studiobrain-manager"
        assert room == "brainmon"

    def test_mempalace_yaml_rooms(self):
        wing, room = resolve_room("studiobrain_rust")
        assert wing == "core"
        assert room == "rust"

    def test_with_wing_context(self):
        wing, room = resolve_room("backend", wing="core")
        assert wing == "core"
        assert room == "backend"

    def test_normalized_room_in_wing(self):
        wing, room = resolve_room("sb-server", wing="core")
        assert wing == "core"
        assert room == "sb-server"


class TestValidateWingRoom:
    def test_valid_canonical_pair(self):
        wing, room = validate_wing_room("pipeline", "agent")
        assert wing == "pipeline"
        assert room == "agent"

    def test_underscore_wing_normalized(self):
        wing, room = validate_wing_room("wing_codex_manager", "agents")
        assert wing == "codex-manager"
        assert room == "agents"

    def test_case_room_normalized(self):
        wing, room = validate_wing_room("studiobrain-manager", "BrainMon")
        assert wing == "studiobrain-manager"
        assert room == "brainmon"

    def test_unknown_wing_raises(self):
        with pytest.raises(ValueError):
            validate_wing_room("nonexistent-wing", "room")


class TestIsCanonicalWing:
    def test_canonical(self):
        assert is_canonical_wing("pipeline") is True
        assert is_canonical_wing("core") is True

    def test_alias_not_canonical(self):
        assert is_canonical_wing("wing_codex_manager") is False


class TestIsCanonicalRoom:
    def test_canonical(self):
        assert is_canonical_room("agent", "pipeline") is True
        assert is_canonical_room("brainmon", "studiobrain-manager") is True

    def test_not_canonical(self):
        assert is_canonical_room("some-random-room", "studiobrain-manager") is False
        assert is_canonical_room("nonexistent-room-xyz123", "pipeline") is False


class TestAssertCanonicalSetNonempty:
    def test_no_raise(self):
        """The canonical set should be non-empty — assertion should pass."""
        assert_canonical_set_nonempty()  # Should not raise

    def test_all_wings_have_rooms_or_not_in_canonical_rooms(self):
        """Each wing in CANONICAL_WINGS that has an entry in CANONICAL_ROOMS
        should have at least one room."""
        for wing in CANONICAL_WINGS:
            if wing in CANONICAL_ROOMS:
                assert len(CANONICAL_ROOMS[wing]) > 0, f"Wing '{wing}' has no rooms"


# ── Integration: alias map consistency ───────────────────────────────────────


class TestAliasMapConsistency:
    def test_all_alias_targets_are_canonical(self):
        """Every value in WING_ALIASES should be in CANONICAL_WINGS."""
        for alias, target in WING_ALIASES.items():
            assert target in CANONICAL_WINGS, (
                f"Wing alias '{alias}' → '{target}' but '{target}' is not in CANONICAL_WINGS"
            )

    def test_room_alias_targets_are_canonical(self):
        """Every room in ROOM_ALIASES should map to a canonical wing+room."""
        for alias, (wing, room) in ROOM_ALIASES.items():
            assert wing in CANONICAL_WINGS, (
                f"Room alias '{alias}' → wing '{wing}' not in CANONICAL_WINGS"
            )
            if wing in CANONICAL_ROOMS:
                assert room in CANONICAL_ROOMS[wing], (
                    f"Room alias '{alias}' → room '{room}' not in CANONICAL_ROOMS['{wing}']"
                )

    def test_no_canonical_wing_is_its_own_alias(self):
        """A canonical wing should not appear as a key pointing to a different value."""
        for wing in CANONICAL_WINGS:
            if wing in WING_ALIASES:
                assert WING_ALIASES[wing] == wing, (
                    f"Canonical wing '{wing}' maps to '{WING_ALIASES[wing]}' in aliases"
                )


# ── SBAI-7040: tool_add_drawer integration tests ─────────────────────────────


class TestToolAddDrawerCanonicalization:
    """Integration tests for tool_add_drawer taxonomy canonicalization.

    These tests verify that the MemPalace tool_add_drawer function
    canonicalizes wing/room slugs BEFORE persistence (SBAI-7040).
    """

    def test_unknown_wing_rejected_before_persistence(self):
        """Unknown wing returns error dict — never reaches ChromaDB."""
        result = tool_add_drawer("nonexistent-wing-xyz", "agent", "test content")
        assert result["success"] is False
        assert "taxonomy rejected" in result["error"]
        assert "Unknown wing" in result["error"]

    def test_alias_wing_normalized_before_delegation(self):
        """Alias wing like 'wing_codex_manager' is resolved to canonical 'codex-manager'."""
        import time
        unique_content = f"test alias normalization {time.time_ns()}"
        result = tool_add_drawer("wing_codex_manager", "agents", unique_content)
        # Result should NOT contain error about unknown wing
        assert "Unknown wing" not in result.get("error", "")
        # Handle idempotency (already_exists) case
        if result.get("reason") == "already_exists":
            # Drawer exists — the drawer_id contains the canonical wing
            assert "codex-manager" in result.get("drawer_id", "")
        else:
            # The stored wing should be canonical
            assert result.get("wing") == "codex-manager", (
                f"Expected canonical wing 'codex-manager', got {result.get('wing')!r}"
            )

    def test_case_drift_normalized(self):
        """'BrainMon' room drift is resolved to canonical wing+room."""
        import time
        unique_content = f"test case drift {time.time_ns()}"
        result = tool_add_drawer("studiobrain-manager", "BrainMon", unique_content)
        assert "taxonomy rejected" not in result.get("error", "")
        # Handle idempotency
        if result.get("reason") == "already_exists":
            assert "brainmon" in result.get("drawer_id", "")
        else:
            assert result.get("room") == "brainmon", (
                f"Expected canonical room 'brainmon', got {result.get('room')!r}"
            )

    def test_canonical_wing_passthrough(self):
        """Canonical wing passes through without alias resolution."""
        import time
        unique_content = f"test canonical passthrough {time.time_ns()}"
        result = tool_add_drawer("pipeline", "agent", unique_content)
        assert "taxonomy rejected" not in result.get("error", "")
        # Handle idempotency (already_exists) case
        if result.get("reason") == "already_exists":
            # Drawer exists from prior run — wing is still canonical
            assert "pipeline" in result.get("drawer_id", "")
        else:
            assert result.get("wing") == "pipeline"
            assert result.get("room") == "agent"

    def test_empty_wing_rejected(self):
        """Empty wing is rejected by normalize_slug before delegation."""
        result = tool_add_drawer("", "agent", "test empty wing")
        assert result["success"] is False
        assert "taxonomy rejected" in result["error"]

    def test_accident_wing_mapped(self):
        """Accident wing 'SBAI-2326' maps to 'tickets' and is accepted."""
        import time
        unique_content = f"test accident wing {time.time_ns()}"
        result = tool_add_drawer("SBAI-2326", "jira", unique_content)
        assert "taxonomy rejected" not in result.get("error", "")
        # Handle idempotency
        if result.get("reason") == "already_exists":
            assert "tickets" in result.get("drawer_id", "")
        else:
            assert result.get("wing") == "tickets", (
                f"Expected wing 'tickets', got {result.get('wing')!r}"
            )

    def test_raw_slug_stored_without_canonicalization_is_gap(self):
        """Regression test: if MemPalace stores raw slugs without normalization,
        this test catches it. After SBAI-7040, this should NOT happen."""
        import time
        unique_content = f"regression test {time.time_ns()}"

        # Use a non-canonical wing that SHOULD be normalized
        result = tool_add_drawer("wing_codex_manager", "agents", unique_content)

        # After SBAI-7040 fix, the wing should be canonicalized
        if result.get("reason") == "already_exists":
            # Check drawer_id for canonical wing
            assert "codex-manager" in result.get("drawer_id", ""), (
                f"SBAI-7040 REGRESSION: drawer_id does not contain canonical wing"
            )
        else:
            assert result.get("wing") == "codex-manager", (
                f"SBAI-7040 REGRESSION: MemPalace stored raw wing slug "
                f"{result.get('wing')!r} instead of canonical 'codex-manager'. "
                f"The taxonomy canonicalization layer is not being called!"
            )
