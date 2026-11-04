"""H0: harness controls over behaviour available before runner installation."""

import json
import os
import subprocess

import pytest

from tests.runner_harness import (
    cli_token,
    fake_t3_config,
    http,
    launches,
    owners,  # noqa: F401
    runner_config,
    signup_hub,  # noqa: F401
)


def test_h0_people_cli_http_and_config(signup_hub, owners, tmp_path):  # noqa: F811
    a, b = owners["A"], owners["B"]
    assert a["user_id"] != b["user_id"]
    assert a["workspace"] != b["workspace"]
    for owner in (a, b):
        status, body = http(signup_hub.port, "GET", "/api/me", cookie=owner["cookie"])
        assert status == 200, (status, body)
        assert body["email"] == owner["email"]
        assert body["actor"] == "user:" + owner["user_id"]
    cid, token = cli_token(a, "worker")
    principal = signup_hub.db.authenticate_token(token)
    assert principal.connection_id == cid
    assert principal.workspace_id == a["workspace"]
    assert principal.policy.trust == "trusted"
    assert principal.policy.read_wings == ("shared",)
    assert principal.policy.write_wing == "shared"
    status, body = http(signup_hub.port, "GET", "/api/missing", cookie=a["cookie"])
    assert status == 404, (status, body)
    assert isinstance(body, (dict, str))
    path = runner_config(tmp_path, signup_hub, token)
    config = json.loads(path.read_text())
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert config["projects"]["demo"]["shared_checkout_ack"] == "demo:shared-checkout"
    assert config["binding_acks"] == ["demo:worker"]
    assert config["hub_url"] == f"http://127.0.0.1:{signup_hub.port}"
    assert os.path.isabs(config["t3_executable"][0])
    assert config["token_env"] == "COMMONPLACE_RUNNER_TOKEN"
    assert config["t3_grant_env"] == "RUNNER_T3_GRANT"
    assert token not in path.read_text()
    omitted = runner_config(
        tmp_path / "omitted",
        signup_hub,
        token,
        omit=("projects.demo.shared_checkout_ack",),
        poll_seconds=30,
    )
    changed = json.loads(omitted.read_text())
    assert "shared_checkout_ack" not in changed["projects"]["demo"]
    assert changed["poll_seconds"] == 30


@pytest.mark.parametrize(
    "mode",
    [
        "ok",
        "sleep",
        "nonzero",
        "bad-json",
        "is-error",
        "no-thread-id",
        "no-run-id",
        "status-failed",
    ],
)
def test_h0_fake_launch(tmp_path, mode):
    assert launches(tmp_path) == []
    argv = fake_t3_config(tmp_path, mode)
    fake_path = tmp_path / "fake-t3.json"
    config = json.loads(fake_path.read_text())
    config["sleep_seconds"] = 0.01
    config["machine_token"] = "synthetic-token-canary"
    fake_path.write_text(json.dumps(config))
    launch = {
        "title": "Demo task",
        "projectId": "demo",
        "message": "Claim the demo task.",
        "runtimeMode": "approval-required",
        "topic": "worker",
        "workspaceStrategy": {"type": "root"},
    }
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    result = subprocess.run(
        [*argv, "acp-mcp-call", "t3_thread_launch", json.dumps(launch)],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    entries = launches(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["launch"] == launch
    assert entry["argv"][-3:] == ["acp-mcp-call", "t3_thread_launch", json.dumps(launch)]
    assert set(env) <= set(entry["env_names"])
    assert entry["token_seen"] is False
    assert "synthetic-token-canary" not in (tmp_path / "launches.jsonl").read_text()
    assert os.stat(tmp_path / "launches.jsonl").st_mode & 0o777 == 0o600
    assert result.returncode == (1 if mode == "nonzero" else 0)
    if mode == "nonzero":
        assert result.stderr == "fake launch failed\n"
    elif mode == "bad-json":
        assert result.stdout == "not JSON\n"
    else:
        payload = json.loads(result.stdout)
        assert payload["isError"] == (mode == "is-error")
        assert ("threadId" in payload["structuredContent"]) == (mode != "no-thread-id")
        assert ("runId" in payload["structuredContent"]) == (mode != "no-run-id")
        assert payload["structuredContent"]["status"] == (
            "failed" if mode == "status-failed" else "running"
        )
