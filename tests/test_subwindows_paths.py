"""Sub-windows stay consistent on every path that writes, moves or deletes drawers.

Every test runs a real write path against a real palace and then checks one
invariant (:func:`assert_windows_consistent`):

* a window row stores no text and its id never names a drawer;
* a window the search would trust (its parent exists and ``parent_sha``
  matches the parent's current text) points at a span inside the parent and
  carries exactly the vector of that span, so trusting it is never wrong;
* in ``complete`` mode, there is no untrusted window left and every long
  drawer has exactly the windows its text needs.

Paths that rewrite drawers without the cascade (repair, migrate, encoding
repair) are allowed to leave untrusted windows behind; for those the test
checks that search ignores them, and that ``windows build`` cleans them up.
"""

import json
import math
from pathlib import Path

import pytest
import yaml

from mempalace import subwindows
from mempalace.subwindows import WindowProfile, parent_sha, plan_windows, window_id
from tests.test_subwindows import (
    DIM,
    WINDOW,
    _long_text,
    _truncating_embedding,
    _TruncatingEF,
    _word_tokenizer,
)

PROFILE = WindowProfile("test-words", DIM, WINDOW, 4)


@pytest.fixture
def windows_on(monkeypatch):
    """Sub-windows on, with an embedder that reads only the first WINDOW words."""
    import mempalace.backends.chroma as chroma_mod
    import mempalace.backends.embedding_wrapper as embedding_wrapper
    import mempalace.embedding as embedding_mod

    ef = _TruncatingEF()
    monkeypatch.setattr(embedding_mod, "get_embedding_function", lambda *_, **__: ef)
    monkeypatch.setattr(
        chroma_mod.ChromaBackend, "_resolve_embedding_function", staticmethod(lambda **_: ef)
    )
    monkeypatch.setattr(embedding_wrapper, "_embed_texts", lambda texts, **_: ef(input=list(texts)))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_SUBWINDOWS", "1")
    tokenizer = _word_tokenizer(WINDOW)
    state = {"profile": PROFILE}
    monkeypatch.setattr(
        subwindows, "active_profile", lambda embedder=None: (ef, tokenizer, state["profile"])
    )
    return state


# ── the invariant ────────────────────────────────────────────────────────


def _drawers(palace):
    from mempalace.palace import get_collection

    got = get_collection(palace, create=False).get(include=["documents", "metadatas"])
    return {i: (d, m or {}) for i, d, m in zip(got["ids"], got["documents"], got["metadatas"])}


def _windows(palace):
    from mempalace.palace import open_windows_collection

    col = open_windows_collection(palace, read_only=True)
    if col is None:
        return {}
    got = col.get(include=["documents", "metadatas", "embeddings"])
    return {
        i: (d, m or {}, list(e))
        for i, d, m, e in zip(got["ids"], got["documents"], got["metadatas"], got["embeddings"])
    }


def _close(a, b):
    return len(a) == len(b) and all(math.isclose(x, y, abs_tol=1e-5) for x, y in zip(a, b))


