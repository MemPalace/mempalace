"""Mutation probes and transaction-time provenance snapshots."""

import time

import pytest

from mempalace import knowledge_graph as mod
from tests._mcp_server_helpers import _patch_mcp_server


def page(kg, kind="save", objects=None, **kw):
    return kg.mutation_candidates(
        kind, "Avery", "Deploy Port", objects or ["old"], wing=" shared ", **kw
    )


def seed(kg, obj="old", **kw):
    return kg.add_triple("Avery", "deploy_port", obj, wing="shared", valid_from="2026-01-01", **kw)


def row(kg, tid):
    return dict(kg._conn().execute("SELECT * FROM triples WHERE id=?", (tid,)).fetchone())


@pytest.mark.parametrize("method", ["add", "save", "invalidate", "supersede"])
def test_u1_a_every_mutation_excludes_disallowed_targets(kg, method):
    tid = seed(kg, source_thread="thr_aa")
    if method == "invalidate":
        assert kg.invalidate("Avery", "Deploy Port", "old", wing=" shared ", allow=[]) == 0
    elif method == "supersede":
        result = kg.supersede("Avery", "Deploy Port", "old", "new", wing="shared", allow=[])
        assert result != tid
    elif method == "add":
        assert seed(kg, allow=[]) != tid
    else:
        result = kg.save_fact("Avery", "Deploy Port", "old", wing="shared", allow=[])
        assert result["action"] == "added" and result["triple_id"] != tid
    assert row(kg, tid)["valid_to"] is None and row(kg, tid)["seen_count"] == 1


@pytest.mark.parametrize("name", ["add", "save", "invalidate", "supersede"])
def test_u1_b_probe_precedes_wal_and_mutation(kg, config, monkeypatch, name):
    from mempalace import mcp_server as mcp

    _patch_mcp_server(monkeypatch, config, kg)
    seed(kg)
    before = kg._conn().total_changes
    logs = []
    monkeypatch.setattr(mcp, "_wal_log", lambda *a, **k: logs.append(a))
    args = dict(
        subject="Avery", predicate="Deploy Port", wing=" shared ", thread_scope={"probe": True}
    )
    args.update(
        dict(old_object="old", new_object="new") if name == "supersede" else dict(object="old")
    )
    result = getattr(mcp, "tool_kg_" + name)(**args)
    assert result["success"] and result["probe"]["facts"] and not logs
    assert kg._conn().total_changes == before


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"probe": False},
        {"allow": None},
        {"allow": [{}]},
        {"probe": True, "after": 4},
        {"allow": [], "extra": True},
    ],
)
def test_u1_c_invalid_scope_refused(kg, config, monkeypatch, scope):
    from mempalace import mcp_server as mcp

    _patch_mcp_server(monkeypatch, config, kg)
    assert mcp.tool_kg_invalidate("A", "b", "C", thread_scope=scope) == {
        "success": False,
        "error": "thread_scope is invalid",
    }


def test_u1_d_none_retains_unscoped_behavior(kg):
    tid = seed(kg)
    assert seed(kg, allow=None) == tid
    assert kg.invalidate("Avery", "deploy_port", "old", wing="shared") == 1


def test_u1_e_json_each_and_empty_allow(kg):
    tid = seed(kg)
    sql, params = mod._allow_sql([tid])
    assert kg._conn().execute("SELECT id FROM triples WHERE 1" + sql, params).fetchone()[0] == tid
    assert mod._allow_sql([]) == (" AND 0", [])


def test_u1_f_stale_provenance_rechecked_in_transaction(kg):
    tid = seed(kg, source_thread="thr_aa")
    allow = page(kg)["facts"]
    seed(kg, source_thread="thr_bb")
    assert kg.invalidate("Avery", "deploy_port", "old", wing="shared", allow=allow) == 0
    assert row(kg, tid)["valid_to"] is None


