"""Regression proof for the first chat implementation review."""

import socket
import subprocess
import threading

import pytest

from tests._chat_helpers import chat_environment, chat_hub_fixture  # noqa: F401
from tests._console_helpers import optional, successful
from tests.test_chat_model_wire import front_settings


def test_route_ui():
    result = subprocess.run(
        ["node", "--test", "tests/commonplace_chat_fix1.mjs"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr  # RED


def test_header_trickle():
    model = optional("commonplace.chat.model")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def serve():
        with listener.accept()[0] as conn:
            conn.recv(65536)
            try:
                conn.sendall(b"HTTP/1.1 200 OK\r\nX-Test: ")
                for _ in range(8):
                    if stop.wait(0.1):
                        break
                    conn.sendall(b"a")
            except OSError:
                pass

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        with pytest.raises(optional("commonplace.chat.types").ModelError) as exc:
            model.complete(
                front_settings(
                    "openai_compatible", "http://127.0.0.1:%d" % listener.getsockname()[1]
                ),
                None,
                "system",
                [],
                timeout=0.5,
            )
        assert exc.value.code == "timeout"  # RED
    finally:
        stop.set()
        thread.join()
        listener.close()


def test_receipt_failure_preserves_handoff(chat_hub, monkeypatch):
    h = chat_hub
    store = optional("commonplace.chat.store")
    with monkeypatch.context() as patch:
        patch.setattr(store, "open_replies", lambda *a: (_ for _ in ()).throw(TimeoutError()))
        mid = h.post(h.owner, "Please implement durable handoffs")
    assert len(h.handoffs()) == 1  # RED
    h.drain(now=h.late())
    assert len(h.cards(mid)) == 1


def test_transient_filing_retries(chat_hub, monkeypatch):
    from commonplace.boards.service import BoardService

    h = chat_hub
    mid = h.post(h.owner, "Please implement retry handling")
    original = BoardService.task_add
    calls = []

    def add(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BoardService, "task_add", add)
    try:
        h.drain(now=h.late())
    except RuntimeError:
        pass
    assert h.handoffs()[0]["state"] == "waiting"  # RED
    h.drain(now=h.late() + 61)
    assert len(h.cards(mid)) == 1


def test_answer_redaction_kinds(chat_hub, monkeypatch):
    from commonplace import redact

    h = chat_hub
    text = "token sk-" + "x" * 48
    _, kinds = redact.redact(text)
    assert kinds
    h.configure()
    h.fake_model(monkeypatch, reply=text)
    h.post(h.owner, "Please explain the project")
    h.drain()
    page = h.chat(h.owner)[1]
    answer = next(r for r in page["replies"] if r["kind"] == "answer")
    assert answer["redacted"] == sorted(set(kinds))  # RED


def test_linked_and_me(chat_hub):
    h = chat_hub
    mid = h.post(h.owner, "Please implement linked cards")
    card = successful(h, h.t1, "task_add", board="work", title="Linked", message_id=mid)
    page = h.chat(h.owner)[1]
    assert [c["number"] for c in page.get("linked", [])] == [card["number"]]  # RED
    assert page["me"]["user"] == h.admin


@pytest.mark.parametrize("model", ["bad model", "x" * 121])
def test_invalid_model(chat_hub, model):
    result = chat_hub.configure(model=model)
    assert result[0] == 400  # RED
    assert result[1] == {"error": "Invalid chat settings"}
