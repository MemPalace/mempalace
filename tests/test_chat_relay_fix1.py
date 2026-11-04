"""Storage recovery, runner failures, bounded output and accurate status."""

import json
import socket
import sqlite3
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace.boards.service import BoardService
from commonplace.chat import store
from commonplace.extensions import ToolContext
from commonplace.runner import chat_agent, chat_context, chat_relay
from commonplace.runner.agent import _Hub, _HubError
from tests._relay_helpers import relay_hub_fixture  # noqa: F401
from tests.runner_harness import _private_json, runner


def queued(h):
    machine = h.machine()
    assert h.poll(machine)[0] == 204
    message = h.post(h.owner, "Please answer this question.")
    return machine, message


def leased(h):
    machine, message = queued(h)
    status, body = h.poll(machine)
    assert status == 200
    return machine, message, body


def answers(h):
    return store.open_replies(h.gateway, h.workspace).page(board="work", for_user=h.admin)


def fake_config(h, tmp_path, *flags):
    return {
        "hub_url": f"http://127.0.0.1:{h.hub.port}",
        "token_env": "COMMONPLACE_RUNNER_TOKEN",
        "chat": {
            "default_cli": "example-cli",
            "context": "minimal",
            "relay_always": False,
            "clis": {
                "example-cli": {
                    "argv": [
                        sys.executable,
                        str(Path(__file__).with_name("runner_fake_cli.py").resolve()),
                        "--record",
                        str(tmp_path / "record.json"),
                        "--model",
                        "{model}",
                        *flags,
                    ],
                    "default_model": "example-model",
                    "tools": True,
                }
            },
        },
    }


def test_lease_poster_storage_error_finishes_job(relay_hub, monkeypatch):
    h = relay_hub
    m, _ = queued(h)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("storage busy")

    monkeypatch.setattr(BoardService, "space_read", broken)
    assert h.poll(m)[0] == 204  # RED
    assert (h.jobs()[0]["state"], h.jobs()[0]["code"]) == ("failed", "bad_output")
    assert h.poll(m)[0] == 204


def test_lease_context_error_finishes_job(relay_hub, monkeypatch):
    h = relay_hub
    m, _ = queued(h)

    def broken(*args, **kwargs):
        raise ValueError("invalid context")

    monkeypatch.setattr(chat_context, "build", broken)
    assert h.poll(m)[0] == 204
    assert (h.jobs()[0]["state"], h.jobs()[0]["code"]) == ("failed", "bad_output")


@pytest.mark.parametrize("error", [TimeoutError, sqlite3.OperationalError])
@pytest.mark.parametrize("stored", [False, True], ids=["before_insert", "after_insert"])
def test_answer_storage_error_preserves_lease_and_retry_is_unique(
    relay_hub, monkeypatch, error, stored
):
    h = relay_hub
    m, message, body = leased(h)
    original = store.ReplyStore.add

    def busy(self, **kwargs):
        if stored:
            original(self, **kwargs)
        raise error("storage busy")

    with monkeypatch.context() as patch:
        patch.setattr(store.ReplyStore, "add", busy)
        status, _ = h.answer(m, body["job"], body["lease_token"], "One answer.")
    assert status == 503  # RED
    assert h.jobs()[0]["state"] == "leased"
    assert h.answer(m, body["job"], body["lease_token"], "One answer.")[0] == 200
    assert h.answer(m, body["job"], body["lease_token"], "Repeated answer.")[0] == 409
    rows = [r for r in answers(h) if r.message_id == message and r.kind == "answer"]
    assert len(rows) == 1 and rows[0].text == "One answer."


