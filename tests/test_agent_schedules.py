"""Scheduled prompts per agent: owners schedule, the worker delivers once, with a fake clock.

Delivery is the ordinary board wake path: an owner-authored message on the board the agent
coordinates. The clock is a plain float the tests move; the worker never sleeps here.
"""

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from commonplace import schedule_time as times
from commonplace.agent_schedules import store, worker
from tests._chat_helpers import chat_environment, chat_hub_fixture  # noqa: F401

START = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc).timestamp()
MIN = 60


@pytest.fixture(autouse=True)
def manual_worker(monkeypatch):
    monkeypatch.setenv("COMMONPLACE_SCHEDULE_WORKER", "off")


class Clock:
    def __init__(self):
        self.t = START

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * MIN


@pytest.fixture
def clock(chat_hub):
    clock = Clock()
    chat_hub.gateway.agent_schedules.now = clock
    return clock


@pytest.fixture
def h(chat_hub, clock):
    chat_hub.set_coordinator("a1")
    return chat_hub


def body(h, **extra):
    return {
        "name": "Morning check",
        "agent": h.a1,
        "board": "work",
        "prompt": "Summarise what is blocked, in two lines.",
        "schedule": {"every_minutes": 30},
        "timezone": "UTC",
        **extra,
    }


def create(h, **extra):
    status, result, _ = h.request(h.owner, "POST", "/api/agent-schedules", body(h, **extra))
    assert status == 201, (status, result)
    return result


def posted(h):
    return [m for m in h.space()["messages"] if "Scheduled prompt" in m["text"]]


def run(h, clock, minutes=0):
    clock.advance(minutes)
    return h.gateway.agent_schedules.run_once()


def show(h, identity):
    status, result, _ = h.request(h.owner, "GET", f"/api/agent-schedules/{identity}")
    assert status == 200, result
    return result


def act(h, identity, action, auth=None):
    return h.request(auth or h.owner, "POST", f"/api/agent-schedules/{identity}/{action}", {})


def test_fires_once_per_due_time_through_the_board_message(h, clock):
    seq = h.seq()
    made = create(h)
    assert made["state"] == "active" and made["next_due"] == "2026-11-11T10:30:00Z"
    assert run(h, clock, 20) == 0 and posted(h) == []  # 10:20, not due
    assert run(h, clock, 11) == 1  # 10:31
    (message,) = posted(h)
    assert message["text"].endswith("Summarise what is blocked, in two lines.")
    assert 'Scheduled prompt "Morning check" (due 2026-11-11T10:30:00Z)' in message["text"]
    assert h.events_since(seq).count("space_message") == 1  # the wake event, once
    assert run(h, clock) == 0 and len(posted(h)) == 1  # same due time never twice
    after = show(h, made["id"])
    assert after["next_due"] == "2026-11-11T11:00:00Z"  # stays on the anchor grid
    assert after["last_status"] == "delivered"
    assert [r["state"] for r in after["runs"]] == ["delivered"]
    assert after["runs"][0]["message_id"] == message["id"]
    run(h, clock, 30)
    assert len(posted(h)) == 2


def test_cron_in_a_timezone(h, clock):
    # 09:00 Amsterdam is 08:00 UTC in November; the fixture clock is Wed 10:07 UTC.
    made = create(h, schedule={"cron": "0 9 * * 1-5"}, timezone="Europe/Amsterdam")
    assert made["next_due"] == "2026-11-12T08:00:00Z"
    clock.t = datetime(2026, 11, 12, 8, 1, tzinfo=timezone.utc).timestamp()
    assert h.gateway.agent_schedules.run_once() == 1
    assert len(posted(h)) == 1


def test_survives_a_restart_without_a_second_delivery(h, clock):
    made = create(h)
    clock.advance(31)

    def restart():
        h.gateway.agent_schedules = worker.ScheduleWorker(h.gateway, start=False, now=clock)

    restart()  # a fresh process before the due time was handled
    assert h.gateway.agent_schedules.run_once() == 1
    restart()  # and another one after it
    assert h.gateway.agent_schedules.run_once() == 0
    assert len(posted(h)) == 1
    clock.advance(30)
    restart()
    assert h.gateway.agent_schedules.run_once() == 1  # the next due time still fires
    assert len(posted(h)) == 2
    assert [r["state"] for r in show(h, made["id"])["runs"]] == ["delivered", "delivered"]