def assert_windows_consistent(palace, *, complete=True, profile=PROFILE):
    """See the module docstring. Returns ``(trusted, untrusted)`` window ids."""
    from mempalace.metadata_types import is_bookkeeping

    drawers = _drawers(palace)
    windows = _windows(palace)
    trusted, untrusted = set(), set()
    for row_id, (doc, meta, vector) in windows.items():
        assert doc in ("", None), f"window {row_id} stores text"
        assert row_id not in drawers, f"window id {row_id} is also a drawer id"
        parent = drawers.get(meta.get("window_of"))
        if (
            parent is None
            or meta.get("parent_sha") != parent_sha(parent[0])
            or meta.get("window_profile") != profile.key
        ):
            untrusted.add(row_id)
            continue
        text = parent[0]
        start, end = meta["char_start"], meta["char_end"]
        assert 0 <= start < end <= len(text), f"window {row_id} span out of range"
        assert _close(vector, _truncating_embedding(text[start:end])), (
            f"window {row_id} vector is not the embedding of its span"
        )
        trusted.add(row_id)
    if complete:
        assert not untrusted, f"stale windows left behind: {sorted(untrusted)}"
        tokenizer = _word_tokenizer(WINDOW)
        for drawer_id, (doc, meta) in drawers.items():
            if not isinstance(doc, str) or is_bookkeeping(meta, drawer_id):
                continue
            spans = plan_windows(doc, tokenizer, profile.overlap)
            want = {window_id(drawer_id, i) for i in range(1, len(spans) + 1)}
            have = {i for i in trusted if windows[i][1]["window_of"] == drawer_id}
            assert have == want, f"drawer {drawer_id}: windows {sorted(have)} != {sorted(want)}"
            for index, (start, end) in enumerate(spans, start=1):
                meta_w = windows[window_id(drawer_id, index)][1]
                assert (meta_w["char_start"], meta_w["char_end"]) == (start, end)
    return trusted, untrusted


def _long_drawer_ids(palace):
    tokenizer = _word_tokenizer(WINDOW)
    return {
        i
        for i, (doc, _) in _drawers(palace).items()
        if isinstance(doc, str) and plan_windows(doc, tokenizer, PROFILE.overlap)
    }


def _search_ids(palace, query, **kwargs):
    from mempalace.searcher import search_memories

    result = search_memories(query, palace, n_results=kwargs.pop("n_results", 5), **kwargs)
    return [hit["drawer_id"] for hit in result["results"]]


def _words(tag, n):
    return " ".join(f"{tag}{i}" for i in range(n))


# ── project miner ────────────────────────────────────────────────────────


def _project(tmp_path, files):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "mempalace.yaml").write_text(
        yaml.dump({"wing": "proj", "rooms": [{"name": "general", "description": "all"}]})
    )
    for name, text in files.items():
        (root / name).write_text(text)
    return root


def test_project_mine_remine_shrink_and_delete_keep_windows_current(windows_on, tmp_path):
    from mempalace.miner import mine

    palace = str(tmp_path / "palace")
    root = _project(
        tmp_path,
        {"a.md": _words("alpha", 300) + " the okapi fact", "b.md": _words("beta", 200)},
    )
    mine(str(root), palace)
    assert _long_drawer_ids(palace)
    assert_windows_consistent(palace)

    # Re-mine after an edit: rewritten drawers get new windows, the old go.
    (root / "a.md").write_text(_words("gamma", 250) + " the walrus fact")
    mine(str(root), palace)
    assert_windows_consistent(palace)

    # A file that shrank below the window keeps no windows.
    (root / "b.md").write_text("this shrunk note now has only twelve words in it, fine")
    mine(str(root), palace)
    trusted, _ = assert_windows_consistent(palace)
    b_ids = {
        i for i, (_, m) in _drawers(palace).items() if m.get("source_file", "").endswith("b.md")
    }
    assert b_ids and not {w for w in trusted if w.split("::w")[0] in b_ids}


def test_sync_prune_of_a_deleted_file_purges_its_windows(windows_on, tmp_path):
    from mempalace.miner import mine
    from mempalace.sync import sync_palace

    palace = str(tmp_path / "palace")
    root = _project(tmp_path, {"a.md": _words("alpha", 300), "b.md": _words("beta", 300)})
    mine(str(root), palace)
    before = _windows(palace)
    (root / "b.md").unlink()

    sync_palace(palace, project_dirs=[str(root)], dry_run=False)

    assert_windows_consistent(palace)
    assert len(_windows(palace)) < len(before)


