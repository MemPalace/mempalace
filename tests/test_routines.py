"""Routines: one card per due time, a visible history, owner-only management."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from commonplace import routines
from tests._console_helpers import console_hub_fixture  # noqa: F401
from tests.test_commonplace_boards import service

T0 = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)


def ts(delta=timedelta()):
    return (T0 + delta).timestamp()


@pytest.fixture(autouse=True)
def routine_environment(monkeypatch):
    monkeypatch.setenv("COMMONPLACE_ROUTINE_WORKER", "off")


@pytest.fixture
def h(console_hub):
    return console_hub


def body(**over):
    value = {
        "name": "Weekly review",
        "template": {"board": "work", "title": "Review {date}", "priority": "high"},
        "schedule": {"every_minutes": 60},
    }
    value.update(over)
    return value


def make(h, **over):
    worker = h.gateway.routine_worker
    # Created at T0 on the fake clock (the worker's clock drives creation time too).
    worker.now = lambda: ts()
    status, result, _ = h.request(h.owner, "POST", "/api/routines", body(**over))
    assert status == 201, result
    return result


def cards(h):
    return service(h, h.person(h.admin)).task_list(board="work", all_statuses=True)


def routine_cards(h):
    return [c for c in cards(h) if "routine" in c["labels"]]


def runs(h, identity):
    return h.get(h.owner, f"/api/routines/{identity}")[1]["runs"]


def test_one_card_per_due_time_with_template_priority_and_history(h):
    r = make(h)
    worker = h.gateway.routine_worker
    assert worker.run_once(now=ts(timedelta(minutes=30))) == 0
    assert routine_cards(h) == []
    assert worker.run_once(now=ts(timedelta(minutes=61))) == 1
    # The same due time never makes a second card, however often the worker passes.
    assert worker.run_once(now=ts(timedelta(minutes=62))) == 0
    assert worker.run_once(now=ts(timedelta(minutes=119))) == 0
    made = routine_cards(h)
    assert len(made) == 1
    assert made[0]["title"] == "Review 2026-11-11"
    assert made[0]["priority"] == "high"
    assert made[0]["status"] == "todo"
    assert worker.run_once(now=ts(timedelta(minutes=121))) == 1
    history = runs(h, r["id"])
    assert [x["state"] for x in history] == ["created", "created"]
    assert {x["card"] for x in history} == {c["number"] for c in routine_cards(h)}
    assert all(x["board"] == "work" for x in history)
    assert history[0]["due_at"] > history[1]["due_at"]


def test_restart_between_claim_and_card_creates_exactly_one_card(h):
    r = make(h)
    worker = h.gateway.routine_worker
    row = h.db._conn.execute("SELECT * FROM routines WHERE id=?", (r["id"],)).fetchone()
    due = T0 + timedelta(hours=1)
    run = worker._insert(row, due, "claimed", ts(timedelta(minutes=61)))
    assert run is not None and routine_cards(h) == []
    # A fresh worker (restart) settles the orphan; the claim is not repeated.
    assert worker.run_once(now=ts(timedelta(minutes=62))) == 1
    assert worker.run_once(now=ts(timedelta(minutes=63))) == 0
    assert len(routine_cards(h)) == 1
    # A crash after the card but before the run row was updated finds the card by its marker.
    row2 = h.db._conn.execute("SELECT * FROM routines WHERE id=?", (r["id"],)).fetchone()
    again = worker._insert(row2, T0 + timedelta(hours=2), "claimed", ts(timedelta(hours=2)))
    s = service(h, h.person(h.admin))
    s.task_add("pre-existing", board="work", labels=["routine", routines.MARKER + again])
    assert worker.run_once(now=ts(timedelta(hours=2, minutes=1))) == 1
    assert len(routine_cards(h)) == 2  # the pre-existing card, no duplicate
    assert runs(h, r["id"])[0]["state"] == "created"


def test_downtime_makes_up_a_few_and_records_one_skipped_row(h):
    r = make(h)
    worker = h.gateway.routine_worker
    worker.run_once(now=ts(timedelta(hours=10, minutes=1)))
    assert len(routine_cards(h)) == routines.MAX_CATCH_UP
    states = [x["state"] for x in runs(h, r["id"])]
    assert states.count("created") == routines.MAX_CATCH_UP
    assert states.count("skipped") == 1
    skipped = next(x for x in runs(h, r["id"]) if x["state"] == "skipped")
    assert "7" in skipped["detail"]


def test_pause_resume_and_delete(h):
    r = make(h)
    worker = h.gateway.routine_worker
    worker.now = lambda: ts(timedelta(minutes=10))
    status, paused, _ = h.request(h.owner, "PATCH", f"/api/routines/{r['id']}", {"paused": True})
    assert status == 200 and paused["state"] == "paused" and paused["next_due"] is None
    assert worker.run_once(now=ts(timedelta(hours=3))) == 0
    assert routine_cards(h) == []
    worker.now = lambda: ts(timedelta(hours=3))
    status, resumed, _ = h.request(h.owner, "PATCH", f"/api/routines/{r['id']}", {"paused": False})
    assert resumed["state"] == "active"
    # Time spent paused is not made up.
    assert worker.run_once(now=ts(timedelta(hours=3, minutes=1))) == 0
    assert worker.run_once(now=ts(timedelta(hours=4, minutes=1))) == 1
    assert len(routine_cards(h)) == 1
    status, gone, _ = h.request(h.owner, "DELETE", f"/api/routines/{r['id']}")
    assert status == 200
    assert h.get(h.owner, f"/api/routines/{r['id']}")[0] == 404
    assert h.get(h.owner, "/api/routines")[1]["routines"] == []
    assert worker.run_once(now=ts(timedelta(hours=9))) == 0
    assert len(routine_cards(h)) == 1


def test_refusals_for_non_owners_and_agents(h):
    r = make(h)
    for who in (h.member, h.t1, None):
        for method, path, payload in (
            ("GET", "/api/routines", None),
            ("POST", "/api/routines", body()),
            ("GET", f"/api/routines/{r['id']}", None),
            ("PATCH", f"/api/routines/{r['id']}", {"paused": True}),
            ("DELETE", f"/api/routines/{r['id']}", None),
        ):
            status, _, _ = h.request(who, method, path, payload)
            assert status in (401, 403), (who, method, path, status)
    assert len(h.get(h.owner, "/api/routines")[1]["routines"]) == 1
    # Another workspace's owner cannot see or touch it either.
    assert h.get(h.other, f"/api/routines/{r['id']}")[0] == 404
    assert h.request(h.other, "DELETE", f"/api/routines/{r['id']}")[0] == 404
    assert h.get(h.owner, f"/api/routines/{r['id']}")[1]["state"] == "active"


def test_owner_must_be_a_workspace_owner(h):
    status, result, _ = h.request(h.owner, "POST", "/api/routines", body(owner=h.member_user))
    assert status == 400 and "owner" in result["error"]


def test_a_failing_run_is_recorded_and_pauses_the_routine(h):
    second = h.db.create_user("second@example.com")
    h.db.add_member(h.workspace, second, "owner", actor=h.admin)
    r = make(h, owner=second)
    worker = h.gateway.routine_worker
    # The routine's owner loses ownership: runs fail visibly instead of creating cards.
    with h.db._lock:
        h.db._conn.execute(
            "UPDATE memberships SET role='member' WHERE workspace_id=? AND user_id=?",
            (h.workspace, second),
        )
    for n in range(1, routines.PAUSE_AFTER_FAILURES + 1):
        worker.run_once(now=ts(timedelta(hours=n, minutes=1)))
    shown = h.get(h.owner, f"/api/routines/{r['id']}")[1]
    assert [x["state"] for x in shown["runs"]] == ["failed"] * routines.PAUSE_AFTER_FAILURES
    assert "no longer an owner" in shown["runs"][0]["detail"]
    assert shown["state"] == "paused" and "in a row" in shown["paused_reason"]
    assert routine_cards(h) == []


def test_missing_board_fails_the_run_without_creating_a_board(h):
    r = make(h)
    template = {"board": "nope", "title": "x", "body": "", "labels": [], "status": "todo"}
    template["priority"] = "normal"
    with h.db._lock:
        h.db._conn.execute(
            "UPDATE routines SET template=? WHERE id=?", (json.dumps(template), r["id"])
        )
    h.gateway.routine_worker.run_once(now=ts(timedelta(minutes=61)))
    assert runs(h, r["id"])[0]["state"] == "failed"
    slugs = [b["slug"] for b in service(h, h.person(h.admin)).board_list()]
    assert "nope" not in slugs


def test_limits_and_validation(h):
    for bad in (
        body(schedule={"every_minutes": 5}),
        body(template={"board": "work"}),
        body(template={"board": "missing-board", "title": "x"}),
        body(template={"board": "work", "title": "x", "priority": "asap"}),
        body(template={"board": "work", "title": "x", "status": "doing"}),
        body(name=""),
    ):
        assert h.request(h.owner, "POST", "/api/routines", bad)[0] == 400, bad
    for n in range(routines.MAX_ROUTINES):
        assert h.request(h.owner, "POST", "/api/routines", body(name=f"r{n}"))[0] == 201
    status, result, _ = h.request(h.owner, "POST", "/api/routines", body(name="one too many"))
    assert status == 400 and "at most" in result["error"]
    listing = h.get(h.owner, "/api/routines")[1]
    assert listing["limits"]["min_interval_minutes"] == 60
    first = listing["routines"][0]["id"]
    h.request(h.owner, "DELETE", f"/api/routines/{first}")
    assert h.request(h.owner, "POST", "/api/routines", body(name="fits again"))[0] == 201


def test_history_is_bounded(h):
    r = make(h)
    worker = h.gateway.routine_worker
    for n in range(1, routines.RUN_HISTORY + 20):
        worker.run_once(now=ts(timedelta(hours=n, minutes=1)))
    count = h.db._conn.execute("SELECT count(*) FROM routine_runs").fetchone()[0]
    assert count == routines.RUN_HISTORY
    assert r["id"]


def test_cron_in_a_time_zone_and_schedule_update(h):
    r = make(h, schedule={"cron": "0 9 * * *"}, timezone="Europe/Amsterdam")
    assert r["timezone"] == "Europe/Amsterdam"
    assert r["next_due"] == "2026-11-12T08:00:00Z"
    worker = h.gateway.routine_worker
    assert worker.run_once(now=ts(timedelta(hours=23))) == 0
    assert worker.run_once(now=ts(timedelta(hours=24, minutes=1))) == 1
    assert routine_cards(h)[0]["title"] == "Review 2026-11-12"
    worker.now = lambda: ts(timedelta(hours=25))
    status, changed, _ = h.request(
        h.owner, "PATCH", f"/api/routines/{r['id']}", {"schedule": {"every_minutes": 120}}
    )
    assert status == 200 and changed["schedule"] == {"every_minutes": 120}
    assert changed["timezone"] == "Europe/Amsterdam"
    assert worker.run_once(now=ts(timedelta(hours=26))) == 0
    assert worker.run_once(now=ts(timedelta(hours=27, minutes=1))) == 1
    bad = {"schedule": {"cron": "*/5 * * * *"}}
    assert h.request(h.owner, "PATCH", f"/api/routines/{r['id']}", bad)[0] == 400
    assert (
        h.request(h.owner, "PATCH", f"/api/routines/{r['id']}", {"timezone": "Mars/Base"})[0] == 400
    )