def test_a_crash_between_claim_and_delivery_is_not_redelivered(h, clock):
    made = create(h)
    clock.advance(31)
    row = store.due(h.db, worker.moment(clock()))[0]
    assert store.claim(h.db, row, worker.moment(clock()), missed=False)  # claimed, then "crash"
    h.gateway.agent_schedules = worker.ScheduleWorker(h.gateway, start=False, now=clock)
    assert h.gateway.agent_schedules.run_once() == 0
    assert posted(h) == []
    after = show(h, made["id"])
    assert after["runs"][0]["state"] == "failed" and after["runs"][0]["code"] == "interrupted"
    assert after["last_code"] == "interrupted"
    clock.advance(30)
    assert h.gateway.agent_schedules.run_once() == 1  # later due times are unaffected


def test_a_due_time_missed_while_down_is_skipped_not_delivered_late(h, clock):
    made = create(h)
    clock.advance(60 * 5)  # hub was down for five hours
    assert h.gateway.agent_schedules.run_once() == 1
    assert posted(h) == []
    after = show(h, made["id"])
    assert after["runs"][0]["state"] == "skipped" and after["runs"][0]["code"] == "missed"
    assert after["next_due"] > times.stamp(worker.moment(clock()))
    assert h.gateway.agent_schedules.run_once() == 0


def test_two_workers_racing_deliver_once(h, clock):
    create(h)
    clock.advance(31)
    other = worker.ScheduleWorker(h.gateway, start=False, now=clock)
    with ThreadPoolExecutor(2) as pool:
        counts = list(pool.map(lambda w: w.run_once(), [h.gateway.agent_schedules, other]))
    assert sorted(counts) == [0, 1]
    assert len(posted(h)) == 1


def test_pause_resume_and_cancel(h, clock):
    made = create(h)
    status, paused, _ = act(h, made["id"], "pause")
    assert status == 200 and paused["state"] == "paused" and paused["next_due"] is None
    assert run(h, clock, 120) == 0 and posted(h) == []  # nothing while paused, no catch-up
    status, resumed, _ = act(h, made["id"], "resume")
    assert resumed["state"] == "active" and resumed["next_due"] > times.stamp(
        worker.moment(clock())
    )
    assert run(h, clock) == 0  # resuming does not fire the hours that passed
    clock.t = times.parse_stamp(resumed["next_due"]).timestamp() + 60
    assert h.gateway.agent_schedules.run_once() == 1 and len(posted(h)) == 1
    status, cancelled, _ = act(h, made["id"], "cancel")
    assert status == 200 and cancelled["state"] == "cancelled" and cancelled["next_due"] is None
    assert run(h, clock, 120) == 0 and len(posted(h)) == 1
    assert act(h, made["id"], "resume")[1]["state"] == "cancelled"  # cancelling is final
    status, listing, _ = h.request(h.owner, "GET", "/api/agent-schedules")
    assert status == 200 and listing["schedules"] == []


def test_listing_shows_the_schedule_and_its_limits(h, clock):
    made = create(h, schedule={"cron": "0 9 * * 1-5"}, timezone="Europe/Amsterdam")
    status, listing, _ = h.request(h.owner, "GET", "/api/agent-schedules")
    assert status == 200
    (row,) = listing["schedules"]
    assert row["id"] == made["id"] and row["schedule"] == {"cron": "0 9 * * 1-5"}
    assert row["timezone"] == "Europe/Amsterdam" and row["board"] == "work"
    assert listing["limits"] == {
        "max_schedules": 20,
        "min_interval_minutes": 15,
        "max_prompt_chars": 4000,
    }


def test_refusals_for_everyone_who_is_not_the_owner(h, clock):
    made = create(h)
    paths = [
        ("GET", "/api/agent-schedules", None),
        ("POST", "/api/agent-schedules", body(h)),
        ("GET", f"/api/agent-schedules/{made['id']}", None),
        ("POST", f"/api/agent-schedules/{made['id']}/pause", {}),
        ("POST", f"/api/agent-schedules/{made['id']}/cancel", {}),
    ]
    for who in (h.member, h.t1, h.tm):
        for method, path, payload in paths:
            status, result, _ = h.request(who, method, path, payload)
            assert status == 403, (who, method, path, status)
            assert "owner" in result["error"]
    for method, path, payload in paths:
        assert h.request(None, method, path, payload)[0] == 401
    assert show(h, made["id"])["state"] == "active"
    assert store.listing(h.db, h.workspace)[0]["state"] == "active"
    assert len(store.listing(h.db, h.workspace)) == 1