@pytest.mark.parametrize("busy_count", [1, 2, 3])
def test_client_answer_503_uses_at_most_three_posts(relay_hub, tmp_path, monkeypatch, busy_count):
    h = relay_hub
    config = fake_config(h, tmp_path)
    m = h.machine()
    h.poll(m, ("example-cli",))
    h.post(h.owner, "Retry this answer.")
    status, body = h.poll(m, ("example-cli",))
    assert status == 200
    calls, delays = [], []
    original = store.ReplyStore.add

    def busy(self, **kwargs):
        calls.append(kwargs)
        if len(calls) <= busy_count:
            raise TimeoutError("storage busy")
        return original(self, **kwargs)

    monkeypatch.setattr(store.ReplyStore, "add", busy)
    monkeypatch.setattr(chat_agent, "_ask_cli", lambda *args: ("ok", "One answer."))
    monkeypatch.setattr(
        chat_agent, "time", SimpleNamespace(monotonic=lambda: 0, sleep=delays.append)
    )
    hub = _Hub(
        {"hub_url": config["hub_url"], "poll_seconds": chat_agent.POLL_S}, m["machine_token"]
    )
    result = None
    try:
        chat_agent._reply(hub, config, body)
    except _HubError as exc:
        result = exc.status
    assert len(calls) == min(busy_count + 1, 3)  # RED
    assert result == (503 if busy_count == 3 else None)
    assert len(delays) == min(busy_count, 2)
    assert all(delay <= chat_agent.POLL_S for delay in delays)
    assert all(call == calls[0] for call in calls)
    assert h.jobs()[0]["state"] == ("leased" if busy_count == 3 else "done")


@pytest.mark.parametrize("expires_in", [2, 4])
def test_client_does_not_retry_after_lease_deadline(monkeypatch, expires_in):
    calls, delays = [], []
    clock = [0]

    def post(route, body, **kwargs):
        calls.append(route)
        raise _HubError("transient", 503)

    def delay(seconds):
        delays.append(seconds)
        clock[0] += expires_in

    monkeypatch.setattr(
        chat_agent, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=delay)
    )
    monkeypatch.setattr(chat_agent, "_ask_cli", lambda *args: ("ok", "Answer"))
    config = {
        "token_env": "EXAMPLE_TOKEN",
        "chat": {
            "default_cli": "example-cli",
            "clis": {"example-cli": {"default_model": "example-model"}},
        },
    }
    job = {
        "job": "chat_0123456789abcdef",
        "lease_token": "synthetic-lease",
        "system": "System",
        "prompt": "Question",
        "expires_in": expires_in,
    }
    with pytest.raises(_HubError):
        chat_agent._reply(SimpleNamespace(post=post), config, job)
    assert calls == ["answer"]
    assert all(seconds <= chat_agent.POLL_S for seconds in delays)


def archive_board(h):
    service = BoardService(ToolContext(principal=h.person(h.admin), gateway=h.gateway))
    with service.store.connection() as conn:
        conn.execute("UPDATE boards SET archived=1 WHERE slug='work'")
    with pytest.raises(Exception, match="board not found"):
        service.space_read("work")


def test_no_access_skips_at_lease(relay_hub):
    h = relay_hub
    m, _ = queued(h)
    archive_board(h)
    assert h.poll(m)[0] == 204
    assert (h.jobs()[0]["state"], h.jobs()[0]["code"]) == ("skipped", "no_access")
    assert all(r.kind != "answer" for r in answers(h))


def test_no_access_fails_at_answer(relay_hub):
    h = relay_hub
    m, _, body = leased(h)
    archive_board(h)
    assert h.answer(m, body["job"], body["lease_token"], "Answer")[0] == 409
    assert (h.jobs()[0]["state"], h.jobs()[0]["code"]) == ("failed", "no_access")
    assert all(r.kind != "answer" for r in answers(h))


