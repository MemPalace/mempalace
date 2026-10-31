"""HubClient: discovery, authenticated requests, and the safe 401 retry."""

import io
import json
import os
import urllib.error
import urllib.request

import pytest

from mempalace import hub_client, server_registry
from mempalace.hub_client import HubClient


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _unauthorized(request):
    return urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO())


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MEMPALACE_MCP_HTTP_TOKEN", raising=False)
    monkeypatch.delenv(hub_client.HUB_FORWARD_ENV, raising=False)
    return tmp_path


def _live_record(monkeypatch, info):
    monkeypatch.setattr(server_registry, "read_live_serverinfo", lambda _palace: info)


def _write_palace_token(palace, token):
    path = server_registry.server_token_path(palace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")


def _capture_urlopen(monkeypatch, answer=b'{"jsonrpc": "2.0", "id": 1, "result": {}}'):
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(request)
        return _Response(answer)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


class TestDiscover:
    def test_no_hub_record_means_no_hub(self, isolated_home, monkeypatch):
        _live_record(monkeypatch, None)
        assert HubClient.discover(str(isolated_home / "palace")) is None

    def test_a_stale_record_for_a_dead_pid_means_no_hub(self, isolated_home):
        palace = str(isolated_home / "palace")
        path = server_registry.serverinfo_path(palace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": 2**31 - 2, "host": "127.0.0.1", "port": 8765}), encoding="utf-8"
        )
        assert HubClient.discover(palace) is None

    def test_a_process_never_forwards_to_itself(self, isolated_home, monkeypatch):
        _live_record(monkeypatch, {"pid": os.getpid(), "host": "127.0.0.1", "port": 1})
        assert HubClient.discover(str(isolated_home / "palace")) is None

    def test_the_kill_switch_turns_forwarding_off(self, isolated_home, monkeypatch):
        _live_record(monkeypatch, {"pid": -1, "host": "127.0.0.1", "port": 1})
        monkeypatch.setenv(hub_client.HUB_FORWARD_ENV, "0")
        assert HubClient.discover(str(isolated_home / "palace")) is None

    def test_no_palace_means_no_hub(self, isolated_home):
        assert HubClient.discover(None) is None

    def test_a_live_hub_is_dialled_on_loopback(self, isolated_home, monkeypatch):
        info = {"pid": -1, "host": "0.0.0.0", "port": 7, "capabilities": ["x"], "read_only": True}
        _live_record(monkeypatch, info)
        hub = HubClient.discover(str(isolated_home / "palace"))
        assert hub.base_url == "http://127.0.0.1:7"
        assert hub.pid == -1
        assert hub.capabilities == ["x"]
        assert hub.read_only is True


class TestRequests:
    def _hub(self, isolated_home):
        return HubClient(str(isolated_home / "palace"), {"host": "127.0.0.1", "port": 7})

    def test_a_tokenless_loopback_hub_gets_no_authorization(self, isolated_home, monkeypatch):
        seen = _capture_urlopen(monkeypatch)
        self._hub(isolated_home).post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        (request,) = seen
        assert request.get_header("Authorization") is None
        assert request.get_header("X-mempalace-exact") == "1"
        assert request.full_url == "http://127.0.0.1:7/mcp"

    def test_a_hub_that_requires_a_token_gets_the_palace_token(self, isolated_home, monkeypatch):
        _write_palace_token(str(isolated_home / "palace"), "palace-token")
        seen = _capture_urlopen(monkeypatch)
        self._hub(isolated_home).post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert seen[0].get_header("Authorization") == "Bearer palace-token"

    def test_a_401_is_retried_once_with_the_second_token(self, isolated_home, monkeypatch):
        _write_palace_token(str(isolated_home / "palace"), "stale-token")
        monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "current-token")
        attempts = []

        def answer(request):
            attempts.append(request.get_header("Authorization"))
            if len(attempts) == 1:
                raise _unauthorized(request)
            return _Response(b'{"ok": true}')

        _capture_urlopen(monkeypatch, answer)
        assert self._hub(isolated_home).post({}) == {"ok": True}
        assert attempts == ["Bearer stale-token", "Bearer current-token"]

    def test_a_401_with_no_other_token_is_raised(self, isolated_home, monkeypatch):
        _write_palace_token(str(isolated_home / "palace"), "only-token")
        _capture_urlopen(monkeypatch, lambda request: (_ for _ in ()).throw(_unauthorized(request)))
        with pytest.raises(urllib.error.HTTPError) as exc:
            self._hub(isolated_home).post({})
        assert exc.value.code == 401

    def test_a_request_body_is_the_exact_json_rpc_request(self, isolated_home, monkeypatch):
        seen = _capture_urlopen(monkeypatch)
        self._hub(isolated_home).call_tool("mempalace_search", {"query": "q"}, timeout=12)
        assert json.loads(seen[0].data) == {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mempalace_search", "arguments": {"query": "q"}},
        }

    def test_an_empty_body_is_none(self, isolated_home, monkeypatch):
        _capture_urlopen(monkeypatch, b"")
        assert self._hub(isolated_home).post({"method": "notifications/initialized"}) is None

    def test_healthy_reads_healthz(self, isolated_home, monkeypatch):
        seen = _capture_urlopen(monkeypatch, b"{}")
        assert self._hub(isolated_home).healthy() is True
        assert seen[0].full_url == "http://127.0.0.1:7/healthz"

    def test_an_unreachable_hub_is_not_healthy(self, isolated_home, monkeypatch):
        _capture_urlopen(monkeypatch, urllib.error.URLError("refused"))
        assert self._hub(isolated_home).healthy() is False


def test_a_mismatched_search_fingerprint_does_not_match(isolated_home):
    palace = str(isolated_home / "palace")
    assert (
        hub_client.search_fingerprint_matches({"search_config_fingerprint": "other"}, palace)
        is False
    )


def test_tool_result_reads_the_first_text_block():
    payload = {"result": {"content": [{"type": "text", "text": '{"answer": 1}'}]}}
    assert hub_client.tool_result(payload) == {"answer": 1}
    with pytest.raises(KeyError):
        hub_client.tool_result({"error": {"message": "no"}})
