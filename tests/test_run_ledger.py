"""Run ledger: a card start writes one durable row; the machine reports its end and usage."""

import pytest

from commonplace.runner import ledger
from mempalace.sqlite_crypto import connect, row_factory
from tests.runner_harness import (
    attest,
    card,
    cli_token,
    enroll as harness_enroll,
    http,
    owners,  # noqa: F401
    runner,
    runner_config,
    signup_hub,  # noqa: F401
)

UNKNOWN = dict(
    ended_at=None,
    log_ref=None,
    input_tokens=None,
    output_tokens=None,
    cost_micros=None,
    currency=None,
    reported_at=None,
)


def _machine(hub, owner, **grant):
    for wing in grant.get("wings", ["shared"]):
        status, body = http(
            hub.port,
            "POST",
            "/api/memory",
            cookie=owner["cookie"],
            body={"text": "Synthetic workspace example.", "theme": wing, "topic": "demo"},
        )
        assert status == 201, (status, body)
    machine = harness_enroll(hub, owner, **grant)
    attest(hub, owner, machine)
    return machine


def _queue(hub, owner, machine, *, token=None):
    task = card(hub, owner)["number"]
    status, body = http(
        hub.port,
        "POST",
        "/api/card-starts",
        token=token,
        cookie=None if token else owner["cookie"],
        body=dict(task=task, machine=machine["machine_id"], project="demo", topic="worker"),
    )
    assert status == 201, (status, body)
    return task, body["job_id"]


def _machine_call(hub, machine, action, body=None):
    return http(
        hub.port,
        "POST",
        "/api/runner/" + action,
        token=machine["machine_token"],
        body={} if body is None else body,
    )


def _started(hub, owner, machine, thread="thread-1"):
    """Queue a start and walk it to ``started`` the way a machine does."""
    task, job_id = _queue(hub, owner, machine)
    status, lease = _machine_call(hub, machine, "lease")
    assert status == 200 and lease["job_id"] == job_id, (status, lease)
    token = {"lease_token": lease["lease_token"]}
    assert _machine_call(hub, machine, "starting", token)[0] == 200
    status, body = _machine_call(
        hub, machine, "result", {**token, "outcome": "started", "thread_id": thread}
    )
    assert status == 200, (status, body)
    return task, job_id


def _usage(hub, machine, job_id, **fields):
    return _machine_call(hub, machine, "usage", {"job_id": job_id, **fields})


def _runs(hub, owner, task, **auth):
    status, body = http(
        hub.port, "GET", f"/api/card-starts?task={task}", **(auth or {"cookie": owner["cookie"]})
    )
    assert status == 200, (status, body)
    return body


def _run(hub, owner, task):
    rows = _runs(hub, owner, task)
    assert len(rows) == 1, rows
    return rows[0]["run"]


def _db(hub, owner):
    conn = connect(
        str(hub.gateway.workspace_dir(owner["workspace"])) + "/runner.sqlite3",
        isolation_level=None,
    )
    conn.row_factory = row_factory
    return conn