def test_an_embedder_profile_change_is_picked_up_by_the_next_mine(windows_on, tmp_path):
    from mempalace.miner import mine

    palace = str(tmp_path / "palace")
    root = _project(tmp_path, {"a.md": _words("alpha", 300)})
    mine(str(root), palace)
    new = WindowProfile("another-model", DIM, WINDOW, 4)
    windows_on["profile"] = new

    # Search ignores the old profile's windows. Re-mining an unchanged file
    # skips it, so its windows stay on the old profile, still ignored...
    mine(str(root), palace)
    _, untrusted = assert_windows_consistent(palace, complete=False, profile=new)
    assert untrusted
    # ...until a build rewrites them for the new one.
    stats = subwindows.build_windows(palace)
    assert stats["skipped"] == 0 and stats["embedded"] == len(untrusted)
    assert_windows_consistent(palace, profile=new)
    # An edited file is re-mined and indexed with the new profile at once.
    (root / "a.md").write_text(_words("delta", 280))
    mine(str(root), palace)
    assert_windows_consistent(palace, profile=new)


# ── conversation, format and diary miners ────────────────────────────────


def _transcript(path, answer):
    path.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "type": "user",
                    "timestamp": "2026-09-09T01:00:00Z",
                    "message": {"role": "user", "content": "What happened on the trip?"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-09-09T01:01:00Z",
                    "message": {"role": "assistant", "content": answer},
                },
            ]
        ),
        encoding="utf-8",
    )


def test_convo_mine_and_remine_keep_windows_current(windows_on, tmp_path):
    from mempalace.convo_miner import mine_convos

    palace = str(tmp_path / "palace")
    convos = tmp_path / "convos"
    convos.mkdir()
    transcript = convos / "session.jsonl"
    _transcript(transcript, _words("trip", 120) + " we saw a tapir")
    mine_convos(str(convos), palace, wing="c")
    assert _long_drawer_ids(palace)
    assert_windows_consistent(palace)

    _transcript(transcript, _words("voyage", 90) + " we saw an ibex")
    mine_convos(str(convos), palace, wing="c")
    assert_windows_consistent(palace)


def test_format_mine_and_remine_keep_windows_current(windows_on, tmp_path, monkeypatch):
    import mempalace.format_miner as format_miner

    palace = str(tmp_path / "palace")
    docs = tmp_path / "docs"
    docs.mkdir()
    pdf = docs / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4 v1")
    text = {"value": _words("report", 300) + " the narwhal finding"}
    monkeypatch.setattr(format_miner, "_extract_via_markitdown", lambda path: text["value"])

    format_miner.mine_formats(str(docs), palace, wing="f")
    assert _long_drawer_ids(palace)
    assert_windows_consistent(palace)

    pdf.write_bytes(b"%PDF-1.4 version two")
    text["value"] = _words("revised", 260) + " the walrus finding"
    format_miner.mine_formats(str(docs), palace, wing="f")
    assert_windows_consistent(palace)


def test_diary_ingest_and_reingest_keep_windows_current(windows_on, tmp_path):
    from mempalace.diary_ingest import ingest_diaries

    palace = str(tmp_path / "palace")
    diaries = tmp_path / "diaries"
    diaries.mkdir()
    day = diaries / "2026-04-13.md"
    day.write_text("# 2026-04-13\n\n## 10:00 — Work\n\n" + _words("built", 120) + "\n")
    ingest_diaries(str(diaries), palace, force=True)
    assert _long_drawer_ids(palace)
    assert_windows_consistent(palace)

    day.write_text("# 2026-04-13\n\n## 10:00 — Work\n\n" + _words("rebuilt", 140) + "\n")
    ingest_diaries(str(diaries), palace, force=True)
    assert_windows_consistent(palace)


# ── MCP tools, sweeper and integration writers ───────────────────────────


@pytest.fixture
def mcp(windows_on, palace_path, config, monkeypatch):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    return palace_path, mcp_server


