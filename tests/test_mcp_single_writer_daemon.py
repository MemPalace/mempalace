from __future__ import annotations

import argparse
import sys
import threading
import time

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


def test_all_tools_call_requests_are_serialized():
    active = 0
    max_active = 0
    state_lock = threading.Lock()
    entered = []

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
