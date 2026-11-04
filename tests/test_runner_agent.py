"""Runner acceptance through the ordinary CLI, HTTP, and subprocess launch double."""

import json
import os
import re

import pytest

from tests.runner_harness import (
    attest,
    card,
    enroll as harness_enroll,
    http,
    launches,
    owners,  # noqa: F401
    runner,
    runner_config,
    signup_hub,  # noqa: F401
)

# Deliberately literal: a change to the agent instruction is an interface change.
_MESSAGE = (
    "You were started for Commonplace card #{n} only "
    "(job marker commonplace-job:{job_id}). "
    "First claim it with task_take_next(task={n}). "
    "If that claim is refused, stop and do nothing else. "
    "Touch no other card: do not claim, update, comment on or hand off "
    "any card except #{n}."
)
_ENV = {"COMMONPLACE_RUNNER_TOKEN": "synthetic-machine-token", "RUNNER_T3_GRANT": "synthetic-grant"}


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


def _offline(tmp, **overrides):
    endpoint = type("Endpoint", (), {"port": 1})()
    return runner_config(tmp, endpoint, _ENV["COMMONPLACE_RUNNER_TOKEN"], **overrides)


def _invoke(path, env, action="run"):
    args = [action, "--config", str(path)]
    if action == "run":
        args.append("--once")
    return runner(args, env)


def _check_control(tmp):
    path = _offline(tmp)
    result = _invoke(path, _ENV, "check")
    assert result.returncode == 0, "positive control: valid runner check"
    return path, result


def _setup(hub, owner, tmp, *, mode="ok", label="example-machine", **grant):
    machine = enroll(hub, owner, label=label, **grant)
    attest(hub, owner, machine)
    path = runner_config(tmp, hub, machine["machine_token"], mode=mode)
    env = {
        "COMMONPLACE_RUNNER_TOKEN": machine["machine_token"],
        "RUNNER_T3_GRANT": "synthetic-grant",
    }
    return machine, path, env


def _job_id(row):
    assert isinstance(row, dict), "expected a job object"
    value = row.get("job_id", row.get("job", row.get("id")))
    if isinstance(value, dict):
        return _job_id(value)
    assert isinstance(value, str) and re.fullmatch(r"job_[0-9a-f]{16}", value), "expected job id"
    return value


def _queue(hub, owner, machine, **overrides):
    task = card(hub, owner)
    number = task["number"]
    status, body = http(
        hub.port,
        "POST",
        f"/api/cards/{number}/handoff",
        cookie=owner["cookie"],
        body={"to": "anyone", "note": "Ready for shared work.", "version": task["version"]},
    )
    assert status == 200, (status, body)
    status, body = http(
        hub.port,
        "POST",
        "/api/card-starts",
        cookie=owner["cookie"],
        body={
            "task": number,
            "machine": machine["machine_id"],
            "project": "demo",
            "topic": "worker",
            **overrides,
        },
    )
    assert status == 201, (status, body)
    return number, _job_id(body)


def _state(hub, owner, number, job_id):
    status, body = http(hub.port, "GET", f"/api/card-starts?task={number}", cookie=owner["cookie"])
    assert status == 200, (status, body)
    rows = body.get("jobs") if isinstance(body, dict) else body
    assert isinstance(rows, list), "expected status rows"
    matches = [row for row in rows if _job_id(row) == job_id]
    assert len(matches) == 1, "expected one matching job"
    return matches[0]


def _run_control(hub, owner, tmp, **kwargs):
    machine, path, env = _setup(hub, owner, tmp, **kwargs)
    result = _invoke(path, env)
    assert result.returncode == 0, "positive control: runner run --once"
    assert launches(tmp) == []
    return machine, path, env


