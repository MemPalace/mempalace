"""Deterministic overview acquisition and retention regressions."""

import json

import pytest


class TrackingCollection:
    overview_projection_bounded = True

    def __init__(self, rows):
        self.rows, self.full, self.gets, self.batches = rows, 0, [], []
        self.closed, self.fail_at = False, None
        self.projected_calls = []

    def count(self):
        return len(self.rows)

    def get(self, *, limit=None, offset=0, where=None, include=None, **kw):
        self.gets.append((limit, where, include))
        rows = self.rows
        if where and "wing" in where:
            rows = [r for r in rows if r and r.get("wing") in where["wing"]["$in"]]
        rows = rows[offset : offset + limit] if limit else rows
        return {
            "metadatas": [
                {
                    k: v
                    for k, v in (r or {}).items()
                    if k in ("wing", "room", "is_sentinel", "ingest_mode")
                }
                for r in rows
            ]
        }

    def get_all_metadata(self, where=None):
        self.full += 1
        return self.get(where=where)["metadatas"]

    def iter_metadata(self, **kw):
        return self._rows()

    def _rows(self):
        from mempalace.backends.chroma_sqlite import MetadataRecoveryPending

        try:
            for i, row in enumerate(self.rows):
                if i == self.fail_at:
                    self.fail_at = None
                    raise MetadataRecoveryPending("synthetic recovery")
                yield row
        finally:
            self.closed = True

    def iter_overview_metadata(self, where=None):
        from mempalace.overview import projected_batches

        self.projected_calls.append(where)
        rows = self._rows()
        if where:
            rows = (r for r in rows if r and r.get("wing") in where["wing"]["$in"])
        for batch in projected_batches(rows):
            self.batches.append(
                len(json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode())
            )
            yield batch


@pytest.fixture
def overview(monkeypatch, config):
    import mempalace.mcp_server as m

    for name, value in {
        "_config": config,
        "_sqlite_taxonomy": lambda: None,
        "_supports_metadata_facets": lambda col: False,
        "_ensure_sqlite_integrity_status": lambda: None,
        "_sqlite_integrity_errors": [],
        "_refresh_vector_disabled_flag": lambda: None,
        "_vector_disabled": False,
        "_describe_device_safe": lambda: "cpu",
    }.items():
        monkeypatch.setattr(m, name, value)
    m._invalidate_overview_caches()

    def install(rows):
        col = TrackingCollection(rows)
        monkeypatch.setattr(m, "_get_collection", lambda **kw: col)
        return m, col

    return install


@pytest.mark.parametrize(
    "tool,key",
    [
        ("tool_status", "total_drawers"),
        ("tool_list_wings", "wings"),
        ("tool_list_rooms", "rooms"),
        ("tool_get_taxonomy", "taxonomy"),
    ],
)
def test_fallback_streams_without_raw_cache(overview, tool, key):
    m, col = overview([{"wing": "w", "room": "r", "unrelated": "z" * 2048} for _ in range(5001)])
    result = getattr(m, tool)()
    expected = (
        5001
        if key == "total_drawers"
        else ({"w": {"r": 5001}} if key == "taxonomy" else {("w" if key == "wings" else "r"): 5001})
    )
    assert result[key] == expected
    assert col.full == 0
    assert m._metadata_cache is None
    assert max(col.batches) <= 1048576


@pytest.mark.parametrize("at", [0, 500])
def test_lazy_recovery_falls_through_without_full_load(overview, at):
    m, col = overview([{"wing": "w", "room": "r"} for _ in range(501)])
    col.fail_at = at
    result = m.tool_status()
    assert result["total_drawers"] == 501
    assert col.full == 0
    assert col.closed
    assert all(limit and limit <= 500 for limit, _, _ in col.gets)
    assert m._metadata_cache is None


def test_projected_page_splits_by_encoded_bytes(overview):
    m, col = overview([{"wing": "é" * 15000, "room": "r"} for _ in range(80)])
    result = m.tool_status()
    assert result["total_drawers"] == 80
    assert col.full == 0
    assert len(col.batches) > 1
    assert max(col.batches) <= 1048576


def test_filtered_rooms_reduces_bounded_pages(overview):
    m, col = overview(
        [{"wing": "w", "room": "r"} for _ in range(501)] + [{"wing": "other", "room": "hidden"}]
    )
    result = m.tool_list_rooms(wing="w")
    assert result["rooms"] == {"r": 501}
    assert col.projected_calls == [None]
    assert col.full == 0
    assert m._metadata_cache is None


@pytest.mark.parametrize(
    "rows",
    [[{"wing": str(i), "room": "r"} for i in range(16385)], [{"wing": "x" * 65537, "room": "r"}]],
)
def test_aggregate_and_projected_cell_limits_fail_explicitly(overview, rows):
    m, col = overview(rows)
    result = m.tool_get_taxonomy()
    assert result.get("error") == "overview_too_large"
    assert not result.get("taxonomy")
    assert m._metadata_cache is None


def test_missing_projected_keys_preserve_legacy_totals_green(overview):
    m, col = overview(
        [
            None,
            {},
            {"other": "value"},
            {"wing": "w"},
            {"room": "r"},
            {"wing": "w", "room": "r", "is_sentinel": "true"},
            {"ingest_mode": "registry"},
        ]
    )
    result = m.tool_status()
    assert result["total_drawers"] == 5
    assert result["wings"] == {"unknown": 4, "w": 1}
    assert result["rooms"] == {"unknown": 4, "r": 1}


def test_bookkeeping_subtraction_is_bounded(overview):
    from test_overview_review_regressions import Facets

    m, _ = overview([])
    col = Facets()
    col.rows = [{"wing": "w", "room": "r", "is_sentinel": "true"} for _ in range(501)]
    assert m._bookkeeping_taxonomy(col) == {"w": {"r": 501}}
    assert all(limit == 16385 for _, _, limit in col.calls)


def test_real_sql_projection_preserves_presence_green(
    monkeypatch, config, palace_path, collection, kg
):
    from _mcp_server_helpers import _patch_mcp_server

    import mempalace.mcp_server as m

    collection.add(
        ids=["empty", "unrelated", "wing", "room"],
        documents=["exact"] * 4,
        metadatas=[{"other": "x"}, {"other": "y"}, {"wing": "w"}, {"room": "r"}],
    )
    _patch_mcp_server(monkeypatch, config, kg)
    monkeypatch.setattr(m, "_sqlite_taxonomy", lambda: None)
    result = m.tool_status()
    assert result["total_drawers"] == 4
    assert result["wings"] == {"unknown": 3, "w": 1}
    assert result["rooms"] == {"unknown": 3, "r": 1}
