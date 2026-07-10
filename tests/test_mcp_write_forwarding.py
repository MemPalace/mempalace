"""Tests for opt-in MCP write forwarding to the daemon (#1646 / #1497).

When MEMPALACE_MCP_FORWARD_WRITES is set, mutating tool calls are submitted to
the write daemon as ``mcp_tool`` jobs instead of executing in-process, so the
MCP server never acquires the per-palace writer lease. These tests exercise
``handle_request`` end-to-end with a fake daemon client, mirroring the style of
the peer-writer guard tests in test_mcp_server.py.
"""

from types import SimpleNamespace

import pytest

from mempalace import mcp_server


WRITE_TOOL = "mempalace_add_drawer"

_WRITE_TOOL_ENTRY = {
    "description": "test write tool",
    "input_schema": {
        "type": "object",
        "properties": {
            "wing": {"type": "string"},
            "room": {"type": "string"},
            "content": {"type": "string"},
        },
    },
}


class _FakeClient:
    """Stand-in for daemon.DaemonClient: records submits, scripted results."""

    def __init__(self, wait_job=None, submit_exc=None, wait_exc=None):
        self.submitted = []
        self.wait_job = wait_job or {
            "id": "job-1",
            "state": "succeeded",
            "result": {"success": True, "ok": 1, "stdout": "noise", "exit_code": 0},
        }
        self.submit_exc = submit_exc
        self.wait_exc = wait_exc

    def submit(self, kind, payload, **kwargs):
        if self.submit_exc is not None:
            raise self.submit_exc
        self.submitted.append((kind, payload))
        return {"id": "job-1"}

    def wait(self, job_id, timeout=None):
        if self.wait_exc is not None:
            raise self.wait_exc
        return self.wait_job


def _forbidden_lock():
    raise AssertionError("forwarding-enabled server must not acquire the writer lock")


def _install(monkeypatch, tmp_path, *, handler=None, client="unset", lock="allow"):
    """Wire the module for a forwarding test.

    handler: fake in-process handler for WRITE_TOOL (records direct dispatch)
    client:  _FakeClient instance, None (daemon down), or "unset" (forbid use)
    lock:    "allow" -> lock acquisition succeeds; "forbid" -> lock use asserts
    """
    called = {"direct": False}

    def _handler(**kwargs):
        called["direct"] = True
        return {"ok": "direct"}

    entry = dict(_WRITE_TOOL_ENTRY)
    entry["handler"] = handler or _handler
    monkeypatch.setitem(mcp_server.TOOLS, WRITE_TOOL, entry)
    monkeypatch.setattr(mcp_server, "_config", SimpleNamespace(palace_path=str(tmp_path)))
    monkeypatch.setattr(mcp_server, "_mcp_forward_mode_warned", False)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", True)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")

    if lock == "allow":
        monkeypatch.setattr(mcp_server, "_acquire_mcp_writer_lock", lambda: (True, ""))
    else:
        monkeypatch.setattr(mcp_server, "_acquire_mcp_writer_lock", _forbidden_lock)

    from mempalace import daemon as daemon_mod

    if client == "unset":

        def _forbidden_client(palace_path):
            raise AssertionError("daemon client must not be consulted in this test")

        monkeypatch.setattr(daemon_mod, "get_client_if_running", _forbidden_client)
    else:
        monkeypatch.setattr(daemon_mod, "get_client_if_running", lambda palace_path: client)

    return called


def _call(tool=WRITE_TOOL, arguments=None):
    if arguments is None:  # explicit None check: {} is a legitimate empty-args call
        arguments = {"content": "hello"}
    return mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )


