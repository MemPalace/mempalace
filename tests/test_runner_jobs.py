"""HTTP acceptance controls for machine fencing and launch job state."""

import json
from types import SimpleNamespace

import pytest

from mempalace.sqlite_crypto import connect
from tests.runner_harness import (
    attest,
    card,
    cli_token,
    enroll as harness_enroll,
    http,
    launches,
    owners,  # noqa: F401
    runner,
    runner_config,
    signup_hub,  # noqa: F401
)


def enroll(hub, owner, **grant):
    # Create the required existing wings through the ordinary public write path.
    for wing in grant.get("wings", ["shared"]):
        status, body = http(
            hub.port,
            "POST",
            "/api/memory",
            cookie=owner["cookie"],
            body={"text": "Synthetic workspace example.", "theme": wing, "topic": "demo"},
        )
        assert status == 201, (status, body)
    return harness_enroll(hub, owner, **grant)


def _setup(hub, owner, **grant):
    machine = enroll(hub, owner, **grant)
    attest(hub, owner, machine)
    return machine


def _start(hub, owner, machine, *, project="demo", token=None, task=None, **args):
    if task is None:
        task = card(hub, owner)["number"]
    status, body = http(
        hub.port,
        "POST",
        "/api/card-starts",
        token=token,
        cookie=None if token else owner["cookie"],
        body=dict(
            task=task, machine=machine["machine_id"], project=project, topic="worker", **args
        ),
    )
    assert status == 201, (status, body)
    return task, body


def _machine_call(hub, machine, action, body=None):
    return http(
        hub.port,
        "POST",
        "/api/runner/" + action,
        token=machine["machine_token"],
        body={} if body is None else body,
    )


def _lease(hub, machine):
    status, body = _machine_call(hub, machine, "lease")
    assert status == 200, (status, body)
    return body


def _starting(hub, machine, job):
    status, body = _machine_call(hub, machine, "starting", {"lease_token": job["lease_token"]})
    assert status == 200, (status, body)
    return body


def _result(hub, machine, job, outcome="started", **fields):
    status, body = _machine_call(
        hub, machine, "result", dict(lease_token=job["lease_token"], outcome=outcome, **fields)
    )
    assert status == 200, (status, body)
    return body


def _status(hub, owner, task):
    status, body = http(hub.port, "GET", f"/api/card-starts?task={task}", cookie=owner["cookie"])
    assert status == 200, (status, body)
    rows = body["jobs"] if isinstance(body, dict) else body
    assert rows, body
    return rows[-1]


def _cancel(hub, owner, job, *, token=None):
    return http(
        hub.port,
        "POST",
        f"/api/card-starts/{job}/cancel",
        body={},
        token=token,
        cookie=None if token else owner["cookie"],
    )


def _patch(hub, owner, machine, **fields):
    status, body = http(
        hub.port,
        "PATCH",
        f"/api/runners/{machine['machine_id']}",
        cookie=owner["cookie"],
        body=fields,
    )
    assert status == 200, (status, body)
    return body


