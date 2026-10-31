"""Persisted timestamps (timeutil) and readers over palaces holding both forms.

Writers used to store naive local wall-clock time; they now store UTC with an
offset. Stored values are never rewritten, so every reader must place both
forms on one timeline. The local zone is pinned through ``timeutil._LOCAL_TZ``
because ``time.tzset`` does not exist on Windows.
"""

import os
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from _mcp_server_helpers import _patch_mcp_server

from mempalace import timeutil
from mempalace.timeutil import (
    filename_stamp,
    localize,
    parse_iso,
    parse_timestamp,
    utc_now_iso,
    utc_now_z,
)

UTC = timezone.utc
ZONES = [timezone(timedelta(hours=-3)), timezone(timedelta(hours=9))]


class TestProducers:
    def test_utc_now_iso_is_utc_with_offset_and_microseconds(self):
        value = utc_now_iso()
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00", value)
        assert abs(datetime.now(UTC) - datetime.fromisoformat(value)) < timedelta(minutes=1)

    def test_utc_now_z(self):
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", utc_now_z())

    def test_filename_stamp(self):
        assert re.fullmatch(r"\d{8}-\d{6}", filename_stamp())
        assert re.fullmatch(r"\d{8}_\d{6}", filename_stamp("_"))
        assert re.fullmatch(r"\d{8}T\d{6}", filename_stamp("T", utc=True))


class TestParsing:
    @pytest.mark.parametrize("zone", ZONES)
    def test_naive_value_is_local_time(self, monkeypatch, zone):
        monkeypatch.setattr(timeutil, "_LOCAL_TZ", zone)
        parsed = parse_timestamp("2026-03-02T10:00:00")
        assert parsed == datetime(2026, 3, 2, 10, tzinfo=zone)

    @pytest.mark.parametrize("zone", ZONES)
    def test_offset_and_zulu_values_ignore_the_local_zone(self, monkeypatch, zone):
        monkeypatch.setattr(timeutil, "_LOCAL_TZ", zone)
        expected = datetime(2026, 3, 2, 10, tzinfo=UTC)
        assert parse_timestamp("2026-03-02T10:00:00.000000+00:00") == expected
        assert parse_timestamp("2026-03-02T10:00:00Z") == expected
        assert parse_timestamp("2026-03-02T19:00:00+09:00") == expected

    def test_date_inputs(self, monkeypatch):
        zone = ZONES[1]
        monkeypatch.setattr(timeutil, "_LOCAL_TZ", zone)
        assert parse_timestamp("2026-03-02") == datetime(2026, 3, 2, tzinfo=zone)
        assert parse_timestamp(date(2026, 3, 2)) == datetime(2026, 3, 2, tzinfo=zone)
        assert parse_timestamp(datetime(2026, 3, 2)) == datetime(2026, 3, 2, tzinfo=zone)

    @pytest.mark.parametrize("value", [None, "", "   ", "garbage", 12345, "2026-13-40"])
    def test_unusable_values_are_none(self, value):
        assert parse_timestamp(value) is None

    def test_parse_iso_raises_on_garbage(self):
        with pytest.raises(ValueError):
            parse_iso("next tuesday")

    def test_system_zone_gives_aware_values(self, monkeypatch):
        monkeypatch.setattr(timeutil, "_LOCAL_TZ", None)
        assert localize(datetime(2026, 3, 2, 10)).utcoffset() is not None
        # Windows cannot resolve the zone before 1970; this must not raise.
        assert localize(datetime(1901, 1, 1)).utcoffset() is not None


# Two drawers from before the upgrade (naive local) and two after (UTC),
# one day apart, so text order and instant order agree.
_INSTANTS = {
    "legacy_1": datetime(2026, 3, 1, 10, tzinfo=UTC),
    "legacy_2": datetime(2026, 3, 2, 10, tzinfo=UTC),
    "utc_1": datetime(2026, 3, 3, 10, tzinfo=UTC),
    "utc_2": datetime(2026, 3, 4, 10, tzinfo=UTC),
}


def _stored(drawer_id, instant, zone):
    if drawer_id.startswith("legacy"):
        return instant.astimezone(zone).replace(tzinfo=None).isoformat()
    return instant.isoformat(timespec="microseconds")


@pytest.fixture(params=ZONES, ids=["utc-3", "utc+9"])
def mixed_palace(request, monkeypatch, collection):
    zone = request.param
    monkeypatch.setattr(timeutil, "_LOCAL_TZ", zone)
    collection.add(
        ids=list(_INSTANTS),
        documents=[f"Quarterly launch review notes, entry {i}." for i in _INSTANTS],
        metadatas=[
            {
                "wing": "timeline",
                "room": "reviews",
                "source_file": f"{drawer_id}.md",
                "chunk_index": 0,
                "filed_at": _stored(drawer_id, instant, zone),
            }
            for drawer_id, instant in _INSTANTS.items()
        ],
    )
    return zone


