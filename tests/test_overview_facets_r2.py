"""Deterministic facet-call, failure and unsupported-acquisition witnesses."""

from collections import Counter
from types import SimpleNamespace

import pytest

TOOLS = ["tool_status", "tool_list_wings", "tool_list_rooms", "tool_get_taxonomy"]


@pytest.fixture
def overview(monkeypatch, config):
    import mempalace.mcp_server as m

    for name, value in {
        "_config": config,
        "_sqlite_taxonomy": lambda: None,
        "_ensure_sqlite_integrity_status": lambda: None,
        "_sqlite_integrity_errors": [],
        "_refresh_vector_disabled_flag": lambda: None,
        "_vector_disabled": False,
        "_describe_device_safe": lambda: "cpu",
        "_selected_backend_name": lambda: "pgvector",
    }.items():
        monkeypatch.setattr(m, name, value)
    m._invalidate_overview_caches()
    return m


class Facets:
    def __init__(self, wings=3, rooms=4, failure=None):
        self._backend = SimpleNamespace(capabilities={"supports_metadata_facets"})
        self.rows = []
        for w in range(wings):
            wing = f"w{w:02d}"
            for r in range(rooms):
                self.rows.extend([{"wing": wing, "room": f"r{r:02d}"}] * 3)
            self.rows.extend(
                [
                    {"wing": wing, "room": "r00", "ingest_mode": "registry"},
                    {"wing": wing, "room": "r01", "is_sentinel": True},
                    {"wing": wing, "room": "r02", "ingest_mode": "registry", "is_sentinel": True},
                ]
            )
        self.calls, self.failure, self.gets = [], failure, 0

    def count(self):
        return len(self.rows)

    def facet_counts(self, field, where=None, limit=1000):
        from mempalace.backends._filters import matches_where

        self.calls.append((field, where, limit))
        if field == "is_sentinel" and self.failure:
            raise self.failure
        result = Counter(
            str(m[field])
            for m in self.rows
            if field in m and (not where or matches_where(m, where))
        )
        return dict(list(result.items())[:limit])

    def get(self, **kw):
        self.gets += 1
        raise AssertionError("facet tool acquired raw metadata")


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("kind", ["backend", "closed"])
def test_bookkeeping_backend_failure_refuses(overview, monkeypatch, tool, kind):
    from mempalace.backends import BackendClosedError, BackendError

    exc = BackendError("synthetic failure") if kind == "backend" else BackendClosedError("closed")
    col = Facets(failure=exc)
    monkeypatch.setattr(overview, "_get_collection", lambda **kw: col)
    result = getattr(overview, tool)()
    assert result.get("error") == "overview_unavailable"
    assert result.get("partial") is True
    assert result.get("total_drawers") is None
    assert not result.get("bookkeeping_error")
    assert not result.get("wings") and not result.get("rooms") and not result.get("taxonomy")
    assert col.gets == 0


@pytest.mark.parametrize("wings,rooms", [(30, 12), (40, 20)])
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("wingless", [False, True])
def test_facet_calls_are_linear_in_wings(overview, monkeypatch, tool, wings, rooms, wingless):
    col = Facets(wings, rooms)
    if wingless:
        col.rows.append({"room": "r05"})
    monkeypatch.setattr(overview, "_get_collection", lambda **kw: col)
    result = getattr(overview, tool)()
    assert "error" not in result and not result.get("partial")
    if tool == "tool_status":
        assert result["total_drawers"] == wings * rooms * 3 + int(wingless)
    elif tool == "tool_list_wings":
        assert sum(result["wings"].values()) == wings * rooms * 3 + int(wingless)
    elif tool == "tool_list_rooms":
        assert sum(result["rooms"].values()) == wings * rooms * 3 + int(wingless)
    else:
        assert sum(sum(rs.values()) for rs in result["taxonomy"].values()) == wings * rooms * 3
    bound = {
        "tool_status": 9 + 3 * wings,
        "tool_list_wings": 8 + 3 * wings,
        "tool_list_rooms": 9 + 3 * wings,
        "tool_get_taxonomy": 8 + 4 * wings,
    }[tool]
    assert len(col.calls) <= bound + 2 * int(wingless)
    # Facet taxonomy enumerates only keyed wings and needs no wingless totals.
    extra = 2 * int(wingless and tool != "tool_get_taxonomy")
    assert len(col.calls) == bound + extra
    assert all(limit == 16385 for _, _, limit in col.calls)
    assert col.gets == 0


def test_transient_overview_error_is_not_cached(monkeypatch, config):
    import mempalace.mcp_server as m
    from mempalace.backends import chroma_sqlite
    from mempalace.overview import OverviewError

    calls = []

    def acquire(*args):
        calls.append(args)
        if len(calls) == 1:
            raise OverviewError("overview_unavailable")
        return 1, {"w": {"r": 1}}

    monkeypatch.setattr(m, "_config", config)
    monkeypatch.setattr(m, "_is_chroma_backend", lambda: True)
    monkeypatch.setattr(m, "_palace_db_fingerprint", lambda: ("fixed",))
    monkeypatch.setattr(chroma_sqlite, "_sqlite_wing_room_counts", acquire)
    m._invalidate_overview_caches()
    assert m._sqlite_taxonomy() == {"error": "overview_unavailable"}
    assert m._sqlite_taxonomy() == (1, {"w": {"r": 1}})
    assert len(calls) == 2


def test_lost_fingerprint_recounts_without_invalidation(monkeypatch, config):
    import mempalace.mcp_server as m
    from mempalace.backends import chroma_sqlite
    from mempalace.overview import OverviewError

    calls, fingerprint = [], [("fixed",)]

    def acquire(*args):
        calls.append(args)
        raise OverviewError("overview_too_large")

    monkeypatch.setattr(m, "_config", config)
    monkeypatch.setattr(m, "_is_chroma_backend", lambda: True)
    monkeypatch.setattr(m, "_palace_db_fingerprint", lambda: fingerprint[0])
    monkeypatch.setattr(m.time, "time", lambda: 100.0)
    monkeypatch.setattr(chroma_sqlite, "_sqlite_wing_room_counts", acquire)
    m._invalidate_overview_caches()
    assert m._sqlite_taxonomy() == {"error": "overview_too_large"}
    fingerprint[0] = None
    assert m._sqlite_taxonomy() == {"error": "overview_too_large"}
    assert len(calls) == 2


@pytest.mark.parametrize("tool", TOOLS)
def test_unsupported_raw_acquisition_never_calls_get(overview, monkeypatch, tool):
    class Raw:
        gets = 0

        def count(self):
            return 1

        def get(self, limit=None, offset=0, **kw):
            self.gets += 1
            rows = [{"wing": "w", "room": "r"}]
            return {"metadatas": rows[offset : offset + limit] if limit else rows[offset:]}

    col = Raw()
    monkeypatch.setattr(overview, "_get_collection", lambda **kw: col)
    result = getattr(overview, tool)()
    assert result.get("error") == "overview_unavailable"
    assert result.get("partial") is True
    assert col.gets == 0
