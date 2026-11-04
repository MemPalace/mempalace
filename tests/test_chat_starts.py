"""A13: runner status comes from the existing requester route."""

from tests._chat_helpers import chat_environment  # noqa: F401
from tests.runner_harness import (
    attest,
    cli_token,
    enroll,
    http,
    member,
    owners as owners,
    signup_hub as signup_hub,
)
from tests.test_runner_start import _ok, _tool


def test_status_lists_start_jobs_for_linked_cards(signup_hub, owners):
    h, owner = signup_hub, owners["A"]
    other_member = member(h, owner, owners["B"])
    response = http(
        h.port,
        "POST",
        "/api/memory",
        cookie=owner["cookie"],
        body={"text": "Synthetic chat workspace.", "theme": "shared", "topic": "demo"},
    )
    assert response[0] == 201
    cid, token = cli_token(owner, "orchestrator", profile="standard")
    machine = enroll(h, owner, requesters=[cid])
    attest(h, owner, machine)
    boards = http(h.port, "GET", "/api/boards", cookie=owner["cookie"])[1]["boards"]
    if not any(b["slug"] == "shared" for b in boards):
        assert (
            http(
                h.port,
                "POST",
                "/api/boards",
                cookie=owner["cookie"],
                body={"slug": "shared", "wing": "shared"},
            )[0]
            == 201
        )
    status, message = http(
        h.port,
        "POST",
        "/api/boards/shared/chat",
        cookie=owner["cookie"],
        body={"text": "Please fix the login bug"},
    )
    assert status == 201
    created = _ok(
        _tool(h, token, "task_add", board="shared", title="Login fix", message_id=message["id"])
    )
    import re

    number = int(re.search(r"#(\d+) added", created["content"][0]["text"])[1])
    status, job = http(
        h.port,
        "POST",
        "/api/card-starts",
        token=token,
        body={
            "task": number,
            "machine": machine["machine_id"],
            "project": "demo",
            "topic": "worker",
        },
    )
    assert status == 201
    status, body = http(h.port, "GET", "/api/chat/shared/status", cookie=owner["cookie"])
    assert status == 200  # RED
    assert len(body["starts"]) == 1
    row = body["starts"][0]
    assert set(row) == {
        "task",
        "job",
        "state",
        "machine_label",
        "thread_id",
        "outcome_code",
        "updated_at",
    }
    assert (row["task"], row["job"], row["state"]) == (number, job["job_id"], "queued")
    assert row["machine_label"] == "example-machine"
    status, body = http(h.port, "GET", "/api/chat/shared/status", cookie=other_member["cookie"])
    assert status == 200 and body["starts"] == []