# since inclusive, before exclusive; the legacy values sit hours away from
# these bounds in wall-clock text, so a wall-clock compare gets them wrong.
_WINDOWS = [
    (("2026-03-02T10:00:00Z", "2026-03-03T10:00:00Z"), {"legacy_2"}),
    (("2026-03-02T10:00:01Z", "2026-03-03T10:00:01Z"), {"utc_1"}),
    (("2026-03-01T10:00:01Z", None), {"legacy_2", "utc_1", "utc_2"}),
    ((None, "2026-03-03T09:59:59Z"), {"legacy_1", "legacy_2"}),
]


@pytest.mark.parametrize(("bounds", "expected"), _WINDOWS)
def test_list_drawers_window_over_mixed_forms(
    monkeypatch, config, kg, mixed_palace, bounds, expected
):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace.mcp_server import tool_list_drawers

    since, before = bounds
    result = tool_list_drawers(since=since, before=before, limit=50)
    assert {d["drawer_id"] for d in result["drawers"]} == expected


@pytest.mark.parametrize("vector_disabled", [False, True])
@pytest.mark.parametrize(("bounds", "expected"), _WINDOWS)
def test_search_window_over_mixed_forms(
    palace_path, mixed_palace, bounds, expected, vector_disabled
):
    from mempalace.searcher import search_memories

    since, before = bounds
    result = search_memories(
        "quarterly launch review notes",
        palace_path,
        n_results=10,
        since=since,
        before=before,
        vector_disabled=vector_disabled,
    )
    assert "error" not in result
    assert {r["source_file"][:-3] for r in result["results"]} == expected


def test_local_day_window_over_mixed_forms(monkeypatch, config, kg, mixed_palace):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace.mcp_server import tool_list_drawers

    for drawer_id, instant in _INSTANTS.items():
        day = instant.astimezone(mixed_palace).date()
        result = tool_list_drawers(
            since=day.isoformat(), before=(day + timedelta(days=1)).isoformat()
        )
        assert [d["drawer_id"] for d in result["drawers"]] == [drawer_id]


def test_recency_order_over_mixed_forms(palace_path, mixed_palace):
    from mempalace.palace import get_collection

    recent = get_collection(palace_path).get_recent(limit=10)
    assert recent.ids == ["utc_2", "utc_1", "legacy_2", "legacy_1"]


# An older writer (hub, hook, peer) keeps filing naive local values after the
# upgrade. In UTC+9, five legacy drawers at 10:00-10:40 local and one UTC
# drawer 8h10m later: the UTC text ("...T09:50:00...+00:00") sorts below the
# legacy text, yet it is the newest drawer.
_DEMO_ZONE = timezone(timedelta(hours=9))
_DEMO_LEGACY = [f"2026-10-02T10:{m}0:00" for m in range(5)]
_DEMO_UTC = "2026-10-02T09:50:00.000000+00:00"
_DEMO_NEWEST_FIRST = ["utc_new", "legacy_4", "legacy_3", "legacy_2", "legacy_1"]


def _demo_rows(**meta):
    stamps = {f"legacy_{i}": value for i, value in enumerate(_DEMO_LEGACY)}
    stamps["utc_new"] = _DEMO_UTC
    ids = list(stamps)
    metas = [{"wing": "timeline", "room": "notes", "filed_at": stamps[i], **meta} for i in ids]
    return ids, [f"Drawer {i}." for i in ids], metas


@pytest.fixture
def demo_chroma(monkeypatch, collection, palace_path):
    monkeypatch.setattr(timeutil, "_LOCAL_TZ", _DEMO_ZONE)
    ids, docs, metas = _demo_rows()
    collection.add(ids=ids, documents=docs, metadatas=metas)
    return palace_path


@pytest.mark.parametrize("equalities", [[], [("wing", ("timeline",))]], ids=["index", "filter"])
def test_chroma_recent_records_keep_newest_utc_entry(demo_chroma, equalities):
    from mempalace.backends.chroma_sqlite import _sqlite_recent_records

    records = _sqlite_recent_records(
        demo_chroma, "mempalace_drawers", limit=5, equalities=equalities, order_field="filed_at"
    )
    assert [record_id for record_id, _, _ in records] == _DEMO_NEWEST_FIRST


def test_chroma_get_recent_and_wake_up_window_keep_newest_utc_entry(demo_chroma):
    from mempalace.layers import fetch_recent_window
    from mempalace.palace import get_collection

    col = get_collection(demo_chroma)
    assert col.get_recent(limit=5).ids == _DEMO_NEWEST_FIRST
    _docs, metas = fetch_recent_window(col, max_scan=5, budget=None)
    assert metas[0]["filed_at"] == _DEMO_UTC


def test_sqlite_exact_get_recent_keeps_newest_utc_entry(monkeypatch, tmp_path):
    from mempalace.backends import PalaceRef
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    monkeypatch.setattr(timeutil, "_LOCAL_TZ", _DEMO_ZONE)
    backend = SQLiteExactBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    try:
        col = backend.get_collection(
            palace=palace, collection_name="mempalace_drawers", create=True
        )
        ids, docs, metas = _demo_rows()
        col.add(ids=ids, documents=docs, metadatas=metas, embeddings=[[1.0, 0.0]] * len(ids))
        assert col.get_recent(limit=5).ids == _DEMO_NEWEST_FIRST
    finally:
        backend.close_palace(str(tmp_path))


def test_diary_read_keeps_newest_utc_entry(demo_chroma, monkeypatch, config, kg):
    from mempalace.backends.chroma_sqlite import sqlite_diary_rows

    ids, docs, metas = _demo_rows(agent="testagent", topic="general")
    for meta in metas:
        meta.update(room="diary", wing="wing_alpha")
    from mempalace.palace import get_collection

    get_collection(demo_chroma).upsert(ids=ids, documents=docs, metadatas=metas)
    total, rows = sqlite_diary_rows(
        demo_chroma, "mempalace_drawers", agent_name="testagent", limit=5
    )
    assert total == 6
    assert [row_id for row_id, _, _ in rows] == _DEMO_NEWEST_FIRST

    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_sqlite_diary_read", lambda *a, **k: None)
    entries = mcp_server.tool_diary_read(agent_name="testagent", last_n=5)["entries"]
    assert entries[0]["timestamp"] == _DEMO_UTC
    assert len(entries) == 5


@pytest.mark.parametrize("zone", ZONES, ids=["utc-3", "utc+9"])
def test_bm25_prefilter_keeps_legacy_value_across_the_utc_day(
    monkeypatch, collection, palace_path, zone
):
    # A naive local value can carry a different calendar date than its UTC
    # instant; the day-granular SQL narrowing must not drop it.
    from mempalace.searcher import search_memories

    monkeypatch.setattr(timeutil, "_LOCAL_TZ", zone)
    instant = datetime(2026, 3, 2, 1 if zone.utcoffset(None) < timedelta(0) else 20, tzinfo=UTC)
    stored = instant.astimezone(zone).replace(tzinfo=None).isoformat()
    assert stored[:10] != instant.date().isoformat()
    collection.add(
        ids=["edge"],
        documents=["Quarterly launch review notes near midnight."],
        metadatas=[
            {"wing": "timeline", "room": "reviews", "source_file": "edge.md", "filed_at": stored}
        ],
    )
    result = search_memories(
        "quarterly launch review notes",
        palace_path,
        since=(instant - timedelta(minutes=30)).isoformat(),
        before=(instant + timedelta(minutes=30)).isoformat(),
        vector_disabled=True,
    )
    assert [r["source_file"] for r in result["results"]] == ["edge.md"]


@pytest.mark.parametrize("zone", ZONES, ids=["utc-3", "utc+9"])
def test_kg_temporal_queries_ignore_the_local_zone(monkeypatch, seeded_kg, zone):
    monkeypatch.setattr(timeutil, "_LOCAL_TZ", zone)
    seeded_kg.add_triple("Max", "attends", "camp", valid_from="2026-03-02T10:00:00Z")

    def objects(as_of):
        return {r["object"] for r in seeded_kg.query_entity("Max", as_of=as_of)}

    assert "chess" in objects("2024-07-01")
    assert "swimming" not in objects("2024-07-01")
    assert "camp" not in objects("2026-03-02T09:59:59Z")
    assert "camp" in objects("2026-03-02T10:00:00Z")
    employers = {r["object"] for r in seeded_kg.query_entity("Alice", as_of="2025-06-01")}
    assert "NewCo" in employers
    assert "Acme Corp" not in employers


def test_logstream_order_is_append_order(monkeypatch, palace_path):
    from mempalace import logstream as logstream_mod

    ls = logstream_mod.Logstream(db_path=os.path.join(palace_path, "logstream.sqlite3"))
    try:
        # Wall clock stepping backwards must not reorder the stream.
        stamps = iter(["2026-03-02T10:00:02Z", "2026-03-02T10:00:01Z", "2026-03-02T10:00:00Z"])
        monkeypatch.setattr(logstream_mod, "_utc_now_iso", lambda: next(stamps))
        appended = [
            ls.append_event("status", "project/t", "status", "agent:a", body=str(i))["id"]
            for i in range(3)
        ]
        listed = [e["id"] for e in ls.list_events(stream="project/t")]
        assert listed == appended
    finally:
        ls.close()