@pytest.mark.parametrize(
    ("flags", "code"),
    [
        (("--exit", "7"), "cli_failed"),
        (("--sleep", "0.5"), "cli_timeout"),
        (("--empty",), "bad_output"),
    ],
)
def test_client_cli_failures_are_recorded(relay_hub, tmp_path, monkeypatch, flags, code):
    h = relay_hub
    config = fake_config(h, tmp_path, *flags)
    if code == "cli_timeout":
        monkeypatch.setattr(chat_agent, "CLI_TIMEOUT_S", 0.5)
    m = h.machine()
    assert h.poll(m, ("example-cli",))[0] == 204
    h.post(h.owner, "Report CLI failure.")
    monkeypatch.setenv(config["token_env"], m["machine_token"])
    assert chat_agent._run(config, once=True) == 0
    assert (h.jobs()[0]["state"], h.jobs()[0]["code"]) == ("failed", code)
    assert all(r.kind != "answer" for r in answers(h))


def test_once_hub_down_exits_three(relay_hub, tmp_path, monkeypatch):
    config = fake_config(relay_hub, tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        config["hub_url"] = f"http://127.0.0.1:{sock.getsockname()[1]}"
        monkeypatch.setenv(config["token_env"], "synthetic-machine-credential")
        assert chat_agent._run(config, once=True) == 3  # RED


@pytest.mark.parametrize(
    ("kind", "status", "expected"),
    [("credential", 401, 3), ("transient", 503, 5), ("refused", 404, 5), ("fenced", 409, 0)],
)
def test_once_hub_exit_codes(relay_hub, tmp_path, monkeypatch, kind, status, expected):
    def post(*args, **kwargs):
        raise _HubError(kind, status)

    config = fake_config(relay_hub, tmp_path)
    monkeypatch.setenv(config["token_env"], "synthetic-machine-credential")
    monkeypatch.setattr(_Hub, "post", post)
    assert chat_agent._run(config, once=True) == expected


def test_context_wire_size_and_newest_line_last(monkeypatch):
    newest = "\x01" * 9_000 + "\nNewest line"
    service = SimpleNamespace(
        board_list=lambda: [{"slug": "work", "name": "Example project"}],
        task_list=lambda board: [
            {"number": i, "title": "\x02" * 12_000, "status": "todo"} for i in range(15)
        ],
        space_read=lambda *args, **kwargs: {"messages": [{"id": 1, "text": newest}]},
        store=SimpleNamespace(
            connection=lambda: nullcontext(
                SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: (0,)))
            )
        ),
    )
    monkeypatch.setattr(chat_context, "BoardService", lambda *args: service)
    system, prompt = chat_context.build(None, None, "work", 1, "minimal")
    wire = json.dumps({"system": system, "prompt": prompt}, ensure_ascii=False).encode()
    assert len(wire) <= 49_152
    assert prompt.endswith("\nNewest line")
    assert "\x01" in prompt
    assert prompt.splitlines()[-1] == "Newest line"


def test_status_does_not_guess_default_cli(relay_hub):
    h = relay_hub
    m = h.machine()
    assert h.poll(m, ("codex", "claude"))[0] == 204
    h.post(h.owner, "Which CLI answers?")
    assert h.status(h.owner)[1]["front"]["runner"]["cli"] is None  # RED
    assert h.jobs()[0]["cli"] is None


def test_status_defers_to_active_main(relay_hub):
    h = relay_hub
    m = h.machine()
    h.poll(m)
    status, connected = h.connect(h.owner, "work")
    assert status == 201
    h.subscribe(connected["token"])
    assert h.mcp(connected["token"], "space_read", board="work")[1]["ok"]
    h.post(h.owner, "Let the main answer.")
    front = h.status(h.owner)[1]["front"]
    assert front["mode"] == "basic"  # RED
    assert front["runner"]["online"] and h.jobs() == []


def test_full_context_egress_describes_title_limit():
    assert "up to 15 visible card titles" in chat_agent.EGRESS_FULL_LINE  # RED
    root = Path(__file__).resolve().parents[1]
    for doc in ("runner.md", "chat.md"):
        text = (root / "docs" / doc).read_text()
        assert "up to 15 visible card titles" in text
        assert "all visible card titles" not in text


