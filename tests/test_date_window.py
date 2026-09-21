"""Unit tests for mempalace.date_window — shared since/before parsing (#463).

The same helpers back the ``list_drawers`` filter (#1128) via aliases in
``mcp_server``; behavioral coverage for that surface lives in
``test_mcp_server.py``. These tests pin the module contract directly.
"""

from datetime import datetime

import pytest

from mempalace.date_window import filed_at_in_window, parse_date_bound, parse_window


class TestParseDateBound:
    def test_none_and_blank_mean_no_filter(self):
        assert parse_date_bound(None) is None
        assert parse_date_bound("") is None
        assert parse_date_bound("   ") is None

    def test_date_only(self):
        assert parse_date_bound("2026-04-01") == datetime(2026, 4, 1)

    def test_naive_datetime(self):
        assert parse_date_bound("2026-04-01T09:30:00") == datetime(2026, 4, 1, 9, 30)

    def test_fractional_seconds(self):
        assert parse_date_bound("2026-04-01T09:30:00.250000") == datetime(
            2026, 4, 1, 9, 30, 0, 250000
        )

    def test_zulu_datetime_parses_on_39_floor(self):
        assert parse_date_bound("2026-04-01T09:30:00Z") == datetime(2026, 4, 1, 9, 30)

    def test_zulu_date_only(self):
        # Regression: appending "+00:00" instead of stripping Z broke this
        # exact shape on Python 3.9/3.10 (caught by review on #1891).
        assert parse_date_bound("2026-04-01Z") == datetime(2026, 4, 1)

    def test_offset_dropped_wall_clock(self):
        assert parse_date_bound("2026-04-01T09:30:00+05:00") == datetime(2026, 4, 1, 9, 30)

    def test_non_string_rejected(self):
        with pytest.raises(ValueError, match="since"):
            parse_date_bound(20260401, "since")

    def test_garbage_rejected_names_field(self):
        with pytest.raises(ValueError, match="before"):
            parse_date_bound("next tuesday", "before")


class TestParseWindow:
    def test_both_none(self):
        assert parse_window() == (None, None)

    def test_valid_window(self):
        since_dt, before_dt = parse_window("2026-04-01", "2026-04-10")
        assert since_dt == datetime(2026, 4, 1)
        assert before_dt == datetime(2026, 4, 10)

    def test_inverted_window_rejected(self):
        with pytest.raises(ValueError, match="must be earlier than"):
            parse_window("2026-04-10", "2026-04-01")

    def test_equal_bounds_rejected(self):
        with pytest.raises(ValueError, match="must be earlier than"):
            parse_window("2026-04-01", "2026-04-01")

    def test_invalid_since_names_field(self):
        with pytest.raises(ValueError, match="since"):
            parse_window("nope", None)


class TestFiledAtInWindow:
    def test_no_bounds_accepts_anything(self):
        assert filed_at_in_window("2026-01-01T00:00:00", None, None)

    def test_since_inclusive(self):
        since = datetime(2026, 1, 2)
        assert filed_at_in_window("2026-01-02T00:00:00", since, None)
        assert not filed_at_in_window("2026-01-01T23:59:59", since, None)

    def test_before_exclusive(self):
        before = datetime(2026, 1, 4)
        assert filed_at_in_window("2026-01-03T23:59:59", None, before)
        assert not filed_at_in_window("2026-01-04T00:00:00", None, before)

    def test_missing_filed_at_excluded_when_bound_active(self):
        assert not filed_at_in_window(None, datetime(2026, 1, 1), None)
        assert not filed_at_in_window("", datetime(2026, 1, 1), None)

    def test_unparseable_filed_at_excluded(self):
        assert not filed_at_in_window("not-a-date", datetime(2026, 1, 1), None)

    def test_aware_filed_at_compared_wall_clock(self):
        # diary_ingest stamps aware UTC ("+00:00"); the offset is dropped and
        # the wall-clock fields are compared.
        since = datetime(2026, 1, 5)
        assert filed_at_in_window("2026-01-05T00:00:00+00:00", since, None)
        assert not filed_at_in_window("2026-01-04T23:59:59+00:00", since, None)