def test_mcp_add_update_move_and_delete_keep_windows_current(mcp):
    palace, server = mcp
    added = [
        server.tool_add_drawer("w", "r", _long_text(f"fact number {i} about otters"))
        for i in range(3)
    ]
    assert all(a["success"] for a in added), added
    assert_windows_consistent(palace)
    first, second, third = (a["drawer_id"] for a in added)

    assert server.tool_update_drawer(first, content=_long_text("a narwhal walrus encounter"))[
        "success"
    ]
    assert_windows_consistent(palace)
    assert _search_ids(palace, "narwhal walrus encounter", n_results=1) == [first]

    # Moving a drawer changes no text: its windows stay and follow it.
    assert server.tool_update_drawer(second, wing="elsewhere", room="moved")["success"]
    assert_windows_consistent(palace)
    assert _search_ids(palace, "fact number 1 about otters", wing="elsewhere") == [second]

    # Shrinking below the window drops the windows.
    assert server.tool_update_drawer(third, content="tiny")["success"]
    assert_windows_consistent(palace)

    assert server.tool_delete_drawer(first)["success"]
    assert_windows_consistent(palace)
    assert server.tool_delete_drawers([second, third])["deleted"] == 2
    assert_windows_consistent(palace)
    assert _windows(palace) == {}


def test_mcp_chunked_add_and_update_keep_windows_current(mcp):
    palace, server = mcp
    big = " ".join(_long_text(f"chapter {i} heron") for i in range(60))
    added = server.tool_add_drawer("w", "r", big)
    assert added["success"], added
    assert_windows_consistent(palace)
    assert len(_drawers(palace)) > 1, "content should have been chunked"

    assert server.tool_update_drawer(
        added["drawer_id"], content=" ".join(_long_text(f"part {i} egret") for i in range(50))
    )["success"]
    assert_windows_consistent(palace)


def test_mcp_delete_by_source_and_its_retry_purge_windows(mcp):
    from mempalace.palace import get_collection

    palace, server = mcp
    col = get_collection(palace)
    for name in ("one", "two"):
        col.upsert(
            ids=[f"{name}-{i}" for i in range(2)],
            documents=[_long_text(f"{name} source fact {i}") for i in range(2)],
            metadatas=[{"wing": "w", "room": "r", "source_file": f"/src/{name}.md"}] * 2,
        )
    subwindows.build_windows(palace)
    assert_windows_consistent(palace)

    assert server.tool_delete_by_source("/src/one.md", dry_run=False)["deleted"] == 2
    assert_windows_consistent(palace)

    # A delete whose window purge did not run (crash, failure): the drawers are
    # gone, the windows linger. Search ignores them, and the retry purges them.
    col.delete(where={"source_file": "/src/two.md"})
    _, untrusted = assert_windows_consistent(palace, complete=False)
    assert untrusted
    assert _search_ids(palace, "two source fact 1") == []
    assert server.tool_delete_by_source("/src/two.md", dry_run=False)["success"]
    assert_windows_consistent(palace)
    assert _windows(palace) == {}


def test_a_failed_window_purge_never_fails_the_delete(mcp, monkeypatch):
    palace, server = mcp
    added = server.tool_add_drawer("w", "r", _long_text("a heron fact"))

    def failing(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(subwindows, "delete_windows", failing)

    assert server.tool_delete_drawer(added["drawer_id"])["success"]
    _, untrusted = assert_windows_consistent(palace, complete=False)
    assert untrusted  # left behind, and ignored by search
    assert _search_ids(palace, "a heron fact") == []
    # The next build prunes them.
    assert subwindows.build_windows(palace)["orphans_pruned"] == len(untrusted)
    assert_windows_consistent(palace)


def test_mcp_diary_write_keeps_windows_current(mcp):
    palace, server = mcp
    assert server.tool_diary_write("agent", _long_text("the ibex chamois climb"))["success"]
    assert server.tool_diary_write("agent", "short entry")["success"]
    long_chunks = " ".join(_long_text(f"diary {i} lynx") for i in range(60))
    assert server.tool_diary_write("agent", long_chunks)["success"]
    assert_windows_consistent(palace)


def test_sweeper_keeps_windows_current(windows_on, tmp_path):
    from mempalace.sweeper import sweep

    palace = str(tmp_path / "palace")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(
                {
                    "type": role,
                    "timestamp": f"2026-01-01T00:00:0{i}Z",
                    "sessionId": "s",
                    "uuid": f"m{i}",
                    "message": {"role": role, "content": _long_text(f"sweep fact {i} tapir")},
                }
            )
            for i, role in enumerate(["user", "assistant", "user"])
        )
    )
    assert sweep(str(transcript), palace)["drawers_added"] == 3
    assert_windows_consistent(palace)
    # A re-sweep writes nothing new and leaves the windows as they are.
    sweep(str(transcript), palace)
    assert_windows_consistent(palace)


