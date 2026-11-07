"""Source/id deletes use concrete rooms and recheck roomless associations."""

import pytest

from mempalace import hallways, mcp_server as mcp, palace_graph
from mempalace.palace import get_aaak_collection, get_closets_collection
from mempalace.revisions import (
    append_drawer_revision,
    load_drawer_revisions,
    purge_drawer_revisions,
)
from tests._mcp_server_helpers import _patch_mcp_server


@pytest.fixture
def worker(config, kg, monkeypatch, tmp_path):
    _patch_mcp_server(monkeypatch, config, kg)
    monkeypatch.setattr(
        hallways, "_get_hallway_file", lambda *a, **k: str(tmp_path / "hallways.json")
    )
    monkeypatch.setattr(
        hallways, "_legacy_hallway_file", lambda: str(tmp_path / "legacy-hallways.json")
    )
    monkeypatch.setattr(
        palace_graph, "_get_tunnel_file", lambda *a, **k: str(tmp_path / "tunnels.json")
    )
    monkeypatch.setattr(
        palace_graph, "_legacy_tunnel_file", lambda: str(tmp_path / "legacy-tunnels.json")
    )
    return config.palace_path


def drawer(room, source="source", wing="shared"):
    result = mcp.tool_add_drawer(
        wing=wing, room=room, content="synthetic " + room, source_file=source
    )
    assert result.get("success"), result
    return result["drawer_id"]


def scope(pairs=(), roomless=()):
    return {
        "allow": {
            "pairs": [list(p) for p in pairs],
            "roomless_ids": [r["kind"] + ":" + r["id"] for r in roomless],
            "expect": {r["kind"] + ":" + r["id"]: r["expect"] for r in roomless},
        }
    }


def test_u2_a_mixed_source_counts_and_ids(worker):
    visible, hidden = drawer("visible"), drawer("thr_aa")
    closets = get_closets_collection(worker, create=True)
    closets.upsert(
        ids=["v", "h"],
        documents=["v", "h"],
        metadatas=[
            {"wing": "shared", "room": r, "source_file": "source"} for r in ["visible", "thr_aa"]
        ],
    )
    allowed = scope([("shared", "visible")])
    dry = mcp.tool_delete_by_source("source", thread_scope=allowed)
    result = mcp.tool_delete_by_source("source", dry_run=False, thread_scope=allowed)
    assert dry["match_count"] == 1 and dry["closet_match_count"] == 1
    assert result["deleted"] == 1 and result["closets_deleted"] == 1
    assert mcp.tool_get_drawer(hidden).get("content") and mcp.tool_get_drawer(visible).get("error")
    assert closets.get()["ids"] == ["h"]


def test_u2_b_c_hidden_matches_and_orphan_closets_are_absent(worker):
    hidden = drawer("thr_aa")
    closets = get_closets_collection(worker, create=True)
    closets.upsert(
        ids=["h"],
        documents=["h"],
        metadatas=[{"wing": "private_threads", "room": "thr_aa", "source_file": "source"}],
    )
    for dry in [True, False]:
        actual = mcp.tool_delete_by_source("source", dry_run=dry, thread_scope=scope())
        absent = mcp.tool_delete_by_source("missing", dry_run=dry, thread_scope=scope())
        actual.pop("source_file")
        absent.pop("source_file")
        assert actual == absent
    assert mcp.tool_get_drawer(hidden).get("content") and closets.get()["ids"] == ["h"]


def test_u2_d_owner_equivalent_visible_cleanup(worker):
    did = drawer("visible")
    append_drawer_revision(worker, did, "prior", {"wing": "shared", "room": "visible"})
    aaak = get_aaak_collection(worker, create=True)
    aaak.upsert(
        ids=[did], documents=["abbreviation"], metadatas=[{"wing": "shared", "room": "visible"}]
    )
    result = mcp.tool_delete_by_source("source", False, thread_scope=scope([("shared", "visible")]))
    assert result["revisions_deleted"] == 1 and not aaak.get()["ids"]


def test_u2_e_id_only_delete_keeps_rows_arriving_after_enumeration(worker, monkeypatch):
    drawer("visible")
    col = mcp._get_collection()
    real_delete = col.delete
    inserted = []

    def delete(*, ids=None, **kw):
        assert ids is not None and not kw
        col.upsert(
            ids=["late-hidden"],
            documents=["hidden"],
            metadatas=[{"wing": "shared", "room": "thr_bb", "source_file": "source"}],
        )
        inserted.append(True)
        return real_delete(ids=ids)

    monkeypatch.setattr(col, "delete", delete)
    mcp.tool_delete_by_source("source", False, thread_scope=scope([("shared", "visible")]))
    assert inserted and col.get(ids=["late-hidden"])["ids"] == ["late-hidden"]


def test_u2_f_i_probe_and_dry_run_never_log_wal(worker, monkeypatch):
    drawer("visible")
    logs = []
    monkeypatch.setattr(mcp, "_wal_log", lambda *a: logs.append(a))
    probe = mcp.tool_delete_by_source("source", False, thread_scope={"probe": True})
    dry = mcp.tool_delete_by_source("source", thread_scope=scope([("shared", "visible")]))
    assert (
        probe["probe"]["pairs"] == [["shared", "visible"]] and dry["match_count"] == 1 and not logs
    )


def test_u2_g_hallway_lock_rechecks_all_rooms(worker):
    hallways._save_hallways([{"id": "h", "wing": "shared", "rooms": ["visible", "thr_aa"]}])
    assert not hallways.delete_hallway("h", allow=[["shared", "visible"]])
    assert hallways.get_hallway("h")
    assert hallways.delete_hallway("h", allow=[["shared", "visible"], ["shared", "thr_aa"]])


