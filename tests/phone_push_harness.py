"""Shared public-route helpers for phone notification tests.

Import ``hub, signup, fresh, remote`` to use the signup-backed hub fixture.
Owners are signup cookie strings (or objects with a ``cookie`` attribute).
Call positive_get before enable in every feature test. The hub's ``ntfy``
records requests and consumes scripted replies: an HTTP status, a tuple of
status/body/headers, or None to close the connection without a response.
"""

import http.client
import importlib
import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from commonplace import sessions, urls, webhooks
from commonplace.boards import display, service as board_service
from commonplace.policy import Policy
from tests.test_commonplace_signup_space import fresh as fresh, remote as remote, signup as signup


class NtfyStub:
    """Loopback-only recorder; no real notification service is contacted."""

    def __init__(self):
        self.requests = []
        self.replies = []
        self.redirect_hits = 0
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def record(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with stub._lock:
                    if self.path == "/redirect":
                        stub.redirect_hits += 1
                        reply = 200
                    else:
                        stub.requests.append(
                            SimpleNamespace(
                                method=self.command,
                                path=self.path,
                                headers=dict(self.headers),
                                body=body,
                            )
                        )
                        reply = stub.replies.pop(0) if stub.replies else 200
                if reply is None:
                    self.close_connection = True
                    return
                status, content, headers = (
                    reply if isinstance(reply, tuple) else (reply, {"id": "abc123", "time": 1}, {})
                )
                if isinstance(content, (dict, list)):
                    content = json.dumps(content).encode()
                elif isinstance(content, str):
                    content = content.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(content)

            do_POST = record
            do_GET = record

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.redirect_url = self.url + "/redirect"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive(), "notification stub did not stop"


@pytest.fixture
def receiver():
    stub = NtfyStub().start()
    try:
        yield stub
    finally:
        stub.close()


@pytest.fixture
def hub(signup, receiver, monkeypatch):
    """Use ordinary signup, never the admin-seeded Hub helper."""
    client, _, _ = signup
    client.ntfy = receiver
    monkeypatch.setattr(urls, "AGENT_URL", "https://cp.example")
    monkeypatch.setattr(urls, "APP_URL", "https://cp.example")
    try:
        yield client
    finally:
        module = sys.modules.get("commonplace.phone_push")
        if module is not None:
            module.stop(client.gateway)


def api(hub, owner, method, path, data=None, *, token=None):
    """Return status and decoded body without inspecting response fields."""
    headers = {"Content-Type": "application/json", "Origin": urls.APP_URL}
    if owner is not None:
        headers["Cookie"] = owner if isinstance(owner, str) else owner.cookie
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=3)
    try:
        conn.request(method, path, json.dumps(data) if data is not None else None, headers)
        response = conn.getresponse()
        body = response.read()
        return response.status, json.loads(body) if body else None
    finally:
        conn.close()


def positive_get(hub, owner):
    code, body = api(hub, owner, "GET", "/api/phone-push")
    assert code == 200, (code, body)
    return body


def enable(monkeypatch, hub, *, token=None):
    """After positive_get, configure the local stub and install without a thread."""
    monkeypatch.setenv("COMMONPLACE_NTFY_URL", hub.ntfy.url)
    if token is None:
        monkeypatch.delenv("COMMONPLACE_NTFY_TOKEN", raising=False)
    else:
        monkeypatch.setenv("COMMONPLACE_NTFY_TOKEN", token)
    monkeypatch.setattr(urls, "AGENT_URL", "https://cp.example")
    monkeypatch.setattr(urls, "APP_URL", "https://cp.example")
    monkeypatch.setattr(webhooks, "BACKOFF", (0, 0, 0))
    monkeypatch.setattr(webhooks, "MAX_ATTEMPTS", 3)
    module = importlib.import_module("commonplace.phone_push")
    dispatcher = importlib.import_module("commonplace.phone_push.dispatcher")
    monkeypatch.setattr(dispatcher.PushDispatcher, "start", lambda self: None)
    module.install(hub.gateway)
    return hub.gateway.phone_push


def on(hub, owner):
    code, body = api(hub, owner, "POST", "/api/phone-push", {})
    assert code == 200, body
    return body["subscription"]


def off(hub, owner):
    code, body = api(hub, owner, "DELETE", "/api/phone-push")
    assert code == 200, body
    return body


def clock(monkeypatch, iso):
    def frozen(at=None):
        return iso

    monkeypatch.setattr(board_service, "stale_before", frozen)
    monkeypatch.setattr(display, "stale_before", frozen)


def agent(hub, owner, name, *, parent=None, unrestricted=False):
    if unrestricted:
        cookie = owner if isinstance(owner, str) else owner.cookie
        person = sessions.session_principal(hub.db, sessions.cookie_value(cookie))
        cid, token = hub.db.create_token(
            name=name,
            client="generic",
            actor=person.user_id,
            user_ref=person.user_id,
            workspace_ref=person.workspace_id,
            parent=parent if isinstance(parent, str) or parent is None else parent.id,
            policy=Policy.build(
                profile="full",
                trust="standard",
                scopes=["memory:read", "memory:write"],
                read_wings=["shared"],
                write_wings=["shared"],
            ),
        )
        return SimpleNamespace(id=cid, token=token)
    data = {"name": name}
    if parent is not None:
        data["parent"] = parent if isinstance(parent, str) else parent.id
    code, body = api(hub, owner, "POST", "/api/connections", data)
    assert code == 201, body
    return SimpleNamespace(id=body["connection"]["id"], token=body["token"])


def revoke(hub, owner, agent):
    code, body = api(hub, owner, "DELETE", f"/api/connections/{agent.id}")
    assert code == 200, body
    return body


def cli(*args):
    """Run ordinary CLI arguments, including --data; return stdout or a login URL."""
    proc = subprocess.run(
        [sys.executable, "-m", "commonplace", *(str(arg) for arg in args)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    if "login-link" in args:
        match = re.search(r"https?://\S+", proc.stdout)
        assert match, proc.stdout
        return match.group(0)
    return proc.stdout


def rows(hub, table):
    """Return plain lists of dictionaries from notification tables or cursor metadata."""
    assert table in {
        "phone_push_subscriptions",
        "phone_push_state",
        "phone_push_deliveries",
        "meta",
    }, table
    db = hub.gateway.db
    with db._lock:
        return [dict(row) for row in db._conn.execute(f"SELECT * FROM {table}").fetchall()]


def s1_counter(monkeypatch):
    counter = SimpleNamespace(calls=0)
    original = board_service.BoardService.needs_me

    def counted(self, *args, **kwargs):
        counter.calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(board_service.BoardService, "needs_me", counted)
    return counter