def test_integration_writers_index_their_drawers(windows_on, palace_path):
    from mempalace.convo_miner import file_conversation_exchange
    from mempalace.miner import add_drawer
    from mempalace.palace import get_collection

    col = get_collection(palace_path, create=True)
    file_conversation_exchange(
        col,
        wing="w",
        room="conversations",
        text=_long_text("a live exchange about kestrels"),
        source_file="hermes-session:s",
        agent="hermes",
    )
    add_drawer(col, "w", "r", _long_text("a legacy add about plovers"), "/x/legacy.md", 0, "me")

    assert len(_long_drawer_ids(palace_path)) == 2
    assert_windows_consistent(palace_path)


# ── maintenance paths: repair, encoding repair, migrate, export ──────────


@pytest.fixture
def windowed(windows_on, palace_path):
    from mempalace.palace import get_collection

    col = get_collection(palace_path, create=True)
    ids = [f"long_{i}" for i in range(4)] + [f"short_{i}" for i in range(6)]
    docs = [_long_text(f"the tail fact {i} zebra quokka") for i in range(4)] + [
        f"short note {i} apples" for i in range(6)
    ]
    col.upsert(
        ids=ids,
        documents=docs,
        metadatas=[{"wing": "w", "room": "r", "source_file": f"/src/{i}.md"} for i in ids],
    )
    subwindows.build_windows(palace_path)
    assert_windows_consistent(palace_path)
    return palace_path, col


def test_prune_corrupt_purges_windows_of_pruned_drawers(windowed):
    from mempalace import repair

    palace, _ = windowed
    Path(palace, "corrupt_ids.txt").write_text("long_0\nlong_1\n")

    repair.prune_corrupt(palace, confirm=True)

    assert "long_0" not in _drawers(palace)
    assert_windows_consistent(palace)


def test_rebuild_from_sqlite_never_yields_a_wrong_window(windowed, tmp_path):
    """A rebuild re-embeds the drawers it restores. Windows are derived data:
    they are either carried over intact, or absent and rebuilt by `windows
    build`; a window with an empty-document vector must never be trusted."""
    from mempalace import repair

    palace, _ = windowed
    dest = str(tmp_path / "rebuilt")
    repair.rebuild_from_sqlite(palace, dest)

    assert len(_drawers(dest)) == 10
    assert_windows_consistent(dest, complete=False)
    subwindows.build_windows(dest)
    assert_windows_consistent(dest)
    assert _search_ids(dest, "the tail fact 2 zebra quokka", n_results=1) == ["long_2"]


def test_migrate_never_yields_a_wrong_window(windowed):
    from unittest.mock import patch

    from mempalace.migrate import migrate

    palace, _ = windowed
    with patch("mempalace.migrate.collection_write_roundtrip_works", return_value=False):
        assert migrate(palace, confirm=True) is True

    assert len(_drawers(palace)) == 10
    assert_windows_consistent(palace, complete=False)
    subwindows.build_windows(palace)
    assert_windows_consistent(palace)


