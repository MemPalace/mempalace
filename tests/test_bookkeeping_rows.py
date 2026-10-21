"""Registry rows and sentinels track sources; no user-facing reader shows them (W1B-06)."""

import argparse
import os
from unittest.mock import patch

import pytest
from _mcp_server_helpers import _patch_mcp_server

from mempalace.metadata_types import is_bookkeeping

# The convo miner's row for a transcript with no chunks, and the format
# miner's for a document that extracted to nothing. Both are filed recently,
# so a recent-memories reader would pick them first.
_REGISTRY_ID = "_reg_0123456789abcdef01234567"
_REGISTRY_META = {
    "wing": "ghosts",
    "room": "_registry",
    "source_file": "/chats/quokka_empty.txt",
    "ingest_mode": "registry",
    "extract_mode": "exchange",
    "filed_at": "2026-02-01T00:00:00",
}
_SENTINEL_ID = "sentinel_project_0123456789abcdef01234567"
_SENTINEL_META = {
    "wing": "project",
    "room": "documents",
    "source_file": "/docs/blank_quokka.pdf",
    "chunk_index": -1,
    "ingest_mode": "extract",
    "extract_mode": "format",
    "is_sentinel": True,
    "filed_at": "2026-02-02T00:00:00",
}
_MARKERS = ("[registry]", "[empty]", "quokka", "_registry", _REGISTRY_ID, _SENTINEL_ID)


@pytest.fixture
def palace(monkeypatch, config, kg, seeded_collection, palace_path):
    seeded_collection.add(
        ids=[_REGISTRY_ID, _SENTINEL_ID],
        documents=[f"[registry] {_REGISTRY_META['source_file']}", "[empty]"],
        metadatas=[_REGISTRY_META, _SENTINEL_META],
    )
    _patch_mcp_server(monkeypatch, config, kg)
    return palace_path


def _shows_bookkeeping(value) -> bool:
    text = repr(value)
    return any(marker in text for marker in _MARKERS)


def test_is_bookkeeping_names_registry_rows_and_sentinels():
    assert is_bookkeeping(_REGISTRY_META)
    assert is_bookkeeping(_SENTINEL_META)
    assert is_bookkeeping({"is_sentinel": "true"})
    assert not is_bookkeeping({"is_sentinel": "false", "room": "notes"})
    assert is_bookkeeping({}, "_reg_abc")
    assert is_bookkeeping(None, "sentinel_w_abc")
    assert not is_bookkeeping({"wing": "w", "room": "r"}, "drawer_w_r_abc")
    # A project may name a real room "_registry"; the room alone proves nothing.
    assert not is_bookkeeping({"wing": "w", "room": "_registry", "ingest_mode": "project"})


_CONVOS = "convos"


@pytest.fixture
def crowded(monkeypatch, config, kg, collection, palace_path):
    """40 registry rows beside 6 real drawers in one wing, plus a real
    ``_registry`` room a project defined, the shape that hid real drawers."""
    collection.add(
        ids=[f"_reg_{i:024x}" for i in range(40)],
        documents=[f"[registry] /chats/session_{i}.txt" for i in range(40)],
        metadatas=[
            {
                "wing": _CONVOS,
                "room": "_registry",
                "source_file": f"/chats/session_{i}.txt",
                "ingest_mode": "registry",
                "filed_at": f"2026-03-{(i % 28) + 1:02d}T00:00:00",
            }
            for i in range(40)
        ],
    )
    collection.add(
        ids=[f"drawer_convos_notes_{i}" for i in range(6)] + ["drawer_proj_registry_0"],
        documents=[f"Real conversation note number {i} about sessions." for i in range(6)]
        + ["The registry module maps plugin names to classes."],
        metadatas=[
            {
                "wing": _CONVOS,
                "room": "notes",
                "source_file": f"/chats/notes_{i}.txt",
                "chunk_index": 0,
                "filed_at": "2026-01-01T00:00:00",
            }
            for i in range(6)
        ]
        + [
            {
                "wing": "proj",
                "room": "_registry",
                "source_file": "/src/registry.py",
                "chunk_index": 0,
                "ingest_mode": "project",
                "filed_at": "2026-01-02T00:00:00",
            }
        ],
    )
    _patch_mcp_server(monkeypatch, config, kg)
    return palace_path


def test_a_real_room_named_registry_is_shown(crowded):
    from mempalace import mcp_server

    assert mcp_server.tool_list_rooms(wing="proj")["rooms"] == {"_registry": 1}
    assert mcp_server.tool_get_taxonomy()["taxonomy"]["proj"] == {"_registry": 1}
    listed = mcp_server.tool_list_drawers(wing="proj", limit=10)
    assert [d["drawer_id"] for d in listed["drawers"]] == ["drawer_proj_registry_0"]
    assert mcp_server.tool_status()["total_drawers"] == 7