def test_another_workspace_owner_cannot_see_or_change_a_schedule(h, clock):
    made = create(h)
    for method, path in (
        ("GET", f"/api/agent-schedules/{made['id']}"),
        ("POST", f"/api/agent-schedules/{made['id']}/cancel"),
    ):
        status, _, _ = h.request(h.other, method, path, {} if method == "POST" else None)
        assert status == 404
    assert h.request(h.other, "GET", "/api/agent-schedules")[1]["schedules"] == []
    assert show(h, made["id"])["state"] == "active"


@pytest.mark.parametrize(
    "extra, status, code",
    [
        ({"schedule": {"every_minutes": 5}}, 400, "invalid"),
        ({"schedule": {"cron": "* * * * *"}}, 400, "invalid"),
        ({"schedule": {"every_minutes": 60, "cron": "0 * * * *"}}, 400, "invalid"),
        ({"timezone": "Nowhere/Land"}, 400, "invalid"),
        ({"prompt": ""}, 400, "invalid"),
        ({"prompt": "x" * 4001}, 400, "invalid"),
        ({"name": "  "}, 400, "invalid"),
        ({"surprise": 1}, 400, "invalid"),
        ({"agent": "no-such-agent"}, 404, "agent_not_found"),
        ({"board": "no-such-board"}, 404, "board_not_found"),
    ],
)
def test_invalid_creates_are_refused_and_store_nothing(h, clock, extra, status, code):
    got, result, _ = h.request(h.owner, "POST", "/api/agent-schedules", body(h, **extra))
    assert (got, result["code"]) == (status, code), result
    assert store.listing(h.db, h.workspace) == []


def test_only_the_boards_coordinator_can_be_scheduled(h, clock):
    status, result, _ = h.request(h.owner, "POST", "/api/agent-schedules", body(h, agent=h.a2))
    assert status == 409 and result["code"] == "not_coordinator"
    assert store.listing(h.db, h.workspace) == []


def test_per_workspace_limit_counts_live_schedules_only(h, clock):
    ids = [create(h, name=f"s{i}")["id"] for i in range(store.MAX_PER_WORKSPACE)]
    status, result, _ = h.request(h.owner, "POST", "/api/agent-schedules", body(h))
    assert status == 409 and result["code"] == "limit"
    act(h, ids[0], "cancel")
    create(h, name="fits again")
    assert len(store.listing(h.db, h.workspace)) == store.MAX_PER_WORKSPACE


def test_delivery_rechecks_the_coordinator_and_five_failures_pause(h, clock):
    made = create(h)
    h.set_coordinator("a2")  # the agent no longer coordinates the board
    for _ in range(store.AUTO_PAUSE_FAILURES):
        run(h, clock, 31)
    assert posted(h) == []
    after = show(h, made["id"])
    assert [r["code"] for r in after["runs"]] == ["not_coordinator"] * 5
    assert after["state"] == "paused" and after["paused_reason"] == "not_coordinator"
    assert run(h, clock, 31) == 0


def test_a_success_resets_the_failure_count(h, clock):
    made = create(h)
    h.set_coordinator("a2")
    for _ in range(2):
        run(h, clock, 31)
    h.set_coordinator("a1")
    run(h, clock, 31)
    with h.db._lock:
        failures = h.db._conn.execute(
            "SELECT failures FROM agent_schedules WHERE id=?", (made["id"],)
        ).fetchone()[0]
    assert failures == 0 and len(posted(h)) == 1


def test_delivery_stops_when_the_creator_is_no_longer_an_owner_or_agent_is_revoked(h, clock):
    made = create(h)
    with h.db._lock, h.db._tx() as conn:
        conn.execute("UPDATE memberships SET role='member' WHERE user_id=?", (h.admin,))
    run(h, clock, 31)
    assert posted(h) == [] and store.get(h.db, h.workspace, made["id"])["last_code"] == "owner_gone"
    with h.db._lock, h.db._tx() as conn:
        conn.execute("UPDATE memberships SET role='owner' WHERE user_id=?", (h.admin,))
        conn.execute("UPDATE connections SET revoked_at='2026-11-11T00:00:00Z' WHERE id=?", (h.a1,))
    run(h, clock, 31)
    assert posted(h) == [] and store.get(h.db, h.workspace, made["id"])["last_code"] == "agent_gone"


def test_view_rendering_in_the_app_module():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = Path(__file__).with_name("agent_schedules_view.mjs")
    result = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr
