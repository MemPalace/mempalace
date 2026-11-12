"""Shared date-window parsing for read-path filters.

``list_drawers`` and ``search`` accept the same optional
``since``/``before`` bounds and compare them against drawer ``filed_at``
metadata. The parsing and window semantics live here — a side-effect-free
module — so ``searcher`` can use them without importing ``mcp_server``
(whose import installs MCP stdio protection) and ``mcp_server`` keeps its
existing behavior by aliasing these functions.

Semantics:
* ``since`` is inclusive, ``before`` is exclusive: ``[since, before)``.
* Bounds and ``filed_at`` are compared as instants. ``filed_at`` is written
  as UTC with an offset; older drawers hold naive local wall-clock values,
  and a bound without an offset is also local time (see ``timeutil``).
* A drawer whose ``filed_at`` is missing or unparseable is EXCLUDED
  whenever a bound is active — a date-filtered result must never
  silently include rows of unknown age.
"""

from datetime import datetime
from typing import Optional

from .timeutil import localize, parse_iso, parse_timestamp


def parse_date_bound(value: Optional[str] = None, field_name: str = "date") -> Optional[datetime]:
    """Parse an optional ISO-8601 date/datetime filter bound into an aware datetime.

    Accepts a date (``"2026-04-01"``, local midnight), a naive timestamp
    (``"2026-04-01T09:30:00"``, local time), or one carrying a ``Z``/``+HH:MM``
    offset. Other ISO 8601 forms (basic format, week dates) are rejected on the
    Python 3.9 floor even where a newer ``fromisoformat`` would accept them.
    Blank / whitespace-only means "no filter" (``None``).
    Raises ``ValueError`` on an unparseable value so the caller can surface a
    clear error, mirroring the wing/room sanitizers.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO date string")
    if not value.strip():
        return None
    try:
        return parse_iso(value)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be an ISO date string "
            f"(e.g. '2026-04-01' or '2026-04-01T09:30:00'), got {value!r}"
        ) from exc


def parse_window(since: Optional[str] = None, before: Optional[str] = None):
    """Parse a ``[since, before)`` pair, rejecting an inverted window.

    Returns ``(since_dt, before_dt)`` — either side ``None`` when absent.
    Raises ``ValueError`` (naming the offending field or the inversion) so
    callers surface the same message everywhere a window is accepted.
    """
    since_dt = parse_date_bound(since, "since")
    before_dt = parse_date_bound(before, "before")
    if since_dt is not None and before_dt is not None and since_dt >= before_dt:
        raise ValueError(f"since ({since!r}) must be earlier than before ({before!r})")
    return since_dt, before_dt


def filed_at_in_window(
    filed_at, since_dt: Optional[datetime], before_dt: Optional[datetime]
) -> bool:
    """True if a drawer's ``filed_at`` falls in ``[since, before)``.

    A drawer whose ``filed_at`` is missing or unparseable cannot be confirmed
    in-window, so it is EXCLUDED whenever a bound is active — a date-filtered
    listing must never silently include rows of unknown age. Naive bounds
    passed in directly are read as local time, like naive ``filed_at``.
    """
    filed_dt = parse_timestamp(filed_at) if isinstance(filed_at, str) else None
    if filed_dt is None:
        return False
    if since_dt is not None and filed_dt < localize(since_dt):
        return False
    if before_dt is not None and filed_dt >= localize(before_dt):
        return False
    return True