def _later(seconds):
    from datetime import datetime, timedelta

    from commonplace.runner import jobs

    return (
        datetime.strptime(jobs._now(), "%Y-%m-%dT%H:%M:%SZ") + timedelta(seconds=seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mark_working(hub, owner, machine, task):
    worker = hub.db.connection(machine["worker_connection_id"], owner["workspace"])["name"]
    conn = connect(str(hub.gateway.workspace_dir(owner["workspace"])) + "/boards.sqlite3")
    try:
        conn.execute(
            "UPDATE tasks SET status='doing',assignee=? WHERE id=?", ("conn:" + worker, task)
        )
        conn.commit()
    finally:
        conn.close()


def test_p1_machine_dispatch_default_deny(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    status, body = _machine_call(hub, machine, "lease")
    assert status == 204, (status, body)
    gateway = hub.gateway
    routes = set(gateway.routes)
    routes.update((method, prefix + "probe") for method, prefix, _handler in gateway.prefix_routes)
    routes.update((method, "/mcp") for method in ("GET", "POST"))
    for method, path in sorted(routes):
        if path.startswith("/api/runner/"):
            continue
        status, body = http(hub.port, method, path, token=machine["machine_token"], body={})
        assert status == 404, (method, path, status, body)
        assert body == {"error": "not found"}


def test_p2_runner_routes_are_machine_only(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    status, body = _machine_call(hub, machine, "lease")
    assert status == 204, (status, body)
    for auth in ({"token": machine["worker_token"]}, {"cookie": owner["cookie"]}):
        for method, path in (("POST", "/api/runner/lease"), ("GET", "/api/runner/jobs")):
            status, body = http(hub.port, method, path, body={}, **auth)
            assert status == (403 if "token" in auth else 404), (status, body)


def test_l1_stale_and_foreign_fences(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _setup(hub, owner)
    _start(hub, owner, machine)
    job = _lease(hub, machine)
    other = _setup(hub, owner, label="example-machine-2")
    for action in ("renew", "starting", "result"):
        extra = {"outcome": "failed", "code": "busy"} if action == "result" else {}
        for current, token in ((machine, "stale-fence"), (other, job["lease_token"])):
            status, body = _machine_call(hub, current, action, dict(lease_token=token, **extra))
            assert status in (404, 409), (status, body)
    status, body = _machine_call(hub, machine, "renew", {"lease_token": job["lease_token"]})
    assert status == 200, (status, body)
    assert body["lease_token"] is None
    from commonplace.runner import jobs

    store = jobs.open_store(hub.gateway, owner["workspace"])
    with store.transaction() as conn:
        stored = conn.execute(
            "SELECT lease_token FROM runner_jobs WHERE id=?", (job["job_id"],)
        ).fetchone()
    assert stored["lease_token"] == job["lease_token"]


def test_l2_expired_lease_is_fenced(signup_hub, owners, monkeypatch):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _setup(hub, owner)
    _start(hub, owner, machine)
    first = _lease(hub, machine)
    from commonplace.runner import jobs

    later = _later(61)
    monkeypatch.setattr(jobs, "_now", lambda: later)
    second = _lease(hub, machine)
    assert second["job_id"] == first["job_id"]
    assert second["lease_token"] != first["lease_token"]
    status, body = _machine_call(hub, machine, "starting", {"lease_token": first["lease_token"]})
    assert status == 409, (status, body)


def test_l3_start_expiry_is_uncertain(signup_hub, owners, monkeypatch):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _setup(hub, owner)
    task, _ = _start(hub, owner, machine)
    job = _lease(hub, machine)
    _starting(hub, machine, job)
    from commonplace.runner import jobs

    later = _later(121)
    monkeypatch.setattr(jobs, "_now", lambda: later)
    assert _status(hub, owner, task)["state"] == "uncertain"
    assert _machine_call(hub, machine, "lease")[0] == 204
    assert _status(hub, owner, task)["state"] == "uncertain"
    _result(hub, machine, job, thread_id="late-thread")
    assert _status(hub, owner, task)["state"] == "started"


def test_l4_concurrent_leases(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _setup(hub, owner)
    _start(hub, owner, machine)
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def acquire():
        barrier.wait(timeout=10)
        return _machine_call(hub, machine, "lease")[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(acquire) for _ in range(2)]
        assert sorted(f.result(timeout=15) for f in pending) == [200, 204]


@pytest.fixture
def started_capacity(tmp_path, monkeypatch):
    from threading import RLock

    from commonplace.projects import orchestration
    from commonplace.runner import jobs, registry

    monkeypatch.setattr(orchestration, "enabled", lambda db, ws: False)
    monkeypatch.setattr(jobs, "_sweep", lambda store: None)
    gw = SimpleNamespace(
        workspace_dir=lambda ws: str(tmp_path),
        db=SimpleNamespace(_lock=RLock(), connection=lambda ref, ws: {"name": "worker"}),
    )
    store = jobs.JobStore(gw, "ws")
    boards = connect(str(tmp_path / "boards.sqlite3"), isolation_level=None)
    boards.executescript(
        "CREATE TABLE boards (id INTEGER PRIMARY KEY, slug TEXT);"
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, board_id INTEGER, status TEXT, assignee TEXT);"
        "INSERT INTO boards VALUES (1, 'work');"
        "INSERT INTO tasks VALUES (1, 1, 'doing', 'conn:worker');"
    )
    machine = {
        "id": "m1",
        "worker_connection_id": "worker-id",
        "binding_state": "owner_attested",
        "max_in_flight": 1,
        "max_starts_per_hour": 30,
        "grant_version": 1,
        "binding_epoch": 1,
        "enabled": 1,
        "revoked_at": None,
    }
    # Supply control authority as well as a caller snapshot. Mutating machine in
    # the tests changes this authoritative fixture too; lease must reread it.
    monkeypatch.setattr(registry, "get_machine", lambda db, ws, ref: dict(machine))
    for n in (1, 2):
        jobs.create(
            store,
            task_number=n,
            board_slug="work",
            machine=machine,
            project="p",
            topic="t",
            requested_by="user:u",
            person=True,
        )
    first = jobs.lease(store, machine)
    assert first is not None
    with store.transaction() as conn:
        conn.execute(
            "UPDATE runner_jobs SET state='started', started_at=? WHERE id=?",
            (jobs._after(jobs._now(), -3601), first["id"]),
        )
    try:
        yield store, machine, first, boards
    finally:
        boards.close()


@pytest.mark.parametrize("max_in_flight", [1, 2])
@pytest.mark.parametrize("terminal_state", ["failed", "cancelled"])
def test_started_job_keeps_counting_after_an_hour(started_capacity, max_in_flight, terminal_state):
    from commonplace.runner import jobs

    store, machine, first, _ = started_capacity
    machine["max_in_flight"] = max_in_flight
    assert jobs.lease(store, machine) is None
    with store.transaction() as conn:
        conn.execute("UPDATE runner_jobs SET state=? WHERE id=?", (terminal_state, first["id"]))
    second = jobs.lease(store, machine)
    assert second is not None
    assert second["task_number"] == 2


@pytest.mark.parametrize("age", [1, 3601])
@pytest.mark.parametrize(
    ("state", "assignee"),
    [
        ("done", "conn:worker"),
        ("review", "conn:worker"),
        ("todo", "conn:worker"),
        ("backlog", "conn:worker"),
        ("doing", "conn:other"),
        ("doing", None),
        ("doing", "user:owner"),
    ],
)
def test_started_job_releases_capacity_when_card_stops_working(
    started_capacity, age, state, assignee
):
    from commonplace.runner import jobs

    store, machine, first, boards = started_capacity
    with store.transaction() as conn:
        conn.execute(
            "UPDATE runner_jobs SET started_at=? WHERE id=?",
            (jobs._after(jobs._now(), -age), first["id"]),
        )
    assert jobs.lease(store, machine) is None
    boards.execute("UPDATE tasks SET status=?,assignee=? WHERE id=1", (state, assignee))
    second = jobs.lease(store, machine)
    assert second is not None
    assert second["task_number"] == 2


def test_started_capacity_uses_read_only_boards(started_capacity, monkeypatch):
    from commonplace.boards.service import BoardService
    from commonplace.boards.store import BoardStore
    from commonplace.runner import jobs

    store, machine, _, boards = started_capacity

    def forbidden(*args, **kwargs):
        raise AssertionError("lease must not construct boards")

    monkeypatch.setattr(BoardService, "__init__", forbidden)
    monkeypatch.setattr(BoardStore, "__init__", forbidden)
    # A separate RESERVED boards write lock permits readers, not schema writes.
    boards.execute("BEGIN IMMEDIATE")
    try:
        assert jobs.lease(store, machine) is None
    finally:
        boards.rollback()


def test_l5_machine_capacity_hold_and_uncertain(signup_hub, owners, monkeypatch):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _setup(hub, owner, projects=["demo", "demo2"], max_in_flight=1)
    first_task, _ = _start(hub, owner, machine)
    second_task, _ = _start(hub, owner, machine, project="demo2")
    first = _lease(hub, machine)
    from commonplace.runner import jobs

    fixed = jobs._now()
    monkeypatch.setattr(jobs, "_now", lambda: fixed)
    assert _machine_call(hub, machine, "lease")[0] == 204
    assert _status(hub, owner, second_task)["state"] == "queued"
    _starting(hub, machine, first)
    _result(hub, machine, first, thread_id="thread-one")
    _mark_working(hub, owner, machine, first_task)
    assert _machine_call(hub, machine, "lease")[0] == 204
    before = _later(3599)
    after = _later(3601)
    monkeypatch.setattr(jobs, "_now", lambda: before)
    assert _machine_call(hub, machine, "lease")[0] == 204
    monkeypatch.setattr(jobs, "_now", lambda: after)
    assert _machine_call(hub, machine, "lease")[0] == 204
    store = jobs.open_store(hub.gateway, owner["workspace"])
    with store.transaction() as conn:
        conn.execute("UPDATE runner_jobs SET state='failed' WHERE id=?", (first["job_id"],))
    second = _lease(hub, machine)
    assert second["task_number"] == second_task
    _starting(hub, machine, second)
    expired = _later(121)
    monkeypatch.setattr(jobs, "_now", lambda: expired)
    assert _status(hub, owner, second_task)["state"] == "uncertain"
    third_task, _ = _start(hub, owner, machine)
    assert _machine_call(hub, machine, "lease")[0] == 204
    status, body = http(
        hub.port,
        "POST",
        f"/api/card-starts/{second['job_id']}/resolve",
        cookie=owner["cookie"],
        body={"outcome": "failed"},
    )
    assert status == 200, (status, body)
    _patch(hub, owner, machine, max_in_flight=2)
    fourth_task, _ = _start(hub, owner, machine, project="demo2")
    assert _lease(hub, machine)["task_number"] == third_task
    assert _lease(hub, machine)["task_number"] == fourth_task
    assert _machine_call(hub, machine, "lease")[0] == 204


def test_l6_capacity_is_exclusive_per_project(signup_hub, owners, monkeypatch):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _setup(hub, owner, projects=["demo", "demo2"], max_in_flight=2)
    first_task, _ = _start(hub, owner, machine)
    second_task, _ = _start(hub, owner, machine)
    third_task, _ = _start(hub, owner, machine, project="demo2")
    first = _lease(hub, machine)
    from commonplace.runner import jobs

    fixed = jobs._now()
    monkeypatch.setattr(jobs, "_now", lambda: fixed)
    assert first["task_number"] == first_task
    third = _lease(hub, machine)
    assert third["task_number"] == third_task
    _starting(hub, machine, third)
    _result(hub, machine, third, thread_id="thread-other-project")
    _mark_working(hub, owner, machine, third_task)
    assert _machine_call(hub, machine, "lease")[0] == 204
    _starting(hub, machine, first)
    _result(hub, machine, first, thread_id="thread-demo")
    _mark_working(hub, owner, machine, first_task)
    before = _later(3599)
    after = _later(3601)
    monkeypatch.setattr(jobs, "_now", lambda: before)
    assert _machine_call(hub, machine, "lease")[0] == 204
    monkeypatch.setattr(jobs, "_now", lambda: after)
    assert _machine_call(hub, machine, "lease")[0] == 204
    store = jobs.open_store(hub.gateway, owner["workspace"])
    with store.transaction() as conn:
        conn.execute("UPDATE runner_jobs SET state='failed' WHERE id=?", (first["job_id"],))
    assert _lease(hub, machine)["task_number"] == second_task


@pytest.mark.parametrize("leased", [False, True], ids=["queued", "leased"])
def test_c1_cancel_before_start(signup_hub, owners, tmp_path, leased):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _setup(hub, owner)
    task, job = _start(hub, owner, machine)
    if leased:
        job = _lease(hub, machine)
    status, body = _cancel(hub, owner, job["job_id"])
    assert status == 200, (status, body)
    assert _status(hub, owner, task)["state"] == "cancelled"
    status, body = _machine_call(
        hub, machine, "starting", {"lease_token": job.get("lease_token") or "no-lease"}
    )
    assert status == 409, (status, body)
    assert launches(tmp_path) == []


def test_c2_cancel_during_start_records_outcome(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _setup(hub, owner)
    task, _ = _start(hub, owner, machine)
    job = _lease(hub, machine)
    _starting(hub, machine, job)
    status, body = _cancel(hub, owner, job["job_id"])
    assert status == 200, (status, body)
    assert _status(hub, owner, task)["state"] == "starting"
    from commonplace.runner import jobs

    store = jobs.open_store(hub.gateway, owner["workspace"])
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM runner_jobs WHERE id=?", (job["job_id"],)).fetchone()
        assert row["cancel_requested_at"]
    _result(hub, machine, job, thread_id="started-after-cancel")
    state = _status(hub, owner, task)
    assert state["state"] == "started"
    assert state["thread_id"] == "started-after-cancel"


def test_c3_revoke_cancels_queue_and_preserves_ambiguity(signup_hub, owners, tmp_path):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _setup(hub, owner)
    started_task, _ = _start(hub, owner, machine)
    job = _lease(hub, machine)
    _starting(hub, machine, job)
    queued_task, _ = _start(hub, owner, machine)
    config = runner_config(tmp_path, hub, machine["machine_token"])
    status, body = http(
        hub.port, "DELETE", f"/api/runners/{machine['machine_id']}", cookie=owner["cookie"]
    )
    assert status == 200, (status, body)
    assert _machine_call(hub, machine, "lease")[0] == 401
    assert http(hub.port, "GET", "/api/me", token=machine["worker_token"])[0] == 401
    assert _status(hub, owner, started_task)["state"] == "uncertain"
    queued = _status(hub, owner, queued_task)
    assert queued["state"] == "cancelled"
    assert queued["outcome_code"] == "machine_revoked"
    result = runner(
        ["run", "--once", "--config", str(config)],
        {
            "COMMONPLACE_RUNNER_TOKEN": machine["machine_token"],
            "RUNNER_T3_GRANT": "synthetic-grant",
        },
    )
    assert result.returncode == 3
    assert launches(tmp_path) == []


def test_c5_uncertain_cannot_be_cancelled(signup_hub, owners, monkeypatch):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    connection, token = cli_token(owner, "coordinator", profile="standard")
    machine = _setup(hub, owner, requesters=[connection])
    task, _ = _start(hub, owner, machine, token=token)
    job = _lease(hub, machine)
    _starting(hub, machine, job)
    from commonplace.runner import jobs

    later = _later(121)
    monkeypatch.setattr(jobs, "_now", lambda: later)
    assert _status(hub, owner, task)["state"] == "uncertain"
    message = "start outcome is uncertain; a workspace owner must resolve it"
    for caller in (token, None):
        status, body = _cancel(hub, owner, job["job_id"], token=caller)
        assert status == 409, (status, body)
        assert body["error"] == "no: " + message
        assert _status(hub, owner, task)["state"] == "uncertain"
    response = hub.rpc(
        token, "tools/call", {"name": "card_start_cancel", "arguments": {"job": job["job_id"]}}
    )
    assert message in json.dumps(response)
    assert _status(hub, owner, task)["state"] == "uncertain"
    status, body = http(
        hub.port,
        "POST",
        "/api/card-starts",
        cookie=owner["cookie"],
        body={
            "task": task,
            "machine": machine["machine_id"],
            "project": "demo",
            "topic": "worker",
            "request_key": "again",
        },
    )
    assert status == 409, (status, body)
    assert body["error"] == "no: card already has an active start"
    status, body = http(
        hub.port,
        "POST",
        f"/api/card-starts/{job['job_id']}/resolve",
        cookie=owner["cookie"],
        body={"outcome": "failed"},
    )
    assert status == 200, (status, body)
    _start(hub, owner, machine, task=task, request_key="after-resolve")
    status, body = _cancel(hub, owner, job["job_id"])
    assert status == 409, (status, body)
    assert body["error"] == "no: start is already finished"


def test_b6_binding_reset_cancels_queue(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _setup(hub, owner)
    task, _ = _start(hub, owner, machine)
    assert _status(hub, owner, task)["state"] == "queued"
    _patch(hub, owner, machine, topics=["worker", "helper"])
    state = _status(hub, owner, task)
    assert state["state"] == "cancelled"
    assert state["outcome_code"] == "binding_not_attested"
    attest(hub, owner, machine)
    _start(hub, owner, machine, task=task)


def test_starting_retry_keeps_job_and_window(signup_hub, owners, monkeypatch):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _setup(hub, owner)
    task, _ = _start(hub, owner, machine)
    job = _lease(hub, machine)
    first = _starting(hub, machine, job)
    from commonplace.runner import jobs

    later = _later(60)
    monkeypatch.setattr(jobs, "_now", lambda: later)
    assert _starting(hub, machine, job) == first
    later = _later(61)
    monkeypatch.setattr(jobs, "_now", lambda: later)
    assert _status(hub, owner, task)["state"] == "uncertain"
    status, body = _machine_call(hub, machine, "starting", {"lease_token": job["lease_token"]})
    assert status == 409, (status, body)
