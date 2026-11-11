"""A machine's worker may start and work cards on every wing in its grant, and no other."""

import pytest

from commonplace.runner import registry
from tests.runner_harness import (
    attest,
    card,
    cli_token,
    enroll,
    http,
    member,
    owners as owners,
    signup_hub as signup_hub,
)

GRANTED = ["shared", "team"]


@pytest.fixture
def owner(signup_hub, owners):
    owner = owners["A"]
    # Enrollment needs readable wings with content; the signup workspace supplies
    # shared and inbox, so add a third wing the machine will not be granted.
    for wing in ("shared", "team", "other"):
        status, body = http(
            signup_hub.port,
            "POST",
            "/api/memory/",
            cookie=owner["cookie"],
            body={"text": "Synthetic workspace example.", "theme": wing},
        )
        assert status == 201, (status, body)
    return owner


def _ready(hub, owner, wings=GRANTED):
    cid, token = cli_token(owner, "requester", profile="standard")
    machine = enroll(hub, owner, requesters=[cid], wings=wings)
    attest(hub, owner, machine)
    return token, machine


def _start(hub, owner, machine, task):
    return http(
        hub.port,
        "POST",
        "/api/card-starts",
        cookie=owner["cookie"],
        body={
            "task": task["number"],
            "machine": machine["machine_id"],
            "project": "demo",
            "topic": "worker",
        },
    )


def _call(hub, token, name, **arguments):
    status, body = http(
        hub.port,
        "POST",
        "/mcp",
        token=token,
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert status == 200, (status, body)
    return body


def _allowed(body):
    return "error" not in body and not body["result"].get("isError")


def _release(hub, owner, machine, task, token):
    """Finish the first card's job so the next start is not held back by the project cap."""
    status, body = http(
        hub.port,
        "DELETE",
        f"/api/card-starts/{task['job_id']}",
        cookie=owner["cookie"],
    )
    assert status in (200, 409), (status, body)


def test_two_granted_wings_start_cards_on_both_boards(signup_hub, owner):
    _token, machine = _ready(signup_hub, owner)
    policy = signup_hub.db.authenticate_connection(machine["worker_connection_id"]).policy
    assert policy.can_write_wing("shared") and policy.can_write_wing("team")
    for board in ("shared", "team"):
        task = card(signup_hub, owner, board=board)
        status, body = _start(signup_hub, owner, machine, task)
        assert status == 201, (board, status, body)
        for name in ("task_show", "task_take_next"):
            assert _allowed(
                _call(signup_hub, machine["worker_token"], name, task=task["number"])
            ), (
                board,
                name,
            )
        # Free the project's single in-flight slot for the next board.
        status, body = http(
            signup_hub.port,
            "POST",
            f"/api/card-starts/{body['job_id']}/cancel",
            cookie=owner["cookie"],
            body={},
        )
        assert status in (200, 409), (status, body)


def test_card_on_an_ungranted_wing_is_refused_naming_both(signup_hub, owner):
    _token, machine = _ready(signup_hub, owner)
    task = card(signup_hub, owner, board="other")
    status, body = _start(signup_hub, owner, machine, task)
    assert status == 403, (status, body)
    message = body["error"]
    assert "outside_worker_wing" in message
    assert "board wing 'other'" in message
    assert "granted wings 'shared', 'team'" in message


def test_worker_cannot_write_outside_its_grant(signup_hub, owner):
    _token, machine = _ready(signup_hub, owner)
    policy = signup_hub.db.authenticate_connection(machine["worker_connection_id"]).policy
    assert set(policy.write_wings) == set(GRANTED)
    assert policy.write_wing in GRANTED
    for wing in ("other", "inbox", "private", "project_demo", "*", "sha*"):
        assert not policy.can_write_wing(wing), wing
    outside = card(signup_hub, owner, board="other")
    before = http(
        signup_hub.port, "GET", f"/api/cards/{outside['number']}", cookie=owner["cookie"]
    )[1]
    worker = machine["worker_token"]
    for name, args in (
        ("task_take_next", {"task": outside["number"]}),
        ("task_comment", {"task": outside["number"], "text": "nope"}),
        ("task_update", {"task": outside["number"], "status": "doing"}),
    ):
        assert not _allowed(_call(signup_hub, worker, name, **args)), name
    after = http(signup_hub.port, "GET", f"/api/cards/{outside['number']}", cookie=owner["cookie"])[
        1
    ]
    for key in ("status", "assignee", "version"):
        assert after[key] == before[key]


def test_patching_wings_rewrites_the_worker_policy(signup_hub, owner):
    _token, machine = _ready(signup_hub, owner)
    path = f"/api/runners/{machine['machine_id']}"
    status, body = http(
        signup_hub.port, "PATCH", path, cookie=owner["cookie"], body={"wings": ["team", "other"]}
    )
    assert status == 200 and body["binding_state"] == "unbound", (status, body)
    policy = signup_hub.db.authenticate_connection(machine["worker_connection_id"]).policy
    assert policy.can_write_wing("other") and policy.can_write_wing("team")
    assert not policy.can_write_wing("shared")


def test_worker_policy_helper_keeps_the_first_wing_as_default():
    policy = registry._worker_policy(["alpha", "beta"])
    assert policy.write_wing == "alpha"
    assert set(policy.write_wings) == {"alpha", "beta"}
    assert not policy.can_write_wing("gamma")


@pytest.mark.parametrize("caller", ["trusted", "worker", "member"])
def test_listing_is_owner_only(signup_hub, owners, owner, caller):
    machine = enroll(signup_hub, owner)
    if caller == "member":
        auth = {"cookie": member(signup_hub, owner, owners["B"])["cookie"]}
    elif caller == "worker":
        auth = {"token": machine["worker_token"]}
    else:
        _cid, token = cli_token(owner, "lister", profile="standard")
        auth = {"token": token}
    status, body = http(signup_hub.port, "GET", "/api/runners", **auth)
    assert status == 403, (status, body)
