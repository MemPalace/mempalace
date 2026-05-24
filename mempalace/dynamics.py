"""dynamics.py — Living-connection math for halls + tunnels.

Hebbian potentiation (strength grows on co-access) and Ebbinghaus exponential
decay (strength fades with time since last activation), with the Cepeda
spacing effect: stability grows when reinforcement is spaced rather than
massed.

This module is pure. No I/O, no DB, no chromadb. It operates on plain
dicts (hall records, tunnel records) and mutates them in place. Callers
in ``hallways.py`` and ``palace_graph.py`` invoke these functions; the
math lives here in one place so both connection kinds share identical
semantics.

Schema fields added to hall + tunnel records (all default-safe — existing
records without them work via ``initialize_dynamics_fields``):

    strength: float           — Hebbian connection weight, floored at STRENGTH_FLOOR,
                                capped at MAX_STRENGTH
    stability: float          — decay resistance; grows with spaced reinforcement
    last_activated: str       — ISO datetime; updates on potentiation
    access_count: int         — cumulative co-access events

Research grounding:
    - Hebb (1949): "neurons that fire together, wire together" → potentiation
    - Ebbinghaus (1885): exponential forgetting curve → apply_decay
    - Cepeda et al. (2006): spacing effect → stability growth on spaced reinforcement
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Tunable constants. Hardcoded for v1; future PRs may expose via
# MempalaceConfig if real-palace empirical tuning calls for it.
# ─────────────────────────────────────────────────────────────────────────────

STRENGTH_FLOOR = 0.05
"""Lower bound on strength. Connections never decay below this — they
become dim but remain queryable explicitly. The palace doesn't forget;
salience just drops."""

MAX_STRENGTH = 5.0
"""Upper bound on strength. Caps so super-frequently-used connections
don't dominate ranking entirely. Above this, the connection is "fully
present" — further potentiation is a no-op."""

DEFAULT_STABILITY = 1.0
"""Initial stability for a newly-created connection. Higher = slower decay.
Grows with spaced reinforcement (Cepeda spacing effect)."""

DEFAULT_STRENGTH = 1.0
"""Initial strength for a newly-created connection. Treats new halls/tunnels
as 'normally present' — neither hot nor cold."""

POTENTIATION_INCREMENT = 0.05
"""How much strength increases on each co-access event. Tuned so that
~20 co-accesses bring a fresh connection to MAX_STRENGTH."""

SPACED_INTERVAL_HOURS = 1.0
"""Minimum gap (in hours) between potentiations to count as 'spaced'
reinforcement. Bursts of rapid co-access don't build stability;
distributed practice does."""

STABILITY_INCREMENT = 0.1
"""How much stability grows on each spaced reinforcement. Tuned so a
connection reinforced once a day for ~30 days roughly doubles its
stability — making it durable against weeks of neglect."""


# ─────────────────────────────────────────────────────────────────────────────
# Field initialization — safe for connections that pre-date L7
# ─────────────────────────────────────────────────────────────────────────────


