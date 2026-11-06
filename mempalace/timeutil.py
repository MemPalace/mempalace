"""Timestamps for persisted values, and the one parser that reads them back.

Every timestamp MemPalace stores is UTC ISO-8601 with an explicit offset
(``utc_now_iso()``, e.g. ``2026-10-17T13:00:00.123456+00:00``). Palaces
written before that also hold naive values, which were local wall-clock
time when written; ``parse_timestamp`` reads those as local time so both
forms compare as the same instants. Stored values are never rewritten.

Text order is chronological within each form but not across them, and
older writers (a hub, hook or peer not yet upgraded) keep adding naive
values. Newest-first readers therefore order by ``recency_key``; SQL
top-N reads fetch each form in text order and merge by it.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

# Tests pin the zone naive values are read in; None means the system zone.
_LOCAL_TZ = None

_MIN = datetime.min.replace(tzinfo=timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """The persisted form: UTC, microseconds, explicit ``+00:00`` offset."""
    return utc_now().isoformat(timespec="microseconds")


def utc_now_z() -> str:
    """Second-precision ``...Z`` form used by the knowledge graph and logstream."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def filename_stamp(sep: str = "-", utc: bool = False) -> str:
    """``YYYYMMDD<sep>HHMMSS`` for backup and set-aside file names."""
    now = utc_now() if utc else datetime.now()
    return now.strftime(f"%Y%m%d{sep}%H%M%S")


def localize(dt: datetime) -> datetime:
    """Attach the local zone to a naive datetime; aware values pass through."""
    if dt.tzinfo is not None:
        return dt
    if _LOCAL_TZ is not None:
        return dt.replace(tzinfo=_LOCAL_TZ)
    try:
        return dt.astimezone()
    except (OverflowError, OSError, ValueError):
        # Windows cannot resolve the zone for dates before 1970.
        return dt.replace(tzinfo=datetime.now().astimezone().tzinfo)


def parse_iso(value: str) -> datetime:
    """Parse an ISO date or timestamp into an aware datetime, or raise ValueError.

    Accepts a date, a timestamp with optional fraction, and an optional
    ``Z``/``±HH:MM`` offset (a trailing ``Z`` is stripped because
    ``fromisoformat`` rejects it before Python 3.11). Naive input is local time.
    """
    iso = value.strip()
    zulu = iso.endswith(("Z", "z"))
    if zulu:
        iso = iso[:-1]
    parsed = datetime.fromisoformat(iso)
    if zulu and parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return localize(parsed)


def parse_timestamp(value) -> Optional[datetime]:
    """Aware datetime for a stored timestamp, or None if absent or unparseable."""
    if isinstance(value, datetime):
        return localize(value)
    if isinstance(value, date):
        return localize(datetime(value.year, value.month, value.day))
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


def recency_key(value) -> tuple:
    """Sort key for newest-first order (``reverse=True``) on a stored timestamp.

    Parsed values order by instant, so naive local and UTC values interleave
    correctly. Unparseable strings follow them in text order; missing or
    non-string values come last.
    """
    if not isinstance(value, str) or not value:
        return (0, _MIN, "")
    parsed = parse_timestamp(value)
    if parsed is None:
        return (1, _MIN, value)
    return (2, parsed, value)


def recency_floor(values: Iterable) -> str:
    """Text lower bound that every value newer than the oldest of ``values`` meets.

    A stored value's text is its instant in UTC or in a local zone, never more
    than a day from its UTC date, so one day below the oldest instant's UTC
    date bounds the text of anything newer. ``""`` when there is no usable
    bound (an unparseable value, or the calendar floor).
    """
    instants = [parse_timestamp(value) for value in values]
    if not instants or any(i is None for i in instants):
        return ""
    try:
        return (min(instants).astimezone(timezone.utc) - timedelta(days=1)).date().isoformat()
    except OverflowError:
        return ""