def test_a_start_writes_one_ledger_row_that_follows_the_run(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    task, job_id = _queue(hub, owner, machine)
    run = _run(hub, owner, task)
    # Created with the start: the state so far, and nothing invented about usage.
    assert run == {
        "status": "queued",
        "started_at": None,
        "model_provider": None,
        "model": None,
        **UNKNOWN,
    }
    conn = _db(hub, owner)
    try:
        rows = conn.execute("SELECT * FROM run_ledger").fetchall()
        assert len(rows) == 1
        stored = dict(rows[0])
        assert stored["job_id"] == job_id and stored["task_number"] == task
        assert stored["board_slug"] == "shared" and stored["project"] == "demo"
        assert stored["machine_id"] == machine["machine_id"]
        assert stored["agent"] == machine["worker_connection_id"]
        assert stored["requested_by"] == "user:" + owner["user_id"]
    finally:
        conn.close()
    status, lease = _machine_call(hub, machine, "lease")
    assert status == 200
    assert _run(hub, owner, task)["status"] == "leased"
    token = {"lease_token": lease["lease_token"]}
    assert _machine_call(hub, machine, "starting", token)[0] == 200
    assert _run(hub, owner, task)["status"] == "starting"
    assert (
        _machine_call(hub, machine, "result", {**token, "outcome": "started", "thread_id": "t-1"})[
            0
        ]
        == 200
    )
    run = _run(hub, owner, task)
    assert run["status"] == "started" and run["started_at"]
    assert {key: run[key] for key in UNKNOWN} == UNKNOWN


def test_cancelled_start_is_in_the_ledger(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    task, job_id = _queue(hub, owner, machine)
    status, body = http(
        hub.port, "POST", f"/api/card-starts/{job_id}/cancel", cookie=owner["cookie"], body={}
    )
    assert status == 200, (status, body)
    run = _run(hub, owner, task)
    assert run["status"] == "cancelled" and run["started_at"] is None
    # A run that never started has no usage to report.
    status, body = _usage(hub, machine, job_id, input_tokens=1)
    assert (status, body) == (409, {"error": "run has not started"})


def test_machine_reports_usage_log_and_end(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    task, job_id = _started(hub, owner, machine)
    # A partial report: what was not reported stays unknown.
    status, body = _usage(hub, machine, job_id, input_tokens=1200)
    assert status == 200, (status, body)
    run = _run(hub, owner, task)
    assert run["status"] == "started" and run["input_tokens"] == 1200 and run["reported_at"]
    assert run["output_tokens"] is None and run["cost_micros"] is None and run["currency"] is None
    assert run["log_ref"] is None and run["ended_at"] is None
    # Totals replace earlier totals; zero is a real value.
    status, body = _usage(
        hub,
        machine,
        job_id,
        status="completed",
        log_ref="https://logs.example.test/runs/1",
        input_tokens=5000,
        output_tokens=0,
        cost_micros=420000,
        currency="USD",
    )
    assert status == 200, (status, body)
    assert body["job_id"] == job_id and body["status"] == "completed"
    run = _run(hub, owner, task)
    assert run["status"] == "completed" and run["ended_at"] and run["started_at"]
    assert (run["input_tokens"], run["output_tokens"]) == (5000, 0)
    assert (run["cost_micros"], run["currency"]) == (420000, "USD")
    assert run["log_ref"] == "https://logs.example.test/runs/1"
    ended = run["ended_at"]
    # The end is reported once: the same status again is fine, another one is refused.
    assert _usage(hub, machine, job_id, status="completed")[0] == 200
    status, body = _usage(hub, machine, job_id, status="errored")
    assert (status, body) == (409, {"error": "run already ended"})
    # Final totals may still arrive after the end.
    assert _usage(hub, machine, job_id, output_tokens=77)[0] == 200
    run = _run(hub, owner, task)
    assert run["status"] == "completed" and run["ended_at"] == ended
    assert run["output_tokens"] == 77 and run["input_tokens"] == 5000
    # The start job itself is untouched by reports.
    assert _runs(hub, owner, task)[0]["state"] == "started"


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"input_tokens": -1},
        {"input_tokens": 1.5},
        {"input_tokens": True},
        {"input_tokens": "12"},
        {"output_tokens": 10**15 + 1},
        {"cost_micros": 5},
        {"currency": "USD"},
        {"cost_micros": 5, "currency": "usd"},
        {"cost_micros": 5, "currency": "DOLLAR"},
        {"status": "started"},
        {"status": None},
        {"log_ref": ""},
        {"log_ref": "line\nbreak"},
        {"log_ref": "x" * 513},
        {"input_tokens": 1, "price": 3},
    ],
)
def test_invalid_reports_are_refused_and_change_nothing(signup_hub, owners, fields):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    task, job_id = _started(hub, owner, machine)
    before = _run(hub, owner, task)
    status, body = _usage(hub, machine, job_id, **fields)
    assert status == 400, (status, body)
    assert _run(hub, owner, task) == before


