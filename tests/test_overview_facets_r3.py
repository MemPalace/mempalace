"""Missing-locus bookkeeping and canonical facet-refusal regressions."""

from collections import Counter

import pytest

from mempalace.metadata_types import is_bookkeeping
from tests.test_overview_facets_r2 import TOOLS, Facets

pytest_plugins = ["tests.test_overview_facets_r2"]


@pytest.mark.parametrize("backend", ["fake", "sqlite_exact"])
@pytest.mark.parametrize("tool", TOOLS)
def test_missing_locus_bookkeeping_matches_oracle(overview, monkeypatch, tmp_path, backend, tool):
    rows = [
        {"wing": "alpha", "room": "r"},
        {"wing": "unknown", "room": "r"},
        {"wing": "alpha"},
        {"room": "r"},
        {},
        {"wing": "registry_no_room", "ingest_mode": "registry"},
        {"wing": "sentinel_no_room", "is_sentinel": True},
        {"ingest_mode": "registry"},
        {"is_sentinel": True},
        {"ingest_mode": "registry", "is_sentinel": True},
        {"wing": "alpha", "room": "r", "is_sentinel": 0.0},
        {"room": "r", "is_sentinel": "0.0"},
        {"room": "r", "is_sentinel": 0.0},
        {"wing": "alpha", "is_sentinel": "yes"},
    ]
    if backend == "fake":
        col = Facets(0)
        col.rows = rows
    else:
        from mempalace.backends import PalaceRef
        from mempalace.backends.sqlite_exact import SQLiteExactBackend

        col = SQLiteExactBackend().get_collection(
            palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
            collection_name="mempalace_drawers",
            create=True,
        )
        col.add(
            ids=[str(i) for i in range(len(rows))],
            documents=["scratch"] * len(rows),
            metadatas=rows,
            embeddings=[[1.0, 0.0]] * len(rows),
        )
        rows = col.get(include=["metadatas"])["metadatas"]
    monkeypatch.setattr(overview, "_get_collection", lambda **kw: col)
    users = [m for m in rows if not is_bookkeeping(m)]
    wings = dict(Counter(m.get("wing", "unknown") for m in users))
    rooms = dict(Counter(m.get("room", "unknown") for m in users))
    taxonomy = {}
    # The existing facet taxonomy enumerates only explicitly keyed wings.
    for m in users:
        if "wing" in m:
            rs = taxonomy.setdefault(m["wing"], {})
            r = m.get("room", "unknown")
            rs[r] = rs.get(r, 0) + 1
    result = getattr(overview, tool)()
    assert "error" not in result and not result.get("partial")
    if tool == "tool_status":
        assert result["total_drawers"] == len(users)
        assert result["wings"] == wings and result["rooms"] == rooms
    elif tool == "tool_list_wings":
        assert result["wings"] == wings
    elif tool == "tool_list_rooms":
        assert result["rooms"] == rooms
    else:
        assert result["taxonomy"] == taxonomy


@pytest.mark.parametrize("tool", TOOLS)
def test_primary_facet_backend_error_is_canonical(overview, monkeypatch, tool):
    from mempalace.backends import BackendError

    col = Facets()
    field = "room" if tool == "tool_list_rooms" else "wing"
    original = col.facet_counts

    def fail(name, **kw):
        if name == field:
            raise BackendError("private backend diagnostic")
        return original(name, **kw)

    monkeypatch.setattr(col, "facet_counts", fail)
    monkeypatch.setattr(overview, "_get_collection", lambda **kw: col)
    result = getattr(overview, tool)()
    assert result.get("error") == "overview_unavailable"
    assert result.get("partial") is True and result.get("total_drawers") is None
    assert not result.get("wings") and not result.get("rooms") and not result.get("taxonomy")
