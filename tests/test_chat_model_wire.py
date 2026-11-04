"""A18–A19: real HTTP wire contract, confined to a loopback stub server."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests._chat_helpers import chat_environment, no_outbound, optional  # noqa: F401


@contextmanager
def model_server(provider="openai_compatible", mode="ok"):
    requests = []
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": self.headers,
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            self.send_response(500 if mode == "http_500" else 200)
            self.send_header(
                "Content-Type", "text/plain" if mode == "non_json" else "application/json"
            )
            payload = (
                b"nope"
                if mode == "non_json"
                else b"PROVIDER-SECRET"
                if mode == "http_500"
                else json.dumps(
                    {"content": [{"type": "text", "text": "hi"}]}
                    if provider == "anthropic"
                    else {"choices": [{"message": {"content": "hi"}}]}
                ).encode()
            )
            if mode != "slow_drip":
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                if mode == "slow_drip":
                    while not stop.is_set():
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        stop.wait(0.2)
                else:
                    self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join()


def front_settings(provider, origin):
    types = optional("commonplace.chat.types")
    return types.FrontSettings(
        workspace_id="synthetic-workspace",
        enabled=True,
        provider=provider,
        model="fast-model",
        base_url=origin if provider == "openai_compatible" else None,
        key_ref=None,
        daily_limit=200,
        user_daily_limit=50,
        updated_by=None,
        updated_at=None,
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai_compatible"])
def test_wire_contract(monkeypatch, provider):
    model = optional("commonplace.chat.model")
    assert model is not None  # RED
    types = optional("commonplace.chat.types")
    connects = no_outbound(monkeypatch)
    turns = [{"role": "user", "content": "hello"}]
    monkeypatch.setenv("COMMONPLACE_CHAT_KEY_HOSTS", "127.0.0.1")
    with model_server(provider) as (origin, requests):
        monkeypatch.setattr(model, "ANTHROPIC_ORIGIN", origin)
        settings = front_settings(provider, origin)
        assert model.complete(settings, "k", "system", turns, max_tokens=5000) == "hi"
        assert len(requests) == 1
        request = requests[0]
        assert request["method"] == "POST"
        assert request["headers"]["Content-Type"].split(";")[0] == "application/json"
        body = request["body"]
        assert (
            body["model"] == "fast-model" and body["max_tokens"] == 300 and body["temperature"] == 0
        )
        if provider == "anthropic":
            assert request["path"] == "/v1/messages"
            assert request["headers"]["x-api-key"] == "k"
            assert request["headers"]["anthropic-version"] == "2023-06-01"
            assert set(body) == {"model", "max_tokens", "temperature", "system", "messages"}
            assert body["system"] == "system" and body["messages"] == turns
        else:
            assert request["path"] == "/v1/chat/completions"
            assert request["headers"]["Authorization"] == "Bearer k"
            assert set(body) == {"model", "max_tokens", "temperature", "stream", "messages"}
            assert body["stream"] is False
            assert body["messages"] == [{"role": "system", "content": "system"}, *turns]
            monkeypatch.delenv("COMMONPLACE_CHAT_KEY_HOSTS")
            with pytest.raises(types.ChatDenied):
                model.complete(settings, "k", "system", turns)
            assert len(requests) == 1
            # No Authorization header on a local endpoint without a key.
            assert model.complete(settings, None, "system", turns) == "hi"
            assert requests[-1]["headers"].get("Authorization") is None
        before = len(requests)
        with pytest.raises(ValueError):
            model.complete(settings, None, "s" * (types.MAX_INPUT_CHARS + 1), turns)
        assert len(requests) == before
    assert connects == []


@pytest.mark.parametrize("mode", ["non_json", "http_500", "slow_drip"])
def test_wire_errors_map_to_codes(monkeypatch, mode):
    model = optional("commonplace.chat.model")
    assert model is not None  # RED
    types = optional("commonplace.chat.types")
    connects = no_outbound(monkeypatch)
    with model_server(mode=mode) as (origin, requests):
        settings = front_settings("openai_compatible", origin)
        with pytest.raises(types.ModelError) as exc:
            model.complete(
                settings, None, "system", [{"role": "user", "content": "hello"}], timeout=0.5
            )
        assert (
            exc.value.code
            == {"non_json": "bad_response", "http_500": "http_error", "slow_drip": "timeout"}[mode]
        )
        assert "PROVIDER-SECRET" not in str(exc.value)
        assert len(requests) == 1
    assert connects == []
