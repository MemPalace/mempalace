"""Typed worker admission through the existing queue and starting routes."""

import pytest

from tests.runner_harness import (
    attest,
    card,
    cli_token,
    http,
    owners as owners,
    signup_hub as signup_hub,
)
from tests.test_runner_start import _accepted, _enroll, _lease, _start


@pytest.mark.parametrize("entry", ["queue", "starting"])
def test_worker_type_checked_at_runner_admission(signup_hub, owners, entry):
    hub, owner = signup_hub, owners["A"]
    cid, token = cli_token(owner, "requester", profile="standard")
    machine = _enroll(hub, owner, requesters=[cid])
    attest(hub, owner, machine)
    task = card(hub, owner, task_type="code")
    from commonplace.runner import registry

    stored = registry.get_machine(hub.db, owner["workspace"], machine["machine_id"])
    path = f"/api/task-types/connections/{stored['worker_connection_id']}"
    if entry == "queue":
        code, body = _start(hub, machine, task, token=token)
        assert (code, body) == (403, {"error": "no: card is not startable: task_type_not_allowed"})
    assert (
        http(hub.port, "POST", path, cookie=owner["cookie"], body={"allowed_types": ["code"]})[0]
        == 200
    )
    _accepted(_start(hub, machine, task, token=token))
    if entry == "starting":
        job = _lease(hub, machine)
        assert (
            http(hub.port, "POST", path, cookie=owner["cookie"], body={"allowed_types": []})[0]
            == 200
        )
        code, body = http(
            hub.port,
            "POST",
            "/api/runner/starting",
            token=machine["machine_token"],
            body={"lease_token": job["lease_token"]},
        )
        assert code == 409 and body["error"] == "card_not_startable", (code, body)
