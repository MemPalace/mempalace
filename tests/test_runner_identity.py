"""Runner identity boundaries through ordinary HTTP and CLI entry points."""

import subprocess
import sys

import pytest

from tests.runner_harness import (
    card,
    cli_token,
    enroll,
    http,
    launches,
    owners,  # noqa: F401
    runner,
    runner_config,
    signup_hub,  # noqa: F401
)


@pytest.fixture
def identity_owner(signup_hub, owners):  # noqa: F811
    owner = owners["A"]
    for wing in ("shared", "team"):
        status, body = http(
            signup_hub.port,
            "POST",
            "/api/memory/",
            cookie=owner["cookie"],
            body={"text": "Demo workspace memory", "theme": wing},
        )
        assert status == 201, (status, body)
    return owner


def _machines(hub, owner):
    status, body = http(hub.port, "GET", "/api/runners", cookie=owner["cookie"])
    assert status == 200, (status, body)
    return body["machines"] if isinstance(body, dict) else body


def _show(hub, owner, number):
    status, body = http(hub.port, "GET", f"/api/cards/{number}", cookie=owner["cookie"])
    assert status == 200, (status, body)
    return body


def _take(hub, token, **arguments):
    status, body = http(
        hub.port,
        "POST",
        "/mcp",
        token=token,
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "task_take_next", "arguments": arguments},
        },
    )
    assert status == 200, (status, body)
    if "error" in body:
        return False, body["error"]["message"]
    assert "result" in body, body
    result = body["result"]
    return not result.get("isError", False), result["content"][0]["text"]


def test_w3_linked_connections_require_runner_management(signup_hub, identity_owner):  # noqa: F811
    hub, owner = signup_hub, identity_owner
    machine = enroll(hub, owner)
    machine_id = machine["machine_id"]
    message = "manage this runner under /api/runners"
    for kind in ("worker", "machine"):
        principal = hub.db.authenticate_token(machine[f"{kind}_token"])
        assert principal is not None
        assert principal.runner == kind and principal.role == "member" and not principal.person
        durable = hub.db.authenticate_connection(principal.connection_id)
        assert durable is not None
        assert durable.runner == kind and durable.role == "member"
        path = f"/api/connections/{principal.connection_id}"
        for method, suffix in (("DELETE", ""), ("POST", "/rotate")):
            status, body = http(hub.port, method, path + suffix, cookie=owner["cookie"], body={})
            assert status == 409, (status, body)
            assert body == {"error": message}
        status, body = http(
            hub.port,
            "POST",
            "/api/connections",
            cookie=owner["cookie"],
            body={"name": f"child-{kind}", "parent": principal.connection_id},
        )
        assert status == 400, (status, body)
        assert hub.db.authenticate_token(machine[f"{kind}_token"]) is not None
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "commonplace",
            "--data",
            str(owner["data_dir"]),
            "token",
            "revoke",
            f"runner-worker:{machine_id}",
            "--workspace",
            owner["workspace"],
            "--by",
            owner["user_id"],
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert message in result.stderr
    assert hub.db.authenticate_token(machine["worker_token"]) is not None
    rows = _machines(hub, owner)
    matches = [row for row in rows if row.get("machine_id", row.get("id")) == machine_id]
    assert len(matches) == 1
    assert matches[0]["enabled"] and not matches[0]["revoked_at"]


def test_w4_worker_cannot_claim_outside_its_write_wing(signup_hub, identity_owner):  # noqa: F811
    hub, owner = signup_hub, identity_owner
    machine = enroll(hub, owner, wings=["shared", "team"])
    created = card(hub, owner, board="team")
    number = created["number"]
    before = _show(hub, owner, number)
    assert before["status"] == "todo" and before["assignee"] is None
    for arguments in ({"task": number}, {"board": "team"}):
        ok, text = _take(hub, machine["worker_token"], **arguments)
        assert not ok, text
        assert "cannot change this board" in text
        after = _show(hub, owner, number)
        for key in ("status", "assignee", "claim_until", "version"):
            assert after[key] == before[key]
    cid, token = cli_token(owner, "coordinator", read_wings=["shared", "team"], write_wing="shared")
    principal = hub.db.authenticate_connection(cid)
    assert principal is not None
    assert principal.runner is None and principal.parent_connection_id is None
    ok, text = _take(hub, token, task=number)
    assert ok, text
    claimed = _show(hub, owner, number)
    assert claimed["status"] == "doing" and claimed["assignee"] == "conn:coordinator"


def test_t1_rotated_and_expired_machine_credentials_are_rejected(
    signup_hub,  # noqa: F811
    identity_owner,
    tmp_path,
    monkeypatch,
):
    hub, owner = signup_hub, identity_owner
    machine = enroll(hub, owner)
    status, body = http(
        hub.port, "POST", "/api/runner/lease", token=machine["machine_token"], body={}
    )
    assert status == 204, (status, body)
    status, body = http(
        hub.port,
        "POST",
        f"/api/runners/{machine['machine_id']}/rotate",
        cookie=owner["cookie"],
        body={"credential": "machine"},
    )
    assert status == 200
    status, body = http(
        hub.port, "POST", "/api/runner/lease", token=machine["machine_token"], body={}
    )
    assert status == 401, (status, body)
    config = runner_config(tmp_path / "rotated", hub, machine["machine_token"])
    result = runner(
        ["run", "--config", str(config), "--once"],
        {"COMMONPLACE_RUNNER_TOKEN": machine["machine_token"], "RUNNER_T3_GRANT": "demo-grant"},
    )
    assert result.returncode == 3
    assert launches(tmp_path / "rotated") == []

    fresh = enroll(hub, owner, label="another-machine")
    status, body = http(
        hub.port, "POST", "/api/runner/lease", token=fresh["machine_token"], body={}
    )
    assert status == 204, (status, body)
    from datetime import timedelta

    from commonplace import control_db

    future = control_db._now() + timedelta(days=91)
    monkeypatch.setattr(control_db, "_now", lambda: future)
    status, body = http(
        hub.port, "POST", "/api/runner/lease", token=fresh["machine_token"], body={}
    )
    assert status == 401, (status, body)
    status, body = http(hub.port, "GET", "/api/cards", token=fresh["worker_token"])
    assert status == 401, (status, body)
    config = runner_config(tmp_path / "expired", hub, fresh["machine_token"])
    result = runner(
        ["run", "--config", str(config), "--once"],
        {"COMMONPLACE_RUNNER_TOKEN": fresh["machine_token"], "RUNNER_T3_GRANT": "demo-grant"},
    )
    assert result.returncode == 3
    assert launches(tmp_path / "expired") == []


@pytest.mark.parametrize("body", [{}, {"credential": "bogus"}], ids=["absent", "unknown"])
def test_rotate_requires_valid_credential(signup_hub, identity_owner, body):  # noqa: F811
    hub, owner = signup_hub, identity_owner
    machine = enroll(hub, owner)
    before = _machines(hub, owner)
    status, response = http(
        hub.port,
        "POST",
        f"/api/runners/{machine['machine_id']}/rotate",
        cookie=owner["cookie"],
        body=body,
    )
    assert (status, response) == (400, {"error": "credential must be machine or worker"})
    assert _machines(hub, owner) == before
    for kind in ("machine", "worker"):
        assert hub.db.authenticate_token(machine[f"{kind}_token"]) is not None