def test_u1_g_normalized_and_legacy_targets(kg):
    kg._conn().execute("INSERT INTO entities(id,name) VALUES ('avery','Avery!')")
    tid = seed(kg)
    found = page(kg, "invalidate")
    assert [f["id"] for f in found["facts"]] == [tid]
    assert kg.invalidate("Avery", "Deploy Port", "old", wing=" shared ", allow=found["facts"]) == 1


def test_u1_h_hidden_approval_and_interval_are_absent(kg):
    tid = seed(kg)
    kg._conn().execute("UPDATE triples SET approval_status='approved' WHERE id=?", (tid,))
    assert (
        kg.invalidate("Avery", "deploy_port", "old", wing="shared", ended="2020-01-01", allow=[])
        == 0
    )
    new = kg.supersede(
        "Avery", "deploy_port", "old", "new", wing="shared", at="2020-01-01", allow=[]
    )
    assert new != tid and row(kg, tid)["valid_to"] is None


def test_u1_i_paging_no_cap_or_loss(kg, monkeypatch):
    monkeypatch.setattr(mod, "_PROBE_PAGE", 2)
    wanted = {seed(kg, str(i)) for i in range(5)}
    actual, after, calls = [], None, 0
    while True:
        found = page(kg, single_valued=True, after=after)
        calls += 1
        actual.extend(f["id"] for f in found["facts"])
        after = found["next"]
        if after is None:
            break
    assert set(actual) == wanted and len(actual) == 5 and calls == 3


def test_u1_j_probe_uses_one_lock(kg):
    seed(kg, source_thread="thr_aa")
    start = time.monotonic()
    assert page(kg)["facts"][0]["threads"] == ["thr_aa"]
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("kind", ["add", "invalidate", "supersede"])
def test_u1_l_closed_history_excluded(kg, kind):
    tid = seed(kg)
    kg.invalidate("Avery", "deploy_port", "old", wing="shared")
    assert tid not in {f["id"] for f in page(kg, kind)["facts"]}


def test_u1_m_flat_and_source_rows_union(kg):
    tid = seed(kg, source_thread="thr_bb")
    kg._conn().execute("DELETE FROM fact_sources WHERE triple_id=?", (tid,))
    assert page(kg)["facts"][0]["threads"] == ["thr_bb"]
    allow = page(kg)["facts"]
    seed(kg, source_thread="thr_aa")
    assert page(kg)["facts"][0]["threads"] == ["thr_aa", "thr_bb"]
    assert kg.invalidate("Avery", "deploy_port", "old", wing="shared", allow=allow) == 0


def test_history_merge_uses_the_same_allow(kg):
    seed(kg, "new", source_thread="thr_aa")
    history = kg.save_fact(
        "Avery", "deploy_port", "old", wing="shared", valid_from="2020-01-01", single_valued=True
    )
    snap = page(kg, single_valued=True, valid_from="2020-01-01")["facts"]
    assert history["triple_id"] in {f["id"] for f in snap}
    result = kg.save_fact(
        "Avery",
        "deploy_port",
        "old",
        wing="shared",
        valid_from="2020-01-01",
        single_valued=True,
        allow=[],
    )
    assert result["action"] == "added" and row(kg, history["triple_id"])["seen_count"] == 1


@pytest.mark.parametrize("missing", [True, False])
def test_closet_citation_probe_missing_collection_only(kg, config, monkeypatch, missing):
    from mempalace import mcp_server as mcp, palace
    from mempalace.backends.base import CollectionNotInitializedError

    _patch_mcp_server(monkeypatch, config, kg)
    error = CollectionNotInitializedError(config.palace_path) if missing else OSError("unavailable")

    def unavailable(*a, **kw):
        raise error

    monkeypatch.setattr(palace, "get_closets_collection", unavailable)
    args = dict(
        subject="A", predicate="b", object="C", thread_scope={"probe": True, "closet": "absent"}
    )
    if missing:
        assert mcp.tool_kg_add(**args)["cited"] == {"closet": None}
    else:
        with pytest.raises(OSError, match="unavailable"):
            mcp.tool_kg_add(**args)
