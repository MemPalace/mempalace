"""Identical pin/head probes for approval routing."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from commonplace import events, session
from commonplace.boards import display, service as bs
from commonplace.boards.store import task_row
from commonplace.extensions import ToolContext, ToolError
from commonplace.phone_push import evaluate
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_boards import api, mcp
from tests.test_commonplace_session import _reads

pytest_plugins = ["tests.test_commonplace_board_authority"]


def approve_compat(hub, agent, n, **kw):
    ok, text = mcp(hub, agent.token, "task_approve", task=n, reason="synthetic reason", **kw)
    if not ok and text == "no: invalid arguments":
        ok, text = mcp(hub, agent.token, "task_approve", task=n, **kw)
    return ok, text


def setup(fresh, *, plan=True, holder=None):
    k = fresh.connect("coordinator")
    fresh.owner.board_set("work", coordinator=k.principal.actor)
    c = fresh.owner.task_add(
        board="work", title="Synthetic review", labels=["plan"] if plan else []
    )
    if holder:
        c = fresh.owner.task_handoff(c["number"], holder.principal.actor, "Synthetic assignment")
    return k, c


def proposal(fresh, **kw):
    p = fresh.connect("parent", **kw)
    c = fresh.connect("child", parent=p.id, trust="untrusted", write_wing="inbox")
    return p, c, c.work.task_add(board="work", title="Synthetic proposal", body="Synthetic body")


def numbers(report, key):
    return {c["number"] for c in report.get(key, [])}


def node(payload):
    if not shutil.which("node"):
        pytest.fail("node is required")
    root = Path(__file__).resolve().parents[1]
    code = """
