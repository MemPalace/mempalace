"""Machine enrollment, owner authority and audited binding lifecycle."""

# ruff: noqa: F811

import json

import pytest

from tests.runner_harness import (
    attest,
    card,
    cli_token,
    enroll as harness_enroll,
    http,
    member,
    owners,  # noqa: F401
    signup_hub,  # noqa: F401
)

GRANT = {
    "label": "example-machine",
    "projects": ["demo"],
    "topics": ["worker"],
    "wings": ["shared"],
}


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


def listing(hub, owner):
    status, body = http(hub.port, "GET", "/api/runners", cookie=owner["cookie"])
    assert status == 200, (status, body)
    return body["machines"]


def machine_row(hub, owner, machine):
    matches = [row for row in listing(hub, owner) if row["id"] == machine["machine_id"]]
    assert len(matches) == 1, "machine missing from owner listing"
    return matches[0]


def edit(hub, owner, machine, method="PATCH", suffix="", body=None, **auth):
    if not auth:
        auth = {"cookie": owner["cookie"]}
    return http(
        hub.port, method, "/api/runners/" + machine["machine_id"] + suffix, body=body, **auth
    )


def test_E1_tokens_are_shown_once(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    assert machine["binding_state"] == "unbound"
    assert machine["machine_token"] != machine["worker_token"]
    assert hub.db.authenticate_token(machine["machine_token"]) is not None
    assert hub.db.authenticate_token(machine["worker_token"]) is not None
    row = machine_row(hub, owner, machine)
    assert row["binding_state"] == "unbound" and row["verified"] is False
    serialized = json.dumps(listing(hub, owner))
    assert machine["machine_token"] not in serialized
    assert machine["worker_token"] not in serialized
    assert not any("token" in key for key in row)


@pytest.mark.parametrize("caller", ["trusted", "untrusted", "worker", "member"])
def test_E2_management_requires_a_person_owner(signup_hub, owners, caller):
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    row = machine_row(hub, owner, machine)
    if caller == "member":
        auth = {"cookie": member(hub, owner, owners["B"])["cookie"]}
    elif caller == "worker":
        auth = {"token": machine["worker_token"]}
    else:
        _, token = cli_token(owner, "requester", profile="standard", trust=caller)
        auth = {"token": token}
    status, body = http(hub.port, "POST", "/api/runners", body=GRANT, **auth)
    assert status == 403, (status, body)
    for method, suffix, data in (
        ("PATCH", "", {"enabled": False}),
        (
            "POST",
            "/attest",
            {
                "topics": ["worker"],
                "provider_instance_id": "worker",
                "worker_connection_id": row["worker_connection_id"],
            },
        ),
        ("POST", "/rotate", {}),
        ("DELETE", "", None),
    ):
        status, body = edit(hub, owner, machine, method, suffix, data, **auth)
        assert status == 403, (status, body)
        assert body == {"error": "a signed-in workspace owner is required"}


@pytest.mark.parametrize(
    "method,suffix,data",
    [
        ("POST", None, GRANT),
        ("PATCH", "", {"enabled": False}),
        ("POST", "/attest", {}),
        ("POST", "/rotate", {}),
        ("DELETE", "", None),
    ],
)
def test_E3_cross_origin_writes_are_refused(signup_hub, owners, method, suffix, data):
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    path = "/api/runners" if suffix is None else "/api/runners/" + machine["machine_id"] + suffix
    status, body = http(
        hub.port,
        method,
        path,
        cookie=owner["cookie"],
        body=data,
        origin="https://foreign.example.test",
    )
    assert status == 403, (status, body)
    assert machine_row(hub, owner, machine)["binding_state"] == "unbound"


@pytest.mark.parametrize(
    "invalid",
    [
        "unknown",
        "missing-label",
        "missing-wings",
        "missing-projects",
        "missing-topics",
        "empty-topics",
        "private-wing",
        "pattern-wing",
        "unknown-wing",
        "id-label",
        "untrusted-requester",
        "oauth-requester",
        "runner-requester",
        "foreign-requester",
    ],
)
def test_E4_grants_are_closed_and_validated(signup_hub, owners, invalid):
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    data = {**GRANT, "label": "other-machine"}
    if invalid.startswith("missing-"):
        del data[invalid.removeprefix("missing-")]
    elif invalid == "unknown":
        data["command"] = "ignored"
    elif invalid == "empty-topics":
        data["topics"] = []
    elif invalid.endswith("-wing"):
        data["wings"] = [
            {"private-wing": "private.notes", "pattern-wing": "shared*", "unknown-wing": "missing"}[
                invalid
            ]
        ]
    elif invalid == "id-label":
        data["label"] = "mach_0123456789abcdef"
    elif invalid == "runner-requester":
        data["requesters"] = [machine_row(hub, owner, machine)["worker_connection_id"]]
    elif invalid == "oauth-requester":
        # The ordinary consent flow creates an OAuth connection, never a PAT.
        from tests.test_commonplace_oauth import Client

        client = Client(hub)
        name, value = owner["cookie"].split("=", 1)
        client.cookies[name] = value
        cid = client.register()
        code = client.consent(cid)
        status, _, text = client.exchange(cid, code)
        assert status == 200, "OAuth exchange failed"
        token = json.loads(text)["access_token"]
        principal = hub.gateway.authenticate("Bearer " + token)
        assert principal is not None and principal.auth == "oauth"
        data["requesters"] = [principal.connection_id]
    else:
        requester_owner = owners["B"] if invalid == "foreign-requester" else owner
        cid, _ = cli_token(
            requester_owner,
            "requester",
            trust="untrusted" if invalid == "untrusted-requester" else "trusted",
        )
        data["requesters"] = [cid]
    status, body = http(hub.port, "POST", "/api/runners", cookie=owner["cookie"], body=data)
    assert status == 400, (status, body)
    if invalid == "id-label":
        assert body == {"error": "label must not look like a machine id"}
        status, body = edit(hub, owner, machine, body={"label": data["label"]})
        assert status == 400, (status, body)
        assert body == {"error": "label must not look like a machine id"}


def test_E5_second_token_failure_rolls_back_the_whole_enrollment(signup_hub, owners, monkeypatch):
    hub = signup_hub
    enroll(hub, owners["A"])
    from commonplace.control_db import ControlError

    original = hub.db.create_token
    calls = 0

    def fail_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ControlError("synthetic token failure")
        return original(**kwargs)

    monkeypatch.setattr(hub.db, "create_token", fail_second)
    status, body = http(hub.port, "POST", "/api/runners", cookie=owners["B"]["cookie"], body=GRANT)
    assert status == 400, (status, body)
    assert calls == 2
    ws = owners["B"]["workspace"]
    with hub.db._lock:
        assert (
            hub.db._conn.execute(
                "SELECT count(*) FROM runner_machines WHERE workspace_id=?", (ws,)
            ).fetchone()[0]
            == 0
        )
        assert (
            hub.db._conn.execute(
                "SELECT count(*) FROM runner_connections r JOIN connections c ON c.id=r.connection_id WHERE c.workspace_id=?",
                (ws,),
            ).fetchone()[0]
            == 0
        )
        assert (
            hub.db._conn.execute(
                "SELECT count(*) FROM connections WHERE workspace_id=? AND name LIKE 'runner-%'",
                (ws,),
            ).fetchone()[0]
            == 0
        )
        assert (
            hub.db._conn.execute(
                "SELECT count(*) FROM audit_log WHERE workspace_id=? AND (action LIKE 'runner.%' OR action LIKE 'token.%')",
                (ws,),
            ).fetchone()[0]
            == 0
        )


def audit_counts(hub, owner):
    with hub.db._lock:
        return dict(
            hub.db._conn.execute(
                "SELECT action, count(*) FROM audit_log WHERE workspace_id=? AND (action LIKE 'runner.%' OR action LIKE 'token.%') GROUP BY action",
                (owner["workspace"],),
            ).fetchall()
        )


def test_AU1_owner_acts_have_one_audit_row_and_job_grant_version(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    assert audit_counts(hub, owner) == {"runner.enroll": 1, "token.create": 2}
    status, body = edit(hub, owner, machine, body={"max_in_flight": 2})
    assert status == 200, (status, body)
    attest(hub, owner, machine)
    task = card(hub, owner)
    status, body = http(
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
    assert status == 201, (status, body)
    import sqlite3
    from pathlib import Path

    with sqlite3.connect(
        Path(hub.supervisor.workspace_dir(owner["workspace"])) / "runner.sqlite3"
    ) as conn:
        versions = conn.execute(
            "SELECT grant_version FROM runner_jobs WHERE machine_id=?", (machine["machine_id"],)
        ).fetchall()
    assert versions == [(machine_row(hub, owner, machine)["grant_version"],)]
    status, body = edit(hub, owner, machine, "POST", "/rotate", {"credential": "worker"})
    assert status == 200, "rotation failed"
    status, body = edit(hub, owner, machine, "DELETE")
    assert status == 200, (status, body)
    assert audit_counts(hub, owner) == {
        "runner.enroll": 1,
        "runner.patch": 1,
        "runner.attest": 1,
        "runner.rotate": 1,
        "runner.revoke": 1,
        "token.create": 2,
        "token.rotate": 1,
        "token.revoke": 2,
    }
    with hub.db._lock:
        text = json.dumps(
            [
                tuple(row)
                for row in hub.db._conn.execute(
                    "SELECT * FROM audit_log WHERE workspace_id=?", (owner["workspace"],)
                )
            ]
        )
    assert machine["machine_token"] not in text and machine["worker_token"] not in text


@pytest.mark.parametrize("bad", ["topics", "worker", "bearer"])
def test_B3_attestation_is_owner_asserted_and_unverified(signup_hub, owners, bad):
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    binding = attest(hub, owner, machine)
    assert binding == {
        "binding_state": "owner_attested",
        "verified": False,
        "note": "attested by the owner; not verified by Commonplace",
        "launch_shape": "uncaptured",
    }
    data = {
        "topics": ["worker"],
        "provider_instance_id": "worker",
        "worker_connection_id": machine_row(hub, owner, machine)["worker_connection_id"],
    }
    auth = {}
    if bad == "topics":
        data["topics"] = ["other"]
    elif bad == "worker":
        data["worker_connection_id"] = "conn_0000000000000000"
    else:
        _, token = cli_token(owner, "requester", profile="standard")
        auth = {"token": token}
    status, body = edit(hub, owner, machine, "POST", "/attest", data, **auth)
    assert status == (403 if bad == "bearer" else 400), (status, body)


@pytest.mark.parametrize("change", ["topics", "wings", "worker-rotate"])
def test_B4_binding_changes_reset_attestation(signup_hub, owners, change):
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    attest(hub, owner, machine)
    if change == "worker-rotate":
        status, body = edit(hub, owner, machine, "POST", "/rotate", {"credential": "worker"})
        assert status == 200, "rotation failed"
        assert hub.db.authenticate_token(machine["worker_token"]) is None
        assert hub.db.authenticate_token(body["worker_token"]) is not None
    else:
        if change == "wings":
            # Create a known public wing through the ordinary memory tool.
            token_id, token = cli_token(
                owner, "requester", profile="standard", read_wings="*", write_wing="notes"
            )
            assert token_id
            status, result = http(
                hub.port,
                "POST",
                "/api/memory",
                token=token,
                body={"text": "Synthetic workspace note", "theme": "notes"},
            )
            assert status == 201 and result is not None, "memory save failed"
        status, body = edit(
            hub, owner, machine, body={change: ["notes"] if change == "wings" else ["other"]}
        )
        assert status == 200, (status, body)
    row = machine_row(hub, owner, machine)
    assert row["binding_state"] == "unbound"
    assert all(
        row[key] is None
        for key in (
            "binding_topics",
            "binding_provider_instance_id",
            "binding_worker_connection_id",
            "binding_attested_by",
            "binding_attested_at",
            "binding_grant_version",
        )
    )


def test_B7_other_grant_edits_keep_binding_and_increment_version(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    attest(hub, owner, machine)
    cid, _ = cli_token(owner, "requester", profile="standard")
    before = machine_row(hub, owner, machine)
    for key, value in (
        ("max_in_flight", 2),
        ("max_runtime_mode", "auto"),
        ("max_starts_per_hour", 9),
        ("requesters", [cid]),
        ("projects", ["demo", "other"]),
        ("enabled", False),
    ):
        status, body = edit(hub, owner, machine, body={key: value})
        assert status == 200, (status, body)
        after = machine_row(hub, owner, machine)
        assert after["binding_state"] == "owner_attested"
        assert after["grant_version"] == before["grant_version"] + 1
        assert after["binding_grant_version"] == 1
        before = after


def test_T2_labels_are_unique_until_revocation(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    status, body = http(hub.port, "POST", "/api/runners", cookie=owner["cookie"], body=GRANT)
    assert status == 409, (status, body)
    other = enroll(hub, owner, label="other-machine")
    status, body = edit(hub, owner, other, body={"label": GRANT["label"]})
    assert status == 409, (status, body)
    status, body = edit(hub, owner, machine, "DELETE")
    assert status == 200, (status, body)
    replacement = enroll(hub, owner)
    assert replacement["machine_id"] != machine["machine_id"]
    assert hub.db.authenticate_token(machine["machine_token"]) is None
    assert hub.db.authenticate_token(machine["worker_token"]) is None
