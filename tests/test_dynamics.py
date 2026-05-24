"""Tests for mempalace.dynamics — living-connection math for halls + tunnels.

Covers Hebbian potentiation, Ebbinghaus exponential decay, the Cepeda
spacing effect, and safe field initialization for connections created
before L7 dynamics shipped. Pure math; no I/O; no chromadb required.

All ``now`` arguments are injected explicitly so tests are deterministic.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from mempalace.dynamics import (
    DEFAULT_STABILITY,
    DEFAULT_STRENGTH,
    MAX_STRENGTH,
    POTENTIATION_INCREMENT,
    STABILITY_INCREMENT,
    STRENGTH_FLOOR,
    apply_decay,
    initialize_dynamics_fields,
    potentiate,
)


# Fixed reference point so test arithmetic is exact.
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _fresh_connection(created_at=None, **overrides):
    """Build a minimal connection dict in the shape persisted by hallways.py
    or palace_graph.py — no dynamics fields yet."""
    base = {
        "id": "test_conn_001",
        "wing": "test_wing",
        "entity_a": "Alpha",
        "entity_b": "Beta",
        "co_occurrence_count": 3,
        "rooms": ["r1"],
        "label": "Alpha ↔ Beta",
        "created_at": (created_at or T0).isoformat(),
        "created_by": "auto",
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# initialize_dynamics_fields — backfill for pre-L7 records
# ─────────────────────────────────────────────────────────────────────────────


class TestInitializeDynamicsFields:
    def test_populates_strength_default_on_missing_field(self):
        conn = _fresh_connection()
        assert "strength" not in conn
        initialize_dynamics_fields(conn, now=T0)
        assert conn["strength"] == DEFAULT_STRENGTH

    def test_populates_stability_default_on_missing_field(self):
        conn = _fresh_connection()
        initialize_dynamics_fields(conn, now=T0)
        assert conn["stability"] == DEFAULT_STABILITY

    def test_populates_last_activated_from_created_at(self):
        """A fresh connection's last_activated should anchor to its
        created_at — decay starts from creation, not init-call-time."""
        conn = _fresh_connection()
        initialize_dynamics_fields(conn, now=T0 + timedelta(days=5))
        assert conn["last_activated"] == T0.isoformat()

    def test_populates_access_count_zero(self):
        conn = _fresh_connection()
        initialize_dynamics_fields(conn, now=T0)
        assert conn["access_count"] == 0

    def test_does_not_overwrite_existing_fields(self):
        """Records that already have dynamics fields are passed through
        unchanged — this is a backfill helper, not a reset."""
        conn = _fresh_connection()
        conn["strength"] = 2.3
        conn["stability"] = 1.7
        conn["last_activated"] = (T0 + timedelta(days=2)).isoformat()
        conn["access_count"] = 17
        initialize_dynamics_fields(conn, now=T0 + timedelta(days=5))
        assert conn["strength"] == 2.3
        assert conn["stability"] == 1.7
        assert conn["last_activated"] == (T0 + timedelta(days=2)).isoformat()
        assert conn["access_count"] == 17

    def test_safe_on_record_missing_created_at(self):
        """Defensive: even if created_at is somehow missing, init should
        not crash — falls back to ``now``."""
        conn = _fresh_connection()
        del conn["created_at"]
        initialize_dynamics_fields(conn, now=T0)
        # Just verify no crash and basic fields populated.
        assert "strength" in conn
        assert "last_activated" in conn


# ─────────────────────────────────────────────────────────────────────────────
# potentiate — Hebbian strengthening on co-access
# ─────────────────────────────────────────────────────────────────────────────


class TestPotentiate:
    def test_increments_strength_by_default_increment(self):
        conn = _fresh_connection()
        potentiate(conn, now=T0 + timedelta(hours=2))
        assert conn["strength"] == pytest.approx(DEFAULT_STRENGTH + POTENTIATION_INCREMENT)

    def test_respects_custom_increment(self):
        conn = _fresh_connection()
        potentiate(conn, increment=0.2, now=T0 + timedelta(hours=2))
        assert conn["strength"] == pytest.approx(DEFAULT_STRENGTH + 0.2)

    def test_caps_at_max_strength(self):
        conn = _fresh_connection()
        conn["strength"] = MAX_STRENGTH - 0.01
        potentiate(conn, increment=1.0, now=T0 + timedelta(hours=2))
        assert conn["strength"] == MAX_STRENGTH

    def test_updates_last_activated(self):
        conn = _fresh_connection()
        new_time = T0 + timedelta(days=3)
        potentiate(conn, now=new_time)
        assert conn["last_activated"] == new_time.isoformat()

    def test_increments_access_count(self):
        conn = _fresh_connection()
        potentiate(conn, now=T0 + timedelta(hours=2))
        assert conn["access_count"] == 1
        potentiate(conn, now=T0 + timedelta(hours=4))
        assert conn["access_count"] == 2

    def test_grows_stability_on_spaced_reinforcement(self):
        """Cepeda spacing effect: when the gap since last activation is
        ``≥ SPACED_INTERVAL_HOURS``, stability grows by
        ``STABILITY_INCREMENT``."""
        conn = _fresh_connection()
        # Initialize at T0; potentiate at T0 + 2 hours (well above the 1-hour
        # spacing threshold) — stability should grow.
        potentiate(conn, now=T0 + timedelta(hours=2))
        assert conn["stability"] == pytest.approx(DEFAULT_STABILITY + STABILITY_INCREMENT)

    def test_does_not_grow_stability_on_rapid_reinforcement(self):
        """Rapid bursts of co-access don't build stability — only spaced
        reinforcement does. Sub-spacing-threshold gap → stability unchanged."""
        conn = _fresh_connection()
        # Initial state has last_activated == created_at == T0.
        # Potentiate just 10 minutes later — well below the 1-hour spacing.
        potentiate(conn, now=T0 + timedelta(minutes=10))
        assert conn["stability"] == DEFAULT_STABILITY

    def test_chains_via_returned_dict(self):
        """potentiate mutates and returns the same dict for chaining."""
        conn = _fresh_connection()
        result = potentiate(conn, now=T0 + timedelta(hours=2))
        assert result is conn

    def test_works_on_record_without_dynamics_fields(self):
        """Backwards-compatibility: a pre-L7 record (no strength field yet)
        can be potentiated directly; missing fields backfill safely."""
        conn = _fresh_connection()
        assert "strength" not in conn
        potentiate(conn, now=T0 + timedelta(hours=2))
        assert conn["strength"] == pytest.approx(DEFAULT_STRENGTH + POTENTIATION_INCREMENT)
        assert conn["access_count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# apply_decay — Ebbinghaus exponential decay
# ─────────────────────────────────────────────────────────────────────────────


class TestApplyDecay:
    def test_reduces_strength_proportionally_to_elapsed_time(self):
        """At ``days_since == stability``, strength multiplies by ``exp(-1)``
        — about 0.3679 of original. This is the Ebbinghaus baseline."""
        conn = _fresh_connection()
        conn["strength"] = 1.0
        conn["stability"] = 1.0
        conn["last_activated"] = T0.isoformat()
        apply_decay(conn, now=T0 + timedelta(days=1))
        assert conn["strength"] == pytest.approx(math.exp(-1.0), rel=1e-6)

    def test_higher_stability_decays_slower(self):
        """Same elapsed time, double the stability → strength stays higher.
        Implements the durability benefit of spaced reinforcement."""
        conn_low = _fresh_connection()
        conn_low["strength"] = 1.0
        conn_low["stability"] = 1.0
        conn_low["last_activated"] = T0.isoformat()
        conn_high = _fresh_connection()
        conn_high["strength"] = 1.0
        conn_high["stability"] = 2.0
        conn_high["last_activated"] = T0.isoformat()

        apply_decay(conn_low, now=T0 + timedelta(days=1))
        apply_decay(conn_high, now=T0 + timedelta(days=1))

        assert conn_high["strength"] > conn_low["strength"]

    def test_floors_at_strength_floor(self):
        """Strength never decays below ``STRENGTH_FLOOR`` — the palace
        doesn't forget; salience just drops."""
        conn = _fresh_connection()
        conn["strength"] = 1.0
        conn["stability"] = 1.0
        conn["last_activated"] = T0.isoformat()
        apply_decay(conn, now=T0 + timedelta(days=10_000))
        assert conn["strength"] == STRENGTH_FLOOR

    def test_idempotent_at_same_instant(self):
        """Calling apply_decay twice at the same ``now`` (without potentiation
        between) produces the same result as calling once."""
        conn_a = _fresh_connection()
        conn_a["strength"] = 1.0
        conn_a["stability"] = 1.0
        conn_a["last_activated"] = T0.isoformat()
        conn_b = dict(conn_a)

        apply_decay(conn_a, now=T0 + timedelta(days=3))
        apply_decay(conn_b, now=T0 + timedelta(days=3))
        apply_decay(conn_b, now=T0 + timedelta(days=3))

        # Both have decayed once total — but conn_b's SECOND apply_decay
        # at the same ``now`` should be a no-op since last_activated hasn't
        # moved. Result: both end at the same strength.
        assert conn_a["strength"] == pytest.approx(conn_b["strength"])

    def test_no_decay_when_no_time_has_passed(self):
        """If ``now == last_activated``, decay is a no-op."""
        conn = _fresh_connection()
        conn["strength"] = 1.5
        conn["stability"] = 1.0
        conn["last_activated"] = T0.isoformat()
        apply_decay(conn, now=T0)
        assert conn["strength"] == 1.5

    def test_handles_missing_fields_via_backfill(self):
        """Pre-L7 records (no strength/stability/last_activated) get safe
        defaults via initialize_dynamics_fields — no crash."""
        conn = _fresh_connection()
        assert "strength" not in conn
        # Should not raise; should not corrupt the record.
        apply_decay(conn, now=T0 + timedelta(days=1))
        assert "strength" in conn
        # With last_activated falling back to created_at (T0) and a 1-day
        # gap at default stability, expect strength == exp(-1).
        assert conn["strength"] == pytest.approx(math.exp(-1.0), rel=1e-6)

    def test_returns_same_dict_for_chaining(self):
        conn = _fresh_connection()
        conn["strength"] = 1.0
        conn["stability"] = 1.0
        conn["last_activated"] = T0.isoformat()
        result = apply_decay(conn, now=T0 + timedelta(days=1))
        assert result is conn


