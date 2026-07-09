from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from mempalace import mcp_bridge, mcp_daemon


def test_socket_path_is_stable_private_and_short(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_MCP_DAEMON_STATE_ROOT", str(tmp_path))

    palace_a = tmp_path / "palace-a"
    palace_b = tmp_path / "palace-b"

    first = mcp_daemon.socket_path_for_palace(str(palace_a))
    second = mcp_daemon.socket_path_for_palace(str(palace_a))
    other = mcp_daemon.socket_path_for_palace(str(palace_b))

    assert first == second
    assert first != other
    assert first.name == "mcp.sock"
    assert first.parent.parent == tmp_path
    assert len(first.parent.name) == 24


def test_state_root_and_socket_override(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    socket_override = tmp_path / "custom.sock"

    monkeypatch.setenv(mcp_daemon.STATE_ROOT_ENV, str(state_root))
    assert mcp_daemon.state_root() == state_root

    monkeypatch.setenv(mcp_daemon.SOCKET_ENV, str(socket_override))
    assert mcp_daemon.socket_path_for_palace(str(tmp_path / "palace")) == socket_override


def test_json_error_includes_optional_data():
    response = mcp_daemon._json_error(
        "abc",
        -32600,
        "bad request",
        {"reason": "synthetic"},
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": "abc",
        "error": {
            "code": -32600,
            "message": "bad request",
            "data": {"reason": "synthetic"},
        },
    }


def test_prepare_socket_path_removes_stale_file(tmp_path, monkeypatch):
    socket_path = tmp_path / "state" / "mcp.sock"
    socket_path.parent.mkdir()
    socket_path.write_text("stale", encoding="utf-8")

    monkeypatch.setattr(mcp_daemon, "_socket_accepting", lambda path: False)

    mcp_daemon._prepare_socket_path(socket_path)

    assert socket_path.parent.exists()
    assert not socket_path.exists()


def test_prepare_socket_path_refuses_live_socket(tmp_path, monkeypatch):
    socket_path = tmp_path / "state" / "mcp.sock"
    socket_path.parent.mkdir()
    socket_path.write_text("live", encoding="utf-8")

    monkeypatch.setattr(mcp_daemon, "_socket_accepting", lambda path: True)

    with pytest.raises(SystemExit, match="already listening"):
        mcp_daemon._prepare_socket_path(socket_path)


def test_close_mcp_client_best_effort_invokes_available_cleanup_hooks():
    calls: list[str] = []

    class FakeClient:
        def persist(self):
            calls.append("persist")

        def close(self):
            calls.append("close")

    fake_module = SimpleNamespace(_client_cache=FakeClient())

    mcp_daemon._close_mcp_client_best_effort(fake_module)

    assert calls == ["persist", "close"]


def test_daemon_parse_args():
    args = mcp_daemon._parse_args(
        [
            "--socket",
            "/tmp/mempalace.sock",
            "--palace",
            "/tmp/palace",
            "--backend",
            "chroma",
            "--read-only",
        ]
    )

    assert args.socket == "/tmp/mempalace.sock"
    assert args.palace == "/tmp/palace"
    assert args.backend == "chroma"
    assert args.read_only is True


def test_all_tools_call_requests_are_serialized():
    active = 0
    max_active = 0
    state_lock = threading.Lock()
    entered: list[int] = []

    def handler(request):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            entered.append(request["id"])

        time.sleep(0.05)

        with state_lock:
            active -= 1

        return {"jsonrpc": "2.0", "id": request["id"], "result": {}}

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "mempalace_search"}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "mempalace_add_drawer"},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "mempalace_diary_write"},
        },
    ]

    threads = [
        threading.Thread(target=mcp_daemon.dispatch_request, args=(request, handler))
        for request in requests
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert max_active == 1
    assert sorted(entered) == [1, 2, 3]


def test_protocol_requests_do_not_need_serial_writer_lock():
    assert not mcp_daemon.request_needs_serial_writer({"method": "initialize", "id": 1})
    assert not mcp_daemon.request_needs_serial_writer({"method": "ping", "id": 2})
    assert not mcp_daemon.request_needs_serial_writer({"method": "tools/list", "id": 3})
    assert not mcp_daemon.request_needs_serial_writer({"method": "notifications/initialized"})
    assert mcp_daemon.request_needs_serial_writer({"method": "tools/call", "id": 4})


def test_bridge_truthy_env(monkeypatch):
    monkeypatch.setenv("MEMPALACE_TEST_BOOL", "YES")
    assert mcp_bridge._truthy_env("MEMPALACE_TEST_BOOL") is True

    monkeypatch.setenv("MEMPALACE_TEST_BOOL", "no")
    assert mcp_bridge._truthy_env("MEMPALACE_TEST_BOOL") is False


def test_bridge_parse_args():
    args = mcp_bridge._parse_args(
        [
            "--palace",
            "/tmp/palace",
            "--backend",
            "chroma",
            "--transport",
            "stdio",
            "--socket",
            "/tmp/mcp.sock",
            "--no-auto-start",
            "--daemon-timeout",
            "0.25",
            "--read-only",
        ]
    )

    assert args.palace == "/tmp/palace"
    assert args.backend == "chroma"
    assert args.transport == "stdio"
    assert args.socket == "/tmp/mcp.sock"
    assert args.no_auto_start is True
    assert args.daemon_timeout == 0.25
    assert args.read_only is True


def test_bridge_does_not_wait_for_json_rpc_notifications():
    assert (
        mcp_bridge.request_expects_response(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        is False
    )
    assert (
        mcp_bridge.request_expects_response({"jsonrpc": "2.0", "method": "ping", "id": 1}) is True
    )


def test_bridge_error_response_is_json_rpc_error():
    response = json.loads(mcp_bridge._bridge_error_response(7, "socket closed"))

    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 7
    assert response["error"]["code"] == -32000
    assert response["error"]["message"] == "MemPalace MCP bridge error"
    assert response["error"]["data"]["message"] == "socket closed"


def test_bridge_builds_daemon_command_without_recursing(tmp_path):
    args = argparse.Namespace(
        palace=str(tmp_path / "palace"),
        backend="chroma",
        read_only=True,
    )
    socket_path = tmp_path / "mcp.sock"

    cmd = mcp_bridge.build_daemon_command(args, socket_path)

    assert cmd[:3] == [sys.executable, "-m", "mempalace.mcp_daemon"]
    assert "--socket" in cmd
    assert str(socket_path) in cmd
    assert "--palace" in cmd
    assert "--backend" in cmd
    assert "chroma" in cmd
    assert "--read-only" in cmd
    assert "mempalace.mcp_bridge" not in cmd


def test_connect_or_start_uses_existing_daemon(tmp_path, monkeypatch):
    sentinel = object()
    args = argparse.Namespace(no_auto_start=False, daemon_timeout=0.1)

    monkeypatch.setattr(mcp_bridge, "_connect", lambda socket_path: sentinel)

    def fail_start(*_args, **_kwargs):
        raise AssertionError("start_daemon should not be called when connect succeeds")

    monkeypatch.setattr(mcp_bridge, "start_daemon", fail_start)

    result = mcp_bridge.connect_or_start(args, tmp_path / "mcp.sock", str(tmp_path / "palace"))

    assert result is sentinel


def test_connect_or_start_starts_daemon_after_initial_miss(tmp_path, monkeypatch):
    sentinel = object()
    calls = {"connect": 0, "start": 0}
    args = argparse.Namespace(no_auto_start=False, daemon_timeout=1.0)

    def fake_connect(_socket_path):
        calls["connect"] += 1
        if calls["connect"] == 1:
            raise mcp_bridge.BridgeError("not ready")
        return sentinel

    def fake_start(_args, _socket_path, _palace_path):
        calls["start"] += 1

    monkeypatch.setattr(mcp_bridge, "_connect", fake_connect)
    monkeypatch.setattr(mcp_bridge, "start_daemon", fake_start)

    result = mcp_bridge.connect_or_start(args, tmp_path / "mcp.sock", str(tmp_path / "palace"))

    assert result is sentinel
    assert calls == {"connect": 2, "start": 1}


def test_connect_or_start_respects_no_auto_start(tmp_path, monkeypatch):
    args = argparse.Namespace(no_auto_start=True, daemon_timeout=0.1)

    monkeypatch.setattr(
        mcp_bridge,
        "_connect",
        lambda _socket_path: (_ for _ in ()).throw(mcp_bridge.BridgeError("missing")),
    )

    with pytest.raises(mcp_bridge.BridgeError, match="missing"):
        mcp_bridge.connect_or_start(args, tmp_path / "mcp.sock", str(tmp_path / "palace"))
