"""dynamics.py — Living-connection math for halls + tunnels.

Hebbian potentiation (strength grows on co-access) and Ebbinghaus exponential
decay (strength fades with time since last activation), with the Cepeda
spacing effect: stability grows when reinforcement is spaced rather than
massed.

The math has no I/O, no DB, no chromadb. It operates on plain
dicts (hall records, tunnel records, drawer metadata records) and mutates
them in place; the math lives here in one place so connection kinds and
drawer salience share identical semantics. The drawer adapter logs invalid
optional field names, never their values or drawer content.

``hallways.py`` and ``palace_graph.py`` currently call only
``initialize_dynamics_fields``: hall and tunnel records get these fields
backfilled and preserved across recomputes, but nothing potentiates them on
access and nothing decays them over time. Wiring that up is a separate
question (which access counts as a co-access?); this docstring previously
said those modules invoked these functions, which was not accurate.

Read strength with ``effective_strength(connection, now=...)``. It is pure
and side-effect free, so a read cannot change a ranking.

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

import logging
import math
from datetime import datetime, timezone
from typing import Optional


logger = logging.getLogger(__name__)


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


def initialize_drawer_dynamics_fields(
    drawer_metadata: dict, *, now: Optional[datetime] = None
) -> dict:
    """Backfill and normalize optional drawer dynamics fields in place.

    Drawer metadata historically has ``filed_at`` rather than connection-style
    ``created_at``. This adapter keeps the shared dynamics math unchanged while
    preserving the drawer metadata shape: ``created_at`` is only supplied as a
    temporary fallback and is not left behind when absent from the input.
    Arbitrary legacy metadata may use these field names for nonnumeric values;
    only drawer dynamics use safe defaults for invalid values. Read callers
    must pass a copy; opted-in writers can persist the normalized fields.
    """

    if now is None:
        now = datetime.now(timezone.utc)
    missing_created_at = "created_at" not in drawer_metadata
    if missing_created_at and drawer_metadata.get("filed_at"):
        drawer_metadata["created_at"] = drawer_metadata["filed_at"]

    initialize_dynamics_fields(drawer_metadata, now=now)

    normalized_fields = []
    for field, default in (("strength", DEFAULT_STRENGTH), ("stability", DEFAULT_STABILITY)):
        value = drawer_metadata[field]
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            number = math.nan
        if (
            isinstance(value, bool)
            or not math.isfinite(number)
            or number < 0
            or (field == "stability" and number == 0)
        ):
            number = default
            normalized_fields.append(field)
        drawer_metadata[field] = number

    value = drawer_metadata["access_count"]
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        count = -1
    if isinstance(value, bool) or count < 0 or (isinstance(value, float) and value != count):
        count = 0
        normalized_fields.append("access_count")
    drawer_metadata["access_count"] = count

    if _parse_iso(drawer_metadata["last_activated"]) is None:
        fallback = (
            _parse_iso(drawer_metadata.get("created_at"))
            or _parse_iso(drawer_metadata.get("filed_at"))
            or now
        )
        drawer_metadata["last_activated"] = fallback.isoformat()
        normalized_fields.append("last_activated")

    if missing_created_at:
        drawer_metadata.pop("created_at", None)

    if normalized_fields:
        logger.warning(
            "Normalized invalid drawer dynamics fields: %s", ", ".join(normalized_fields)
        )

    return drawer_metadata


def drawer_salience(drawer_metadata: dict, *, now: Optional[datetime] = None) -> dict:
    """Return lazy-decayed salience for drawer metadata without mutating input."""

    if now is None:
        now = datetime.now(timezone.utc)
    record = dict(drawer_metadata or {})
    initialize_drawer_dynamics_fields(record, now=now)
    return {
        "strength": effective_strength(record, now=now),
        "stability": record["stability"],
        "last_activated": record["last_activated"],
        "access_count": record["access_count"],
    }


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


def effective_strength(connection: dict, *, now: Optional[datetime] = None) -> float:
    """Return what ``connection``'s strength is worth at ``now``.

    ``strength * exp(-days_since_last_activated / stability)``, floored at
    ``STRENGTH_FLOOR``. Pure: the connection is not modified, so reading a
    strength cannot change it.

    Prefer this to ``apply_decay``. Because the stored ``strength`` stays the
    value as of ``last_activated``, the answer depends only on elapsed time —
    reading twice gives the same number as reading once, and there is no
    maintenance pass to schedule, to forget, or to accidentally run twice.
    See ``apply_decay``'s note for what happens otherwise.

    ``now`` is dependency injection for tests. A naive datetime is read as
    UTC rather than raising: ``_parse_iso`` already forces awareness on the
    stored timestamp, so without the same treatment here a caller passing
    ``datetime.now()`` gets a TypeError from the subtraction below.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    last_activated_str = connection.get("last_activated") or connection.get("created_at")
    last_dt = _parse_iso(last_activated_str)
    if last_dt is None:
        return float(connection.get("strength", DEFAULT_STRENGTH))

    days_since = (now - last_dt).total_seconds() / 86400.0
    if days_since <= 0:
        return float(connection.get("strength", DEFAULT_STRENGTH))

    stability = float(connection.get("stability", DEFAULT_STABILITY))
    if stability <= 0:
        stability = DEFAULT_STABILITY

    current_strength = float(connection.get("strength", DEFAULT_STRENGTH))
    return max(STRENGTH_FLOOR, current_strength * math.exp(-days_since / stability))


def apply_decay(connection: dict, *, now: Optional[datetime] = None) -> dict:
    """Apply Ebbinghaus exponential decay to ``connection``'s strength.

    The decay model is ``new = old * exp(-days_since_last / stability)``,
    floored at ``STRENGTH_FLOOR`` so connections never reach zero. Higher
    stability = slower decay (the Cepeda principle: spaced reinforcement
    builds durability).

    Idempotent at the same instant — calling twice at the same ``now``
    without a potentiation in between produces the same final strength.

    NOT idempotent across different instants, and that is worth knowing
    before wiring it into a maintenance pass. It multiplies the *stored*
    strength while only ``potentiate`` resets ``last_activated``, so the
    result depends on how many times it ran rather than on how much time
    passed::

        strength 1.0, stability 5, last activated 10 days ago
        applied once at day 10        → 0.135335
        applied at day 5 and day 10   → 0.050000   (STRENGTH_FLOOR)

    Two installations with identical usage would then rank memories
    differently because one ran maintenance more often, and the
    twice-decayed connection sits at the floor where it is
    indistinguishable from one nobody has touched in a year.

    ``effective_strength`` computes the same curve at read time without
    that hazard, and needs no maintenance pass at all.

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
    "effective_strength",
]