def test_encoding_repair_rewrites_leave_only_ignored_windows(windowed, tmp_path):
    from mempalace.encoding_repair import repair_collection

    palace, col = windowed
    broken = _long_text("the cafÃ© tail fact")
    col.update(ids=["long_3"], documents=[broken])
    subwindows.build_windows(palace)
    assert_windows_consistent(palace)

    report = repair_collection(col, apply=True, backup_path=tmp_path / "backup.jsonl")

    assert report.get("updated", report.get("changed")), report
    assert "café" in _drawers(palace)["long_3"][0]
    # The repaired text no longer matches its windows' fingerprint, so search
    # ignores them; the next build replaces them.
    _, untrusted = assert_windows_consistent(palace, complete=False)
    assert untrusted
    subwindows.build_windows(palace)
    assert_windows_consistent(palace)


def test_export_lists_only_drawers(windowed, tmp_path):
    from mempalace.exporter import export_palace

    palace, _ = windowed
    assert _windows(palace)
    out = tmp_path / "export"
    stats = export_palace(palace, str(out))

    assert stats["drawers"] == 10
    exported = "\n".join(p.read_text() for p in out.rglob("*.md"))
    assert "::w0" not in exported
    assert "drawer_window" not in exported


def test_mcp_listings_and_counts_see_only_drawers(windowed, config, monkeypatch):
    from mempalace import mcp_server

    palace, _ = windowed
    monkeypatch.setattr(mcp_server, "_config", config)
    assert len(_windows(palace)) > 10

    assert mcp_server.tool_status()["total_drawers"] == 10
    assert mcp_server.tool_list_wings()["wings"] == {"w": 10}
    listed = mcp_server.tool_list_drawers(wing="w", limit=100)
    ids = [d["drawer_id"] for d in listed["drawers"]]
    assert sorted(ids) == sorted(_drawers(palace))
    hits = _search_ids(palace, "the tail fact zebra quokka", n_results=50)
    assert hits and set(hits) <= set(_drawers(palace))


# ── flag off: exactly what develop does ──────────────────────────────────


def test_flag_off_mine_write_delete_and_search_never_touch_windows(tmp_path, monkeypatch):
    """With the flag off and no windows ever built, no path opens the windows
    collection, writes the marker, or runs an extra query."""
    import chromadb

    import mempalace.palace as palace_mod
    from mempalace import mcp_server
    from mempalace.config import MempalaceConfig
    from mempalace.miner import mine

    monkeypatch.delenv("MEMPALACE_EMBEDDING_SUBWINDOWS", raising=False)
    opened = []
    real_get = palace_mod.get_windows_collection
    monkeypatch.setattr(
        palace_mod,
        "get_windows_collection",
        lambda *a, **k: opened.append(a) or real_get(*a, **k),
    )
    for name in ("window_candidates", "active_profile", "index_after_write"):
        real = getattr(subwindows, name)
        monkeypatch.setattr(
            subwindows, name, lambda *a, _n=name, _r=real, **k: opened.append(_n) or _r(*a, **k)
        )

    palace = str(tmp_path / "palace")
    root = _project(tmp_path, {"a.md": _words("alpha", 300), "b.md": _words("beta", 300)})
    mine(str(root), palace)
    (root / "a.md").write_text(_words("gamma", 300))
    mine(str(root), palace)

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"palace_path": palace}))
    monkeypatch.setattr(mcp_server, "_config", MempalaceConfig(config_dir=str(cfg_dir)))
    added = mcp_server.tool_add_drawer("w", "r", _long_text("an otter"))
    mcp_server.tool_update_drawer(added["drawer_id"], content=_long_text("a heron"))
    mcp_server.tool_delete_drawer(added["drawer_id"])

    from mempalace.palace import get_collection

    col = get_collection(palace, create=False)
    queries = []
    real_query = col.query
    monkeypatch.setattr(col, "query", lambda **k: queries.append(k) or real_query(**k))
    monkeypatch.setattr("mempalace.searcher._open_search_collection", lambda *a, **k: (col, None))
    _search_ids(palace, "alpha gamma")

    assert len(queries) == 1 and "query_texts" in queries[0]
    assert opened == []
    assert not Path(palace, subwindows.WINDOWS_MARKER).exists()
    client = chromadb.PersistentClient(path=palace)
    try:
        names = {c.name if hasattr(c, "name") else c for c in client.list_collections()}
    finally:
        client.close() if hasattr(client, "close") else None
    assert palace_mod.WINDOWS_COLLECTION_NAME not in names


