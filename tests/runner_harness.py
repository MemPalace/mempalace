"""Public HTTP and CLI helpers for machine runner acceptance tests.

Import fixtures by name in the test that needs them. No helper imports or
patches the runner package, so the same controls work before it is installed.
"""

import json
import os
import re
import subprocess
import sys
from http.client import HTTPConnection

import pytest

from commonplace import mailer
from tests._commonplace_helpers import Hub, InProcessLauncher, _patch_mcp_server


class Mailbox:
    configured = True

    def __init__(self):
        from threading import Condition

        self.messages = []
        self.changed = Condition()

    def send(self, to, subject, text, html=None):
        with self.changed:
            self.messages.append((to, subject, text, html))
            self.changed.notify_all()

    def link(self, index=-1):
        with self.changed:
            count = index + 1 if index >= 0 else -index
            assert self.changed.wait_for(lambda: len(self.messages) >= count, timeout=3)
            match = re.search(r"https?://\S+", self.messages[index][2])
            assert match is not None, "sign-in email has no link"
            return match[0]


@pytest.fixture
def signup_hub(tmp_path, monkeypatch, config, kg):
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "open")
    box = Mailbox()
    monkeypatch.setattr(mailer, "from_env", lambda: box)
    _patch_mcp_server(monkeypatch, config, kg)
    hub = Hub(tmp_path / "data", InProcessLauncher(monkeypatch))
    hub.box = box
    hub.data_dir = tmp_path / "data"
    try:
        assert hub.gateway.email_auth.mailer is box
        yield hub
    finally:
        hub.gateway.email_auth.close()
        hub.gateway.webhooks.stop()
        hub.gateway.handoff.close()
        hub.close()


def _http(port, method, path, token=None, cookie=None, body=None, origin=True, headers=None):
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    if cookie:
        hdrs["Cookie"] = cookie
    if origin:
        hdrs["Origin"] = f"http://127.0.0.1:{port}" if origin is True else origin
    hdrs.update(headers or {})
    conn = HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, path, None if body is None else json.dumps(body), hdrs)
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return response.status, dict(response.getheaders()), payload
    finally:
        conn.close()


def http(port, method, path, token=None, cookie=None, body=None, origin=True, headers=None):
    """Return errors as data, preserving JSON bodies on every HTTP method."""
    status, _, payload = _http(port, method, path, token, cookie, body, origin, headers)
    return status, payload


def _person(hub, cookie, email):
    status, body = http(hub.port, "GET", "/api/me", cookie=cookie)
    assert status == 200, (status, body)
    return {
        "cookie": cookie,
        "user_id": body["user"],
        "workspace": body["workspace"],
        "email": email,
        "data_dir": hub.data_dir,
        "port": hub.port,
    }


def _signup(hub, email, index):
    from urllib.parse import parse_qs, urlsplit

    status, body = http(hub.port, "POST", "/app/auth/email", body={"email": email})
    assert status == 202, (status, body)
    parts = urlsplit(hub.box.link(index))
    query = parse_qs(parts.query)
    assert "t" in query and query["t"], "sign-in link has no token"
    status, body = http(hub.port, "GET", parts.path + "?" + parts.query)
    assert status == 200, (status, body)
    status, headers, body = _http(hub.port, "POST", parts.path, body={"t": query["t"][0]})
    assert status == 303, (status, body)
    assert "Set-Cookie" in headers, "sign-in response has no cookie"
    return _person(hub, headers["Set-Cookie"].split(";", 1)[0], email)


@pytest.fixture
def owners(signup_hub):
    return {
        "A": _signup(signup_hub, "a@example.test", 0),
        "B": _signup(signup_hub, "b@example.test", 1),
    }


