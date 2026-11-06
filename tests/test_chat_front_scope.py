"""Project consent, bounded disclosure and local transport contracts."""

import inspect
import json
import socket
from dataclasses import replace

import pytest

from commonplace.chat import model, settings, store, worker
from tests._chat_helpers import chat_environment, chat_hub_fixture  # noqa: F401
from tests.test_chat_model_wire import front_settings, model_server
from tests.test_commonplace_boards import service


def put(h, **body):
    return h.settings(h.owner, body)[:2]


def seed_config(h, **overrides):
    with h.db._lock, h.db._tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_model_settings "
            "(workspace_id, scope, role, provider, model, base_url, updated_by, updated_at) "
            "VALUES (?, '*', 'front', ?, 'fast-model', ?, ?, '2026-01-01T00:00:00Z')",
            (
                h.workspace,
                overrides.get("provider", "openai_compatible"),
                overrides.get("base_url", "https://example.invalid"),
                h.admin,
            ),
        )


def test_r01_local_put(chat_hub):
    h = chat_hub
    status, body = put(h, front={"provider": "openai_compatible", "model": "small"}, mode="local")
    assert status == 200  # RED
    assert body["front"]["base_url"] == "http://127.0.0.1:11434"


def test_r02_scope(chat_hub):
    h = chat_hub
    status, _ = put(
        h,
        front={
            "provider": "openai_compatible",
            "model": "small",
            "base_url": "https://example.invalid",
        },
        scope="work",
        mode="hosted",
    )
    assert status == 200  # RED
    assert "board" in inspect.signature(settings.front).parameters
    assert settings.front(h.db, h.workspace, "work").provider == "openai_compatible"
    assert settings.front(h.db, h.workspace, "harbor").provider == "none"


def test_r03_hosted_context(chat_hub):
    h = chat_hub
    seed_config(h, provider="anthropic", base_url=None)
    owner = service(h, h.person(h.admin))
    owner.task_add("PRIVATE CARD", board="work")
    h.post(h.owner, "earlier exact words")
    mid = h.post(h.owner, "hello")
    job = store.claim(h.db)
    # The receipt may deliberately decline to enqueue inherited hosted settings.
    if job is None:
        from commonplace.chat.types import Job

        job = Job(h.workspace, "work", mid, h.admin, "pending", "", False, "", "")
    _, turns = worker.build_context(h.gateway, h.person(h.admin), replace(job, message_id=mid))
    content = turns[0]["content"]
    assert "Card (quoted data)" not in content  # RED
    assert "Start jobs" not in content and "Decisions waiting" not in content
    assert "user:" not in content and "earlier exact words" in content


def test_r04_probe_owner(chat_hub):
    h = chat_hub
    assert h.get(h.member, "/api/chat-settings?probe=1")[0] == 403  # RED


def test_r05_local_marker(chat_hub, monkeypatch):
    h = chat_hub
    seed_config(h, base_url="http://127.0.0.1:9")
    h.fake_model(monkeypatch)
    mid = h.post(h.owner, "hello")
    h.drain()
    replies = h.chat(h.owner)[1]["replies"]
    answers = [r for r in replies if r["kind"] == "answer" and r["message_id"] == mid]
    assert answers
    assert answers[0]["model"].startswith("local:")  # RED
    seed_config(h, base_url="https://example.invalid")
    # Explicit board consent, seeded without using the new API signature.
    with h.db._lock, h.db._tx() as conn:
        conn.execute(
            "UPDATE chat_model_settings SET scope='work' WHERE workspace_id=?", (h.workspace,)
        )
    mid = h.post(h.owner, "hello again")
    h.drain()
    answer = next(
        r for r in h.chat(h.owner)[1]["replies"] if r["kind"] == "answer" and r["message_id"] == mid
    )
    before = answer["model"]
    seed_config(h, base_url="http://127.0.0.1:9")
    with h.db._lock, h.db._tx() as conn:
        conn.execute(
            "UPDATE chat_model_settings SET base_url='http://127.0.0.1:9' WHERE workspace_id=? AND scope='work'",
            (h.workspace,),
        )
    assert not before.startswith("local:")
    assert (
        next(r for r in h.chat(h.owner)[1]["replies"] if r["id"] == answer["id"])["model"] == before
    )


def test_r06_refusal_body(chat_hub):
    status, body = put(
        chat_hub,
        front={
            "provider": "openai_compatible",
            "model": "small",
            "base_url": "http://10.0.0.5:8080",
        },
        mode="local",
    )
    assert status == 400
    assert body == {"error": "not_loopback"}  # RED


def test_r07_inherited_hosted_no_call(chat_hub, monkeypatch):
    h = chat_hub
    with model_server() as (origin, calls):
        # A non-local stored host routed only to a counting loopback fake.
        seed_config(h, base_url=origin.replace("127.0.0.1", "model.ts.net"))
        real = socket.getaddrinfo
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, *a, **kw: real("127.0.0.1" if host == "model.ts.net" else host, *a, **kw),
        )
        mid = h.post(h.owner, "hello")
        # Also prove a job already queued before the settings changed is skipped.
        store.enqueue(h.db, h.workspace, "work", mid, h.admin)
        assert h.gateway.chat_worker.run_once()
        assert calls == []  # RED
        assert h.rows("chat_front_jobs")[0]["code"] == "off"
        status_code, body, _ = h.get(h.owner, "/api/chat-settings?board=work")
        assert status_code == 200
        assert "inherited" in body["front"] and "applies" in body["front"]
        assert body["front"]["inherited"] and not body["front"]["applies"]
        assert h.status(h.owner)[1]["front"]["mode"] == "off"
        assert (
            put(
                h,
                front={
                    "provider": "openai_compatible",
                    "model": "small",
                    "base_url": origin.replace("127.0.0.1", "model.ts.net"),
                },
                scope="work",
                mode="hosted",
            )[0]
            == 200
        )
        h.post(h.owner, "hello once enabled")
        h.drain()
        assert len(calls) == 1