def _spawn_control(hub, owner, tmp, **kwargs):
    machine, path, env = _setup(hub, owner, tmp, **kwargs)
    number, job_id = _queue(hub, owner, machine)
    result = _invoke(path, env)
    assert result.returncode == 0, "positive control: successful runner launch"
    assert len(launches(tmp)) == 1, "positive control: one spawn"
    return machine, path, env, number, job_id, result


@pytest.mark.parametrize(
    "case",
    [
        "binding",
        "empty-topics",
        "relative-executable",
        "http-hub",
        "http-bridge",
        "missing-shared-ack",
        "wrong-shared-ack",
        "duplicate-project-id",
    ],
)
def test_b5_config_invalid(tmp_path, case):
    path, _ = _check_control(tmp_path)
    config = json.loads(path.read_text())
    message = {
        "binding": "binding_ack required",
        "empty-topics": "topics must not be empty",
        "relative-executable": "invalid t3_executable",
        "http-hub": "endpoints require https or loopback http",
        "http-bridge": "endpoints require https or loopback http",
    }.get(case)
    if case == "binding":
        config["binding_acks"] = []
    elif case == "empty-topics":
        config["topics"] = []
    elif case == "relative-executable":
        config["t3_executable"] = ["t3"]
    elif case == "http-hub":
        config["hub_url"] = "http://example.test"
    elif case == "http-bridge":
        config["t3_mcp_endpoint"] = "http://example.test"
    elif case in {"missing-shared-ack", "wrong-shared-ack"}:
        config["projects"]["demo"].pop("shared_checkout_ack")
        if case == "wrong-shared-ack":
            config["projects"]["demo"]["shared_checkout_ack"] = "demo:yes"
        message = 'project demo needs shared_checkout_ack "demo:shared-checkout"'
    else:
        config["projects"]["alias"] = {
            "project_id": "demo",
            "shared_checkout_ack": "alias:shared-checkout",
        }
        config["binding_acks"].append("alias:worker")
        message = "project labels must use distinct project_id values"
    path.write_text(json.dumps(config))
    result = _invoke(path, _ENV, "check")
    assert result.returncode == 4
    assert result.stderr == "runner: config_invalid: " + message + "\n"
    assert launches(tmp_path) == []


def test_k1_check_notices(tmp_path):
    _, result = _check_control(tmp_path)
    assert "launch shape uncaptured (D2 open); attestation is not verification" in result.stdout
    assert (
        "workspace strategy root (unverified, D2); started agents share the project checkout"
        in result.stdout
    )
    assert (
        "project demo: shared checkout acknowledged; one agent per project per hour"
        in result.stdout
    )
    assert result.stderr == ""


@pytest.mark.parametrize("case", ["network", "disabled"])
def test_k2_hub_errors(signup_hub, owners, tmp_path, case):  # noqa: F811
    machine, path, env = _run_control(signup_hub, owners["A"], tmp_path)
    if case == "network":
        # Reserve a loopback port without listening: a deterministic closed endpoint.
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            config = json.loads(path.read_text())
            config["hub_url"] = f"http://127.0.0.1:{sock.getsockname()[1]}"
            path.write_text(json.dumps(config))
            result = _invoke(path, env)
        expected = "runner: hub unavailable (network)\n"
    else:
        status, body = http(
            signup_hub.port,
            "PATCH",
            f"/api/runners/{machine['machine_id']}",
            cookie=owners["A"]["cookie"],
            body={"enabled": False},
        )
        assert status == 200, (status, body)
        result = _invoke(path, env)
        expected = "runner: hub refused (404)\n"
    assert result.returncode == 5
    assert result.stderr == expected
    assert launches(tmp_path) == []


def _result_failure(hub):
    original = hub.gateway.find_route("POST", "/api/runner/result")
    assert callable(original)
    calls = []

    def route(handler):
        calls.append(True)
        if len(calls) == 1:
            handler._send_json(503, {"error": "temporary failure"})
        else:
            original(handler)

    hub.gateway.add_route("POST", "/api/runner/result", route)
    return calls