# ── marker gating ────────────────────────────────────────────────────────


def test_flag_on_purges_windows_even_when_the_marker_is_missing(windowed, monkeypatch):
    """A palace whose windows predate the marker, or a copy that lost it."""
    from mempalace.palace import drawers_removed

    palace, col = windowed
    Path(palace, subwindows.WINDOWS_MARKER).unlink()
    col.delete(ids=["long_0"])
    drawers_removed(palace, aaak_ids=["long_0"])

    assert_windows_consistent(palace)
    # A writer that found the collection restored the marker for flag-off runs.
    assert Path(palace, subwindows.WINDOWS_MARKER).exists()


def test_flag_off_still_purges_windows_once_they_exist(windowed, monkeypatch):
    from mempalace.palace import drawers_removed

    palace, col = windowed
    monkeypatch.setenv("MEMPALACE_EMBEDDING_SUBWINDOWS", "0")
    col.delete(ids=["long_1"])
    drawers_removed(palace, aaak_ids=["long_1"])

    assert_windows_consistent(palace)


# ── collection-name collision ────────────────────────────────────────────


def _snapshot(col):
    got = col.get(include=["documents", "metadatas"])
    return sorted(
        zip(got["ids"], got["documents"], [json.dumps(m, sort_keys=True) for m in got["metadatas"]])
    )


@pytest.fixture
def colliding(windows_on, tmp_path, monkeypatch, request):
    """A palace whose configured drawer collection uses the windows name."""
    from mempalace.config import MempalaceConfig, get_configured_collection_name
    from mempalace.palace import WINDOWS_COLLECTION_NAME, get_collection

    # As if config.json said ``"collection_name": "mempalace_drawer_windows"``.
    monkeypatch.setattr(
        MempalaceConfig, "collection_name", property(lambda self: WINDOWS_COLLECTION_NAME)
    )
    get_configured_collection_name.cache_clear()
    request.addfinalizer(get_configured_collection_name.cache_clear)
    assert get_configured_collection_name() == WINDOWS_COLLECTION_NAME
    palace = str(tmp_path / "palace")
    col = get_collection(palace, create=True)
    col.upsert(
        ids=["real-1", "real-2"],
        documents=[_long_text("real tail one"), _long_text("real tail two")],
        metadatas=[{"wing": "w", "room": "r", "source_file": "/src/real.md"}] * 2,
    )
    return palace, col


def test_backfill_never_touches_drawers_stored_under_the_windows_name(colliding):
    palace, col = colliding
    before = _snapshot(col)

    try:
        subwindows.build_windows(palace)
    except subwindows.SubwindowsUnsupported:
        pass

    assert _snapshot(col) == before
    assert not [i for i, _, _ in _snapshot(col) if "::w" in i]


def test_colliding_writes_deletes_and_search_never_touch_real_drawers(colliding, tmp_path):
    from mempalace.palace import drawers_removed

    palace, col = colliding
    before = _snapshot(col)

    subwindows.index_after_write(palace, ["real-1"], [before[0][1]], [{}])
    drawers_removed(palace, aaak_ids=["gone"], aaak_source="/src/other.md")
    hits = _search_ids(palace, "real tail two")

    assert _snapshot(col) == before
    assert "real-2" in hits