def test_r08_hosted_options(chat_hub, monkeypatch):
    h = chat_hub
    monkeypatch.setenv("COMMONPLACE_CHAT_KEY_WORKSPACES", h.workspace)
    monkeypatch.setattr(model, "probe_models", lambda config: ([], False), raising=False)
    status, body, _ = h.get(h.owner, "/api/chat-settings?probe=1")
    assert status == 200
    assert "hosted" in body  # RED
    assert body["hosted"]
    assert any(
        k["ref"] == "env:COMMONPLACE_CHAT_KEY_TEST"
        for option in body["hosted"]
        for k in option["keys"]
    )
    import os

    assert os.environ["COMMONPLACE_CHAT_KEY_TEST"] not in json.dumps(body)
    assert all(
        "COMMONPLACE_" not in k["label"] for option in body["hosted"] for k in option["keys"]
    )


def test_r09_loopback_rebinding(chat_hub, monkeypatch, caplog):
    h = chat_hub
    assert callable(getattr(settings, "is_local", None))
    assert callable(getattr(model, "probe_models", None))
    assert (
        put(
            h,
            front={
                "provider": "openai_compatible",
                "model": "small",
                "base_url": "http://localhost:9",
            },
            mode="local",
        )[0]
        == 200
    )
    config = settings.front(h.db, h.workspace)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 9))],
    )
    attempts = []
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: attempts.append(a))
    assert model.probe_models(config) == ([], False)
    from commonplace.chat.types import ModelError

    with pytest.raises(ModelError):
        model.complete(
            config, None, "private message", [{"role": "user", "content": "private message"}]
        )
    assert attempts == []
    assert "private message" not in caplog.text
    assert "Local model address refused" in caplog.text


@pytest.mark.parametrize(
    "host", ["127.0.0.2", "::1", "localhost", "::ffff:127.0.0.1", "10.0.0.5", "model.ts.net"]
)
def test_r10_local_predicate(host):
    assert callable(getattr(settings, "is_local", None))
    url = "http://" + ("[" + host + "]" if ":" in host else host)
    assert settings.is_local(front_settings("openai_compatible", url)) == (
        host in {"127.0.0.2", "::1", "localhost"}
    )


def test_g01_invalid_model_does_not_write(chat_hub):
    h = chat_hub
    before = settings.front(h.db, h.workspace).public()
    code, body = put(
        h,
        front={"provider": "openai_compatible", "model": "", "base_url": "http://127.0.0.1:9"},
        mode="local",
    )
    assert code == 400 and body != {"error": "not_loopback"}
    assert settings.front(h.db, h.workspace).public() == before


def test_g02_member_cannot_write(chat_hub):
    h = chat_hub
    assert h.settings(h.member, {"front": {"provider": "none"}})[0] == 403


def test_g03_no_config_no_job(chat_hub):
    h = chat_hub
    h.post(h.owner, "hello")
    assert h.rows("chat_front_jobs") == []
    assert h.status(h.owner)[1]["front"]["mode"] == "basic"


@pytest.mark.parametrize("response_status", [200, 302])
def test_r11_probe_wire(response_status):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    assert callable(getattr(model, "probe_models", None))  # RED
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(self.path)
            payload = b'{"data":[{"id":"small"}]}'
            self.send_response(response_status)
            self.send_header("Location", "/redirected")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = front_settings(
            "openai_compatible", f"http://127.0.0.1:{server.server_port}/prefix"
        )
        assert model.probe_models(config) == (
            (["small"], True) if response_status == 200 else ([], False)
        )
        assert calls == ["/prefix/v1/models"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


# Fix-round consent and freshness proofs use existing HTTP and worker APIs.
def test_fix_r01_scoped_hosted_requires_turn_on(chat_hub, monkeypatch):
    h = chat_hub
    with model_server() as (origin, calls):
        url = origin.replace("127.0.0.1", "model.ts.net")
        real = socket.getaddrinfo
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, *a, **kw: real("127.0.0.1" if host == "model.ts.net" else host, *a, **kw),
        )
        front = {"provider": "openai_compatible", "model": "small", "base_url": url}
        code, _ = put(h, front=front, scope="work")
        h.post(h.owner, "hello before consent")
        h.drain()
        assert (code, len(calls)) == (400, 0)  # RED
        assert put(h, front=front, scope="work", mode="hosted")[0] == 200
        h.post(h.owner, "hello after consent")
        h.drain()
        assert len(calls) == 1


def test_fix_r02_local_provider_mismatch(chat_hub):
    code, body = put(chat_hub, front={"provider": "none", "model": "small"}, mode="local")
    assert code == 400
    assert body == {"error": "invalid_local_provider"}  # RED


def test_fix_r03_revoke_during_context_build(chat_hub, monkeypatch):
    h = chat_hub
    assert h.configure()[0] == 200
    calls = h.fake_model(monkeypatch)
    original = worker.build_context

    def build(*args):
        result = original(*args)
        settings.set_front(h.db, h.person(h.admin), {"front": {"provider": "none"}})
        return result

    monkeypatch.setattr(worker, "build_context", build)
    h.post(h.owner, "hello")
    h.drain()
    assert calls == []  # RED
    assert h.rows("chat_front_jobs")[0]["code"] == "off"


def test_fix_g01_legacy_workspace_hosted_write(chat_hub):
    code, _ = put(
        chat_hub,
        front={
            "provider": "openai_compatible",
            "model": "small",
            "base_url": "https://example.invalid",
        },
    )
    assert code == 200
