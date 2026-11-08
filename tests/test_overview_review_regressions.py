"""Public facet compatibility and real-wrapper recovery witnesses."""

import types

import pytest


@pytest.fixture
def overview(monkeypatch, config):
    import mempalace.mcp_server as m

    for key, value in {
        "_config": config,
        "_sqlite_taxonomy": lambda: None,
        "_ensure_sqlite_integrity_status": lambda: None,
        "_sqlite_integrity_errors": [],
        "_refresh_vector_disabled_flag": lambda: None,
        "_vector_disabled": False,
        "_describe_device_safe": lambda: "cpu",
    }.items():
        monkeypatch.setattr(m, key, value)
    m._invalidate_overview_caches()
    return m


class Facets:
    def __init__(self):
        self._backend = types.SimpleNamespace(capabilities={"supports_metadata_facets"})
        self.calls = []
        self.rows = [{"wing": "alpha", "room": "r1"}] * 2 + [
            {"wing": "alpha", "room": "r2"},
            {"wing": "beta", "room": "r3"},
        ] * 2
        self.rows += [
            {"wing": "alpha", "room": "r1", "ingest_mode": "registry"},
            {"wing": "beta", "room": "r3", "is_sentinel": "true"},
        ]

    def count(self):
        return len(self.rows)

    def facet_counts(self, field, where=None, limit=1000):
        from mempalace.backends._filters import matches_where

        self.calls.append((field, where, limit))
        counts = {}
        for meta in self.rows:
            if (not where or matches_where(meta, where)) and field in meta:
                k = str(meta[field])
                counts[k] = counts.get(k, 0) + 1
        return dict(list(counts.items())[:limit])

    def get(self, **kw):
        raise AssertionError("facet overview must not acquire raw rows")


@pytest.mark.parametrize("backend", ["pgvector", "qdrant", "sqlite_exact", "compact"])
@pytest.mark.parametrize(
    "tool,key,expected",
    [
        ("tool_status", "total_drawers", 6),
        ("tool_list_wings", "wings", {"alpha": 4, "beta": 2}),
        ("tool_list_rooms", "rooms", {"r1": 2, "r2": 2, "r3": 2}),
        ("tool_get_taxonomy", "taxonomy", {"alpha": {"r1": 2, "r2": 2}, "beta": {"r3": 2}}),
    ],
)
def test_facet_backed_counts_remain_available(overview, monkeypatch, backend, tool, key, expected):
    col = Facets()
    monkeypatch.setattr(overview, "_selected_backend_name", lambda: backend)
    monkeypatch.setattr(overview, "_get_collection", lambda **kw: col)
    result = getattr(overview, tool)()
    assert result.get(key) == expected
    assert "error" not in result
    assert all(0 < limit <= 16385 for _, _, limit in col.calls)
    assert overview._metadata_cache is None


@pytest.mark.parametrize("error_name", ["pending", "operational"])
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("after_batch", [False, True])
def test_real_chroma_reopens_projection_once(
    overview, monkeypatch, collection, palace_path, persistent, after_batch, error_name
):
    import sqlite3

    from mempalace.backends.chroma import ChromaCollection
    from mempalace.backends.chroma_sqlite import MetadataRecoveryPending

    collection.add(
        ids=[f"r{i}" for i in range(501)],
        documents=["verbatim"] * 501,
        metadatas=[{"wing": "w", "room": "r"}] * 501,
    )
    col = ChromaCollection(collection, palace_path=palace_path)
    original = col.iter_overview_metadata
    calls, closed = [], []

    def projected(where=None):
        calls.append(where)
        attempt = len(calls)

        def rows():
            source = original(where=where)
            try:
                if after_batch:
                    yield next(source)
                if persistent or attempt == 1:
                    error = (
                        MetadataRecoveryPending
                        if error_name == "pending"
                        else sqlite3.OperationalError
                    )
                    raise error("synthetic transient")
                yield from source
            finally:
                source.close()
                closed.append(attempt)

        return rows()

    monkeypatch.setattr(col, "iter_overview_metadata", projected)
    monkeypatch.setattr(overview, "_get_collection", lambda **kw: col)
    monkeypatch.setattr(overview, "_supports_metadata_facets", lambda _: False)
    result = overview.tool_status()
    assert len(calls) == 2
    assert closed == [1, 2]
    assert calls == [None, None]
    assert not hasattr(col, "overview_page_bytes_bounded")
    if persistent:
        assert result.get("error") == "overview_unavailable"
        assert result.get("partial") is True
        assert result.get("total_drawers") is None
        assert result["wings"] == result["rooms"] == {}
    else:
        assert result.get("total_drawers") == 501
        assert result["wings"] == {"w": 501}
        assert "error" not in result
    assert overview._metadata_cache is None