def test_forwarding_off_by_default_uses_direct_path(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMPALACE_MCP_FORWARD_WRITES", raising=False)
    called = _install(monkeypatch, tmp_path, client="unset", lock="allow")

    response = _call()

    assert called["direct"] is True
    assert '"ok": "direct"' in response["result"]["content"][0]["text"]


def test_prefer_forwards_and_never_takes_the_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_MCP_FORWARD_WRITES", "prefer")
    client = _FakeClient()
    called = _install(monkeypatch, tmp_path, client=client, lock="forbid")

    response = _call()

    assert called["direct"] is False
    kind, payload = client.submitted[0]
    assert kind == "mcp_tool"
    assert payload["name"] == WRITE_TOOL
    assert payload["arguments"] == {"content": "hello"}
    text = response["result"]["content"][0]["text"]
    assert '"ok": 1' in text
    # Daemon job-transport keys must not leak into the tool result.
    assert "stdout" not in text
    assert "exit_code" not in text


def test_prefer_falls_back_to_direct_when_daemon_down(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_MCP_FORWARD_WRITES", "prefer")
    called = _install(monkeypatch, tmp_path, client=None, lock="allow")

    response = _call()

    assert called["direct"] is True
    assert '"ok": "direct"' in response["result"]["content"][0]["text"]


def test_require_refuses_when_daemon_down(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_MCP_FORWARD_WRITES", "require")
    called = _install(monkeypatch, tmp_path, client=None, lock="forbid")

    response = _call()

    assert called["direct"] is False
    assert response["error"]["code"] == -32004
    assert "require" in response["error"]["message"]
    assert response["error"]["data"]["tool"] == WRITE_TOOL


def test_no_direct_fallback_after_submission(monkeypatch, tmp_path):
    """Once the job is submitted the daemon owns the write: a wait failure must
    surface as an error, never retry in-process (double-write risk for
    non-idempotent mutations like diary_write)."""
    monkeypatch.setenv("MEMPALACE_MCP_FORWARD_WRITES", "prefer")
    client = _FakeClient(wait_exc=RuntimeError("daemon restarted mid-wait"))
    called = _install(monkeypatch, tmp_path, client=client, lock="forbid")

    response = _call()

    assert called["direct"] is False
    assert response["error"]["code"] == -32004
    assert "double-write" in response["error"]["message"]


def test_failed_daemon_job_surfaces_error(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_MCP_FORWARD_WRITES", "prefer")
    client = _FakeClient(
        wait_job={
            "id": "job-1",
            "state": "failed",
            "result": {"success": False, "error": "boom from daemon"},
        }
    )
    called = _install(monkeypatch, tmp_path, client=client, lock="forbid")

    response = _call()

    assert called["direct"] is False
    assert response["error"]["code"] == -32004
    assert "boom from daemon" in response["error"]["message"]


def test_read_tools_are_never_forwarded(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_MCP_FORWARD_WRITES", "prefer")
    _install(monkeypatch, tmp_path, client="unset", lock="forbid")
    monkeypatch.setitem(
        mcp_server.TOOLS,
        "mempalace_status",
        {
            "description": "test read tool",
            "input_schema": {"type": "object", "properties": {}},
            "handler": lambda: {"ok": True},
        },
    )

    response = _call(tool="mempalace_status", arguments={})

    assert "result" in response, response
    assert '"ok": true' in response["result"]["content"][0]["text"]


def test_maintenance_tools_keep_direct_path(monkeypatch, tmp_path):
    """mempalace_mine is in _MUTATING_TOOLS but classified 'maintenance' — the
    daemon's mcp_tool kind would reject it, so it must fall through to the
    direct path (the daemon has a dedicated 'mine' job kind via the CLI)."""
    monkeypatch.setenv("MEMPALACE_MCP_FORWARD_WRITES", "prefer")
    called = _install(monkeypatch, tmp_path, client="unset", lock="allow")
    entry = dict(_WRITE_TOOL_ENTRY)

    def _handler(**kwargs):
        called["direct"] = True
        return {"ok": "direct"}

    entry["handler"] = _handler
    monkeypatch.setitem(mcp_server.TOOLS, "mempalace_mine", entry)

    response = _call(tool="mempalace_mine", arguments={"content": "x"})

    assert called["direct"] is True
    assert '"ok": "direct"' in response["result"]["content"][0]["text"]


def test_diary_content_alias_resolves_before_forwarding(monkeypatch, tmp_path):
    """A diary_write call using the 'content' alias must forward with the
    resolved 'entry' argument, not the raw alias."""
    monkeypatch.setenv("MEMPALACE_MCP_FORWARD_WRITES", "prefer")
    client = _FakeClient()
    _install(monkeypatch, tmp_path, client=client, lock="forbid")
    monkeypatch.setitem(
        mcp_server.TOOLS,
        "mempalace_diary_write",
        {
            "description": "test diary tool",
            "input_schema": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string"},
                    "entry": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
            "handler": lambda **kw: {"success": True},
        },
    )

    _call(
        tool="mempalace_diary_write",
        arguments={"agent_name": "qa", "content": "aliased entry"},
    )

    _kind, payload = client.submitted[0]
    assert payload["arguments"]["entry"] == "aliased entry"
    assert "content" not in payload["arguments"]


def test_unrecognized_mode_value_stays_off(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_MCP_FORWARD_WRITES", "sometimes")
    called = _install(monkeypatch, tmp_path, client="unset", lock="allow")

    response = _call()

    assert called["direct"] is True
    assert '"ok": "direct"' in response["result"]["content"][0]["text"]


def test_require_mode_read_only_gate_still_wins(monkeypatch, tmp_path):
    """A server in read-only mode must refuse mutating tools, not forward them."""
    monkeypatch.setenv("MEMPALACE_MCP_FORWARD_WRITES", "require")
    _install(monkeypatch, tmp_path, client="unset", lock="forbid")
    monkeypatch.setattr(mcp_server, "_READ_ONLY", True)

    response = _call()

    assert response["error"]["code"] == -32003


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("prefer", "prefer"),
        ("require", "require"),
        ("1", "prefer"),
        ("true", "prefer"),
        ("", ""),
        ("nonsense", ""),
    ],
)
def test_forward_mode_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("MEMPALACE_MCP_FORWARD_WRITES", raw)
    monkeypatch.setattr(mcp_server, "_mcp_forward_mode_warned", False)
    assert mcp_server._mcp_forward_mode() == expected
