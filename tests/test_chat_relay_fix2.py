"""Storage retry coverage, inherited pipe deadlines and shared model validation."""

import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from commonplace.boards.service import BoardService
from commonplace.chat import store
from commonplace.runner import chat_agent, chat_context, chat_relay
from commonplace.runner.agent import _Hub
from tests._relay_helpers import relay_hub_fixture  # noqa: F401
from tests.test_chat_relay_fix1 import answers, fake_config, leased, queued


@pytest.mark.parametrize("error", [sqlite3.OperationalError, OSError])
def test_answer_poster_storage_failure_is_retryable(relay_hub, monkeypatch, error):
    h = relay_hub
    machine, message, body = leased(h)

    def busy(*args, **kwargs):
        raise error("storage busy")

    with monkeypatch.context() as patch:
        patch.setattr(BoardService, "space_read", busy)
        status, _ = h.answer(machine, body["job"], body["lease_token"], "One answer.")
    assert status == 503  # RED
    assert h.jobs()[0]["state"] == "leased"
    assert h.answer(machine, body["job"], body["lease_token"], "One answer.")[0] == 200
    rows = [r for r in answers(h) if r.message_id == message and r.kind == "answer"]
    assert len(rows) == 1 and rows[0].text == "One answer."


@pytest.mark.parametrize("stored", [False, True], ids=["before_insert", "after_insert"])
def test_answer_add_oserror_is_retryable_and_unique(relay_hub, monkeypatch, stored):
    h = relay_hub
    machine, message, body = leased(h)
    original = store.ReplyStore.add

    def busy(self, **kwargs):
        if stored:
            original(self, **kwargs)
        raise OSError("storage unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(store.ReplyStore, "add", busy)
        status, _ = h.answer(machine, body["job"], body["lease_token"], "One answer.")
    assert status == 503  # RED
    assert h.jobs()[0]["state"] == "leased"
    assert h.answer(machine, body["job"], body["lease_token"], "One answer.")[0] == 200
    assert h.answer(machine, body["job"], body["lease_token"], "Duplicate")[0] == 409
    rows = [r for r in answers(h) if r.message_id == message and r.kind == "answer"]
    assert len(rows) == 1 and rows[0].text == "One answer."


def test_lease_finish_storage_failure_does_not_escape(relay_hub, monkeypatch, caplog):
    h = relay_hub
    machine, _ = queued(h)

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("private storage details")

    with monkeypatch.context() as patch:
        patch.setattr(chat_context, "build", busy)
        patch.setattr(chat_relay, "_finish", busy)
        status, _ = h.poll(machine)
    assert status == 204  # RED
    assert h.jobs()[0]["state"] == "leased"
    assert "chat relay finish failed" in caplog.text
    assert "private storage details" not in caplog.text
    chat_relay.sweep(h.db, time.time() + chat_relay.CHAT_LEASE_S + 1)
    assert (h.jobs()[0]["state"], h.jobs()[0]["code"]) == ("failed", "lease_expired")


def cli_config(code, *args):
    return {
        "clis": {
            "example-cli": {
                "argv": [sys.executable, "-c", code, *map(str, args)],
                "default_model": "example-model",
            }
        }
    }


@pytest.mark.parametrize("held_pipe", ["stdout", "stdin"])
def test_cli_descendant_pipe_does_not_extend_timeout(tmp_path, monkeypatch, held_pipe):
    pid_path = tmp_path / "descendant.pid"
    output = ", stdout=subprocess.DEVNULL" if held_pipe == "stdin" else ""
    code = (
        "import subprocess, sys; from pathlib import Path; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(12)']"
        + output
        + "); Path(sys.argv[1]).write_text(str(child.pid)); sys.stdout.write('hi')"
    )
    chat = cli_config(code, pid_path)
    monkeypatch.setattr(chat_agent, "CLI_TIMEOUT_S", 1)
    prompt = "x" * 131_072 if held_pipe == "stdin" else "Question"
    start = time.monotonic()
    try:
        result = chat_agent._ask_cli(chat, "example-cli", None, "System", prompt)
        elapsed = time.monotonic() - start
    finally:
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
    assert pid_path.exists()
    assert elapsed < 4  # RED: the descendant holds its pipe for 12 seconds
    assert result == ("fail", "cli_timeout")


def test_cli_stdout_overflow_kills_before_timeout(monkeypatch):
    timeout = 2
    code = (
        "import sys, time; sys.stdout.buffer.write(b'x' * 70000); "
        f"sys.stdout.flush(); time.sleep({timeout})"
    )
    monkeypatch.setattr(chat_agent, "CLI_TIMEOUT_S", timeout)
    start = time.monotonic()
    result = chat_agent._ask_cli(cli_config(code), "example-cli", None, "System", "Question")
    elapsed = time.monotonic() - start
    assert result == ("fail", "bad_output")
    assert elapsed < timeout


def test_client_reports_cli_not_configured(relay_hub, tmp_path, monkeypatch):
    h = relay_hub
    machine, _, body = leased(h)
    config = fake_config(h, tmp_path)
    # The job requests claude; only example-cli is configured locally.
    body["cli"] = "claude"
    invoked = []
    monkeypatch.setattr(chat_agent, "_ask_cli", lambda *args: invoked.append(args))
    hub = _Hub(
        {"hub_url": config["hub_url"], "poll_seconds": chat_agent.POLL_S},
        machine["machine_token"],
    )
    chat_agent._reply(hub, config, body)
    assert invoked == []
    assert (h.jobs()[0]["state"], h.jobs()[0]["code"]) == ("failed", "cli_not_configured")
    assert all(r.kind != "answer" for r in answers(h))


def test_client_missing_credential_exits_three(relay_hub, tmp_path, monkeypatch):
    config = fake_config(relay_hub, tmp_path)
    monkeypatch.delenv(config["token_env"], raising=False)
    calls = []
    monkeypatch.setattr(_Hub, "post", lambda *args, **kwargs: calls.append(args))
    assert chat_agent._run(config, once=True) == 3
    assert calls == []


@pytest.mark.parametrize("model", ["--tools", "_example"])
def test_answer_route_rejects_flag_shaped_model(relay_hub, model):
    h = relay_hub
    machine, _, body = leased(h)
    status, _ = h.answer(machine, body["job"], body["lease_token"], "Answer", model=model)
    assert status == 400  # RED
    assert h.jobs()[0]["state"] == "leased"
    assert all(r.kind != "answer" for r in answers(h))


@pytest.mark.parametrize("model", ["--tools", "_example"])
def test_answer_relay_rejects_flag_shaped_model(relay_hub, model):
    h = relay_hub
    machine, _, body = leased(h)
    with h.db._lock:
        row = dict(
            h.db._conn.execute(
                "SELECT * FROM runner_machines WHERE id=?", (machine["machine_id"],)
            ).fetchone()
        )
    invalid = False
    try:
        chat_relay.answer(
            h.gateway, row, body["job"], body["lease_token"], "Answer", "claude", model, time.time()
        )
    except chat_relay.InvalidReply:
        invalid = True
    assert invalid  # RED
    assert h.jobs()[0]["state"] == "leased"
    assert all(r.kind != "answer" for r in answers(h))


def test_runner_docs_state_three_posts_total():
    text = (Path(__file__).resolve().parents[1] / "docs" / "runner.md").read_text()
    assert "at most three POSTs total (the initial POST plus two retries)" in text  # RED
