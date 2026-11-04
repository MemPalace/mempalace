"""Regression proof for the final chat fix round."""

import json
import socket
import time

from tests._chat_helpers import chat_environment, chat_hub_fixture  # noqa: F401
from tests._console_helpers import optional, successful
from tests.test_chat_model_wire import front_settings


def test_linked_payload_has_no_secret(chat_hub):
    h = chat_hub
    secret = "sk-" + "x" * 48
    h.post(h.owner, "Please fix authentication with " + secret)
    h.drain(now=h.late())
    page = h.chat(h.owner)[1]
    assert secret not in json.dumps(page)  # RED
    assert set(page["linked"][0]) == {"number", "message_id", "status", "title"}


def test_drain_stops_on_settle_exception(chat_hub, monkeypatch):
    h = chat_hub
    h.post(h.owner, "Please fix claim errors")
    calls = []

    def settle(*args):
        calls.append(1)
        raise RuntimeError("claim failed")

    monkeypatch.setattr(optional("commonplace.chat.handoff"), "settle", settle)
    count = h.drain(now=h.late())
    assert len(calls) == 1  # RED
    assert count == 0
    assert h.handoffs()[0]["state"] == "waiting"


def test_tool_error_is_terminal(chat_hub, monkeypatch):
    from commonplace.boards.service import BoardService
    from commonplace.extensions import ToolError

    h = chat_hub
    h.post(h.owner, "Please fix terminal refusals")
    calls = []

    def add(*args, **kwargs):
        calls.append(1)
        raise ToolError("Denied")

    monkeypatch.setattr(BoardService, "task_add", add)
    h.drain(now=h.late())
    assert h.handoffs()[0]["state"] == "failed"  # RED
    h.drain(now=h.late() + 120)
    assert len(calls) == 1
    replies = h.chat(h.owner)[1]["replies"]
    assert len([r for r in replies if r["code"] == "file_failed"]) == 1


def test_cursor_poll_includes_late_link(chat_hub):
    h = chat_hub
    mid = h.post(h.owner, "Please implement late linking")
    page = h.chat(h.owner)[1]
    card = successful(h, h.t1, "task_add", board="work", title="Linked", message_id=mid)
    poll = h.chat(h.owner, after=page["cursor"])[1]
    assert [c["number"] for c in poll.get("linked", [])] == [card["number"]]  # RED
    assert poll["messages"] == []


def test_connect_addresses_share_deadline(monkeypatch):
    model = optional("commonplace.chat.model")
    types = optional("commonplace.chat.types")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 9))] * 3,
    )

    class Timer:
        def __init__(self, *args):
            pass

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(model.threading, "Timer", Timer)

    def refuse(sock, address):
        time.sleep(sock.gettimeout())
        raise ConnectionRefusedError()

    monkeypatch.setattr(socket.socket, "connect", refuse)
    code = None
    try:
        model.complete(
            front_settings("openai_compatible", "http://127.0.0.1:9"),
            None,
            "system",
            [],
            timeout=0.5,
        )
    except types.ModelError as exc:
        code = exc.code
    assert code == "timeout"  # RED
