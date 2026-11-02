"""A real gateway in front of a real in-process MemPalace worker, for tests."""

import http.client
import json
import secrets
import threading

import pytest

from commonplace.control_db import ControlDB
from commonplace.gateway import Gateway
from commonplace.policy import Policy
from commonplace.workers import Worker, WorkerSupervisor
from tests._mcp_server_helpers import _patch_mcp_server

REVIEW_QUESTION = "Should this change ship now or wait for the next release?"
REVIEW_CONTEXT = "The implementation is complete and the affected tests have passed."
REVIEW_OPTIONS = ["Proceed with the change", "Wait for more information"]


@pytest.fixture
def functional_worker_transport(monkeypatch):
    """Keep functional reads independent of the real startup socket budget.

    Logical deadline checks still run first. Only worker transport gets the
    Hub client's bounded allowance; explicit deadline tests use no shim.
    """
    from commonplace.gateway import WORKER_TIMEOUT_S
    from commonplace.rules import checks

    original = checks.remaining
    calls = 0

    def remaining(timeout):
        nonlocal calls
        bounded = original(timeout)
        if timeout == WORKER_TIMEOUT_S:
            calls += 1
            return 30.0
        return bounded

    monkeypatch.setattr(checks, "remaining", remaining)
    yield
    assert calls, "Functional acceptance must exercise the worker transport"


class InProcessLauncher:
    """Starts the production MCP HTTP server in this process, on loopback.

    MemPalace's MCP server holds one palace per process, so this launcher
    serves one workspace; tests that need two use a stub launcher.
    """

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.started = []

    def start(self, workspace_id, workspace_dir):
        from mempalace import mcp_server as mcp

        token = secrets.token_urlsafe(16)
        self.monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", token)
        self.monkeypatch.setenv("MEMPALACE_MCP_TRUSTED_IDENTITY", "1")
        self.monkeypatch.delenv("MEMPALACE_MCP_HTTP_ALLOW_INSECURE_NO_TOKEN", raising=False)
        httpd = mcp._build_http_server("127.0.0.1", 0)
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()

        def stop():
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        self.started.append(workspace_id)
        return Worker(workspace_id, "127.0.0.1", httpd.server_address[1], token, stop)


class Hub:
    """Control DB + gateway on an ephemeral port, plus an MCP client."""

    def __init__(self, data_dir, launcher):
        self.db = ControlDB(str(data_dir / "control.sqlite3"))
        self.supervisor = WorkerSupervisor(str(data_dir), launcher=launcher)
        self.gateway = Gateway(self.db, self.supervisor)
        self.server = self.gateway.build_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.admin = self.db.create_user("owner@example.com", is_admin=True)
        self.workspace = self.db.create_workspace("home", self.admin, personal=True)
        self._ids = 0

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.supervisor.stop_all()
        self.db.close()

    def token(self, name, user=None, workspace=None, client="test", **policy_kwargs):
        policy = Policy.build(**policy_kwargs)
        _cid, token = self.db.create_token(
            workspace_ref=workspace or self.workspace,
            user_ref=user or self.admin,
            name=name,
            client=client,
            policy=policy,
            actor=self.admin,
        )
        return token

    def owner_token(self, name="owner"):
        return self.token(
            name,
            profile="full",
            scopes=[
                "memory:read",
                "memory:write",
                "memory:delete",
                "kg:read",
                "kg:write",
                "events:read",
                "events:write",
                "private:read",
                "private:write",
            ],
            read_wings=["*"],
            write_wings=["*"],
            trust="trusted",
        )

    def raw(self, token, body, headers=None, method="POST", path="/mcp"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            hdrs = {"Content-Type": "application/json"}
            if token:
                hdrs["Authorization"] = f"Bearer {token}"
            hdrs.update(headers or {})
            conn.request(method, path, raw if method == "POST" else None, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()

    def rpc(self, token, method, params=None):
        self._ids += 1
        body = {"jsonrpc": "2.0", "id": self._ids, "method": method}
        if params is not None:
            body["params"] = params
        status, text = self.raw(token, body)
        assert status == 200, (status, text)
        return json.loads(text)

    def call(self, token, name, **arguments):
        """tools/call; returns (parsed result or None, full response text)."""
        response = self.rpc(token, "tools/call", {"name": name, "arguments": arguments})
        text = json.dumps(response, ensure_ascii=False)
        if "error" in response:
            return None, text
        return json.loads(response["result"]["content"][0]["text"]), text

    def tool_names(self, token):
        return {t["name"] for t in self.rpc(token, "tools/list")["result"]["tools"]}


@pytest.fixture(name="hub")
def hub_fixture(tmp_path, monkeypatch, config, kg):
    _patch_mcp_server(monkeypatch, config, kg)
    h = Hub(tmp_path / "data", InProcessLauncher(monkeypatch))
    try:
        yield h
    finally:
        h.close()
