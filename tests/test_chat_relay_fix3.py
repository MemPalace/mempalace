"""Retry server errors in answer completion and both failure-report paths."""

import sqlite3
from types import SimpleNamespace

import pytest

from commonplace.runner import chat_agent, chat_relay
from commonplace.runner.agent import _Hub, _HubError
from tests._relay_helpers import relay_hub_fixture  # noqa: F401
from tests.test_chat_relay_fix1 import answers, fake_config


def client_job(h, tmp_path, monkeypatch):
    config = fake_config(h, tmp_path)
    machine = h.machine()
    assert h.poll(machine, ("example-cli",))[0] == 204
    message = h.post(h.owner, "Complete this job once.")
    status, body = h.poll(machine, ("example-cli",))
    assert status == 200
    clock, sleeps, posts = [0], [], []

    def delay(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(
        chat_agent, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=delay)
    )
    hub = _Hub(
        {"hub_url": config["hub_url"], "poll_seconds": chat_agent.POLL_S},
        machine["machine_token"],
    )
    original = hub.post

    def post(action, payload, **kwargs):
        posts.append((action, dict(payload)))
        return original(action, payload, **kwargs)

    monkeypatch.setattr(hub, "post", post)
    return config, machine, message, body, hub, sleeps, posts


def reply_error(hub, config, body):
    try:
        chat_agent._reply(hub, config, body)
    except _HubError as exc:
        return exc.status
    return None


def assert_retried_once(posts, sleeps, action):
    assert len(posts) == 2 and posts[0] == posts[1]
    assert posts[0][0] == action
    assert sleeps == [chat_agent.POLL_S]


def test_answer_retries_finish_done_storage_error(relay_hub, tmp_path, monkeypatch):
    h = relay_hub
    config, machine, message, body, hub, sleeps, posts = client_job(h, tmp_path, monkeypatch)
    monkeypatch.setattr(chat_agent, "_ask_cli", lambda *args: ("ok", "One answer."))
    original, finishes = chat_relay._finish, []

    def finish(conn, row, state, code, now):
        finishes.append(state)
        if state == "done" and len(finishes) == 1:
            raise sqlite3.OperationalError("storage busy")
        return original(conn, row, state, code, now)

    monkeypatch.setattr(chat_relay, "_finish", finish)
    error = reply_error(hub, config, body)
    assert h.jobs()[0]["state"] == "done"  # RED
    assert error is None and finishes == ["done", "done"]
    assert_retried_once(posts, sleeps, "answer")
    rows = [r for r in answers(h) if r.message_id == message and r.kind == "answer"]
    assert len(rows) == 1 and rows[0].text == "One answer."
    assert h.answer(machine, body["job"], body["lease_token"], "Duplicate")[0] == 409


def test_answer_retries_in_transaction_fence_storage_error(relay_hub, tmp_path, monkeypatch):
    h = relay_hub
    config, _, message, body, hub, sleeps, posts = client_job(h, tmp_path, monkeypatch)
    monkeypatch.setattr(chat_agent, "_ask_cli", lambda *args: ("ok", "One answer."))
    original, fences = chat_relay._fenced, []

    def fenced(*args):
        fences.append(True)
        if len(fences) == 2:
            raise sqlite3.OperationalError("storage busy")
        return original(*args)

    monkeypatch.setattr(chat_relay, "_fenced", fenced)
    error = reply_error(hub, config, body)
    assert h.jobs()[0]["state"] == "done"  # RED
    assert error is None and len(fences) == 4
    assert_retried_once(posts, sleeps, "answer")
    rows = [r for r in answers(h) if r.message_id == message and r.kind == "answer"]
    assert len(rows) == 1 and rows[0].text == "One answer."


@pytest.mark.parametrize("source", ["cli", "validation"])
def test_fail_retries_server_error(relay_hub, tmp_path, monkeypatch, source):
    h = relay_hub
    config, _, message, body, hub, sleeps, posts = client_job(h, tmp_path, monkeypatch)
    cli_calls = []

    def ask(*args):
        cli_calls.append(True)
        return "fail", "cli_failed"

    monkeypatch.setattr(chat_agent, "_ask_cli", ask)
    if source == "validation":
        body["cli"] = "missing-cli"
    original, failures = chat_relay.fail, []

    def fail(*args, **kwargs):
        failures.append(True)
        if len(failures) == 1:
            raise sqlite3.OperationalError("storage busy")
        return original(*args, **kwargs)

    monkeypatch.setattr(chat_relay, "fail", fail)
    error = reply_error(hub, config, body)
    assert h.jobs()[0]["state"] == "failed"  # RED
    assert error is None and len(failures) == 2
    assert h.jobs()[0]["code"] == ("cli_failed" if source == "cli" else "cli_not_configured")
    assert cli_calls == ([True] if source == "cli" else [])
    assert_retried_once(posts, sleeps, "fail")
    assert all(r.kind != "answer" for r in answers(h) if r.message_id == message)