def initialize_dynamics_fields(connection: dict, *, now: Optional[datetime] = None) -> dict:
    """Populate strength/stability/last_activated/access_count if missing.

    Existing fields are NOT overwritten — this is a backfill helper for
    records created before L7 dynamics shipped. Safe to call on any record;
    a no-op when all fields are already present.

    The ``now`` parameter is dependency injection for tests; defaults to
    current UTC time. Same pattern as the rest of this module.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    now_iso = now.isoformat() if isinstance(now, datetime) else now

    # ``created_at`` exists on every connection per existing schema; use it as
    # the natural fallback for last_activated so a brand-new record's decay
    # starts from creation, not from initialization-call-time.
    created_at = connection.get("created_at", now_iso)

    connection.setdefault("strength", DEFAULT_STRENGTH)
    connection.setdefault("stability", DEFAULT_STABILITY)
    connection.setdefault("last_activated", created_at)
    connection.setdefault("access_count", 0)
    return connection


# ─────────────────────────────────────────────────────────────────────────────
# Hebbian potentiation — strengthen on co-access
# ─────────────────────────────────────────────────────────────────────────────


def potentiate(
    connection: dict,
    *,
    increment: float = POTENTIATION_INCREMENT,
    now: Optional[datetime] = None,
) -> dict:
    """Strengthen ``connection`` on a co-access event.

    Updates ``strength`` (capped at ``MAX_STRENGTH``), ``last_activated``,
    and ``access_count``. Grows ``stability`` by ``STABILITY_INCREMENT``
    only if the gap since the prior activation is at least
    ``SPACED_INTERVAL_HOURS`` (the Cepeda spacing effect — rapid bursts
    don't build durability; distributed practice does).

    Mutates and returns the same dict for chaining. Pure aside from that
    mutation — no I/O.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Backfill any missing fields so callers can pass partial records.
    initialize_dynamics_fields(connection, now=now)

    # Compute the gap since the last activation to decide if this counts
    # as spaced reinforcement.
    last_activated_str = connection.get("last_activated") or connection.get("created_at")
    last_dt = _parse_iso(last_activated_str)
    if last_dt is not None:
        hours_since = (now - last_dt).total_seconds() / 3600.0
    else:
        hours_since = 0.0

    # Strength grows by increment, capped at MAX_STRENGTH.
    current_strength = float(connection.get("strength", DEFAULT_STRENGTH))
    connection["strength"] = min(MAX_STRENGTH, current_strength + float(increment))

    # Spacing effect: only grow stability when reinforcement is spaced.
    if hours_since >= SPACED_INTERVAL_HOURS:
        current_stability = float(connection.get("stability", DEFAULT_STABILITY))
        connection["stability"] = current_stability + STABILITY_INCREMENT

    # Always update last_activated and the cumulative counter.
    connection["last_activated"] = now.isoformat()
    connection["access_count"] = int(connection.get("access_count", 0)) + 1

    return connection


# ─────────────────────────────────────────────────────────────────────────────
# Ebbinghaus exponential decay — fade with time since last activation
# ─────────────────────────────────────────────────────────────────────────────


def apply_decay(connection: dict, *, now: Optional[datetime] = None) -> dict:
    """Apply Ebbinghaus exponential decay to ``connection``'s strength.

    The decay model is ``new = old * exp(-days_since_last / stability)``,
    floored at ``STRENGTH_FLOOR`` so connections never reach zero. Higher
    stability = slower decay (the Cepeda principle: spaced reinforcement
    builds durability).

    Idempotent at the same instant — calling twice at the same ``now``
    without a potentiation in between produces the same final strength.

    Mutates and returns the same dict for chaining. Pure aside from that
    mutation — no I/O.

    ``now`` is dependency injection for tests.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Backfill missing fields so callers can pass partial records.
    initialize_dynamics_fields(connection, now=now)

    last_activated_str = connection.get("last_activated") or connection.get("created_at")
    last_dt = _parse_iso(last_activated_str)
    if last_dt is None:
        # If we can't parse the timestamp, leave the strength as-is rather
        # than corrupting it. A malformed timestamp is a data-integrity
        # issue, not a math problem.
        return connection

    days_since = (now - last_dt).total_seconds() / 86400.0
    if days_since <= 0:
        # No time has passed (or clock skew); idempotent — return unchanged.
        return connection

    stability = float(connection.get("stability", DEFAULT_STABILITY))
    if stability <= 0:
        stability = DEFAULT_STABILITY

    current_strength = float(connection.get("strength", DEFAULT_STRENGTH))
    decay_factor = math.exp(-days_since / stability)
    new_strength = current_strength * decay_factor

    connection["strength"] = max(STRENGTH_FLOOR, new_strength)
    return connection


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _parse_iso(value) -> Optional[datetime]:
    """Parse an ISO-8601 string into a timezone-aware datetime.

    Returns None on any parse failure rather than raising — callers should
    handle the None case as "unknown timestamp." Old records may have
    timestamps in slightly different formats; be liberal in what we accept.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # fromisoformat handles most ISO-8601 variants including those
        # written by datetime.isoformat(). Z-suffix not always accepted
        # by older Python versions; convert to +00:00 explicitly.
        v = value.strip()
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = datetime.fromisoformat(v)
        # Force timezone awareness so subtraction with timezone-aware
        # ``now`` doesn't raise TypeError.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


__all__ = [
    "STRENGTH_FLOOR",
    "MAX_STRENGTH",
    "DEFAULT_STABILITY",
    "DEFAULT_STRENGTH",
    "POTENTIATION_INCREMENT",
    "SPACED_INTERVAL_HOURS",
    "STABILITY_INCREMENT",
    "initialize_dynamics_fields",
    "potentiate",
    "apply_decay",
]
