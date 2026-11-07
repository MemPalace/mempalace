"""Opt-in main-role triggers and bounded reconciliation on an injected clock."""

from concurrent.futures import ThreadPoolExecutor

from commonplace.orch import durable, runner_bridge, triggers
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._phase_b_helpers import cap, doing
from tests._split_helpers import DDL, age, tool, world

T0 = "2030-01-01T00:00:00Z"


def test_r19h_main_and_inbox(hub):
    w = world(hub, runner=True)
    a = w.card()
    report = triggers.run(w.js, now=T0)
    rows = w.jobs_for(a)
    assert len(rows) == 1 and rows[0]["topic"] == "orchestrate"  # RED
    with w.js.connection() as conn:
        message = runner_bridge.launch_fields(conn, rows[0])["message"]
    assert "orchestrator_inbox" in message and "Coordinate this project" in message
    assert rows[0]["request_key"].startswith("trig:")
    assert report["acme"][0]["job"] == rows[0]["id"]
    assert "1 unsplit cards" in str(durable.checkpoint(w.ctx(w.owner), "acme", "main"))
    triggers.run(w.js, now=T0)
    assert len(w.jobs_for(a)) == 1


def test_r19h2_no_child_trigger(hub):
    w = world(hub, runner=True)
    a, child = w.card(status="backlog"), w.card()
    with w.boards.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET parent_number=?,labels=? WHERE id=?",
            (a, f'["split:{a}:build"]', child),
        )
    triggers.run(w.js, now=T0)
    assert not w.jobs_for(child)  # RED


def test_r19i_retry_after_cap_and_marker(hub):
    w = world(hub, runner=True)
    cap(w, 1)
    held = w.card(status="backlog")
    doing(w, held, w.a.actor)
    a = w.card()
    age(w, a)
    triggers.run(w.js, now=T0)
    assert not w.jobs_for(a)
    w.update(held, status="done")
    triggers.run(w.js, now="2030-01-01T00:10:00Z")
    rows = w.jobs_for(a)
    assert len(rows) == 1 and rows[0]["request_key"] == f"split-retry:{a}:1"  # RED
    assert tool(w, "card_split_done", task=a, state="kept")[0] == 200
    with w.js.transaction() as conn:
        conn.execute("UPDATE runner_jobs SET state='cancelled' WHERE task_number=?", (a,))
    triggers.run(w.js, now="2030-01-01T02:30:00Z")
    assert len(w.jobs_for(a)) == 1


def test_r19i2_two_sweeps_one_retry(hub, monkeypatch):
    w = world(hub, runner=True)
    a = w.card()
    age(w, a)
    # Consume the event while at cap; the following sweeps reconcile the same root.
    cap(w, 1)
    held = w.card(status="backlog")
    doing(w, held, w.a.actor)
    triggers.run(w.js, now=T0)
    w.update(held, status="done")
    from threading import Barrier

    both_ready = Barrier(2)
    start = runner_bridge.fenced_start

    def together(*args, **kwargs):
        if kwargs.get("trigger") and kwargs["trigger"].retry:
            both_ready.wait(timeout=8)
        return start(*args, **kwargs)

    monkeypatch.setattr(runner_bridge, "fenced_start", together)
    with ThreadPoolExecutor(2) as pool:
        reports = list(pool.map(lambda _: triggers.run(w.js, now="2030-01-01T00:10:00Z"), range(2)))
    assert len(w.jobs_for(a)) == 1  # RED
    assert len(reports) == 2
    retries = [r for r in w.misses() if r["event_id"] < 0 and r["card"] == a]
    assert len(retries) == 1 and retries[0]["code"] == "split_started"


def test_r19k_three_refusals_no_fourth(hub):
    w = world(hub, runner=True)
    cap(w, 1)
    held = w.card(status="backlog")
    doing(w, held, w.a.actor)
    a = w.card()
    age(w, a)
    triggers.run(w.js, now=T0)
    for at in (
        "2030-01-01T00:10:00Z",
        "2030-01-01T00:40:00Z",
        "2030-01-01T02:10:00Z",
        "2030-01-02T00:00:00Z",
    ):
        triggers.run(w.js, now=at)
    reason = w.boards.task_show(a).get("waiting")
    assert reason and reason["code"] == "not_released" and "split stuck" in reason["text"]  # RED
    retries = [r for r in w.misses() if r["event_id"] < 0 and r["card"] == a]
    assert len(retries) == 3 and not w.jobs_for(a)
    assert "split stuck" in str(durable.checkpoint(w.ctx(w.owner), "acme", "main"))


def test_guard_r19j_off_baseline(hub):
    w = world(hub, on=False, runner=True)
    a = w.card()
    triggers.run(w.js, now=T0)
    assert len(w.jobs_for(a)) == 1 and w.jobs_for(a)[0]["topic"] == "worker"
    assert w.jobs_for(a)[0]["request_key"].startswith("trig:")


def test_guard_g8_hub_never_splits(hub):
    import ast
    from pathlib import Path

    for path in ("commonplace/orch/triggers.py", "commonplace/projects/split.py"):
        if Path(path).exists():
            tree = ast.parse(Path(path).read_text())
            assert not any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "task_add"
                for n in ast.walk(tree)
            )
    w = world(hub, runner=True)
    a = w.card()
    triggers.run(w.js, now=T0)
    with w.boards.store.connection() as conn:
        conn.executescript(DDL)
        assert (
            conn.execute("SELECT count(*) FROM tasks WHERE parent_number=?", (a,)).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT count(*) FROM card_splits").fetchone()[0] == 0