def test_k3_result_replay(signup_hub, owners, tmp_path):  # noqa: F811
    _spawn_control(signup_hub, owners["A"], tmp_path / "control")
    tmp = tmp_path / "case"
    machine, path, env = _setup(signup_hub, owners["A"], tmp, label="other-machine")
    number, job_id = _queue(signup_hub, owners["A"], machine)
    calls = _result_failure(signup_hub)
    result = _invoke(path, env)
    assert result.returncode == 5
    assert result.stderr == "runner: hub unavailable (503)\n"
    journals = list((tmp / "journal").glob("*.json"))
    assert len(journals) == 1
    assert os.stat(journals[0]).st_mode & 0o777 == 0o600
    assert json.loads(journals[0].read_text())["outcome"] == "started"
    assert _state(signup_hub, owners["A"], number, job_id)["state"] == "starting"
    result = _invoke(path, env)
    assert result.returncode == 0
    assert len(calls) == 2
    assert _state(signup_hub, owners["A"], number, job_id)["state"] == "started"
    assert len(launches(tmp)) == 1
    assert list((tmp / "journal").glob("*.json")) == []


@pytest.mark.parametrize("case", ["runtime-ack", "binding-ack", "duplicate-project-id"])
def test_m1_local_ack_rules(tmp_path, case):
    path, _ = _check_control(tmp_path)
    config = json.loads(path.read_text())
    if case == "runtime-ack":
        config["projects"]["demo"]["runtime_ceiling"] = "auto"
    elif case == "binding-ack":
        config["topics"].append("second")
    else:
        config["projects"]["alias"] = {
            "project_id": "demo",
            "shared_checkout_ack": "alias:shared-checkout",
        }
        config["binding_acks"].append("alias:worker")
    path.write_text(json.dumps(config))
    result = _invoke(path, _ENV, "check")
    assert result.returncode == 4
    message = {
        "runtime-ack": "runtime_ack required",
        "binding-ack": "binding_ack required",
        "duplicate-project-id": "project labels must use distinct project_id values",
    }[case]
    assert result.stderr == "runner: config_invalid: " + message + "\n"
    if case == "duplicate-project-id":
        assert (
            result.stderr
            == "runner: config_invalid: project labels must use distinct project_id values\n"
        )
    else:
        if case == "runtime-ack":
            config["projects"]["demo"]["runtime_ack"] = "demo:auto"
        else:
            config["binding_acks"].append("demo:second")
        path.write_text(json.dumps(config))
        assert _invoke(path, _ENV, "check").returncode == 0


@pytest.mark.parametrize(
    "case,code",
    [
        ("mode", "mode_not_allowed"),
        ("topic", "topic_not_allowed"),
        ("project", "project_not_configured"),
    ],
)
def test_m1_local_job_limits(signup_hub, owners, tmp_path, case, code):  # noqa: F811
    machine, path, env = _run_control(
        signup_hub,
        owners["A"],
        tmp_path,
        projects=["demo", "other"],
        topics=["worker", "second"],
        max_runtime_mode="full-access",
    )
    overrides = {
        "mode": {"runtime_mode": "auto"},
        "topic": {"topic": "second"},
        "project": {"project": "other"},
    }[case]
    number, job_id = _queue(signup_hub, owners["A"], machine, **overrides)
    result = _invoke(path, env)
    assert result.returncode == 0
    row = _state(signup_hub, owners["A"], number, job_id)
    assert row["state"] == "failed" and row["outcome_code"] == code
    assert launches(tmp_path) == []


