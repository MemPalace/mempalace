"""Due-time calculation for schedules: one small, pure module any feature can reuse.

A schedule is a plain dict with exactly one of two forms and a timezone:

    {"every_minutes": 60}        an interval, counted from an anchor instant
    {"cron": "0 9 * * 1-5"}      five fields: minute hour day-of-month month day-of-week

Cron fields take ``*``, numbers, ``a-b`` ranges, ``*/s`` and ``a-b/s`` steps and comma
lists (day-of-week 0-6 with 0 = Sunday, 7 also means Sunday). Names are not supported.
As in classic cron, a day matches when both day fields are restricted and either matches.
The timezone is an IANA name; wall times that do not exist (spring-forward gap) are
skipped and a repeated wall time (fall-back) fires once, on its first occurrence.

There is no I/O and no clock here: callers pass instants in, so a fake clock is a plain
``datetime``. Times in and out are timezone-aware UTC ``datetime`` values.

    validate(schedule, tz, now)           -> canonical schedule dict, or ValueError
    next_due(schedule, tz, after, anchor) -> first due instant strictly after ``after``
    stamp(dt) / parse_stamp(text)         -> the ``YYYY-MM-DDTHH:MM:SSZ`` form stored in tables
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 7 * 24 * 60
SEARCH_DAYS = 366 * 8  # a cron that never matches within eight years is refused
_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day-of-month", 1, 31), ("month", 1, 12))
_DOW = ("day-of-week", 0, 7)
_PART = re.compile(r"(\*|\d{1,2}(?:-\d{1,2})?)(?:/(\d{1,2}))?")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_STAMP)


def parse_stamp(text: str) -> datetime:
    return datetime.strptime(text, _STAMP).replace(tzinfo=timezone.utc)


def zone(name) -> ZoneInfo:
    if not isinstance(name, str) or not name or len(name) > 64 or name.startswith(("/", ".")):
        raise ValueError("timezone must be an IANA name such as Europe/Amsterdam")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError("timezone must be an IANA name such as Europe/Amsterdam") from exc


def _field(text: str, label: str, low: int, high: int) -> tuple[frozenset, bool]:
    """``(allowed values, restricted)`` of one cron field."""
    values: set[int] = set()
    for part in text.split(","):
        match = _PART.fullmatch(part)
        if not match:
            raise ValueError(f"cron {label} field is not valid")
        base, step = match.group(1), int(match.group(2) or 1)
        if step < 1:
            raise ValueError(f"cron {label} step must be at least 1")
        if base == "*":
            first, last = low, high if label != "day-of-week" else 6
        elif "-" in base:
            first, last = (int(x) for x in base.split("-"))
        else:
            first = int(base)
            last = high if match.group(2) else first
        if not low <= first <= last <= high:
            raise ValueError(f"cron {label} must be from {low} to {high}")
        values.update(range(first, last + 1, step))
    if label == "day-of-week":
        values = {v % 7 for v in values}
    return frozenset(values), not text.startswith("*")


def _cron(text) -> tuple:
    if not isinstance(text, str) or len(text) > 100:
        raise ValueError("cron must be five fields: minute hour day-of-month month day-of-week")
    parts = text.split()
    if len(parts) != 5:
        raise ValueError("cron must be five fields: minute hour day-of-month month day-of-week")
    return tuple(_field(p, *spec) for p, spec in zip(parts, (*_FIELDS, _DOW)))


def _min_gap_minutes(fields: tuple) -> int:
    """Smallest gap between two fire times; a day holds one fire time per (hour, minute)."""
    times = sorted(h * 60 + m for h in fields[1][0] for m in fields[0][0])
    if len(times) < 2:
        return 24 * 60 - 60  # one fire time a day: never closer than a day less a DST hour
    gaps = [b - a for a, b in zip(times, times[1:])] + [24 * 60 - times[-1] + times[0]]
    return min(gaps)


def validate(schedule, tz, now: datetime, *, min_interval: int = MIN_INTERVAL_MINUTES) -> dict:
    """The canonical form of ``schedule`` or ``ValueError`` with a message for the user.

    Refuses unknown keys, both forms at once, an interval or cron gap under ``min_interval``
    minutes and a cron that has no due time in the next eight years."""
    zone(tz)
    if not isinstance(schedule, dict) or len(schedule) != 1:
        raise ValueError("schedule must hold exactly one of every_minutes or cron")
    ((key, value),) = schedule.items()
    if key == "every_minutes":
        if type(value) is not int or not min_interval <= value <= MAX_INTERVAL_MINUTES:
            raise ValueError(
                f"every_minutes must be a whole number from {min_interval} to {MAX_INTERVAL_MINUTES}"
            )
        return {"every_minutes": value}
    if key != "cron":
        raise ValueError("schedule must hold exactly one of every_minutes or cron")
    fields = _cron(value)
    if _min_gap_minutes(fields) < min_interval:
        raise ValueError(f"cron may fire at most once every {min_interval} minutes")
    canonical = {"cron": " ".join(value.split())}
    if next_due(canonical, tz, now) is None:
        raise ValueError("cron never fires")
    return canonical


def next_due(schedule: dict, tz, after: datetime, anchor: datetime | None = None):
    """First due instant strictly after ``after``; ``None`` if a cron never matches.

    ``anchor`` counts an interval schedule (due = anchor + k * interval, k >= 1)."""
    if "every_minutes" in schedule:
        step = timedelta(minutes=schedule["every_minutes"])
        base = anchor or after
        if after < base:
            return base + step
        return base + step * ((after - base) // step + 1)
    minute, hour, dom, month, dow = _cron(schedule["cron"])
    zi = zone(tz)
    local = after.astimezone(zi)
    day = local.date()
    for _ in range(SEARCH_DAYS):
        if _day_matches(day, dom, month, dow):
            for h in sorted(hour[0]):
                for m in sorted(minute[0]):
                    moment = _resolve(day, h, m, zi)
                    if moment is not None and moment > after:
                        return moment
        day += timedelta(days=1)
    return None


def _day_matches(day, dom, month, dow) -> bool:
    if day.month not in month[0]:
        return False
    in_dom, in_dow = day.day in dom[0], (day.weekday() + 1) % 7 in dow[0]
    if dom[1] and dow[1]:
        return in_dom or in_dow
    return in_dom and in_dow


def _resolve(day, hour: int, minute: int, zi: ZoneInfo):
    """UTC instant of a local wall time, or None when it falls in a spring-forward gap."""
    wall = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zi)
    moment = wall.astimezone(timezone.utc)
    if moment.astimezone(zi).replace(tzinfo=None) != wall.replace(tzinfo=None):
        return None
    return moment