@pytest.mark.parametrize("size", [65_537, 131_072])
def test_cli_stdout_overrun_is_bad_output(size):
    chat = {
        "clis": {
            "example-cli": {
                "argv": [
                    sys.executable,
                    "-c",
                    f"import sys; sys.stdout.buffer.write(b'x' * {size})",
                ],
                "default_model": "example-model",
            }
        }
    }
    result = chat_agent._ask_cli(chat, "example-cli", None, "System", "Question")
    assert result == ("fail", "bad_output")  # RED


@pytest.mark.parametrize("size", [65_535, 65_536])
def test_cli_stdout_within_bound_is_accepted(size):
    chat = {
        "clis": {
            "example-cli": {
                "argv": [
                    sys.executable,
                    "-c",
                    f"import sys; sys.stdout.buffer.write(b'x' * {size})",
                ],
                "default_model": "example-model",
            }
        }
    }
    assert chat_agent._ask_cli(chat, "example-cli", None, "System", "Question") == (
        "ok",
        "x" * 2_000,
    )


def test_returned_answer_documentation():
    text = (Path(__file__).resolve().parents[1] / "docs" / "runner.md").read_text()
    assert "returned answer at most 8,000 bytes" in text  # RED


def test_reply_rejects_flag_shaped_model(relay_hub, tmp_path, monkeypatch):
    config = fake_config(relay_hub, tmp_path)
    called, posted = [], []
    monkeypatch.setattr(
        chat_agent, "_ask_cli", lambda *args: (called.append(args) or "ok", "Answer")
    )
    hub = SimpleNamespace(post=lambda route, body, **kwargs: posted.append((route, body)))
    job = {
        "job": "chat_0123456789abcdef",
        "lease_token": "synthetic-lease",
        "system": "System",
        "prompt": "Question",
        "model": "--tools",
        "expires_in": 120,
    }
    chat_agent._reply(hub, config, job)
    assert called == []  # RED
    assert posted[0][0] == "fail" and posted[0][1]["code"] == "bad_output"


def test_runner_docs_use_placeholder_model():
    text = (Path(__file__).resolve().parents[1] / "docs" / "runner.md").read_text()
    assert '"default_model": "your-codex-model"' in text  # RED


@pytest.mark.parametrize("action", ["lease", "answer", "fail"])
def test_unexpected_value_error_is_private_server_error(relay_hub, monkeypatch, caplog, action):
    h = relay_hub
    m, _, body = leased(h)
    private = "Private reply content " + m["machine_token"]

    def broken(*args, **kwargs):
        raise ValueError(private)

    monkeypatch.setattr(chat_relay, action, broken)
    if action == "lease":
        response = h.poll(m)
    elif action == "answer":
        response = h.answer(m, body["job"], body["lease_token"], "Answer")
    else:
        response = h.fail(m, body["job"], body["lease_token"], "bad_output")
    assert response[0] == 500  # RED
    assert "chat relay request failed" in caplog.text and "ValueError" in caplog.text
    assert "Private reply content" not in caplog.text and m["machine_token"] not in caplog.text
    assert private not in json.dumps(response)


def test_invalid_reply_cli_is_bad_request(relay_hub):
    h = relay_hub
    h.set_main_model("claude", "example-model")
    m, _, body = leased(h)
    assert h.answer(m, body["job"], body["lease_token"], "Answer", cli="codex")[0] == 400
    assert h.jobs()[0]["state"] == "leased"


def test_check_rejects_flag_model_exit_four(relay_hub, tmp_path):
    config = fake_config(relay_hub, tmp_path)
    config["chat"]["clis"]["example-cli"]["default_model"] = "--tools"
    path = tmp_path / "chat.json"
    _private_json(path, config)
    result = runner(["check", "--config", str(path)], {})
    assert result.returncode == 4  # RED