def test_layers_fill_their_page_past_bookkeeping_rows(crowded):
    from mempalace.layers import Layer2, Layer3

    retrieved = Layer2(palace_path=crowded).retrieve(wing=_CONVOS, n_results=5)
    assert retrieved.startswith("## L2 — ON-DEMAND (5 drawers)")
    assert len(Layer3(palace_path=crowded).search_raw("/chats/session_3.txt", n_results=6)) == 6


def test_layer2_stops_its_scan_and_says_so(monkeypatch):
    from mempalace import layers

    calls = []

    class _AllRegistry:
        def get(self, **kwargs):
            calls.append(kwargs)
            n = kwargs["limit"]
            meta = {"wing": "w", "room": "_registry", "ingest_mode": "registry"}
            return {"documents": ["[registry] x"] * n, "metadatas": [meta] * n}

    monkeypatch.setattr(layers, "_open_for_read", lambda _path: _AllRegistry())
    out = layers.Layer2(palace_path="unused").retrieve(wing="w", n_results=5)
    assert len(calls) <= 4
    assert "stopped after scanning 2000 rows" in out


def test_search_fills_its_page_past_bookkeeping_rows(crowded, capsys):
    from mempalace.mcp_server import tool_search
    from mempalace.searcher import search, search_memories

    query = "/chats/session_3.txt"
    for strategy in ("vector", "union"):
        found = search_memories(
            query, palace_path=crowded, wing=_CONVOS, n_results=6, candidate_strategy=strategy
        )
        assert len(found["results"]) == 6, strategy
        assert not _shows_bookkeeping(found["results"])
    assert len(tool_search(query=query, wing=_CONVOS, limit=6)["results"]) == 6

    search(query, palace_path=crowded, wing=_CONVOS, n_results=6)
    printed = capsys.readouterr().out
    assert printed.count("notes_") == 6
    assert "[registry]" not in printed


@pytest.mark.parametrize("fast_path", [True, False])
def test_overview_tools_leave_the_registry_out(palace, monkeypatch, fast_path):
    from mempalace import mcp_server

    if not fast_path:
        monkeypatch.setattr(mcp_server, "_sqlite_taxonomy", lambda: None)

    status = mcp_server.tool_status()
    assert "_registry" not in status["rooms"]
    assert "ghosts" not in status["wings"]
    assert status["total_drawers"] == sum(status["wings"].values())
    assert "_registry" not in mcp_server.tool_list_rooms()["rooms"]
    assert "ghosts" not in mcp_server.tool_list_wings()["wings"]
    assert "ghosts" not in mcp_server.tool_get_taxonomy()["taxonomy"]


def test_list_drawers_leaves_bookkeeping_rows_out(palace):
    from mempalace.mcp_server import tool_list_drawers

    listed = tool_list_drawers(limit=100)
    assert listed["total"] == 4
    assert not _shows_bookkeeping(listed["drawers"])


@pytest.mark.parametrize("strategy", ["vector", "union"])
def test_search_never_returns_bookkeeping_rows(palace, strategy):
    from mempalace.searcher import search_memories

    found = search_memories(
        "registry empty quokka", palace_path=palace, n_results=10, candidate_strategy=strategy
    )
    assert found["results"]
    assert not _shows_bookkeeping(found["results"])


def test_bm25_only_search_never_returns_bookkeeping_rows(palace):
    from mempalace.searcher import _bm25_only_via_sqlite

    found = _bm25_only_via_sqlite(query="registry quokka", palace_path=palace, n_results=10)
    assert not _shows_bookkeeping(found["results"])


def test_wake_up_and_layers_leave_bookkeeping_rows_out(palace):
    from mempalace.layers import Layer2, Layer3, MemoryStack

    assert not _shows_bookkeeping(MemoryStack(palace_path=palace).wake_up())
    assert not _shows_bookkeeping(Layer2(palace_path=palace).retrieve(wing="project", n_results=20))
    # The header echoes the query, so the query avoids the marker words.
    assert not _shows_bookkeeping(Layer3(palace_path=palace).search("registry chats", n_results=10))
    assert not _shows_bookkeeping(Layer3(palace_path=palace).search_raw("empty pdf", n_results=10))


def test_export_leaves_bookkeeping_rows_out(palace, tmp_path):
    from mempalace.exporter import export_palace

    out = tmp_path / "export"
    stats = export_palace(palace, str(out))
    assert stats["drawers"] == 4
    exported = "".join(
        open(os.path.join(dirpath, name), encoding="utf-8").read()
        for dirpath, _dirs, names in os.walk(out)
        for name in names
    )
    assert not _shows_bookkeeping(exported)


def test_compress_leaves_bookkeeping_rows_out(palace):
    from mempalace.cli import cmd_compress
    from mempalace.palace import get_aaak_collection

    args = argparse.Namespace(palace=palace, wing=None, dry_run=False, config=None)
    with patch("mempalace.cli.MempalaceConfig") as config:
        config.return_value.palace_path = palace
        cmd_compress(args)
    summarized = get_aaak_collection(palace, create=False).get(include=["metadatas"])
    assert len(summarized["ids"]) == 4
    assert _REGISTRY_ID not in summarized["ids"]
    assert _SENTINEL_ID not in summarized["ids"]
