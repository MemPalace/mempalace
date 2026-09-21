"""Tests for authenticated local hub discovery and JSON-RPC forwarding."""

import json

from mempalace import hub_client, server_registry


class _Response:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self._payload


def test_discover_hub_propagates_local_bearer_token(monkeypatch):
    monkeypatch.delenv(hub_client.HUB_FORWARD_ENV, raising=False)
    monkeypatch.setattr(server_registry, "read_live_serverinfo", lambda _: {"pid": -1})
    monkeypatch.setattr(server_registry, "client_base_url", lambda _: "http://127.0.0.1:7")
    monkeypatch.setattr(server_registry, "load_server_token", lambda _: "private-token")

    assert hub_client.discover_hub("C:/private-palace") == (
        "http://127.0.0.1:7",
        {"Content-Type": "application/json", "Authorization": "Bearer private-token"},
    )


def test_forward_json_rpc_serializes_request_and_decodes_response(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(b'{"jsonrpc":"2.0","id":"q","result":{"ok":true}}')

    monkeypatch.setattr(hub_client.urllib.request, "urlopen", fake_urlopen)

    response = hub_client.forward_json_rpc(
        "http://127.0.0.1:7",
        {"Authorization": "Bearer private-token"},
        {"jsonrpc": "2.0", "id": "q", "method": "ping"},
        timeout=12,
    )

    request = captured["request"]
    assert request.full_url == "http://127.0.0.1:7/mcp"
    assert request.get_header("Authorization") == "Bearer private-token"
    assert json.loads(request.data) == {"jsonrpc": "2.0", "id": "q", "method": "ping"}
    assert captured["timeout"] == 12
    assert response == {"jsonrpc": "2.0", "id": "q", "result": {"ok": True}}


def test_forward_json_rpc_returns_none_for_empty_notification_response(monkeypatch):
    monkeypatch.setattr(
        hub_client.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(b""),
    )

    assert (
        hub_client.forward_json_rpc(
            "http://127.0.0.1:7",
            {},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        is None
    )