def test_m2_bridge_shape(signup_hub, owners, tmp_path):  # noqa: F811
    _, path, env, _, _, _ = _spawn_control(signup_hub, owners["A"], tmp_path)
    entry = launches(tmp_path)[0]
    launch = entry["launch"]
    assert set(launch) == {
        "title",
        "projectId",
        "message",
        "runtimeMode",
        "topic",
        "workspaceStrategy",
    }
    assert launch["topic"] == "worker" and launch["runtimeMode"] == "approval-required"
    assert launch["projectId"] == "demo" and launch["workspaceStrategy"] == {"type": "root"}
    assert entry["argv"][-3:-1] == ["acp-mcp-call", "t3_thread_launch"]
    assert "thread start" not in json.dumps(entry["argv"])
    config = json.loads(path.read_text())
    assert config["projects"]["demo"]["shared_checkout_ack"] == "demo:shared-checkout"
    config["workspace_strategy"] = {"type": "root"}
    path.write_text(json.dumps(config))
    result = _invoke(path, env, "check")
    assert result.returncode == 4
    assert result.stderr == "runner: config_invalid: invalid configuration\n"


def test_m3_fixed_agent_message(signup_hub, owners, tmp_path):  # noqa: F811
    _, _, _, number, job_id, _ = _spawn_control(signup_hub, owners["A"], tmp_path)
    launch = launches(tmp_path)[0]["launch"]
    assert launch["message"] == _MESSAGE.format(n=number, job_id=job_id)
    assert launch["title"] == f"Card #{number} [commonplace-job:{job_id}]"


def test_m4_shared_checkout_gate_before_hub(signup_hub, owners, tmp_path):  # noqa: F811
    _spawn_control(signup_hub, owners["A"], tmp_path / "control")
    tmp = tmp_path / "case"
    machine, path, env = _setup(signup_hub, owners["A"], tmp, label="other-machine")
    number, job_id = _queue(signup_hub, owners["A"], machine)
    config = json.loads(path.read_text())
    config["projects"]["demo"].pop("shared_checkout_ack")
    path.write_text(json.dumps(config))
    calls = []
    original = signup_hub.gateway.find_route("POST", "/api/runner/lease")
    assert callable(original)

    def lease(handler):
        calls.append(True)
        original(handler)

    signup_hub.gateway.add_route("POST", "/api/runner/lease", lease)
    result = _invoke(path, env)
    assert result.returncode == 4
    assert (
        result.stderr
        == 'runner: config_invalid: project demo needs shared_checkout_ack "demo:shared-checkout"\n'
    )
    assert calls == []
    assert _state(signup_hub, owners["A"], number, job_id)["state"] == "queued"
    assert launches(tmp) == []