def test_fast_refusal_has_normal_envelope(overview, monkeypatch):
    monkeypatch.setattr(overview, "_sqlite_taxonomy", lambda: {"error": "overview_too_large"})
    result = overview.tool_status()
    assert result.get("partial") is True
    assert result.get("total_drawers") is None
    assert result["wings"] == result["rooms"] == {}
    assert "protocol" in result and "backend" in result


def test_oversize_refusal_is_fingerprint_cached(monkeypatch, config):
    import mempalace.mcp_server as m
    from mempalace.backends import chroma_sqlite
    from mempalace.overview import OverviewError

    calls = []
    fingerprint = [("fixed",)]

    def refuse(*args):
        calls.append(args)
        raise OverviewError("overview_too_large")

    monkeypatch.setattr(m, "_config", config)
    monkeypatch.setattr(m, "_is_chroma_backend", lambda: True)
    monkeypatch.setattr(m, "_palace_db_fingerprint", lambda: fingerprint[0])
    monkeypatch.setattr(chroma_sqlite, "_sqlite_wing_room_counts", refuse)
    m._invalidate_overview_caches()
    assert m._sqlite_taxonomy() == m._sqlite_taxonomy() == {"error": "overview_too_large"}
    assert len(calls) == 1
    fingerprint[0] = ("changed",)
    assert m._sqlite_taxonomy() == {"error": "overview_too_large"}
    assert len(calls) == 2


@pytest.mark.parametrize("backend_name", ["pgvector", "qdrant", "sqlite_exact", "compact"])
def test_real_facet_wrappers_keep_public_results(overview, monkeypatch, tmp_path, backend_name):
    import importlib

    from mempalace.backends import PalaceRef

    module = importlib.import_module("mempalace.backends." + backend_name)
    if backend_name == "pgvector":
        from test_pgvector_backend import _FakePgVectorClient

        monkeypatch.setattr(module, "_PgVectorClient", _FakePgVectorClient)
        monkeypatch.delenv("MEMPALACE_PGVECTOR_DSN", raising=False)
    if backend_name == "qdrant":
        from test_qdrant_backend import _FakeQdrantClient

        monkeypatch.setattr(module, "_QdrantRESTClient", _FakeQdrantClient)
        monkeypatch.delenv("MEMPALACE_QDRANT_URL", raising=False)
        monkeypatch.delenv("MEMPALACE_QDRANT_API_KEY", raising=False)
    cls = {
        "pgvector": "PgVectorBackend",
        "qdrant": "QdrantBackend",
        "sqlite_exact": "SQLiteExactBackend",
        "compact": "CompactBackend",
    }[backend_name]
    backend = getattr(module, cls)()
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    try:
        col = backend.get_collection(palace=ref, collection_name="drawers", create=True)
        rows = Facets().rows
        col.add(
            ids=[str(i) for i in range(len(rows))],
            documents=["verbatim"] * len(rows),
            metadatas=rows,
            embeddings=[[0.1, 1]] * len(rows),
        )
        monkeypatch.setattr(overview, "_get_collection", lambda **kw: col)
        monkeypatch.setattr(overview, "_selected_backend_name", lambda: backend_name)
        assert overview.tool_status().get("total_drawers") == 6
        assert overview.tool_list_wings() == {"wings": {"alpha": 4, "beta": 2}}
        assert overview.tool_list_rooms(wing="alpha") == {
            "wing": "alpha",
            "rooms": {"r1": 2, "r2": 2},
        }
        assert overview.tool_get_taxonomy() == {
            "taxonomy": {"alpha": {"r1": 2, "r2": 2}, "beta": {"r3": 2}}
        }
    finally:
        backend.close()


@pytest.mark.parametrize(
    "tool,key,expected",
    [
        ("tool_status", "total_drawers", 8),
        ("tool_list_wings", "wings", {"alpha": 5, "beta": 3}),
        ("tool_list_rooms", "rooms", {"r1": 3, "r2": 2, "r3": 3}),
        ("tool_get_taxonomy", "taxonomy", {"alpha": {"r1": 3, "r2": 2}, "beta": {"r3": 3}}),
    ],
)
def test_unavailable_bookkeeping_preserves_explicit_partial_facets(
    overview, monkeypatch, tool, key, expected
):
    from mempalace.backends import UnsupportedCapabilityError

    col = Facets()
    original = col.facet_counts

    def facets(field, where=None, limit=1000):
        if field not in {"wing", "room"}:
            raise UnsupportedCapabilityError("field not indexed for facets")
        return original(field, where=where, limit=limit)

    monkeypatch.setattr(col, "facet_counts", facets)
    monkeypatch.setattr(overview, "_get_collection", lambda **kw: col)
    result = getattr(overview, tool)()
    assert result.get(key) == expected
    assert result.get("partial") is True
    assert result.get("bookkeeping_error") == "bookkeeping_unavailable"
    assert result.get("error") != "overview_unavailable"
