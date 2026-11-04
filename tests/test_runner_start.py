"""Requester boundaries through public HTTP and ordinary CLI-created identities."""

import pytest

from tests.runner_harness import (
    attest,
    card,
    cli_token,
    enroll,
    http,
    launches,
    owners as owners,
    signup_hub as signup_hub,
)

START_TOOLS = {"card_start", "card_start_cancel", "card_start_resolve", "card_start_status"}
JOB_KEYS = {
    "job_id",
    "lease_token",
    "lease_until",
    "task_number",
    "board_slug",
    "project",
    "topic",
    "runtime_mode",
    "title_marker",
}


def _enroll(hub, owner, **grant):
    # Enrollment requires existing wings, so create synthetic content through HTTP.
    for wing in grant.get("wings", ["shared"]):
        response = http(
            hub.port,
            "POST",
            "/api/memory",
            cookie=owner["cookie"],
            body={"text": "Synthetic workspace example.", "theme": wing, "topic": "demo"},
        )
        assert response[0] == 201, response
    return enroll(hub, owner, **grant)


def _untrusted(hub, owner, name):
    code, body = http(
        hub.port,
        "POST",
        "/api/connections",
        cookie=owner["cookie"],
        body={"name": name, "read_wings": ["shared"]},
    )
    assert code == 201, (code, body)
    assert "connection" in body and "token" in body, "connection credentials missing"
    return body["connection"]["id"], body["token"]


def _ready(hub, owner, **grant):
    cid, token = cli_token(owner, "requester", profile="standard")
    machine = _enroll(hub, owner, requesters=[cid], **grant)
    attest(hub, owner, machine)
    return cid, token, machine


def _start(hub, enrollment, task, *, token=None, owner=None, **arguments):
    return http(
        hub.port,
        "POST",
        "/api/card-starts",
        token=token,
        cookie=owner["cookie"] if owner else None,
        body={
            "task": task["number"],
            "machine": enrollment["machine_id"],
            "project": "demo",
            "topic": "worker",
            **arguments,
        },
    )


def _accepted(response, status=201):
    code, body = response
    assert code == status, (code, body)
    assert set(body) == JOB_KEYS, body
    assert body["title_marker"] == "commonplace-job:" + body["job_id"]
    return body