def _cli(owner, args):
    return subprocess.run(
        [sys.executable, "-m", "commonplace", "--data", str(owner["data_dir"]), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def cli_token(owner, name, **flags):
    """Create a trusted PAT through the CLI; return its connection id and token."""
    options = {
        "trust": "trusted",
        "scopes": "memory:read,memory:write",
        "read_wings": "shared",
        "write_wing": "shared",
        **flags,
    }
    args = ["token", "add", name, "--workspace", owner["workspace"], "--by", owner["user_id"]]
    for key, value in options.items():
        if value is not None:
            if isinstance(value, (list, tuple)):
                value = ",".join(value)
            args.extend(["--" + key.replace("_", "-"), str(value)])
    result = _cli(owner, args)
    assert result.returncode == 0, "token add failed"
    assert re.fullmatch(r"cmmn_[A-Za-z0-9_-]{43}\n?", result.stdout), "invalid token line"
    token = result.stdout.splitlines()[0]
    status, body = http(owner["port"], "GET", "/api/connections", cookie=owner["cookie"])
    assert status == 200, (status, body)
    matches = [row for row in body["connections"] if row["name"] == name]
    assert len(matches) == 1, "token connection not found"
    return matches[0]["id"], token


def member(hub, owner, person):
    """Add a signed-up person as a member using the ordinary CLI login link."""
    from urllib.parse import urlsplit

    result = _cli(
        owner,
        [
            "member",
            "add",
            owner["workspace"],
            person["email"],
            "--role",
            "member",
            "--by",
            owner["user_id"],
        ],
    )
    assert result.returncode == 0, "member add failed"
    result = _cli(owner, ["login-link", person["email"], "--workspace", owner["workspace"]])
    assert result.returncode == 0, "login-link failed"
    parts = urlsplit(result.stdout.strip())
    status, headers, body = _http(hub.port, "GET", parts.path + "?" + parts.query)
    assert status == 302, (status, body)
    assert "Set-Cookie" in headers, "login-link response has no cookie"
    return _person(hub, headers["Set-Cookie"].split(";", 1)[0], person["email"])


def enroll(hub, owner, **grant):
    grant = {
        "label": "example-machine",
        "projects": ["demo"],
        "topics": ["worker"],
        "wings": ["shared"],
        **grant,
    }
    status, body = http(hub.port, "POST", "/api/runners", cookie=owner["cookie"], body=grant)
    assert status == 201, (status, body)
    return body


def attest(hub, owner, machine):
    machine_id = machine["machine_id"]
    status, body = http(hub.port, "GET", "/api/runners", cookie=owner["cookie"])
    assert status == 200, (status, body)
    rows = body["machines"] if isinstance(body, dict) else body
    matches = [row for row in rows if row.get("machine_id", row.get("id")) == machine_id]
    assert len(matches) == 1, "machine not found in owner listing"
    row = matches[0]
    status, body = http(
        hub.port,
        "POST",
        f"/api/runners/{machine_id}/attest",
        cookie=owner["cookie"],
        body={
            "topics": row["topics"],
            "provider_instance_id": "worker",
            "worker_connection_id": row["worker_connection_id"],
        },
    )
    assert status == 200, (status, body)
    return body


def card(hub, owner, **fields):
    board = fields.pop("board", "shared")
    status, body = http(hub.port, "GET", "/api/boards", cookie=owner["cookie"])
    assert status == 200, (status, body)
    if not any(row["slug"] == board for row in body["boards"]):
        status, body = http(
            hub.port,
            "POST",
            "/api/boards",
            cookie=owner["cookie"],
            body={"slug": board, "wing": board},
        )
        assert status == 201, (status, body)
    status, body = http(
        hub.port,
        "POST",
        f"/api/boards/{board}/cards",
        cookie=owner["cookie"],
        body={"title": "Demo task", **fields},
    )
    assert status == 201, (status, body)
    return body


def runner(args, env):
    """Invoke the ordinary runner CLI without interpreting its output."""
    return subprocess.run(
        [sys.executable, "-m", "commonplace", "runner", *args],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=90,
    )


def _private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream)
    os.chmod(path, 0o600)


def fake_t3_config(tmp, mode):
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / "fake-t3.json"
    _private_json(path, {"mode": mode, "log": str(tmp / "launches.jsonl"), "sleep_seconds": 65})
    return [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "runner_fake_t3.py"),
        "--fake-config",
        str(path),
    ]


def runner_config(tmp, hub, machine_token, *, mode="ok", omit=(), **overrides):
    """Write config; callers pass COMMONPLACE_RUNNER_TOKEN and RUNNER_T3_GRANT in env."""
    argv = fake_t3_config(tmp, mode)
    fake_path = tmp / "fake-t3.json"
    fake = json.loads(fake_path.read_text())
    fake["machine_token"] = machine_token
    _private_json(fake_path, fake)
    config = {
        "hub_url": f"http://127.0.0.1:{hub.port}",
        "token_env": "COMMONPLACE_RUNNER_TOKEN",
        "t3_grant_env": "RUNNER_T3_GRANT",
        "t3_mcp_endpoint": "http://127.0.0.1:1",
        "t3_executable": argv,
        "t3_timeout_seconds": 2,
        "poll_seconds": 15,
        "journal_dir": str(tmp / "journal"),
        "topics": ["worker"],
        "binding_acks": ["demo:worker"],
        "projects": {"demo": {"project_id": "demo", "shared_checkout_ack": "demo:shared-checkout"}},
        **overrides,
    }
    for dotted in omit:
        parts = dotted.split(".")
        parent = config
        for part in parts[:-1]:
            parent = parent[part]
        parent.pop(parts[-1], None)
    path = tmp / "runner.json"
    _private_json(path, config)
    return path


def launches(tmp):
    path = tmp / "launches.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]
