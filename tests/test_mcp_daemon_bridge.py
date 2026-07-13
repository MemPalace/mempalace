from __future__ import annotations

import argparse
import json
import threading
import time

import pytest

from mempalace import daemon, mcp_bridge


def _args(**overrides):
    values = {
        "palace": None,
        "backend": None,
        "read_only": False,
        "no_auto_start": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_bridge_identity_includes_palace_backend_readonly_and_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_COLLECTION_NAME", "drawers_custom")

    args = _args(
        palace=str(tmp_path / "palace"),
        backend="Chroma",
        read_only=True,
    )

    identity = mcp_bridge.build_daemon_identity(args)

    assert identity == {
        "palace_path": daemon.canonical_palace_path(str(tmp_path / "palace")),
        "backend": "chroma",
        "read_only": True,
        "collection_name": "drawers_custom",
    }


def test_bridge_health_validation_rejects_backend_mismatch(tmp_path):
    identity = {
        "palace_path": daemon.canonical_palace_path(str(tmp_path / "palace")),
        "backend": "qdrant",
        "read_only": False,
        "collection_name": "",
    }
    health = {
        "palace_path": identity["palace_path"],
        "backend": "chroma",
    }

    with pytest.raises(mcp_bridge.BridgeError, match="different backend"):
        mcp_bridge.validate_daemon_health(health, identity)


def test_bridge_health_validation_allows_blank_backend(tmp_path):
    identity = {
        "palace_path": daemon.canonical_palace_path(str(tmp_path / "palace")),
        "backend": "",
        "read_only": False,
        "collection_name": "",
    }

    mcp_bridge.validate_daemon_health(
        {"palace_path": identity["palace_path"], "backend": None},
        identity,
    )


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
    response = json.loads(mcp_bridge.bridge_error_response(7, "socket closed"))

    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 7
    assert response["error"]["code"] == -32000
    assert response["error"]["message"] == "MemPalace MCP bridge error"
    assert response["error"]["data"]["message"] == "socket closed"


def test_forward_request_posts_to_daemon_mcp_endpoint():
    calls = []

    class FakeClient:
        def request(self, method, path, body, *, timeout):
            calls.append((method, path, body, timeout))
            return {"response": {"jsonrpc": "2.0", "id": 1, "result": {}}}

    identity = {"palace_path": "/tmp/palace", "backend": "", "read_only": False}
    request = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    response = mcp_bridge.forward_request(FakeClient(), request, identity, timeout=12.5)

    assert response == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert calls == [
        (
            "POST",
            "/mcp",
            {"request": request, "identity": identity},
            12.5,
        )
    ]


def test_daemon_mcp_tools_call_requests_share_writer_lock(tmp_path):
    runtime = daemon.DaemonRuntime(str(tmp_path / "palace"))
    identity = {
        "palace_path": runtime.palace_path,
        "backend": "",
        "read_only": False,
        "collection_name": "",
    }

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

    runtime._mcp_handler = handler

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "a"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "b"}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "c"}},
    ]

    threads = [
        threading.Thread(
            target=runtime.handle_mcp_request,
            args=({"request": request, "identity": identity},),
        )
        for request in requests
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert max_active == 1
    assert sorted(entered) == [1, 2, 3]


def test_daemon_mcp_protocol_request_does_not_need_writer_lock(tmp_path):
    runtime = daemon.DaemonRuntime(str(tmp_path / "palace"))
    identity = {
        "palace_path": runtime.palace_path,
        "backend": "",
        "read_only": False,
        "collection_name": "",
    }

    def handler(request):
        return {"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}}

    runtime._mcp_handler = handler

    with runtime._writer_lock:
        response = runtime.handle_mcp_request(
            {
                "identity": identity,
                "request": {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            }
        )

    assert response == {"response": {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}}


def test_daemon_mcp_identity_mismatch_is_refused(tmp_path):
    runtime = daemon.DaemonRuntime(str(tmp_path / "palace"))
    runtime._mcp_handler = lambda request: {"jsonrpc": "2.0", "id": request["id"], "result": {}}

    good_identity = {
        "palace_path": runtime.palace_path,
        "backend": "",
        "read_only": False,
        "collection_name": "",
    }
    bad_identity = {
        "palace_path": runtime.palace_path,
        "backend": "",
        "read_only": True,
        "collection_name": "",
    }

    runtime.handle_mcp_request(
        {
            "identity": good_identity,
            "request": {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        }
    )

    with pytest.raises(daemon.DaemonError, match="different identity"):
        runtime.handle_mcp_request(
            {
                "identity": bad_identity,
                "request": {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            }
        )


def test_daemon_blank_bridge_backend_inherits_runtime_backend(tmp_path):
    runtime = daemon.DaemonRuntime(str(tmp_path / "palace"), backend="chroma")
    runtime._mcp_handler = lambda request: {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {},
    }
    blank_identity = {
        "palace_path": runtime.palace_path,
        "backend": "",
        "read_only": False,
        "collection_name": "",
    }

    first = runtime.handle_mcp_request(
        {
            "identity": blank_identity,
            "request": {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        }
    )
    second = runtime.handle_mcp_request(
        {
            "identity": {**blank_identity, "backend": "chroma"},
            "request": {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        }
    )

    assert first["response"]["id"] == 1
    assert second["response"]["id"] == 2
    assert runtime._mcp_identity["backend"] == "chroma"

    with pytest.raises(daemon.DaemonError, match="backend mismatch"):
        runtime.handle_mcp_request(
            {
                "identity": {**blank_identity, "backend": "qdrant"},
                "request": {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            }
        )


def test_daemon_mcp_jobs_and_mcp_tools_share_one_writer_lock(tmp_path, monkeypatch):
    runtime = daemon.DaemonRuntime(str(tmp_path / "palace"))
    identity = {
        "palace_path": runtime.palace_path,
        "backend": "",
        "read_only": False,
        "collection_name": "",
    }

    entered = threading.Event()
    release = threading.Event()
    tool_finished = threading.Event()

    runtime._mcp_handler = lambda request: (
        tool_finished.set() or {"jsonrpc": "2.0", "id": request["id"], "result": {}}
    )

    def hold_writer_lock():
        with runtime._writer_lock:
            entered.set()
            release.wait(timeout=2)

    holder = threading.Thread(target=hold_writer_lock)
    holder.start()
    assert entered.wait(timeout=2)

    tool_thread = threading.Thread(
        target=runtime.handle_mcp_request,
        args=(
            {
                "identity": identity,
                "request": {"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
            },
        ),
    )
    tool_thread.start()

    time.sleep(0.05)
    assert not tool_finished.is_set()

    release.set()
    holder.join(timeout=2)
    tool_thread.join(timeout=2)

    assert tool_finished.is_set()


def test_daemon_client_mcp_request_uses_mcp_endpoint(monkeypatch, tmp_path):
    client = object.__new__(daemon.DaemonClient)
    calls = []

    def fake_request(method, path, body, *, timeout):
        calls.append((method, path, body, timeout))
        return {"response": {"jsonrpc": "2.0", "id": 1, "result": {"pong": True}}}

    client.request = fake_request

    identity = {"palace_path": str(tmp_path), "backend": "", "read_only": False}
    request = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    response = client.mcp_request(request, identity, timeout=3.0)

    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"pong": True}}
    assert calls == [
        (
            "POST",
            "/mcp",
            {"request": request, "identity": identity},
            3.0,
        )
    ]