def _rpc(hub, token, method, **params):
    status, body = http(
        hub.port,
        "POST",
        "/mcp",
        token=token,
        body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert status == 200, (status, body)
    return body


def _tool(hub, token, name, **arguments):
    return _rpc(hub, token, "tools/call", name=name, arguments=arguments)


def _ok(response):
    assert "error" not in response, response
    assert "result" in response and not response["result"].get("isError"), response
    return response["result"]


def _denied(response):
    assert "error" in response or response.get("result", {}).get("isError"), response


def _tools(hub, token):
    return _ok(_rpc(hub, token, "tools/list"))["tools"]


def _status(hub, task, *, token=None, owner=None):
    code, body = http(
        hub.port,
        "GET",
        f"/api/card-starts?task={task['number']}",
        token=token,
        cookie=owner["cookie"] if owner else None,
    )
    assert code == 200, (code, body)
    assert isinstance(body, list), body
    return body


def _lease(hub, machine):
    code, body = http(
        hub.port, "POST", "/api/runner/lease", token=machine["machine_token"], body={}
    )
    assert code == 200, (code, body)
    assert set(body) == JOB_KEYS, body
    return body


def _starting(hub, machine, job):
    code, body = http(
        hub.port,
        "POST",
        "/api/runner/starting",
        token=machine["machine_token"],
        body={"lease_token": job["lease_token"]},
    )
    assert code == 200, (code, body)


def _result(hub, machine, job, outcome, **fields):
    code, body = http(
        hub.port,
        "POST",
        "/api/runner/result",
        token=machine["machine_token"],
        body={"lease_token": job["lease_token"], "outcome": outcome, **fields},
    )
    assert code == 200, (code, body)


def _machine_row(hub, owner, machine):
    code, body = http(hub.port, "GET", "/api/runners", cookie=owner["cookie"])
    assert code == 200, (code, body)
    rows = body["machines"] if isinstance(body, dict) else body
    found = [row for row in rows if row.get("machine_id", row.get("id")) == machine["machine_id"]]
    assert len(found) == 1, body
    return found[0]


def test_B1_unbound_is_inert(signup_hub, owners, tmp_path):
    hub, owner = signup_hub, owners["A"]
    cid, token = cli_token(owner, "requester", profile="standard")
    machine = _enroll(hub, owner, requesters=[cid])  # positive control: enroll 201
    task = card(hub, owner)
    for auth in ({"owner": owner}, {"token": token}):
        assert _start(hub, machine, task, **auth) == (403, {"error": "no: binding_not_attested"})
    assert not START_TOOLS & {tool["name"] for tool in _tools(hub, token)}
    _denied(
        _tool(
            hub,
            token,
            "card_start",
            task=task["number"],
            machine=machine["machine_id"],
            project="demo",
            topic="worker",
        )
    )
    assert (
        http(hub.port, "POST", "/api/runner/lease", token=machine["machine_token"], body={})[0]
        == 204
    )
    assert launches(tmp_path) == []


@pytest.mark.parametrize("topic", [None, "other"])
@pytest.mark.parametrize("caller", ["owner", "coordinator"])
def test_B2_topic_is_required_and_granted(signup_hub, owners, topic, caller):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    _accepted(_start(hub, machine, card(hub, owner), token=token))
    task = card(hub, owner)
    body = {"task": task["number"], "machine": machine["machine_id"], "project": "demo"}
    if topic is not None:
        body["topic"] = topic
    code, payload = http(
        hub.port,
        "POST",
        "/api/card-starts",
        body=body,
        token=token if caller == "coordinator" else None,
        cookie=owner["cookie"] if caller == "owner" else None,
    )
    assert code == 403, (code, payload)


@pytest.mark.parametrize("trust", ["trusted", "standard", "untrusted"])
def test_A1_only_granted_trusted_requesters(signup_hub, owners, trust):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    _accepted(_start(hub, machine, card(hub, owner), token=token))
    if trust == "untrusted":
        _cid, stranger = _untrusted(hub, owner, "stranger")
    else:
        _cid, stranger = cli_token(owner, "stranger", profile="standard", trust=trust)
    assert _start(hub, machine, card(hub, owner), token=stranger)[0] == 403
    assert not START_TOOLS & {tool["name"] for tool in _tools(hub, stranger)}


def test_A2_owner_cannot_bypass_worker_origin(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    _accepted(_start(hub, machine, card(hub, owner), owner=owner))
    _cid, untrusted = _untrusted(hub, owner, "submitter")
    status, payload = http(
        hub.port,
        "POST",
        "/api/boards/shared/cards",
        token=untrusted,
        body={"title": "Restricted task"},
    )
    assert status == 201, (status, payload)
    from commonplace.boards.store import BoardStore

    with BoardStore(hub.gateway.workspace_dir(owner["workspace"])).transaction() as conn:
        conn.execute("UPDATE tasks SET assignee=NULL WHERE id=?", (payload["number"],))
    for auth in ({"owner": owner}, {"token": token}):
        code, body = _start(hub, machine, payload, **auth)
        assert code == 403 and body["error"].endswith("origin_not_released"), (code, body)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("ask", "waiting_on_person"),
        ("review_ask", "waiting_on_person"),
        ("ask_to", "waiting_on_person"),
        ("review", "not_todo"),
        ("done", "not_todo"),
        ("backlog", "not_todo"),
        ("assigned", "assigned"),
        ("proposal", "proposal_open"),
    ],
)
def test_A3_only_unassigned_ready_todo(signup_hub, owners, case, reason):
    hub, owner = signup_hub, owners["A"]
    cid, token, machine = _ready(hub, owner)
    _accepted(_start(hub, machine, card(hub, owner), token=token))
    task = card(hub, owner)
    # Synthetic card state covers otherwise independently managed board transitions.
    directory = hub.gateway.workspace_dir(owner["workspace"])
    from commonplace.boards.store import BoardStore

    changes = {
        "ask": {"status": "todo", "ask": "Choose"},
        "review_ask": {"status": "review", "ask": "Choose"},
        "review": {"status": "review", "ask": ""},
        "ask_to": {"ask_to": "user:" + owner["user_id"]},
        "assigned": {"assignee": "conn:" + cid},
        "proposal": {"proposal_status": "pending"},
    }
    values = changes.get(case, {"status": case})
    with BoardStore(directory).transaction() as conn:
        if case == "ask":
            # Exercise defensive handling of legacy todo+ask rows despite the current CHECK.
            conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "UPDATE tasks SET " + ",".join(key + "=?" for key in values) + " WHERE id=?",
            (*values.values(), task["number"]),
        )
    code, body = _start(hub, machine, task, owner=owner)
    assert (code, body) == (403, {"error": "no: card is not startable: " + reason})