def test_x1_child_environment(signup_hub, owners, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("COMMONPLACE_PRIVATE_CANARY", "synthetic-private-canary")
    monkeypatch.setenv("UNRELATED_CANARY", "synthetic-unrelated-canary")
    monkeypatch.setenv("T3_ACP_MCP_AUTHORIZATION", "synthetic-parent-grant")
    _spawn_control(signup_hub, owners["A"], tmp_path)
    entry = launches(tmp_path)[0]
    allowed = {
        "PATH",
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "T3CODE_HOME",
        "T3_ACP_MCP_ENDPOINT",
        "T3_ACP_MCP_AUTHORIZATION",
    }
    assert set(entry["env_names"]) <= allowed
    assert {"T3_ACP_MCP_ENDPOINT", "T3_ACP_MCP_AUTHORIZATION"} <= set(entry["env_names"])
    assert not any(name.startswith("COMMONPLACE_") for name in entry["env_names"])
    assert entry["token_seen"] is False


def test_x2_no_secret_or_stderr_in_results(signup_hub, owners, tmp_path):  # noqa: F811
    machine, path, env = _run_control(signup_hub, owners["A"], tmp_path, mode="nonzero")
    number, job_id = _queue(signup_hub, owners["A"], machine)
    result = _invoke(path, env)
    assert result.returncode == 0
    assert len(launches(tmp_path)) == 1
    row = _state(signup_hub, owners["A"], number, job_id)
    assert row["state"] == "uncertain"
    status, body = http(signup_hub.port, "GET", "/api/runners", cookie=owners["A"]["cookie"])
    assert status == 200, (status, body)
    output = result.stdout + result.stderr + json.dumps(row) + json.dumps(body)
    for forbidden in (
        machine["machine_token"],
        machine["worker_token"],
        env["RUNNER_T3_GRANT"],
        "fake launch failed",
    ):
        assert forbidden not in output
    assert result.stdout == result.stderr == ""


def test_s1_success_idempotency_and_fence(signup_hub, owners, tmp_path):  # noqa: F811
    machine, path, env = _run_control(signup_hub, owners["A"], tmp_path)
    number, job_id = _queue(signup_hub, owners["A"], machine)
    original = signup_hub.gateway.find_route("POST", "/api/runner/result")
    assert callable(original)
    bodies = []

    def result_route(handler):
        raw = handler.rfile.read(int(handler.headers["Content-Length"]))
        bodies.append(json.loads(raw))
        # Preserve the real handler and its ordinary request parsing.
        import io

        handler.rfile = io.BytesIO(raw)
        original(handler)

    signup_hub.gateway.add_route("POST", "/api/runner/result", result_route)
    assert _invoke(path, env).returncode == 0
    assert len(launches(tmp_path)) == 1
    assert len(bodies) == 1
    assert _state(signup_hub, owners["A"], number, job_id)["state"] == "started"
    status, body = http(
        signup_hub.port,
        "POST",
        "/api/runner/result",
        token=machine["machine_token"],
        body=bodies[0],
    )
    assert status == 200, (status, body)
    changed = {**bodies[0], "thread_id": "another-thread"}
    status, body = http(
        signup_hub.port, "POST", "/api/runner/result", token=machine["machine_token"], body=changed
    )
    assert status == 409, (status, body)
    assert _state(signup_hub, owners["A"], number, job_id)["thread_id"] == "demo-thread"


@pytest.mark.parametrize(
    "mode",
    ["sleep", "nonzero", "bad-json", "is-error", "no-thread-id", "no-run-id", "status-failed"],
)
def test_s2_ambiguous_launch(signup_hub, owners, tmp_path, mode):  # noqa: F811
    machine, path, env = _run_control(signup_hub, owners["A"], tmp_path, mode=mode)
    number, job_id = _queue(signup_hub, owners["A"], machine)
    assert _invoke(path, env).returncode == 0
    assert len(launches(tmp_path)) == 1
    assert _state(signup_hub, owners["A"], number, job_id)["state"] == "uncertain"


def test_s3_restart_never_respawns_and_late_result_fenced(signup_hub, owners, tmp_path):  # noqa: F811
    machine, path, env = _run_control(signup_hub, owners["A"], tmp_path)
    number, job_id = _queue(signup_hub, owners["A"], machine)
    _result_failure(signup_hub)
    assert _invoke(path, env).returncode == 5
    assert len(launches(tmp_path)) == 1
    journals = list((tmp_path / "journal").glob("*.json"))
    assert len(journals) == 1
    assert os.stat(journals[0]).st_mode & 0o777 == 0o600
    entry = json.loads(journals[0].read_text())
    lease_token = entry["lease_token"]
    # Simulate the persisted pre-spawn boundary surviving a process crash.
    entry = {"lease_token": lease_token, "outcome": "spawning"}
    journals[0].write_text(json.dumps(entry))
    assert _invoke(path, env).returncode == 0
    assert len(launches(tmp_path)) == 1
    row = _state(signup_hub, owners["A"], number, job_id)
    assert row["state"] == "uncertain" and row["outcome_code"] == "restart_unknown"
    status, body = http(
        signup_hub.port,
        "POST",
        f"/api/card-starts/{job_id}/resolve",
        cookie=owners["A"]["cookie"],
        body={"outcome": "failed"},
    )
    assert status == 200, (status, body)
    status, body = http(
        signup_hub.port,
        "POST",
        "/api/runner/result",
        token=machine["machine_token"],
        body={"lease_token": lease_token, "outcome": "started", "thread_id": "late-thread"},
    )
    assert status == 409, (status, body)
    assert list((tmp_path / "journal").glob("*.json")) == []


def test_s6_lost_starting_reply_retries_before_one_spawn(signup_hub, owners, tmp_path):  # noqa: F811
    machine, path, env = _run_control(signup_hub, owners["A"], tmp_path)
    number, job_id = _queue(signup_hub, owners["A"], machine)
    original = signup_hub.gateway.find_route("POST", "/api/runner/starting")
    assert callable(original)
    calls, accepted = [], []

    def starting(handler):
        calls.append(True)
        assert launches(tmp_path) == [], "no spawn before starting confirmation"
        if len(calls) == 1:
            import io

            send, stream = handler.send_response, handler.wfile

            def capture_status(status, *args, **kwargs):
                accepted.append(status)
                send(status, *args, **kwargs)

            handler.send_response, handler.wfile = capture_status, io.BytesIO()
            try:
                original(handler)
            finally:
                handler.send_response, handler.wfile = send, stream
            # The real state transition commits, but its successful response is lost.
            handler._send_json(503, {"error": "reply lost"})
        else:
            original(handler)

    signup_hub.gateway.add_route("POST", "/api/runner/starting", starting)
    result = _invoke(path, env)
    assert result.returncode == 0
    assert accepted == [200]
    assert len(calls) == 2
    assert len(launches(tmp_path)) == 1
    row = _state(signup_hub, owners["A"], number, job_id)
    assert row["state"] == "started" and row["thread_id"] == "demo-thread"
    assert list((tmp_path / "journal").glob("*.json")) == []


@pytest.mark.parametrize("body", [{}, {"lease_token": None}, {"lease_token": ""}])
def test_malformed_lease_is_transient_without_spawn(signup_hub, owners, tmp_path, body):  # noqa: F811
    _machine, path, env = _run_control(signup_hub, owners["A"], tmp_path)
    signup_hub.gateway.add_route(
        "POST", "/api/runner/lease", lambda handler: handler._send_json(200, body)
    )
    result = _invoke(path, env)
    assert result.returncode == 5
    assert result.stderr == "runner: hub unavailable (200)\n"
    assert launches(tmp_path) == []
    assert list((tmp_path / "journal").glob("*.json")) == []
    # The same protocol failure must advance the ordinary continuous-mode back-off.
    from commonplace.runner import agent

    hub = agent._Hub(json.loads(path.read_text()), env["COMMONPLACE_RUNNER_TOKEN"])
    delays = []
    for _ in range(2):
        with pytest.raises(agent._HubError) as error:
            hub.post("lease", {})
        assert error.value.kind == "transient"
        delays.append(hub.delay())
    assert delays == [30, 60]


@pytest.mark.parametrize("content", ["{broken", "[]", '{"outcome":"spawning"}'])
def test_corrupt_journal_is_quarantined_and_leasing_continues(
    signup_hub,  # noqa: F811
    owners,  # noqa: F811
    tmp_path,
    content,
):
    _machine, path, env = _run_control(signup_hub, owners["A"], tmp_path)
    journal = tmp_path / "journal"
    damaged = journal / "damaged.json"
    damaged.write_text(content)
    calls = []
    original = signup_hub.gateway.find_route("POST", "/api/runner/lease")

    def lease(handler):
        calls.append(True)
        original(handler)

    signup_hub.gateway.add_route("POST", "/api/runner/lease", lease)
    result = _invoke(path, env)
    assert result.returncode == 0
    assert result.stderr == "runner: invalid journal quarantined\n"
    assert calls == [True]
    assert not damaged.exists()
    quarantined = list(journal.glob("damaged.json.quarantine-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == content
    assert launches(tmp_path) == []
    result = _invoke(path, env)
    assert result.returncode == 0 and result.stderr == ""
    assert calls == [True, True]
    assert launches(tmp_path) == []