# ─────────────────────────────────────────────────────────────────────────────
# Integration scenarios — potentiate + decay interleaved
# ─────────────────────────────────────────────────────────────────────────────


class TestIntegrationScenarios:
    def test_potentiation_after_decay_restores_strength(self):
        """A connection that decayed substantially can recover its strength
        through subsequent co-access. This is the live-organism behavior:
        attention rebuilds salience."""
        conn = _fresh_connection()
        conn["strength"] = 1.0
        conn["stability"] = 1.0
        conn["last_activated"] = T0.isoformat()

        # Decay for 3 days.
        apply_decay(conn, now=T0 + timedelta(days=3))
        decayed_strength = conn["strength"]
        assert decayed_strength < 1.0

        # Potentiate at day 3 — strength should jump up.
        potentiate(conn, now=T0 + timedelta(days=3))
        assert conn["strength"] > decayed_strength

    def test_repeated_spaced_reinforcement_grows_stability(self):
        """Cepeda spacing effect over time: daily reinforcement should
        steadily grow stability, making the connection durable."""
        conn = _fresh_connection()
        stability_history = []
        for day in range(1, 6):
            potentiate(conn, now=T0 + timedelta(days=day))
            stability_history.append(conn["stability"])

        # Stability should have grown monotonically.
        for i in range(1, len(stability_history)):
            assert stability_history[i] > stability_history[i - 1]

    def test_burst_reinforcement_does_not_grow_stability(self):
        """Rapid bursts within the spacing window don't build durability —
        only access_count grows."""
        conn = _fresh_connection()
        for minutes_offset in (5, 10, 15, 20, 25):
            potentiate(conn, now=T0 + timedelta(minutes=minutes_offset))
        assert conn["stability"] == DEFAULT_STABILITY
        assert conn["access_count"] == 5
