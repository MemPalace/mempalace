"""Same-board blockers: paired public behavior and immutable audit evidence."""

import pytest

from commonplace.boards import tools
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._split_helpers import blockers, invoke, take, world
from tests.runner_harness import http


def test_r11_rest_and_mcp(hub):
    w = world(hub)
    a = w.card(title="Prerequisite")
    token = hub.owner_token("dependency-writer")
    status, b = http(
        hub.port,
        "POST",
        "/api/boards/acme/cards",
        token=token,
        body={"title": "Dependent", "blocked_by": [a]},
    )
    assert status == 201 and b.get("blocked_by") == [a]  # RED
    assert w.boards.task_show(a)["blocks"] == [b["number"]]
    assert f"blocks: #{b['number']}" in tools.run(w.ctx(w.owner), "task_show", {"task": a})
    assert f"blocked_by: #{a}" in tools.run(w.ctx(w.owner), "task_show", {"task": b["number"]})
    assert "added" in tools.run(
        w.ctx(w.owner), "task_add", {"title": "MCP child", "board": "acme", "blocked_by": [a]}
    )
    token = hub.token(
        "rest-owner",
        profile="full",
        trust="trusted",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wings=["shared"],
    )
    status, payload = http(
        hub.port,
        "POST",
        "/api/boards/acme/cards",
        token=token,
        body={"title": "REST child", "blocked_by": [a]},
    )
    assert status == 201 and payload["blocked_by"] == [a]
    updated = w.boards.task_update(b["number"], blocked_by=[])
    assert updated["blocked_by"] == [] and b["number"] not in w.boards.task_show(a)["blocks"]
    assert any(e["kind"] == "edit" for e in w.boards.task_show(b["number"])["events"])


def test_r12_sql_skip_and_done_only(hub):
    w = world(hub)
    b = w.card(title="First but blocked")
    a = w.card(title="Prerequisite")
    blockers(w, b, a)
    status, picked = take(w)
    assert status == 200 and picked and picked["number"] == a  # RED
    w.update(
        a,
        status="review",
        question="Is this prerequisite ready to accept?",
        options=["Yes", "No"],
        context="The prerequisite has been implemented and checked.",
    )
    assert take(w) == (200, None)
    w.boards.decide(a, choice="Yes", then="done")
    status, picked = take(w)
    assert status == 200 and isinstance(picked, dict) and picked.get("number") == b


@pytest.mark.parametrize("kind", ["cycle", "self", "other_board"])
def test_r13_invalid_edges(hub, kind):
    w = world(hub)
    a, b = w.card(), w.card()
    blockers(w, b, a)
    target = b if kind == "cycle" else a if kind == "self" else w.card(board="plain")
    status, result = invoke(w.boards.task_update, a, blocked_by=[target])
    assert status >= 400 and kind in str(result)  # RED
    assert w.boards.task_show(a)["blocked_by"] == []


def test_r14_startable(hub):
    w = world(hub)
    a, b = w.card(), w.card()
    blockers(w, b, a)
    result = invoke(w.person_start, b)
    assert result[0] >= 400 and "blocked_by" in str(result) and not w.jobs_for(b)  # RED


def test_guard_g15_blocker_is_not_needs_me(hub):
    w = world(hub)
    a, b = w.card(), w.card()
    blockers(w, b, a)
    report = w.boards.needs_me()
    assert all(
        b not in [c["number"] for c in rows] for rows in report.values() if isinstance(rows, list)
    )


@pytest.mark.parametrize("values", [True, [True], [-1], list(range(1, 18))])
def test_blocker_input_bounds_and_rollback(hub, values):
    w = world(hub)
    a = w.card()
    before = w.boards.task_show(a)
    status, result = invoke(w.boards.task_update, a, blocked_by=values)
    assert status == 400 and result.get("code") == "invalid_blockers"
    assert w.boards.task_show(a) == before
    assert invoke(w.boards.task_add, "Rollback", board="acme", blocked_by=values)[0] == 400
    with w.boards.store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_blockers_update_is_versioned_and_audited(hub):
    w = world(hub)
    a, b = w.card(), w.card()
    before = w.boards.task_show(b)
    status, after = invoke(w.boards.task_update, b, version=before["version"], blocked_by=[a, a])
    assert status == 200, after
    assert after["version"] == before["version"] + 1 and after["blocked_by"] == [a]
    assert invoke(w.boards.task_update, b, version=before["version"], blocked_by=[])[0] >= 400
    shown = w.boards.task_show(b)
    assert shown["blocked_by"] == [a] and shown["events"][-1]["kind"] == "edit"


def test_dfs_bound_is_refused_atomically(hub):
    w = world(hub)
    a = w.card()
    from tests._split_helpers import DDL

    with w.boards.store.connection() as conn:
        conn.executescript(DDL)
    with w.boards.store.transaction() as conn:
        # Synthetic chain avoids spending hundreds of service round-trips on setup.
        for n in range(2, 204):
            conn.execute(
                "INSERT INTO tasks(id,board_id,title,body,status,position,flags,stamp_user,stamp_trust,created_at,updated_at,updated_by,created_by,restricted_origin) SELECT ?,board_id,title,body,status,?,flags,stamp_user,stamp_trust,created_at,updated_at,updated_by,created_by,restricted_origin FROM tasks WHERE id=?",
                (n, n, a),
            )
        conn.executemany(
            "INSERT INTO task_blockers VALUES (?,?,?,?)",
            [(n, n + 1, w.owner.actor, "2030-01-01T00:00:00Z") for n in range(2, 203)],
        )
    status, result = invoke(w.boards.task_update, a, blocked_by=[2])
    assert status == 400 and result["code"] == "blocker_graph_too_large"
    assert w.boards.task_show(a)["blocked_by"] == []
