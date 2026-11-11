"""Due-time calculation: pure functions, plain datetimes as the clock."""

from datetime import datetime, timedelta, timezone

import pytest

from commonplace import schedule_time as t

UTC = timezone.utc
NOW = datetime(2026, 11, 11, 10, 7, tzinfo=UTC)


def due(schedule, tz="UTC", after=NOW, anchor=None):
    return t.next_due(schedule, tz, after, anchor)


def test_interval_counts_from_the_anchor_and_is_strictly_after():
    anchor = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    every = {"every_minutes": 30}
    assert due(every, after=NOW, anchor=anchor) == anchor + timedelta(minutes=30)
    exact = anchor + timedelta(minutes=30)
    assert due(every, after=exact, anchor=anchor) == anchor + timedelta(minutes=60)
    assert due(every, after=anchor - timedelta(days=1), anchor=anchor) == exact


def test_cron_weekday_morning_in_a_timezone():
    # 09:00 Amsterdam on weekdays; 2026-11-11 is a Wednesday, UTC+1 in November.
    first = due({"cron": "0 9 * * 1-5"}, "Europe/Amsterdam")
    assert first == datetime(2026, 11, 12, 8, 0, tzinfo=UTC)
    friday = datetime(2026, 11, 13, 8, 0, tzinfo=UTC)
    assert due({"cron": "0 9 * * 1-5"}, "Europe/Amsterdam", friday) == datetime(
        2026, 11, 16, 8, 0, tzinfo=UTC
    )


def test_cron_steps_lists_and_sunday_as_zero_or_seven():
    assert due({"cron": "*/20 * * * *"}) == datetime(2026, 11, 11, 10, 20, tzinfo=UTC)
    assert due({"cron": "5,50 11 * * *"}) == datetime(2026, 11, 11, 11, 5, tzinfo=UTC)
    sunday = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
    assert due({"cron": "0 0 * * 0"}) == sunday == due({"cron": "0 0 * * 7"})


def test_day_of_month_and_weekday_both_restricted_match_either():
    # 13th of the month OR a Monday: after Wed 11 Nov the Friday 13th comes first.
    assert due({"cron": "0 0 13 * 1"}) == datetime(2026, 11, 13, 0, 0, tzinfo=UTC)


def test_spring_forward_gap_is_skipped_and_fall_back_fires_once():
    gap = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)  # 02:30 local does not exist that night
    assert due({"cron": "30 2 * * *"}, "Europe/Amsterdam", gap) == datetime(
        2026, 3, 30, 0, 30, tzinfo=UTC
    )
    day = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)  # 02:30 local happens twice on 25 Oct
    first = due({"cron": "30 2 * * *"}, "Europe/Amsterdam", day)
    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # the first occurrence only
    assert due({"cron": "30 2 * * *"}, "Europe/Amsterdam", first) == datetime(
        2026, 10, 26, 1, 30, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "schedule, tz",
    [
        ({"every_minutes": 14}, "UTC"),
        ({"every_minutes": 0}, "UTC"),
        ({"every_minutes": True}, "UTC"),
        ({"every_minutes": "60"}, "UTC"),
        ({"every_minutes": 7 * 24 * 60 + 1}, "UTC"),
        ({"every_minutes": 60, "cron": "0 * * * *"}, "UTC"),
        ({}, "UTC"),
        ("0 9 * * *", "UTC"),
        ({"cron": "*/5 * * * *"}, "UTC"),
        ({"cron": "0,10 * * * *"}, "UTC"),
        ({"cron": "0 9 * *"}, "UTC"),
        ({"cron": "60 9 * * *"}, "UTC"),
        ({"cron": "0 24 * * *"}, "UTC"),
        ({"cron": "0 9 * * mon"}, "UTC"),
        ({"cron": "0 9 */0 * *"}, "UTC"),
        ({"cron": "0 9 31 2 *"}, "UTC"),
        ({"cron": "0 9 * * *" + "x" * 100}, "UTC"),
        ({"cron": "0 9 * * *"}, "Mars/Olympus"),
        ({"cron": "0 9 * * *"}, "../etc/passwd"),
        ({"cron": "0 9 * * *"}, ""),
        ({"cron": "0 9 * * *"}, None),
        ({"cron": "0 9 * * *", "extra": 1}, "UTC"),
    ],
)
def test_refuses_invalid_schedules(schedule, tz):
    with pytest.raises(ValueError):
        t.validate(schedule, tz, NOW)


def test_validate_returns_canonical_form_and_respects_a_custom_minimum():
    assert t.validate({"cron": "  0   9 * *  1-5 "}, "UTC", NOW) == {"cron": "0 9 * * 1-5"}
    assert t.validate({"every_minutes": 15}, "UTC", NOW) == {"every_minutes": 15}
    assert t.validate({"cron": "*/30 * * * *"}, "UTC", NOW, min_interval=30)
    with pytest.raises(ValueError):
        t.validate({"cron": "*/15 * * * *"}, "UTC", NOW, min_interval=30)


def test_stamp_round_trip():
    assert t.stamp(NOW) == "2026-11-11T10:07:00Z"
    assert t.parse_stamp("2026-11-11T10:07:00Z") == NOW
