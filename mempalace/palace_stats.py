"""Wing / room / taxonomy statistics over the storage seam (#1657).

The single place that turns the backend's aggregation primitives
(:meth:`BaseCollection.count_by` / :meth:`BaseCollection.crosstab`) into the
shapes the CLI ``status`` and the MCP read tools return. Each function issues
one aggregate query — backed by SQL ``GROUP BY`` on the ChromaDB backend — so
the count/taxonomy read path is O(distinct values), not O(drawers).

The backend primitives label a missing metadata key with the Python value
``None``; this module relabels that bucket to a caller-chosen string
(``"unknown"`` for the MCP tools, ``"?"`` for the CLI) so behaviour matches the
legacy whole-palace Python scan byte for byte.
"""

from __future__ import annotations


def _relabel(value, missing: str) -> str:
    return value if value is not None else missing


def taxonomy(col, *, missing: str = "unknown") -> dict:
    """Return ``{wing: {room: count}}`` with missing keys labeled ``missing``."""
    out: dict = {}
    for wing, rooms in col.crosstab("wing", "room").items():
        bucket = out.setdefault(_relabel(wing, missing), {})
        for room, count in rooms.items():
            label = _relabel(room, missing)
            bucket[label] = bucket.get(label, 0) + count
    return out


def wing_counts(col, *, missing: str = "unknown") -> dict:
    """Return ``{wing: count}``."""
    out: dict = {}
    for wing, count in col.count_by("wing").items():
        label = _relabel(wing, missing)
        out[label] = out.get(label, 0) + count
    return out


def room_counts(col, *, wing: str | None = None, missing: str = "unknown") -> dict:
    """Return ``{room: count}``, optionally restricted to one ``wing``.

    When ``wing`` is given the counts come from the wing×room cross-tab (one
    query) so no separate filtered scan is needed.
    """
    if wing is not None:
        return taxonomy(col, missing=missing).get(wing, {})
    out: dict = {}
    for room, count in col.count_by("room").items():
        label = _relabel(room, missing)
        out[label] = out.get(label, 0) + count
    return out


def wing_room_summary(col, *, missing: str = "unknown") -> tuple[dict, dict]:
    """Return ``(wings, rooms)`` derived from a single wing×room cross-tab.

    Each drawer lands in exactly one cell, so summing rows gives ``{wing: n}``
    and summing columns gives ``{room: n}`` — both consistent with one another
    and with ``col.count()`` — from one query instead of two scans.
    """
    wings: dict = {}
    rooms: dict = {}
    for wing, room_map in taxonomy(col, missing=missing).items():
        for room, count in room_map.items():
            wings[wing] = wings.get(wing, 0) + count
            rooms[room] = rooms.get(room, 0) + count
    return wings, rooms