def test_A4_closed_schema_and_minimal_job(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    job = _accepted(_start(hub, machine, card(hub, owner), token=token))
    assert job["lease_token"] is None and job["lease_until"] is None
    for key in ("argv", "path", "message", "model", "shell"):
        assert _start(hub, machine, card(hub, owner), token=token, **{key: "unused"})[0] == 403
    _denied(
        _tool(
            hub,
            token,
            "card_start",
            task=1,
            machine=machine["machine_id"],
            project="demo",
            topic="worker",
            argv=[],
        )
    )


@pytest.mark.parametrize("caller", ["owner", "coordinator"])
@pytest.mark.parametrize(
    "runtime_mode", ["approval-required", "auto-accept-edits", "auto", "full-access"]
)
def test_A5_accepted_start_is_claimable_by_real_worker(signup_hub, owners, caller, runtime_mode):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner, max_runtime_mode="full-access")
    task = card(hub, owner)
    _accepted(
        _start(
            hub,
            machine,
            task,
            runtime_mode=runtime_mode,
            **({"owner": owner} if caller == "owner" else {"token": token}),
        )
    )
    _ok(_tool(hub, machine["worker_token"], "task_take_next", task=task["number"]))


def test_A6_machine_resolution_is_canonical(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    task = card(hub, owner)
    first = _accepted(
        _start(hub, machine, task, token=token, machine="example-machine", request_key="k")
    )
    second = _accepted(_start(hub, machine, task, token=token, request_key="k"), 200)
    assert first == second
    foreign = _enroll(hub, owners["B"], label="foreign-machine")
    for ref in ("missing", "foreign-machine", "mach_0123456789abcdef", foreign["machine_id"]):
        assert _start(hub, machine, task, token=token, machine=ref) == (
            404,
            {"error": "no: machine not found"},
        )


@pytest.mark.parametrize("arguments", [{"project": "other"}, {"runtime_mode": "auto"}])
def test_G1_grant_limits(signup_hub, owners, arguments):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    _accepted(_start(hub, machine, card(hub, owner), token=token))
    assert _start(hub, machine, card(hub, owner), token=token, **arguments)[0] == 403


@pytest.mark.parametrize("change", ["project", "requester"])
def test_G2_starting_rechecks_authority(signup_hub, owners, tmp_path, change):
    hub, owner = signup_hub, owners["A"]
    cid, token, machine = _ready(hub, owner)
    task = card(hub, owner)
    _accepted(_start(hub, machine, task, token=token))
    leased = _lease(hub, machine)
    if change == "project":
        response = http(
            hub.port,
            "PATCH",
            "/api/runners/" + machine["machine_id"],
            cookie=owner["cookie"],
            body={"projects": ["other"]},
        )
    else:
        response = http(hub.port, "DELETE", "/api/connections/" + cid, cookie=owner["cookie"])
    assert response[0] == 200, response
    code, body = http(
        hub.port,
        "POST",
        "/api/runner/starting",
        token=machine["machine_token"],
        body={"lease_token": leased["lease_token"]},
    )
    assert code == 409, (code, body)
    assert _status(hub, task, owner=owner)[0]["state"] == "cancelled"
    assert launches(tmp_path) == []


def test_R1_revocation_restores_inert_surface(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    cid, token = cli_token(owner, "requester", profile="standard")
    task = card(hub, owner)
    tools_before = _tools(hub, token)
    card_before = http(hub.port, "GET", f"/api/cards/{task['number']}", token=token)
    assert card_before[0] == 200, card_before
    machine = _enroll(hub, owner, requesters=[cid])  # positive control: enroll 201
    attest(hub, owner, machine)
    assert START_TOOLS <= {tool["name"] for tool in _tools(hub, token)}
    response = http(
        hub.port, "DELETE", "/api/runners/" + machine["machine_id"], cookie=owner["cookie"]
    )
    assert response[0] == 200, response
    assert _tools(hub, token) == tools_before
    assert http(hub.port, "GET", f"/api/cards/{task['number']}", token=token) == card_before


def test_R2_start_does_not_change_card(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    task = card(hub, owner)
    before = http(hub.port, "GET", f"/api/cards/{task['number']}", cookie=owner["cookie"])
    assert before[0] == 200, before
    _accepted(_start(hub, machine, task, token=token))
    assert http(hub.port, "GET", f"/api/cards/{task['number']}", cookie=owner["cookie"]) == before


def test_RT1_routes_and_tool_gate(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    machine = _enroll(hub, owner)  # positive control: enroll 201
    assert (
        http(hub.port, "POST", "/api/runner/lease", token=machine["machine_token"], body={})[0]
        == 204
    )
    assert (
        http(hub.port, "POST", "/api/runners/lease", token=machine["machine_token"], body={})[0]
        == 404
    )
    assert http(hub.port, "POST", "/api/runner/x", cookie=owner["cookie"], body={})[0] == 404
    attest(hub, owner, machine)
    _accepted(_start(hub, machine, card(hub, owner), owner=owner))
    _cid, token = cli_token(owner, "limited", profile="standard", tools=["task_show"])
    assert _start(hub, machine, card(hub, owner), token=token) == (
        403,
        {"error": "tool not allowed for this connection"},
    )


def test_RU1_rest_rules_freshness(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    task = card(hub, owner)
    response = http(
        hub.port,
        "PATCH",
        "/api/rules/checks",
        cookie=owner["cookie"],
        body={"agent": "*", "mode": "enforce"},
    )
    assert response[0] == 200, response
    result = _ok(_tool(hub, token, "rules_get", effective=True, format="md"))
    seq = int(result["content"][0]["text"].splitlines()[-1].split()[1])
    body = {
        "task": task["number"],
        "machine": machine["machine_id"],
        "project": "demo",
        "topic": "worker",
    }
    _accepted(
        http(
            hub.port,
            "POST",
            "/api/card-starts",
            token=token,
            body=body,
            headers={"Commonplace-Rules-Seq": str(seq)},
        )
    )
    other = card(hub, owner)
    code, payload = _start(hub, machine, other, token=token)
    assert code == 428 and payload["rules_refresh"] is True, (code, payload)
    assert _status(hub, other, owner=owner) == []


def test_S4_idempotency_precedes_active_limit(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner, projects=["demo", "other"])
    task = card(hub, owner)
    first = _accepted(_start(hub, machine, task, token=token, request_key="k"))
    assert _accepted(_start(hub, machine, task, token=token, request_key="k"), 200) == first
    assert _start(hub, machine, task, token=token, project="other", request_key="k") == (
        409,
        {"error": "no: request_key reused with different arguments"},
    )
    assert _start(hub, machine, task, token=token) == (
        409,
        {"error": "no: card already has an active start"},
    )


def test_S5_queue_limit(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    for _ in range(8):
        _accepted(_start(hub, machine, card(hub, owner), token=token))
    assert _start(hub, machine, card(hub, owner), token=token) == (409, {"error": "no: busy"})


def test_S5_recent_card_and_hourly_cap(signup_hub, owners, monkeypatch):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner, max_starts_per_hour=1)
    task = card(hub, owner)
    _accepted(_start(hub, machine, task, token=token))
    from commonplace.runner import jobs

    clock = jobs._now()
    monkeypatch.setattr(jobs, "_now", lambda: clock)
    leased = _lease(hub, machine)
    _starting(hub, machine, leased)
    _result(hub, machine, leased, "started", thread_id="thread-demo")
    assert _start(hub, machine, task, token=token) == (
        409,
        {"error": "no: card was started recently"},
    )
    assert _start(hub, machine, card(hub, owner), owner=owner) == (409, {"error": "no: busy"})
    from datetime import datetime, timedelta, timezone

    later = datetime.strptime(clock, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(
        hours=2
    )
    monkeypatch.setattr(jobs, "_now", lambda: later.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert _start(hub, machine, task, token=token) == (
        409,
        {"error": "no: card was started recently"},
    )
    _accepted(_start(hub, machine, task, owner=owner))


def test_V1_status_is_requester_scoped(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    cid, token = cli_token(owner, "requester", profile="standard")
    other_id, other_token = cli_token(owner, "other", profile="standard")
    machine = _enroll(hub, owner, requesters=[cid, other_id])
    attest(hub, owner, machine)
    task = card(hub, owner)
    job = _accepted(_start(hub, machine, task, token=token))
    cancel = http(
        hub.port, "POST", f"/api/card-starts/{job['job_id']}/cancel", token=token, body={}
    )
    assert cancel[0] == 200, cancel
    second = _accepted(_start(hub, machine, task, token=other_token))
    assert {row["job"] for row in _status(hub, task, token=token)} == {job["job_id"]}
    assert {row["job"] for row in _status(hub, task, token=other_token)} == {second["job_id"]}
    assert {row["job"] for row in _status(hub, task, owner=owner)} == {
        job["job_id"],
        second["job_id"],
    }
    denied = http(
        hub.port, "POST", f"/api/card-starts/{second['job_id']}/cancel", token=token, body={}
    )
    assert denied[0] == 403, denied


def test_U1_resolve_requires_person_owner_and_audit(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    task = card(hub, owner)
    job = _accepted(_start(hub, machine, task, token=token))
    leased = _lease(hub, machine)
    _starting(hub, machine, leased)
    _result(hub, machine, leased, "uncertain", code="spawn_ambiguous")
    path = f"/api/card-starts/{job['job_id']}/resolve"
    response = http(hub.port, "POST", path, cookie=owner["cookie"], body={"outcome": "failed"})
    assert response[0] == 200, response  # positive control: owner resolve 200
    assert _status(hub, task, owner=owner)[0]["state"] == "failed"
    rows = [
        row for row in hub.db.audit_entries(owner["workspace"]) if row["action"] == "runner.resolve"
    ]
    assert len(rows) == 1, rows
    _cid, owner_pat = cli_token(owner, "owner-role", profile="standard")
    for forbidden in (owner_pat, token, machine["worker_token"]):
        assert http(hub.port, "POST", path, token=forbidden, body={"outcome": "failed"})[0] == 403


def test_W1_worker_claim_and_write_wing(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    # Signup already supplies the public shared and inbox wings.
    _cid, token, machine = _ready(hub, owner, wings=["shared", "inbox"])
    task = card(hub, owner)
    _accepted(_start(hub, machine, task, token=token))
    _ok(_tool(hub, machine["worker_token"], "task_show", task=task["number"]))
    _ok(_tool(hub, machine["worker_token"], "task_take_next", task=task["number"]))
    code, body = _start(hub, machine, card(hub, owner, board="inbox"), owner=owner)
    assert code == 403 and body["error"].endswith("outside_worker_wing"), (code, body)


def test_W2_worker_cannot_escalate(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    machine = _enroll(hub, owner)  # positive control: enroll 201
    token = machine["worker_token"]
    assert {tool["name"] for tool in _tools(hub, token)} == {
        "task_show",
        "task_take_next",
        "task_update",
        "task_comment",
        "task_heartbeat",
        "task_handoff",
        "ask",
    }
    for name in (
        "mempalace_kg_add",
        "mempalace_add_drawer",
        "mempalace_diary_write",
        "task_approve",
        "decide",
        "facts_import",
        *sorted(START_TOOLS),
    ):
        _denied(_tool(hub, token, name))
    for method, path, body in (
        ("POST", "/api/cards/1/approve", {}),
        ("POST", "/api/cards/1/decide", {}),
        ("POST", "/api/card-starts", {}),
        ("POST", "/api/card-starts/job_missing/cancel", {}),
        ("POST", "/api/card-starts/job_missing/resolve", {"outcome": "failed"}),
        ("GET", "/api/card-starts?task=1", None),
        ("GET", "/api/imports", None),
        ("POST", "/api/threads/thread-demo/recall", {}),
        ("GET", "/api/needs-me", None),
        ("GET", "/api/needs-me/digest", None),
        ("GET", "/api/runners", None),
        ("POST", "/api/runners", {}),
    ):
        response = http(hub.port, method, path, token=token, body=body)
        assert response[0] == 403, (path, response)
    row = _machine_row(hub, owner, machine)
    response = http(
        hub.port,
        "POST",
        "/api/connections",
        cookie=owner["cookie"],
        body={"name": "child", "parent": row["worker_connection_id"]},
    )
    assert response[0] == 400, response


def test_I1_workspace_isolation(signup_hub, owners):
    hub, owner, foreign_owner = signup_hub, owners["A"], owners["B"]
    _cid, token, machine = _ready(hub, owner)
    row = _machine_row(hub, owner, machine)  # positive control: owner GET 200 lists machine
    assert row["label"] == "example-machine"
    task = card(hub, owner)
    job = _accepted(_start(hub, machine, task, token=token))
    _cid, stranger, other_machine = _ready(hub, foreign_owner)
    for auth in ({"owner": foreign_owner}, {"token": stranger}):
        assert _start(hub, machine, task, **auth) == (404, {"error": "no: machine not found"})
        kwargs = {"cookie": foreign_owner["cookie"]} if "owner" in auth else {"token": stranger}
        assert (
            http(hub.port, "POST", f"/api/card-starts/{job['job_id']}/cancel", body={}, **kwargs)[0]
            == 404
        )
        assert http(
            hub.port,
            "PATCH",
            "/api/runners/" + machine["machine_id"],
            body={"enabled": False},
            **kwargs,
        )[0] in (403, 404)
    assert _start(hub, other_machine, task, owner=foreign_owner)[0] == 404
    assert (
        http(
            hub.port,
            "POST",
            "/api/runner/result",
            token=other_machine["machine_token"],
            body={"lease_token": "foreign", "outcome": "uncertain"},
        )[0]
        == 404
    )
    assert _start(hub, machine, task, token=other_machine["machine_token"])[0] == 404


@pytest.mark.parametrize(
    "state", ["queued", "leased", "starting", "uncertain", "started", "failed", "cancelled"]
)
def test_V1_cancel_state_rules(signup_hub, owners, state):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    task = card(hub, owner)
    job = _accepted(_start(hub, machine, task, token=token))
    path = f"/api/card-starts/{job['job_id']}/cancel"
    if state not in ("queued", "cancelled"):
        leased = _lease(hub, machine)
        if state in ("starting", "uncertain", "started"):
            _starting(hub, machine, leased)
        if state == "uncertain":
            _result(hub, machine, leased, "uncertain")
        elif state == "started":
            _result(hub, machine, leased, "started", thread_id="thread-demo")
        elif state == "failed":
            _result(hub, machine, leased, "failed", code="project_not_configured")
    elif state == "cancelled":
        response = http(hub.port, "POST", path, token=token, body={})
        assert response[0] == 200, response
    if state == "uncertain":
        before = _status(hub, task, owner=owner)
        expected = (
            409,
            {"error": "no: start outcome is uncertain; a workspace owner must resolve it"},
        )
        assert http(hub.port, "POST", path, token=token, body={}) == expected
        assert http(hub.port, "POST", path, cookie=owner["cookie"], body={}) == expected
        _denied(_tool(hub, token, "card_start_cancel", job=job["job_id"]))
        assert _status(hub, task, owner=owner) == before
        assert _start(hub, machine, task, token=token) == (
            409,
            {"error": "no: card already has an active start"},
        )
    elif state in ("started", "failed", "cancelled"):
        before = _status(hub, task, owner=owner)
        assert http(hub.port, "POST", path, token=token, body={}) == (
            409,
            {"error": "no: start is already finished"},
        )
        assert _status(hub, task, owner=owner) == before
    else:
        code, body = http(hub.port, "POST", path, token=token, body={})
        assert code == 200, (code, body)
        assert body["state"] == ("starting" if state == "starting" else "cancelled")
        assert body["cancel_requested_at"]


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_requester_replay_never_returns_lease_token(signup_hub, owners, surface):
    import json

    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    task = card(hub, owner)
    first = _accepted(_start(hub, machine, task, token=token, request_key="replay"))
    leased = _lease(hub, machine)
    stored_token = leased["lease_token"]
    assert isinstance(stored_token, str) and stored_token
    from commonplace.runner import jobs

    store = jobs.open_store(hub.gateway, owner["workspace"])
    with store.transaction() as conn:
        row = dict(
            conn.execute("SELECT * FROM runner_jobs WHERE id=?", (first["job_id"],)).fetchone()
        )
    assert row["lease_token"] == stored_token
    if surface == "rest":
        replay = _accepted(_start(hub, machine, task, token=token, request_key="replay"), 200)
        serialized = json.dumps(replay)
    else:
        response = _tool(
            hub,
            token,
            "card_start",
            task=task["number"],
            machine=machine["machine_id"],
            project="demo",
            topic="worker",
            request_key="replay",
        )
        result = _ok(response)
        replay = json.loads(result["content"][0]["text"])
        serialized = json.dumps(response)
    assert replay["job_id"] == first["job_id"]
    assert replay["lease_token"] is None
    assert stored_token not in serialized
    assert jobs.job_object(row)["lease_token"] is None
    assert stored_token not in json.dumps(jobs.job_object(row))


@pytest.mark.parametrize("surface", ["rest", "mcp"])
def test_request_key_has_explicit_128_character_limit(signup_hub, owners, surface):
    hub, owner = signup_hub, owners["A"]
    _cid, token, machine = _ready(hub, owner)
    _accepted(_start(hub, machine, card(hub, owner), token=token, request_key="k" * 128))
    task = card(hub, owner)
    if surface == "rest":
        assert _start(hub, machine, task, token=token, request_key="k" * 129) == (
            403,
            {"error": "no: invalid request_key"},
        )
    else:
        response = _tool(
            hub,
            token,
            "card_start",
            task=task["number"],
            machine=machine["machine_id"],
            project="demo",
            topic="worker",
            request_key="k" * 129,
        )
        _denied(response)
        import json

        assert "no: invalid request_key" in json.dumps(response)
    schema = next(tool for tool in _tools(hub, token) if tool["name"] == "card_start")[
        "inputSchema"
    ]
    assert schema["properties"]["request_key"].get("maxLength") == 128
    assert _status(hub, task, owner=owner) == []


def test_worker_scope_documentation_states_card_guidance(signup_hub, owners):
    from pathlib import Path

    _ready(signup_hub, owners["A"])
    text = (Path(__file__).resolve().parents[1] / "docs" / "runner.md").read_text()
    assert "Card scope from the spawn message is guidance only." in text
    assert "A worker can claim any startable todo card in its write wing." in text