def test_u2_h_hidden_orphans_and_unassociated_roomless_survive(worker):
    drawer("visible")
    for did, meta in [
        ("hidden-old", {"wing": "shared", "room": "thr_aa", "source_file": "source"}),
        ("unknown-old", {"source_file": "source"}),
        ("visible-old", {"wing": "shared", "room": "visible", "source_file": "source"}),
    ]:
        append_drawer_revision(worker, did, "old", meta)
    mcp.tool_delete_by_source("source", False, thread_scope=scope([("shared", "visible")]))
    assert {r["drawer_id"] for r in load_drawer_revisions(worker)} == {"hidden-old", "unknown-old"}


def test_u2_j_tunnel_snapshot_includes_drawer_ids(worker):
    original = {
        "source": {"wing": "shared", "room": "a"},
        "target": {"wing": "shared", "room": "b", "drawer_id": "old"},
    }
    target = {**original, "target": {**original["target"], "drawer_id": "changed"}}
    palace_graph._save_tunnels([{"id": "t", **target}])
    assert palace_graph.delete_tunnel("t", expect=original) == {"deleted": "t"}
    assert palace_graph.list_tunnels()[0]["id"] == "t"


def test_u2_k_revision_filter_and_none(worker):
    for r in ["visible", "hidden"]:
        append_drawer_revision(
            worker, r, "old", {"wing": "shared", "room": r, "source_file": "source"}
        )
    assert (
        purge_drawer_revisions(worker, source_file="source", allow_rooms={("shared", "visible")})
        == 1
    )
    assert purge_drawer_revisions(worker, source_file="source", allow_rooms=None) == 1


@pytest.mark.parametrize(
    "name,arg",
    [
        ("delete_by_source", "source_file"),
        ("delete_tunnel", "tunnel_id"),
        ("delete_hallway", "hallway_id"),
    ],
)
@pytest.mark.parametrize(
    "invalid", [{}, {"probe": 1}, {"allow": {}}, {"allow": {"pairs": [["shared"]]}}]
)
def test_u2_l_invalid_scope(worker, name, arg, invalid):
    drawer("visible")
    result = getattr(mcp, "tool_" + name)(**{arg: "source", "thread_scope": invalid})
    assert result.get("error") == "thread_scope is invalid"


@pytest.mark.parametrize("kind", ["revision", "aaak", "closet"])
def test_u2_m_roomless_probe_associations_and_refusals(worker, kind):
    did = drawer("visible", source="/x/thr_aa.md")
    meta = {"source_file": "/x/thr_aa.md"}
    if kind == "revision":
        append_drawer_revision(worker, did, "old", meta)
    else:
        col = (get_aaak_collection if kind == "aaak" else get_closets_collection)(
            worker, create=True
        )
        col.upsert(ids=[did if kind == "aaak" else "c"], documents=["old"], metadatas=[meta])
    probe = mcp.tool_delete_by_source("/x/thr_aa.md", thread_scope={"probe": True})["probe"]
    item = next(r for r in probe["roomless"] if r["kind"] == kind)
    assert item["tokens"] == ["thr_aa"] and not item["refused"]
    assert item["drawer"] == (["shared", "visible"] if kind != "closet" else "missing")
    mcp.tool_delete_by_source("/x/thr_aa.md", False, thread_scope=scope(probe["pairs"], [item]))
    assert not load_drawer_revisions(worker) if kind == "revision" else not col.get()["ids"]


def test_u2_n_stale_roomless_row_and_drawer_kept_under_lock(worker, monkeypatch):
    did = drawer("visible")
    rid = append_drawer_revision(worker, did, "old", {"source_file": "source"})
    probe = mcp.tool_delete_by_source("source", thread_scope={"probe": True})["probe"]
    item = next(r for r in probe["roomless"] if r["id"] == rid)
    col = mcp._get_collection()
    col.update(
        ids=[did],
        metadatas=[{"wing": "private_threads", "room": "thr_bb", "source_file": "source"}],
    )
    forwarded = []
    from mempalace import revisions

    real = revisions.purge_drawer_revisions

    def recording(*a, **kw):
        forwarded.append(kw.get("allow_rooms"))
        return real(*a, **kw)

    monkeypatch.setattr(revisions, "purge_drawer_revisions", recording)
    drawer("other-visible")
    mcp.tool_delete_by_source(
        "source", False, thread_scope=scope([("shared", "other-visible")], [item])
    )
    assert (
        load_drawer_revisions(worker)[0]["revision_id"] == rid
        and forwarded
        and forwarded[0] is not None
    )


def test_missing_and_error_drawer_are_distinct(worker, monkeypatch):
    drawer("visible")
    col = mcp._get_collection()
    assert mcp._source_drawer_state(col, "absent") == "missing"
    monkeypatch.setattr(
        mcp, "_collection_logical_drawer_record", lambda *a: (_ for _ in ()).throw(OSError())
    )
    assert mcp._source_drawer_state(col, "absent") == "error"


@pytest.mark.parametrize("missing", [True, False])
def test_source_probe_missing_closets_only(worker, monkeypatch, missing):
    from mempalace import palace
    from mempalace.backends.base import CollectionNotInitializedError

    drawer("visible")
    error = CollectionNotInitializedError(worker) if missing else OSError("unavailable")

    def unavailable(*a, **kw):
        raise error

    monkeypatch.setattr(palace, "get_closets_collection", unavailable)
    if missing:
        probe = mcp.tool_delete_by_source("source", thread_scope={"probe": True})
        assert probe["probe"] == {"pairs": [["shared", "visible"]], "roomless": []}
    else:
        with pytest.raises(OSError, match="unavailable"):
            mcp.tool_delete_by_source("source", thread_scope={"probe": True})