def test_only_the_runs_own_machine_reports(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    cid, token = cli_token(owner, "coordinator", profile="standard")
    other = _machine(hub, owner, label="second-machine", requesters=[cid])
    task, job_id = _started(hub, owner, machine)
    before = _run(hub, owner, task)
    body = {"job_id": job_id, "input_tokens": 9}
    # Another machine, the worker, a coordinator, the owner, nobody, another workspace's owner.
    callers = [
        dict(token=other["machine_token"]),
        dict(token=machine["worker_token"]),
        dict(token=token),
        dict(cookie=owner["cookie"]),
        dict(),
        dict(cookie=owners["B"]["cookie"]),
    ]
    for caller in callers:
        status, answer = http(hub.port, "POST", "/api/runner/usage", body=body, **caller)
        assert status in (401, 403, 404), (caller.keys(), status, answer)
    assert _usage(hub, machine, "job_0000000000000000", input_tokens=9)[0] == 404
    assert _run(hub, owner, task) == before
    # The requester of a start reads its ledger; a start it did not request stays hidden.
    mine, _ = _queue(hub, owner, other, token=token)
    assert _runs(hub, owner, mine, token=token)[0]["run"]["status"] == "queued"
    assert _runs(hub, owner, task, token=token) == []


def test_jobs_from_before_the_ledger_get_their_row(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    task, job_id = _started(hub, owner, machine)
    queued_task, queued_job = _queue(hub, owner, machine)
    conn = _db(hub, owner)
    try:
        conn.execute("DROP TABLE run_ledger")
    finally:
        conn.close()
    # Opening the store backfills: the state is carried over, usage and agent stay unknown.
    run = _run(hub, owner, task)
    assert run["status"] == "started" and run["started_at"]
    assert {key: run[key] for key in UNKNOWN} == UNKNOWN
    conn = _db(hub, owner)
    try:
        rows = {r["job_id"]: dict(r) for r in conn.execute("SELECT * FROM run_ledger")}
        assert rows.keys() == {job_id, queued_job}
        assert rows[job_id]["agent"] is None and rows[queued_job]["status"] == "queued"
        # A job written through a connection that never opened the store still gets its row.
        conn.execute("DROP TABLE run_ledger")
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT * FROM runner_jobs WHERE id=?", (queued_job,)).fetchone()
        ledger.sync(conn, job)
        conn.execute("COMMIT")
        assert [r["job_id"] for r in conn.execute("SELECT job_id FROM run_ledger")] == [queued_job]
    finally:
        conn.close()
    assert _usage(hub, machine, job_id, input_tokens=3)[0] == 200
    assert _run(hub, owner, task)["input_tokens"] == 3
    assert _run(hub, owner, queued_task)["status"] == "queued"


def test_runner_report_command(signup_hub, owners, tmp_path):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = _machine(hub, owner)
    task, job_id = _started(hub, owner, machine)
    path = runner_config(tmp_path, hub, machine["machine_token"])
    env = {"COMMONPLACE_RUNNER_TOKEN": machine["machine_token"]}
    args = ["report", "--config", str(path), "--job", job_id]
    result = runner(
        [
            *args,
            "--status",
            "errored",
            "--log-ref",
            "thread-1",
            "--input-tokens",
            "321",
            "--output-tokens",
            "45",
            "--cost-micros",
            "1500000",
            "--currency",
            "EUR",
        ],
        env,
    )
    assert result.returncode == 0, result.stderr
    assert machine["machine_token"] not in result.stdout + result.stderr
    run = _run(hub, owner, task)
    assert run["status"] == "errored" and run["log_ref"] == "thread-1"
    assert (run["input_tokens"], run["output_tokens"]) == (321, 45)
    assert (run["cost_micros"], run["currency"]) == (1500000, "EUR")
    # Refused by the hub (the end is already recorded), and refused locally before any call.
    refused = runner([*args, "--status", "completed"], env)
    assert refused.returncode == 5 and "report refused (409)" in refused.stderr
    for bad in (["--cost-micros", "5"], ["--input-tokens", "-4"], []):
        invalid = runner([*args, *bad], env)
        assert invalid.returncode == 4 and "config_invalid" in invalid.stderr, invalid.stderr
    assert runner(args + ["--input-tokens", "1"], {}).returncode == 4
    assert _run(hub, owner, task)["input_tokens"] == 321


def test_clean_accepts_only_whole_reports():
    assert ledger.clean({"job_id": "job_1", "input_tokens": 0}) == {
        "job_id": "job_1",
        "input_tokens": 0,
    }
    for body in (None, [], {"input_tokens": 1}, {"job_id": 7, "input_tokens": 1}, {"job_id": ""}):
        with pytest.raises(ValueError):
            ledger.clean(body)
