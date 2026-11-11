"""The REST contract the card panel's Runs section relies on, and who may see or change a card's runs."""

from tests.runner_harness import (
    _signup,
    attest,
    card,
    cli_token,
    http,
    member,
    owners as owners,
    signup_hub as signup_hub,
)
from tests.test_runner_start import _accepted, _enroll, _lease, _result, _start, _starting, _status

ROW_KEYS = {"job", "machine_label", "state", "thread_id", "outcome_code", "updated_at"}


def _uncertain(hub, owner):
    cid, token = cli_token(owner, "requester", profile="standard")
    machine = _enroll(hub, owner, requesters=[cid])
    attest(hub, owner, machine)
    task = card(hub, owner)
    job = _accepted(_start(hub, machine, task, token=token))
    leased = _lease(hub, machine)
    _starting(hub, machine, leased)
    _result(hub, machine, leased, "uncertain", code="spawn_ambiguous")
    return task, job, machine


def test_owner_rows_carry_what_the_panel_shows(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    task, job, _machine = _uncertain(hub, owner)
    [row] = _status(hub, task, owner=owner)
    assert set(row) == ROW_KEYS
    assert row["job"] == job["job_id"]
    assert (row["state"], row["outcome_code"], row["machine_label"]) == (
        "uncertain",
        "spawn_ambiguous",
        "example-machine",
    )
    assert row["thread_id"] is None and row["updated_at"]


def test_owner_resolves_with_the_body_the_panel_sends(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    task, job, _machine = _uncertain(hub, owner)
    path = f"/api/card-starts/{job['job_id']}/resolve"
    # The panel checks the thread id first; the server still refuses a malformed one without a 5xx.
    for bad in ("has space", "-x", "a" * 257):
        code, _body = http(
            hub.port,
            "POST",
            path,
            cookie=owner["cookie"],
            body={"outcome": "started", "thread_id": bad},
        )
        assert 400 <= code < 500, (bad, code)
    assert _status(hub, task, owner=owner)[0]["state"] == "uncertain"
    body = {"outcome": "started", "thread_id": "thread-42"}
    assert http(hub.port, "POST", path, cookie=owner["cookie"], body=body)[0] == 200
    [row] = _status(hub, task, owner=owner)
    assert (row["state"], row["thread_id"]) == ("started", "thread-42")


def test_resolving_a_settled_start_is_a_conflict_not_a_permission_error(signup_hub, owners):
    # The panel shows a 409 as "this already changed" and a 403 as "you may not"; a start that
    # an owner already resolved is the first kind.
    hub, owner = signup_hub, owners["A"]
    task, job, _machine = _uncertain(hub, owner)
    path = f"/api/card-starts/{job['job_id']}/resolve"
    first = {"outcome": "started", "thread_id": "thread-42"}
    assert http(hub.port, "POST", path, cookie=owner["cookie"], body=first)[0] == 200
    code, payload = http(hub.port, "POST", path, cookie=owner["cookie"], body={"outcome": "failed"})
    assert (code, payload) == (409, {"error": "no: start is not uncertain"})
    assert _status(hub, task, owner=owner)[0]["state"] == "started"


def test_member_sees_no_runs_and_cannot_cancel_or_resolve(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    task, job, _machine = _uncertain(hub, owner)
    cid, token = cli_token(owner, "second", profile="standard")
    queued_machine = _enroll(hub, owner, label="second-machine", requesters=[cid])
    attest(hub, owner, queued_machine)
    other = card(hub, owner)
    queued = _accepted(_start(hub, queued_machine, other, token=token))
    person = member(hub, owner, _signup(hub, "c@example.test", 2))
    for number in (task, other):
        code, body = http(
            hub.port,
            "GET",
            f"/api/card-starts?task={number['number']}",
            cookie=person["cookie"],
        )
        assert code == 403, (code, body)
        assert not isinstance(body, list)
    for job_id, body in ((queued["job_id"], {}), (job["job_id"], {})):
        code, _ = http(
            hub.port,
            "POST",
            f"/api/card-starts/{job_id}/cancel",
            cookie=person["cookie"],
            body=body,
        )
        assert code == 403, code
    code, _ = http(
        hub.port,
        "POST",
        f"/api/card-starts/{job['job_id']}/resolve",
        cookie=person["cookie"],
        body={"outcome": "failed"},
    )
    assert code == 403, code
    assert [r["state"] for r in _status(hub, task, owner=owner)] == ["uncertain"]
    assert [r["state"] for r in _status(hub, other, owner=owner)] == ["queued"]


def test_another_workspace_sees_no_runs_and_cannot_act(signup_hub, owners):
    hub, owner, stranger = signup_hub, owners["A"], owners["B"]
    task, job, _machine = _uncertain(hub, owner)
    code, body = http(
        hub.port, "GET", f"/api/card-starts?task={task['number']}", cookie=stranger["cookie"]
    )
    assert not (code == 200 and body), (code, body)
    for action, extra in (("cancel", {}), ("resolve", {"outcome": "failed"})):
        code, _ = http(
            hub.port,
            "POST",
            f"/api/card-starts/{job['job_id']}/{action}",
            cookie=stranger["cookie"],
            body=extra,
        )
        assert code in (403, 404), (action, code)
    assert [r["state"] for r in _status(hub, task, owner=owner)] == ["uncertain"]