import * as home from './commonplace/web/static/home.js';
import * as decide from './commonplace/web/static/decide.js';
const p=JSON.parse(process.argv[1]);
const rows={dot:()=>'',taskRow:c=>c.title};
console.log(JSON.stringify({html:home.homeView(p,rows),kind:typeof decide.eventItem,
event:typeof decide.eventItem==='function'?decide.eventItem(p.event||{},x=>x):''}));
"""
    out = subprocess.run(
        ["node", "--input-type=module", "-e", code, json.dumps(payload)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if out.returncode:
        pytest.fail(out.stderr)
    return json.loads(out.stdout)


def home(fresh, c):
    return node({"open": [fresh.owner.task_show(c["number"])], "needs": fresh.owner.needs_me()})[
        "html"
    ]


def start(hub, agent, monkeypatch):
    ctx = ToolContext(agent.principal, hub.gateway)
    _reads(monkeypatch, rules="Synthetic effective rules", memories="Synthetic memories")
    return session.session_start(ctx, {})


def test_r01(hub, fresh):
    k, c = setup(fresh)
    html = home(fresh, c)
    assert ">Approve<" not in html  # RED


def test_r02(hub, fresh):
    k, c = setup(fresh)
    assert c["number"] in numbers(k.work.needs_me(), "approvals")  # RED


def test_r03(hub, fresh):
    k, c = setup(fresh)
    held = k.work.task_add(board="work", title="Synthetic held", claim=True)
    ok, text = mcp(hub, k.token, "task_heartbeat", task=held["number"])
    assert ok and f"#{c['number']}" in text  # RED


def test_r04(hub, fresh):
    c = fresh.owner.task_add(board="work", title="Synthetic owner plan", labels=["plan"])
    assert c["number"] in numbers(fresh.owner.needs_me(), "plans")  # RED
    assert ">Approve<" in home(fresh, c)
    assert fresh.owner.task_show(c["number"])["can_approve"]
    status, out = api(hub, fresh.cookie, "POST", f"/api/cards/{c['number']}/approve", {})
    assert status == 200 and "plan" not in out["labels"]


def test_r05():
    c = dict(status="review", labels=[], ask=None, updated_at="2000")
    assert display.need(c, owner=True) != "plan"  # RED


def test_r06(hub, fresh):
    t = fresh.connect("root")
    c = fresh.connect("child", parent=t.id)
    row = t.work.task_add(
        board="work",
        title="Synthetic assigned plan",
        labels=["plan"],
        assignee=c.principal.actor,
        claim=True,
    )
    assert row["number"] in numbers(t.work.needs_me(), "approvals")  # RED
    assert ">Approve<" not in home(fresh, row)
    assert approve_compat(hub, t, row["number"])[0]
    out = fresh.owner.task_show(row["number"])
    assert out["status"] == "doing" and out["assignee"] == c.principal.actor


def test_r07(hub, fresh):
    p, c, row = proposal(fresh)
    ok, text = mcp(hub, p.token, "needs_me")
    assert ok and f"#{row['number']}" in text and "parent" in text  # RED


def test_r08(hub, fresh):
    p, c, row = proposal(fresh)
    ok, text = mcp(
        hub,
        p.token,
        "task_approve",
        task=row["number"],
        escalate=True,
        reason="Synthetic uncertainty",
    )
    assert ok  # RED
    report = fresh.owner.needs_me()
    item = next(c for c in report["backlog_from_agents"] if c["number"] == row["number"])
    assert '"Synthetic uncertainty"' in item["why"]
    assert "need-approve" in home(fresh, row)
    assert row["number"] not in numbers(p.work.needs_me(), "approvals")
    emitted = events.after(hub.db, fresh.workspace)
    event = next(e for e in reversed(emitted) if e["kind"] == "card.review_needed")
    assert event["data"]["ask_to"] == fresh.owner.principal.actor
    from commonplace import webhooks

    hook = dict(
        workspace_id=fresh.workspace,
        kinds='["card.review_needed"]',
        board="work",
        waiting_on=fresh.owner.principal.actor,
        owner_actor=p.principal.actor,
    )
    assert webhooks._matches(hook, event)


def test_r09(hub, fresh):
    k, row = setup(fresh)
    ok, _ = mcp(
        hub, k.token, "task_approve", task=row["number"], escalate=True, reason="Synthetic doubt"
    )
    assert ok  # RED
    assert row["number"] in numbers(fresh.owner.needs_me(), "plans")
    assert ">Approve<" in home(fresh, row)
    assert row["number"] in evaluate.current(hub.gateway, fresh.owner.principal)


def test_r10(hub, fresh):
    p, c, row = proposal(fresh)
    ok, text = mcp(hub, p.token, "task_approve", task=row["number"])
    assert not ok and "reason" in text  # RED
    ok, text = mcp(hub, p.token, "task_approve", task=row["number"], reason="x" * 501)
    assert not ok and "500" in text
    assert approve_compat(hub, p, row["number"])[0]
    assert fresh.owner.task_show(row["number"])["events"][-1]["reason"] == "synthetic reason"


def test_r11(hub, fresh, monkeypatch):
    p, c, row = proposal(fresh)
    text = start(hub, p, monkeypatch)
    assert f"#{row['number']}" in text  # RED
    assert text.index("Synthetic effective rules") < text.index(f"#{row['number']}")


def test_r12(hub, fresh):
    k, row = setup(fresh)
    with k.work.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET approved_by=? WHERE id=?", (k.principal.actor, row["number"])
        )
    ok, _ = approve_compat(hub, k, row["number"], approve=False)
    assert ok  # RED
    out = fresh.owner.task_show(row["number"])
    assert out["status"] == "backlog" and "plan" in out["labels"]
    assert out["approved_by"] == k.principal.actor


def test_r12b(hub, fresh):
    k, row = setup(fresh)
    w = fresh.connect("worker")
    w.work.task_take_next(task=row["number"])
    ok, text = approve_compat(hub, k, row["number"], approve=False)
    assert not ok and "held plan card" in text  # RED
    before = fresh.owner.task_show(row["number"])
    assert before["claim_until"]
    status, out = api(
        hub, fresh.cookie, "POST", f"/api/cards/{row['number']}/approve", {"approve": False}
    )
    assert status == 200 and out["claim_until"] is None and out["assignee"] == w.principal.actor


def test_r13(hub, fresh):
    t = fresh.connect("root")
    p = fresh.connect("parent", parent=t.id, trust="standard")
    c = fresh.connect("child", parent=p.id, trust="untrusted", write_wing="inbox")
    row = c.work.task_add(board="work", title="Synthetic restricted proposal")
    assert approve_compat(hub, p, row["number"])[0]
    assert fresh.owner.task_show(row["number"])["approved_by"] == p.principal.actor  # RED


def test_r15(hub, fresh):
    k, _ = setup(fresh)
    x = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    row = x.work.task_add(board="work", title="Synthetic restricted plan", labels=["plan"])
    with k.work.store.transaction() as conn:
        conn.execute("UPDATE tasks SET approved_by=NULL WHERE id=?", (row["number"],))
    approve_compat(hub, k, row["number"])
    assert fresh.owner.task_show(row["number"])["approved_by"] == k.principal.actor  # RED


def test_r16(hub, fresh):
    t = fresh.connect("restricted-root", trust="untrusted", write_wing="inbox")
    c = fresh.connect("child", parent=t.id)
    # Restricted parents can assign their own work without a proposal.
    row = t.work.task_add(
        board="work", title="Synthetic child plan", assignee=c.principal.actor, labels=["plan"]
    )
    ok, _ = approve_compat(hub, t, row["number"])
    assert ok  # RED
    assert fresh.owner.task_show(row["number"])["approved_by"] is None


def test_r16b(hub, fresh):
    t = fresh.connect("root")
    c = fresh.connect("child", parent=t.id)
    row = t.work.task_add(
        board="work",
        title="Synthetic changed trust plan",
        assignee=c.principal.actor,
        labels=["plan"],
    )
    with t.work.store.transaction() as conn:
        conn.execute("UPDATE tasks SET restricted_origin=1 WHERE id=?", (row["number"],))
    approve_compat(hub, t, row["number"])
    assert fresh.owner.task_show(row["number"])["approved_by"] == t.principal.actor  # RED


def test_r17(hub, fresh):
    k, row = setup(fresh)
    with k.work.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET approved_by=? WHERE id=?",
            (fresh.owner.principal.actor, row["number"]),
        )
    ok, _ = approve_compat(hub, k, row["number"])
    marker = fresh.owner.task_show(row["number"])["approved_by"]
    assert ok and marker == fresh.owner.principal.actor  # RED
    p, child, pending = proposal(fresh)
    with k.work.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET approved_by=? WHERE id=?", (k.principal.actor, pending["number"])
        )
    restored = fresh.owner.task_approve(pending["number"])
    assert restored["approved_by"] == k.principal.actor


def test_r18(hub, fresh):
    t = fresh.connect("root")
    c = fresh.connect("child", parent=t.id)
    w = fresh.connect("worker")
    a = t.work.task_add(
        board="work", title="Synthetic routed", assignee=c.principal.actor, labels=["plan"]
    )
    b = t.work.task_add(
        board="work", title="Synthetic unrouted", assignee=w.principal.actor, labels=["plan"]
    )
    plans = numbers(fresh.owner.needs_me(), "plans")
    assert b["number"] in plans and a["number"] not in plans  # RED


def test_r19(hub, fresh):
    x = fresh.connect("restricted-root", trust="untrusted", write_wing="inbox")
    k = fresh.connect("coordinator")
    fresh.owner.board_set("work", coordinator=k.principal.actor)
    row = x.work.task_add(
        board="work", title="Synthetic self plan", assignee=k.principal.actor, labels=["plan"]
    )
    ok, text = approve_compat(hub, k, row["number"])
    assert fresh.owner.task_show(row["number"])["need"] == "you"  # RED
    assert not ok and "direct parent" in text
    w = fresh.connect("worker")
    with pytest.raises(ToolError):
        w.work.task_take_next(task=row["number"])


def test_r19b(hub, fresh):
    k, row = setup(fresh)
    x = fresh.connect("other")
    with k.work.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET approved_by=?,assignee=? WHERE id=?",
            (k.principal.actor, k.principal.actor, row["number"]),
        )
        assert not k.work._origin_released(task_row(conn, row["number"]))  # RED
        conn.execute("UPDATE tasks SET assignee=? WHERE id=?", (x.principal.actor, row["number"]))
        assert k.work._origin_released(task_row(conn, row["number"]))


def test_r20(hub, fresh):
    p, c, row = proposal(fresh)
    ok, text = mcp(hub, p.token, "needs_me", format="digest")
    assert ok and row["title"] in text  # RED


def count_roles(monkeypatch):
    calls = []
    original = getattr(bs.BoardService, "_approval_role", None)
    if original:

        def wrapped(self, q, row):
            calls.append(row["id"])
            return original(self, q, row)

        monkeypatch.setattr(bs.BoardService, "_approval_role", wrapped)
    return calls


def test_r21(hub, fresh, monkeypatch):
    k, _ = setup(fresh)
    for i in range(200):
        fresh.owner.task_add(board="work", title=f"Synthetic plan {i}", labels=["plan"])
    calls = count_roles(monkeypatch)
    text = start(hub, k, monkeypatch)
    assert "approvals waiting on you" in text and "more" in text  # RED
    assert text.count("(board coordinator, plan)") == 5
    assert len(calls) <= 60


def test_r22(hub, fresh):
    k, row = setup(fresh)
    old = fresh.connect("old")
    hub.db.revoke(old.id, actor=fresh.user, workspace_ref=fresh.workspace)
    with k.work.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET approved_by=? WHERE id=?", (old.principal.actor, row["number"])
        )
    approve_compat(hub, k, row["number"])
    assert fresh.owner.task_show(row["number"])["approved_by"] == k.principal.actor  # RED


def test_r23(hub, fresh):
    p, c, row = proposal(fresh)
    assert approve_compat(hub, p, row["number"])[0]
    event = fresh.owner.task_show(row["number"])["events"][-1]
    assert event.get("reason") == "synthetic reason"  # RED
    rendered = node({"event": event})
    assert rendered["kind"] == "function" and "synthetic reason" in rendered["event"]


def test_r24(hub, fresh):
    p, c, row = proposal(fresh)
    ok, _ = mcp(
        hub,
        p.token,
        "task_approve",
        task=row["number"],
        escalate=True,
        reason="Synthetic uncertainty",
    )
    assert ok  # RED
    assert fresh.owner.task_show(row["number"]).get("escalated_to")
    fresh.owner.task_approve(row["number"])
    assert fresh.owner.task_show(row["number"]).get("escalated_to") is None


def test_r25(hub, fresh, monkeypatch):
    k, _ = setup(fresh)
    for i in range(60):
        k.work.task_add(board="work", title=f"Synthetic own plan {i}", labels=["plan"], claim=True)
    row = fresh.owner.task_add(board="work", title="Synthetic newest", labels=["plan"])
    calls = count_roles(monkeypatch)
    text = start(hub, k, monkeypatch)
    assert f"#{row['number']}" in text or "list truncated" in text  # RED
    assert len(calls) <= 60


def test_r26(hub, fresh, monkeypatch):
    p, c, row = proposal(fresh, read_wings=["elsewhere"])
    k = fresh.connect("coordinator")
    fresh.owner.board_set("work", coordinator=k.principal.actor)
    held = k.work.task_add(board="work", title="Synthetic held", claim=True)
    ok, text = mcp(hub, k.token, "task_heartbeat", task=held["number"])
    assert ok and f"#{row['number']}" in text  # RED: readable coordinator positive control
    assert row["number"] in numbers(k.work.needs_me(), "approvals")
    status, report = api(hub, k.token, "GET", "/api/needs-me")
    assert status == 200 and row["title"] in json.dumps(report)
    assert row["number"] not in numbers(p.work.needs_me(), "approvals")
    fresh.owner.board_add(slug="private", wing="elsewhere")
    held = p.work.task_add(board="private", title="Synthetic private held", claim=True)
    assert row["title"] not in mcp(hub, p.token, "task_heartbeat", task=held["number"])[1]
    assert row["title"] not in start(hub, p, monkeypatch)
    status, report = api(hub, p.token, "GET", "/api/needs-me")
    assert status == 200 and row["body"] not in json.dumps(report)


def test_r27(hub, fresh):
    p, c, row = proposal(fresh)
    ok, _ = mcp(
        hub,
        p.token,
        "task_approve",
        task=row["number"],
        escalate=True,
        reason="Synthetic uncertainty",
    )
    assert ok  # RED
    ok, text = approve_compat(hub, p, row["number"])
    assert not ok and "escalated to the owner" in text
    out = fresh.owner.task_show(row["number"])
    assert out["approved_by"] is None
    assert row["number"] in numbers(fresh.owner.needs_me(), "backlog_from_agents")


@pytest.mark.parametrize(
    "case",
    [
        "g01",
        "g02",
        "g03",
        "g04",
        "g05",
        "g06",
        "g07",
        "g08",
        "g09",
        "g10",
        "g11",
        "g12",
        "g13",
        "g14",
        "g15",
        "g16",
    ],
)
def test_guard(hub, fresh, monkeypatch, case):
    p, c, row = proposal(fresh)
    if case in ("g01", "g10"):
        assert fresh.owner.task_approve(row["number"])["approved_by"] == fresh.owner.principal.actor
    elif case == "g03":
        creator = fresh.connect("restricted-creator", trust="untrusted", write_wing="inbox")
        row = creator.work.task_add(board="work", title="Synthetic root claim", claim=True)
        creator.work.task_handoff(row["number"], "anyone", "Synthetic shared queue")
        claimer = fresh.connect("top-level-claimer")
        claimer.work.task_take_next(task=row["number"])
        ok, text = approve_compat(hub, claimer, row["number"])
        assert not ok and "direct parent" in text
        second = fresh.connect("second-worker")
        with pytest.raises(ToolError):
            second.work.task_take_next(task=row["number"])
    elif case in ("g02", "g06"):
        a = c if case == "g06" else fresh.connect("unrelated")
        ok, text = approve_compat(hub, a, row["number"])
        assert not ok and "direct parent" in text
    elif case == "g04":
        assert row["number"] not in numbers(fresh.owner.needs_me(), "backlog_from_agents")
        assert fresh.owner.task_show(row["number"])["need"] == "plan"
    elif case == "g05":
        from tests._commonplace_helpers import REVIEW_CONTEXT, REVIEW_OPTIONS, REVIEW_QUESTION

        fresh.owner.task_approve(row["number"])
        c.work.task_take_next(task=row["number"])
        c.work.ask(REVIEW_QUESTION, REVIEW_OPTIONS, task=row["number"], context=REVIEW_CONTEXT)
        assert ">Answer<" in home(fresh, row)
    elif case == "g07":
        k = fresh.connect("unrelated")
        foreign = fresh.connect("foreign", workspace_ref=hub.workspace, user_ref=hub.admin)
        foreign.work.board_add(slug="other", wing="shared")
        foreign.work.task_add(board="other", title="Synthetic foreign plan", labels=["plan"])
        report = k.work.needs_me()
        assert row["number"] not in numbers(report, "approvals")
        assert "Synthetic foreign plan" not in json.dumps(report)
    elif case == "g08":
        a = fresh.connect("empty")
        path = Path(a.work.ctx.workspace_dir()) / "boards.sqlite3"
        # Missing-store session reader must not create a store.
        path.unlink()
        start(hub, a, monkeypatch)
        assert not path.exists()
    elif case == "g09":
        hub.db.revoke(p.id, actor=fresh.user, workspace_ref=fresh.workspace)
        assert "need-approve" in home(fresh, row)
    elif case in ("g11", "g12", "g13", "g14"):
        if case == "g11":
            a = fresh.connect("restricted-root", trust="untrusted", write_wing="inbox")
            child = fresh.connect("root-child", parent=a.id, trust="untrusted", write_wing="inbox")
            row = child.work.task_add(board="work", title="Synthetic root release")
        elif case == "g12":
            a = fresh.connect("coordinator")
            fresh.owner.board_set("work", coordinator=a.principal.actor)
        else:
            t = fresh.connect("root")
            a = fresh.connect(
                "parent2",
                parent=t.id,
                trust="untrusted" if case == "g14" else "trusted",
                write_wing="inbox" if case == "g14" else "shared",
            )
            child = fresh.connect("child2", parent=a.id, trust="untrusted", write_wing="inbox")
            row = child.work.task_add(board="work", title="Synthetic authority")
        assert approve_compat(hub, a, row["number"])[0]
        assert fresh.owner.task_show(row["number"])["approved_by"] == (
            None if case == "g14" else a.principal.actor
        )
    elif case == "g15":
        k = fresh.connect("coordinator")
        fresh.owner.board_set("work", coordinator=k.principal.actor)
        for i in range(200):
            fresh.owner.task_add(
                board="work",
                title=f"Synthetic held plan {i}",
                labels=["plan"],
                assignee=k.principal.actor,
                claim=True,
            )
        calls = count_roles(monkeypatch)
        text = start(hub, k, monkeypatch)
        assert len(calls) <= 60
        if hasattr(bs.BoardService, "approvals_for_me"):
            assert calls and "list truncated" in text
    elif case == "g16":
        status, _ = api(
            hub, fresh.cookie, "POST", f"/api/cards/{row['number']}/approve", {"approve": None}
        )
        assert status == 403


def test_g_reject(hub, fresh):
    k, row = setup(fresh)
    x = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    row = x.work.task_add(board="work", title="Synthetic restricted proposal")
    approve_compat(hub, k, row["number"], approve=False)
    assert fresh.owner.task_show(row["number"])["approved_by"] is None
    fresh.owner.task_update(row["number"], status="todo")
    child = fresh.connect("child", parent=x.id)
    with pytest.raises(ToolError):
        child.work.task_take_next(task=row["number"])


def test_g_premigration_reader(hub, fresh, monkeypatch):
    p, c, row = proposal(fresh)
    with p.work.store.transaction() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "escalated_to" in columns:
            conn.execute("ALTER TABLE tasks DROP COLUMN escalated_to")
        board_columns = {r[1] for r in conn.execute("PRAGMA table_info(boards)")}
        if "coordinator" in board_columns:
            conn.execute("ALTER TABLE boards DROP COLUMN coordinator")
        before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    text = start(hub, p, monkeypatch)
    if hasattr(bs.BoardService, "approvals_for_me"):
        assert f"#{row['number']}" in text
    with p.work.store.connection() as conn:
        after = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_r28_truncated_list_digest(hub, fresh, monkeypatch):
    k, _ = setup(fresh)
    cards = []
    for i in range(205):
        cards.append(
            k.work.task_add(
                board="work", title=f"Synthetic held plan {i}", labels=["plan"], claim=True
            )
        )
    report = k.work.needs_me()
    assert report.get("approvals_truncated") is True  # RED
    for worker in (k.work, fresh.owner):
        result = worker.needs_me()
        assert isinstance(result["count"], int)
        assert isinstance(result["approvals_truncated"], bool)
        digest = worker.needs_me(format="digest")
        assert isinstance(digest["text"], str)
        assert digest["count"] == result["count"]
    owner_report = fresh.owner.needs_me()
    assert {c["number"] for c in owner_report["plans"]} == {c["number"] for c in cards}
    assert owner_report["count"] == len(cards)
    ok, text = mcp(hub, k.token, "needs_me")
    assert ok and "list truncated" in text
    ok, text = mcp(hub, k.token, "needs_me", format="digest")
    assert ok and "list truncated" in text
