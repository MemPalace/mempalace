"""Client hook tests: only loopback HTTP and temporary local processes."""

import hashlib
import io
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from commonplace.handoff import hook
from commonplace.handoff.models import ContextUsage


@pytest.fixture
def local(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    for name in (
        "COMMONPLACE_URL",
        "COMMONPLACE_TOKEN",
        "COMMONPLACE_HANDOFF",
        "COMMONPLACE_AGENT",
        "COMMONPLACE_TASK",
        "COMMONPLACE_HANDOFF_ID",
        "COMMONPLACE_THREAD_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMONPLACE_TOKEN", "cmmn_test-secret")
    transcript = tmp_path / "thread.jsonl"
    transcript.write_bytes(b'{"message":"exact text"}\r\n{"tool":"result"}\r\n')
    state = SimpleNamespace(
        home=tmp_path,
        data={
            "session_id": "session-one",
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
            "hook_event_name": "PostToolUse",
        },
        usage=ContextUsage(180_000, 200_000, "claude-test"),
        tail_calls=[],
        launches=[],
        children=[],
    )

    def usage_from_tail(path, **kwargs):
        state.tail_calls.append((path, kwargs))
        return state.usage

    monkeypatch.setattr(hook.trigger, "usage_from_tail", usage_from_tail)
    monkeypatch.setattr(
        hook.trigger,
        "should_handoff",
        lambda usage, settings: bool(
            settings.enabled and usage and usage.fraction >= settings.threshold
        ),
    )
    original = subprocess.Popen

    def start(*args, **kwargs):
        child = original(*args, **kwargs)
        if kwargs.get("start_new_session"):
            state.launches.append((args, kwargs))
            state.children.append(child)
        return child

    monkeypatch.setattr(hook.subprocess, "Popen", start)
    yield state
    for child in state.children:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)


@pytest.fixture
def api(local, monkeypatch):
    state = SimpleNamespace(
        calls=[],
        settings={"enabled": True, "threshold": 0.8, "context_window": None},
        reply={
            "action": "handoff",
            "launch": True,
            "prompt": "Continue with these exact words.\r\n{handoff_id} stays literal.",
            "handoff": {
                "id": "hof_one",
                "stored_thread_id": "thr_one",
                "source_thread_id": "session-one",
                "state": "launched",
                "successor": None,
            },
        },
        statuses={},
        raw={},
        delays={},
        release=threading.Event(),
        location="",
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            route = urlsplit(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            state.calls.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "route": route,
                    "body": body,
                    "token": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                }
            )
            if route in state.delays:
                state.release.wait(timeout=state.delays[route])
            if route == "/api/settings/handoff":
                reply = {"rows": [], "effective": state.settings}
            elif route == "/api/handoff":
                reply = state.reply
            else:
                reply = {"id": "hof_one", "state": "done"}
            raw = state.raw.get(route, json.dumps(reply).encode())
            try:
                self.send_response(state.statuses.get(route, 200))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                if state.location:
                    self.send_header("Location", state.location)
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("COMMONPLACE_URL", state.url)
    yield state
    state.release.set()
    server.shutdown()
    worker.join(timeout=2)
    server.server_close()


def run_hook(local, monkeypatch, capsys, *, data=None, raw=None, client="claude-code"):
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(raw if raw is not None else json.dumps(data or local.data))
    )
    result = hook.main(["--client", client])
    captured = capsys.readouterr()
    assert result == 0
    assert len(captured.err.splitlines()) <= 1
    assert "cmmn_test-secret" not in captured.err
    assert "exact text" not in captured.err
    return captured.out, captured.err


def cache_paths(local):
    key = hashlib.sha256(local.data["session_id"].encode()).hexdigest()
    directory = local.home / ".commonplace" / "handoff-cache"
    return directory / f"{key}.json", directory / f"{key}.launch.json"


def seed_cache(local, api, *, age=0, effective=None, client="claude-code"):
    path, _ = cache_paths(local)
    path.parent.mkdir(parents=True, exist_ok=True)
    context = hook._context(
        api.url,
        os.environ["COMMONPLACE_TOKEN"],
        client,
        os.environ.get("COMMONPLACE_AGENT", ""),
        os.environ.get("COMMONPLACE_TASK", ""),
    )
    path.write_text(
        json.dumps(
            {
                "context": context,
                "fetched_at": time.time() - age,
                "effective": api.settings if effective is None else effective,
            }
        )
    )
    return path


def configure_successor(local, *, argv=None):
    marker = local.home / "successor.json"
    program = (
        "import json,os,pathlib,sys; "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
        "'args':sys.argv[2:],'handoff':os.environ['COMMONPLACE_HANDOFF_ID'],"
        "'thread':os.environ['COMMONPLACE_THREAD_ID'],'cwd':os.getcwd(),"
        "'pid':os.getpid(),'session':os.getsid(0)}))"
    )
    command = (
        argv
        if argv is not None
        else [
            sys.executable,
            "-c",
            program,
            str(marker),
            "{prompt}",
            "{prompt_file}",
            "{handoff_id}",
            "{thread_id}",
            "{cwd}",
            "{unknown}",
            "literal; $(touch SHOULD_NOT_EXIST)",
        ]
    )
    directory = local.home / ".commonplace"
    directory.mkdir(exist_ok=True)
    (directory / "handoff-client.json").write_text(json.dumps({"successor_argv": command}))
    return marker


def read_marker(local, marker):
    for child in local.children:
        child.wait(timeout=2)
    return json.loads(marker.read_text())


@pytest.mark.parametrize("reason", ["off", "url", "token", "empty-url", "empty-token"])
def test_early_exit_touches_nothing(local, monkeypatch, capsys, reason):
    monkeypatch.setenv("COMMONPLACE_URL", "http://127.0.0.1:1")
    if reason == "off":
        monkeypatch.setenv("COMMONPLACE_HANDOFF", " OFF ")
    elif reason in ("url", "token"):
        monkeypatch.delenv("COMMONPLACE_" + reason.upper())
    else:
        monkeypatch.setenv("COMMONPLACE_" + reason.split("-")[1].upper(), " ")

    class Unreadable:
        def read(self, *args):
            pytest.fail("off hook read stdin")

    monkeypatch.setattr(sys, "stdin", Unreadable())
    monkeypatch.setattr(Path, "home", lambda: pytest.fail("off hook touched HOME"))
    assert hook.main([]) == 0
    assert capsys.readouterr() == ("", "")
    assert not (local.home / ".commonplace").exists()


@pytest.mark.timing
def test_warm_below_threshold_has_no_http_or_full_read(local, api, monkeypatch, capsys):
    local.usage = ContextUsage(10, 200_000, "tiny")
    seed_cache(local, api)
    original = Path.open

    def guarded_open(path, *args, **kwargs):
        if str(path) == local.data["transcript_path"]:
            pytest.fail("below threshold read the full transcript")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    started = time.monotonic()
    assert run_hook(local, monkeypatch, capsys) == ("", "")
    elapsed = time.monotonic() - started
    assert elapsed < 0.2
    assert api.calls == []
    assert len(local.tail_calls) == 1


def test_above_threshold_payload_and_detached_successor(local, api, monkeypatch, capsys):
    monkeypatch.setenv("COMMONPLACE_AGENT", "agent / one")
    monkeypatch.setenv("COMMONPLACE_TASK", "#142 & other")
    monkeypatch.setenv("COMMONPLACE_HANDOFF_ID", "hof_parent")
    monkeypatch.setattr(hook, "_branch", lambda cwd: "feature/context")
    marker = configure_successor(local)
    out, err = run_hook(local, monkeypatch, capsys)
    assert err == ""
    output = json.loads(out)
    assert output["continue"] is False
    data = read_marker(local, marker)
    ref = f"pid:{data['pid']}"
    assert output["stopReason"] == f"Context handed off. Successor: {ref}"
    assert data["handoff"] == "hof_one"
    assert data["thread"] == "thr_one"
    assert data["pid"] == data["session"]
    assert data["cwd"] == str(local.home)
    prompt_file = Path(data["args"][1])
    assert prompt_file.read_bytes() == api.reply["prompt"].encode()
    assert data["args"] == [
        api.reply["prompt"],
        str(prompt_file),
        "hof_one",
        "thr_one",
        str(local.home),
        "{unknown}",
        "literal; $(touch SHOULD_NOT_EXIST)",
    ]
    assert not (local.home / "SHOULD_NOT_EXIST").exists()
    assert prompt_file.stat().st_mode & 0o777 == 0o600
    assert prompt_file.parent.stat().st_mode & 0o777 == 0o700
    call = api.calls[1]
    assert call["route"] == "/api/handoff"
    assert call["body"] == {
        "meta": {
            "thread_id": "session-one",
            "agent": "agent / one",
            "client": "claude-code",
            "model": "claude-test",
            "task_ref": "#142 & other",
            "branch": "feature/context",
            "pr_url": "",
            "cwd": str(local.home),
            "parent_handoff_id": "hof_parent",
        },
        "transcript": Path(local.data["transcript_path"]).read_bytes().decode(),
        "transcript_format": "claude-code",
        "usage": {"used_tokens": 180_000, "window_tokens": 200_000, "model": "claude-test"},
        "note": "",
        "idempotency_key": "",
        "next_message": "",
    }
    assert api.calls[2]["route"] == "/api/handoffs/hof_one/successor"
    assert api.calls[2]["body"] == {"agent_ref": ref}
    assert all(call["token"] == "Bearer cmmn_test-secret" for call in api.calls)
    assert all(call["content_type"] == "application/json" for call in api.calls)
    assert parse_qs(urlsplit(api.calls[0]["path"]).query) == {
        "agent": ["agent / one"],
        "task": ["#142 & other"],
    }
    assert local.launches[0][1]["stdin"] == subprocess.DEVNULL
    assert local.launches[0][1]["stdout"] == subprocess.DEVNULL
    assert local.launches[0][1]["stderr"] == subprocess.DEVNULL
    assert local.launches[0][1].get("shell", False) is False


@pytest.mark.parametrize("argv", [None, []])
def test_missing_successor_command_leaves_private_prompt(local, api, monkeypatch, capsys, argv):
    if argv is not None:
        configure_successor(local, argv=argv)
    out, err = run_hook(local, monkeypatch, capsys)
    assert err == ""
    path = Path(json.loads(out)["stopReason"].removeprefix("Context handed off. Successor: "))
    assert path.is_file()
    assert path.read_bytes() == api.reply["prompt"].encode()
    assert len(api.calls) == 2
    assert local.children == []


@pytest.mark.parametrize("usage", [None, ContextUsage(1, 200_000)])
@pytest.mark.parametrize("trigger_name", ["auto", "manual"])
def test_precompact_backstop(local, api, monkeypatch, capsys, usage, trigger_name):
    local.usage = usage
    local.data.update(hook_event_name="PreCompact", trigger=trigger_name)
    out, err = run_hook(local, monkeypatch, capsys)
    assert json.loads(out)["continue"] is False
    assert err == ""
    assert api.calls[1]["route"] == "/api/handoff"
    assert api.calls[1]["body"]["usage"] == (
        None if usage is None else {"used_tokens": 1, "window_tokens": 200_000, "model": ""}
    )


def test_precompact_disabled_remains_silent(local, api, monkeypatch, capsys):
    api.settings["enabled"] = False
    local.data["hook_event_name"] = "PreCompact"
    assert run_hook(local, monkeypatch, capsys) == ("", "")
    assert [call["method"] for call in api.calls] == ["GET"]
    assert local.tail_calls == []


@pytest.mark.parametrize("reply", [{"action": "compact"}, {"action": "other"}])
def test_non_handoff_reply_is_silent(local, api, monkeypatch, capsys, reply):
    api.reply = reply
    assert run_hook(local, monkeypatch, capsys) == ("", "")
    assert not cache_paths(local)[1].exists()


@pytest.mark.parametrize("route", ["/api/settings/handoff", "/api/handoff"])
@pytest.mark.parametrize("status", [401, 404, 500])
def test_http_error_falls_back(local, api, monkeypatch, capsys, route, status):
    api.statuses[route] = status
    api.raw[route] = b"cmmn_test-secret\nexact text"
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert len(err.splitlines()) == int(route != "/api/settings/handoff")
    assert local.children == []


@pytest.mark.parametrize("route", ["/api/settings/handoff", "/api/handoff"])
@pytest.mark.parametrize("raw", [b"not json", b"[]", b"null"])
def test_bad_response_json_falls_back(local, api, monkeypatch, capsys, route, raw):
    api.raw[route] = raw
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert bool(err) == (route != "/api/settings/handoff")
    assert local.children == []


@pytest.mark.parametrize("route", ["/api/settings/handoff", "/api/handoff"])
@pytest.mark.timing
def test_timeout_falls_back(local, api, monkeypatch, capsys, route):
    if route == "/api/handoff":
        seed_cache(local, api)
    original = hook._request
    calls = []

    def request(server, token, path, body=None):
        calls.append(urlsplit(path).path)
        return original(server, token, path, body)

    monkeypatch.setattr(hook, "_request", request)
    api.delays[route] = 1
    monkeypatch.setattr(hook, "HTTP_TIMEOUT", 0.03)
    started = time.monotonic()
    out, err = run_hook(local, monkeypatch, capsys)
    api.release.set()
    assert out == ""
    assert bool(err) == (route != "/api/settings/handoff")
    assert time.monotonic() - started < 1
    assert local.children == []
    assert calls == [route]


@pytest.mark.parametrize("route", ["/api/settings/handoff", "/api/handoff"])
@pytest.mark.parametrize("wrapped", [False, True], ids=["timeout", "url-timeout"])
def test_request_timeout_falls_back(local, monkeypatch, capsys, route, wrapped):
    monkeypatch.setenv("COMMONPLACE_URL", "http://127.0.0.1:1")
    calls = []

    def open_request(request, *, timeout):
        path = urlsplit(request.full_url).path
        calls.append((request.get_method(), path, timeout))
        if path == route:
            error = TimeoutError("cmmn_test-secret\nexact text")
            raise URLError(error) if wrapped else error
        if path != "/api/settings/handoff":
            pytest.fail(f"Unexpected request: {path}")
        return io.BytesIO(
            json.dumps(
                {
                    "rows": [],
                    "effective": {"enabled": True, "threshold": 0.8, "context_window": None},
                }
            ).encode()
        )

    monkeypatch.setattr(hook, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert len(err.splitlines()) == int(route != "/api/settings/handoff")
    assert local.children == []
    assert not cache_paths(local)[1].exists()
    expected = [("GET", "/api/settings/handoff", 30)]
    if route == "/api/handoff":
        expected.append(("POST", "/api/handoff", 30))
    assert calls == expected


def test_redirect_is_not_followed(local, api, monkeypatch, capsys):
    api.statuses["/api/settings/handoff"] = 302
    api.location = api.url + "/redirected"
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert err == ""
    assert len(api.calls) == 1


def test_lock_contention_skips_everything(local, api, monkeypatch, capsys):
    import fcntl

    path, _ = cache_paths(local)
    seed_cache(local, api)
    lock = path.with_suffix(".lock")
    with lock.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_hook(local, monkeypatch, capsys) == ("", "")
    assert api.calls == []
    assert local.tail_calls == []


@pytest.mark.parametrize(
    "age,refresh", [(0, False), (59, False), (60, True), (61, True), (-1, True)]
)
def test_cache_ttl(local, api, monkeypatch, capsys, age, refresh):
    local.usage = ContextUsage(1, 200_000)
    monkeypatch.setattr(hook.time, "time", lambda: 10_000)
    seed_cache(local, api, age=age)
    assert run_hook(local, monkeypatch, capsys) == ("", "")
    assert len(api.calls) == int(refresh)
    if refresh:
        assert api.calls[0]["route"] == "/api/settings/handoff"


@pytest.mark.parametrize("change", ["agent", "task", "token", "server", "client"])
def test_cache_is_bound_to_context(local, api, monkeypatch, capsys, change):
    local.usage = ContextUsage(1, 200_000)
    path = seed_cache(local, api)
    client = "claude-code"
    if change == "server":
        cached = json.loads(path.read_text())
        cached["context"] = "another-server"
        path.write_text(json.dumps(cached))
    elif change == "client":
        client = "codex"
    else:
        monkeypatch.setenv("COMMONPLACE_" + change.upper(), "different")
    assert run_hook(local, monkeypatch, capsys, client=client) == ("", "")
    assert len(api.calls) == 1


def test_cache_is_private_and_contains_no_credentials_or_launcher(local, api, monkeypatch, capsys):
    local.usage = ContextUsage(1, 200_000)
    api.settings["launcher"] = {"secret": "launcher-secret"}
    assert run_hook(local, monkeypatch, capsys) == ("", "")
    path, _ = cache_paths(local)
    raw = path.read_text()
    assert "cmmn_test-secret" not in raw
    assert "launcher-secret" not in raw
    assert "launcher" not in raw
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("raw", ["not json", "[]", "null", '{"enabled":true}'])
def test_corrupt_cache_refreshed(local, api, monkeypatch, capsys, raw):
    local.usage = ContextUsage(1, 200_000)
    path = seed_cache(local, api)
    path.write_text(raw)
    assert run_hook(local, monkeypatch, capsys) == ("", "")
    assert len(api.calls) == 1


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"enabled": "false"},
        {"enabled": True, "threshold": "0.8"},
        {"enabled": True, "threshold": True},
        {"enabled": True, "threshold": 0.1},
        {"enabled": True, "threshold": 1},
        {"enabled": True, "threshold": float("nan")},
        {"enabled": True, "context_window": 0},
        {"enabled": True, "context_window": True},
        {"enabled": True, "context_window": "1000"},
    ],
)
def test_invalid_settings_do_not_assume_enabled(local, api, monkeypatch, capsys, value):
    api.settings = value
    local.data["hook_event_name"] = "PreCompact"
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert err == ""
    assert local.tail_calls == []
    assert len(api.calls) == 1


def test_settings_window_and_threshold_used(local, api, monkeypatch, capsys):
    api.settings.update(context_window=500_000, threshold=0.95)
    local.usage = ContextUsage(450_000, 500_000, "override-model")
    assert run_hook(local, monkeypatch, capsys) == ("", "")
    assert local.tail_calls == [
        (
            local.data["transcript_path"],
            {"fmt": "claude-code", "override_window": 500_000, "configured": ""},
        )
    ]
    assert len(api.calls) == 1


@pytest.mark.parametrize("raw", ["", "not json", "null", "[]", "1", "{}"])
def test_bad_input_is_safe(local, api, monkeypatch, capsys, raw):
    out, err = run_hook(local, monkeypatch, capsys, raw=raw)
    assert out == ""
    assert err
    assert api.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", ""),
        ("session_id", 1),
        ("session_id", None),
        ("transcript_path", ""),
        ("transcript_path", []),
        ("transcript_path", "/missing/thread.jsonl"),
        ("cwd", {}),
    ],
)
def test_invalid_metadata(local, api, monkeypatch, capsys, field, value):
    local.data[field] = value
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert err
    assert api.calls == []


def test_unrelated_event_ignored(local, api, monkeypatch, capsys):
    local.data["hook_event_name"] = "SessionEnd"
    assert run_hook(local, monkeypatch, capsys) == ("", "")
    assert api.calls == []


def _handoff_posts(api):
    return [call for call in api.calls if call["route"] == "/api/handoff"]


@pytest.mark.parametrize("aliases", [False, True])
def test_codex_marks_pending_then_hands_off_at_next_turn(local, api, monkeypatch, capsys, aliases):
    local.usage = ContextUsage(180_000, 200_000, "gpt-test")
    local.data["hook_event_name"] = "Stop"
    if aliases:
        local.data["thread_id"] = local.data.pop("session_id")
        local.data["rollout_path"] = local.data.pop("transcript_path")
    # Over the threshold mid-turn: only a notice, nothing stored yet.
    out, err = run_hook(local, monkeypatch, capsys, client="codex")
    assert err == ""
    assert json.loads(out) == {"systemMessage": hook.PENDING_NOTICE}
    assert _handoff_posts(api) == []
    # Notice once; later hooks in the same turn stay quiet.
    assert run_hook(local, monkeypatch, capsys, client="codex") == ("", "")
    assert _handoff_posts(api) == []
    # The next turn starts: hand off now and pass the new message on.
    message = "Now ship it.\n```py\nprint('x')\n```"
    local.data.update(hook_event_name="UserPromptSubmit", prompt=message)
    out, err = run_hook(local, monkeypatch, capsys, client="codex")
    assert err == ""
    result = json.loads(out)
    assert result["decision"] == "block"
    assert result["reason"].startswith("Context handed off. Successor: ")
    [post] = _handoff_posts(api)
    request = post["body"]
    assert request["meta"]["client"] == "codex"
    assert request["meta"]["thread_id"] == "session-one"
    assert request["meta"]["model"] == "gpt-test"
    assert request["transcript_format"] == "codex"
    # The hub adds the message to the successor prompt, whatever launcher runs.
    assert request["next_message"] == message
    prompt_file = Path(result["reason"].removeprefix("Context handed off. Successor: "))
    assert prompt_file.read_bytes().decode() == api.reply["prompt"]
    # Nothing pending any more; the same session keeps pointing at the successor.
    local.data["prompt"] = "another"
    out, _ = run_hook(local, monkeypatch, capsys, client="codex")
    assert json.loads(out)["decision"] == "block"
    assert len(_handoff_posts(api)) == 1


def test_codex_turn_start_without_pending_does_nothing(local, api, monkeypatch, capsys):
    local.usage = ContextUsage(180_000, 200_000, "gpt-test")
    local.data.update(hook_event_name="UserPromptSubmit", prompt="hi")
    assert run_hook(local, monkeypatch, capsys, client="codex") == ("", "")
    assert _handoff_posts(api) == []


def test_claude_ignores_turn_start(local, api, monkeypatch, capsys):
    local.data.update(hook_event_name="UserPromptSubmit", prompt="hi")
    assert run_hook(local, monkeypatch, capsys) == ("", "")
    assert _handoff_posts(api) == []


def test_codex_pending_cleared_when_handoff_is_off(local, api, monkeypatch, capsys):
    local.usage = ContextUsage(180_000, 200_000, "gpt-test")
    local.data["hook_event_name"] = "PostToolUse"
    run_hook(local, monkeypatch, capsys, client="codex")
    api.reply = {"action": "compact", "reason": "off"}
    local.data.update(hook_event_name="UserPromptSubmit", prompt="hi")
    assert run_hook(local, monkeypatch, capsys, client="codex") == ("", "")
    assert not list((local.home / ".commonplace" / "handoff-cache").glob("*.pending.json"))


def test_codex_pending_kept_when_the_hub_is_down(local, api, monkeypatch, capsys):
    local.usage = ContextUsage(180_000, 200_000, "gpt-test")
    local.data["hook_event_name"] = "PostToolUse"
    run_hook(local, monkeypatch, capsys, client="codex")
    api.statuses["/api/handoff"] = 500
    local.data.update(hook_event_name="UserPromptSubmit", prompt="hi")
    out, err = run_hook(local, monkeypatch, capsys, client="codex")
    assert out == "" and err
    assert list((local.home / ".commonplace" / "handoff-cache").glob("*.pending.json"))


def test_unsafe_session_id_cannot_escape_cache_directory(local, api, monkeypatch, capsys):
    local.data["session_id"] = "../../outside/with spaces\n\x00"
    api.reply["handoff"]["source_thread_id"] = local.data["session_id"]
    out, err = run_hook(local, monkeypatch, capsys)
    assert json.loads(out)["continue"] is False
    assert err == ""
    path, journal = cache_paths(local)
    assert path.exists() and journal.exists()
    assert path.parent == local.home / ".commonplace" / "handoff-cache"
    assert not (local.home / "outside").exists()
    assert api.calls[1]["body"]["meta"]["thread_id"] == local.data["session_id"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("handoff", None),
        ("launch", "true"),
        ("launch", None),
        ("prompt", None),
        ("prompt", ""),
        ("id", ""),
        ("stored_thread_id", None),
        ("source_thread_id", "different-session"),
    ],
)
def test_invalid_handoff_cannot_launch(local, api, monkeypatch, capsys, field, value):
    configure_successor(local)
    if field in ("id", "stored_thread_id", "source_thread_id"):
        api.reply["handoff"][field] = value
    else:
        api.reply[field] = value
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert err
    assert local.children == []
    assert not cache_paths(local)[1].exists()


@pytest.mark.parametrize(
    "raw", ["bad json", "null", "[]", '{"successor_argv":"sh"}', '{"successor_argv":[1]}']
)
def test_invalid_client_config(local, api, monkeypatch, capsys, raw):
    configure_successor(local)
    (local.home / ".commonplace" / "handoff-client.json").write_text(raw)
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert err
    assert local.children == []


@pytest.mark.parametrize(
    "argv", [["/missing/successor"], [sys.executable, "-c", "raise SystemExit(7)"]]
)
def test_launch_failure_does_not_stop_or_retry(local, api, monkeypatch, capsys, argv):
    configure_successor(local, argv=argv)
    start = hook.subprocess.Popen

    def exited_child(*args, **kwargs):
        child = start(*args, **kwargs)
        # Observe a completed failure, independent of interpreter startup time.
        child.wait(timeout=10)
        return child

    monkeypatch.setattr(hook.subprocess, "Popen", exited_child)
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert err
    reports = [c for c in api.calls if c["route"] == "/api/handoffs/hof_one/launch-failed"]
    assert len(reports) == 1 and reports[0]["body"]["error"].startswith("successor command failed")
    calls = len(api.calls)
    assert run_hook(local, monkeypatch, capsys) == ("", "")
    assert len(api.calls) == calls
    assert len(local.children) <= 1


def test_registration_failure_retries_without_second_launch(local, api, monkeypatch, capsys):
    marker = configure_successor(local)
    api.statuses["/api/handoffs/hof_one/successor"] = 500
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert err
    data = read_marker(local, marker)
    assert len(local.children) == 1
    journal = cache_paths(local)[1]
    assert json.loads(journal.read_text())["registered"] is False
    api.statuses.clear()
    out, err = run_hook(local, monkeypatch, capsys)
    assert err == ""
    assert json.loads(out)["stopReason"].endswith(f"pid:{data['pid']}")
    assert len(local.children) == 1
    assert [call["route"] for call in api.calls].count("/api/handoff") == 1
    assert [call["route"] for call in api.calls].count("/api/handoffs/hof_one/successor") == 2
    assert json.loads(journal.read_text())["registered"] is True


def test_sequential_duplicate_never_posts_or_launches_again(local, api, monkeypatch, capsys):
    marker = configure_successor(local)
    first, err = run_hook(local, monkeypatch, capsys)
    assert err == ""
    read_marker(local, marker)
    count = len(api.calls)
    second, err = run_hook(local, monkeypatch, capsys)
    assert second == first
    assert err == ""
    assert len(api.calls) == count
    assert len(local.children) == 1


def test_server_already_launched_successor(local, api, monkeypatch, capsys):
    api.reply.update(launch=False, prompt="")
    api.reply["handoff"]["successor"] = {"agent_ref": "t3:successor-thread"}
    out, err = run_hook(local, monkeypatch, capsys)
    assert json.loads(out)["stopReason"].endswith("t3:successor-thread")
    assert err == ""
    assert local.children == []
    assert len(api.calls) == 2


def test_server_claim_without_successor_falls_back(local, api, monkeypatch, capsys):
    api.reply["launch"] = False
    assert run_hook(local, monkeypatch, capsys) == ("", "")
    assert local.children == []


def test_manual_prompt_duplicate_is_stable(local, api, monkeypatch, capsys):
    first, err = run_hook(local, monkeypatch, capsys)
    assert err == ""
    second, err = run_hook(local, monkeypatch, capsys)
    assert first == second
    assert err == ""
    assert len(api.calls) == 2


@pytest.mark.parametrize("raw", ["broken", "null", "[]"])
def test_corrupt_journal_never_relaunches(local, api, monkeypatch, capsys, raw):
    seed_cache(local, api)
    cache_paths(local)[1].write_text(raw)
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert err
    assert api.calls == []


def test_journal_write_failure_after_spawn_does_not_duplicate(local, api, monkeypatch, capsys):
    marker = configure_successor(local)
    original = hook._write_json

    def write(path, value):
        if value.get("state") == "launched":
            raise OSError("cmmn_test-secret\nexact text")
        original(path, value)

    monkeypatch.setattr(hook, "_write_json", write)
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert err
    read_marker(local, marker)
    assert run_hook(local, monkeypatch, capsys) == ("", "")
    assert len(local.children) == 1
    assert len(api.calls) == 2


def test_atomic_write_failure_removes_temporary_file(local, api, monkeypatch, capsys):
    monkeypatch.setattr(hook.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("failed")))
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert err == ""
    directory = cache_paths(local)[0].parent
    assert not list(directory.glob(".handoff-*"))
    assert len(api.calls) == 1


@pytest.mark.parametrize("failure", ["missing", "timeout", "bad-exit"])
def test_git_lookup_failure_is_nonfatal(local, api, monkeypatch, capsys, failure):
    def git(args, **kwargs):
        assert args == ["git", "-C", str(local.home), "rev-parse", "--abbrev-ref", "HEAD"]
        assert kwargs["timeout"] == 2
        assert kwargs["stdin"] == subprocess.DEVNULL
        if failure == "missing":
            raise FileNotFoundError
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 2)
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(hook.subprocess, "run", git)
    out, err = run_hook(local, monkeypatch, capsys)
    assert json.loads(out)["continue"] is False
    assert err == ""
    assert api.calls[1]["body"]["meta"]["branch"] == ""


def test_git_branch_detected_in_real_repository(local, api, monkeypatch, capsys):
    subprocess.run(
        ["git", "init", "-b", "hook-branch", str(local.home)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(local.home),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "test",
        ],
        check=True,
        capture_output=True,
    )
    out, err = run_hook(local, monkeypatch, capsys)
    assert json.loads(out)["continue"] is False
    assert err == ""
    assert api.calls[1]["body"]["meta"]["branch"] == "hook-branch"


@pytest.mark.parametrize(
    "server",
    ["file:///tmp/a", "http://user:pass@127.0.0.1", "http:///empty", "http://127.0.0.1?token=x"],
)
def test_bad_gateway_url_rejected(local, api, monkeypatch, capsys, server):
    monkeypatch.setenv("COMMONPLACE_URL", server)
    out, err = run_hook(local, monkeypatch, capsys)
    assert out == ""
    assert err
    assert api.calls == []


@pytest.mark.parametrize("scheme,remote", [("http", True), ("https", True), ("http", False)])
def test_plain_remote_http_warns_without_blocking(local, api, monkeypatch, capsys, scheme, remote):
    host = str(ipaddress.ip_address(0x08080808)) if remote else urlsplit(api.url).hostname
    server = f"{scheme}://{host}"
    monkeypatch.setenv("COMMONPLACE_URL", server)
    original = hook._request
    calls = []

    def request(configured, token, route, body=None):
        calls.append(configured)
        return original(api.url, token, route, body)

    monkeypatch.setattr(hook, "_request", request)
    out, err = run_hook(local, monkeypatch, capsys)
    assert calls and all(value == server for value in calls)
    assert json.loads(out)["continue"] is False
    if scheme == "http" and remote:
        assert "use HTTPS or a loopback HTTP URL" in err
        assert "continuing normally" in err
        assert server not in err and "cmmn_test-secret" not in err
    else:
        assert err == ""


def test_invalid_cli_arguments_return_zero(local, api, monkeypatch, capsys):
    assert hook.main(["--unknown"]) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert len(err.splitlines()) == 1


def test_plain_http_hook_does_not_import_oauth(local, api):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from commonplace.handoff import hook
from commonplace.handoff.models import HandoffSettings
hook._settings = lambda *args: HandoffSettings(enabled=False)
assert hook.main(['--client', 'claude-code']) == 0
assert not any(name == 'commonplace.oauth' or name.startswith('commonplace.oauth.') for name in sys.modules)
""",
        ],
        input=json.dumps(local.data),
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_wrapper_honors_interpreter_and_forwards_arguments(local, tmp_path):
    marker = tmp_path / "wrapper-args.json"
    interpreter = tmp_path / "custom python"
    interpreter.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,pathlib,sys\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps("
        "{'args':sys.argv[1:],'input':sys.stdin.read(),'safe':os.environ['PYTHONSAFEPATH']}))\n"
    )
    interpreter.chmod(0o700)
    wrapper = Path(__file__).resolve().parents[1] / "hooks" / "commonplace_handoff_hook.sh"
    result = subprocess.run(
        [str(wrapper), "--client", "codex"],
        input="hook input",
        text=True,
        capture_output=True,
        env={**os.environ, "COMMONPLACE_PYTHON": str(interpreter)},
        timeout=3,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert json.loads(marker.read_text()) == {
        "args": ["-m", "commonplace.handoff.hook", "--client", "codex"],
        "input": "hook input",
        "safe": "1",
    }


# ── Chain without COMMONPLACE_HANDOFF_ID (e.g. a T3 thread successor) ─────


def _first_message(local, text):
    path = Path(local.data["transcript_path"])
    line = json.dumps({"type": "user", "message": {"role": "user", "content": text}})
    path.write_bytes(line.encode() + b"\n" + path.read_bytes())


def test_parent_comes_from_the_successor_prompt_marker(local, api, monkeypatch, capsys):
    _first_message(local, "You continue...\n\ncommonplace-handoff-id: hof_0123456789abcdef")
    run_hook(local, monkeypatch, capsys)
    assert api.calls[1]["body"]["meta"]["parent_handoff_id"] == "hof_0123456789abcdef"


def test_environment_pointer_wins_over_the_marker(local, api, monkeypatch, capsys):
    _first_message(local, "commonplace-handoff-id: hof_0123456789abcdef")
    monkeypatch.setenv("COMMONPLACE_HANDOFF_ID", "hof_fedcba9876543210")
    run_hook(local, monkeypatch, capsys)
    assert api.calls[1]["body"]["meta"]["parent_handoff_id"] == "hof_fedcba9876543210"


@pytest.mark.parametrize(
    "text", ["no marker here", "commonplace-handoff-id: hof_XYZ", "commonplace-handoff-id: thr_ab"]
)
def test_no_or_malformed_marker_sends_no_parent(local, api, monkeypatch, capsys, text):
    _first_message(local, text)
    run_hook(local, monkeypatch, capsys)
    assert api.calls[1]["body"]["meta"]["parent_handoff_id"] == ""


# ── Review fixes: Codex flow and the chain marker ──────────────────────────


def _pending_files(local):
    return list((local.home / ".commonplace" / "handoff-cache").glob("*.pending.json"))


def _mark_codex(local, monkeypatch, capsys, event="Stop"):
    local.usage = ContextUsage(180_000, 200_000, "gpt-test")
    local.data["hook_event_name"] = event
    out, _ = run_hook(local, monkeypatch, capsys, client="codex")
    assert json.loads(out) == {"systemMessage": hook.PENDING_NOTICE}


def test_codex_hub_started_successor_keeps_blocking(local, api, monkeypatch, capsys):
    _mark_codex(local, monkeypatch, capsys)
    api.reply = {
        "action": "handoff",
        "launch": False,
        "prompt": "",
        "handoff": {
            "id": "hof_one",
            "stored_thread_id": "thr_one",
            "source_thread_id": "session-one",
            "state": "done",
            "successor": {"agent_ref": "t3:thread-b", "url": "", "thread_id": ""},
        },
    }
    local.data.update(hook_event_name="UserPromptSubmit", prompt="next step please")
    out, _ = run_hook(local, monkeypatch, capsys, client="codex")
    assert json.loads(out)["decision"] == "block"
    assert _handoff_posts(api)[0]["body"]["next_message"] == "next step please"
    assert not _pending_files(local)
    # Later prompts in the old session keep pointing at the successor.
    local.data["prompt"] = "and another"
    out, _ = run_hook(local, monkeypatch, capsys, client="codex")
    assert json.loads(out)["decision"] == "block" and len(_handoff_posts(api)) == 1
    # A Stop in the old session never "blocks" (in Codex that means: go on).
    local.data["hook_event_name"] = "Stop"
    out, _ = run_hook(local, monkeypatch, capsys, client="codex")
    assert set(json.loads(out)) == {"systemMessage"}


def test_codex_stale_mark_from_another_identity_is_replaced(local, api, monkeypatch, capsys):
    local.usage = ContextUsage(180_000, 200_000, "gpt-test")
    local.data["hook_event_name"] = "Stop"
    run_hook(local, monkeypatch, capsys, client="codex")
    [pending] = _pending_files(local)
    pending.write_text(json.dumps({"context": "someone else", "event": "Stop"}))
    out, _ = run_hook(local, monkeypatch, capsys, client="codex")
    assert json.loads(out) == {"systemMessage": hook.PENDING_NOTICE}
    local.data.update(hook_event_name="UserPromptSubmit", prompt="go")
    out, _ = run_hook(local, monkeypatch, capsys, client="codex")
    assert json.loads(out)["decision"] == "block"


def test_codex_mark_expires_when_context_is_no_longer_full(local, api, monkeypatch, capsys):
    _mark_codex(local, monkeypatch, capsys)
    local.usage = ContextUsage(20_000, 200_000, "gpt-test")  # compacted meanwhile
    local.data.update(hook_event_name="UserPromptSubmit", prompt="hi")
    assert run_hook(local, monkeypatch, capsys, client="codex") == ("", "")
    assert _handoff_posts(api) == [] and not _pending_files(local)


def test_codex_precompact_mark_hands_off_even_below_threshold(local, api, monkeypatch, capsys):
    _mark_codex(local, monkeypatch, capsys, event="PreCompact")
    local.usage = ContextUsage(20_000, 200_000, "gpt-test")
    local.data.update(hook_event_name="UserPromptSubmit", prompt="hi")
    out, _ = run_hook(local, monkeypatch, capsys, client="codex")
    assert json.loads(out)["decision"] == "block"


def test_codex_failed_turn_start_backs_off(local, api, monkeypatch, capsys):
    _mark_codex(local, monkeypatch, capsys)
    api.statuses["/api/handoff"] = 500
    local.data.update(hook_event_name="UserPromptSubmit", prompt="hi")
    run_hook(local, monkeypatch, capsys, client="codex")
    assert len(_handoff_posts(api)) == 1
    [pending] = _pending_files(local)
    marked = json.loads(pending.read_text())
    assert marked["attempts"] == 1 and marked["retry_after"] > hook.time.time() + 50
    # Within the wait: no new upload.
    assert run_hook(local, monkeypatch, capsys, client="codex") == ("", "")
    assert len(_handoff_posts(api)) == 1
    # After it: one more try, and the wait doubles.
    now = hook.time.time()
    monkeypatch.setattr(hook.time, "time", lambda: now + 61)
    run_hook(local, monkeypatch, capsys, client="codex")
    assert len(_handoff_posts(api)) == 2
    marked = json.loads(pending.read_text())
    assert marked["attempts"] == 2 and marked["retry_after"] >= now + 61 + 120


def _user_line(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


@pytest.mark.parametrize(
    "lines,expected",
    [
        # Marker on the last line of the first user message.
        (
            [_user_line("Continue.\n\ncommonplace-handoff-id: hof_00000000000000bb")],
            "hof_00000000000000bb",
        ),
        # A quoted older marker in the doc doesn't win over the last line.
        (
            [
                _user_line(
                    "commonplace-handoff-id: hof_00000000000000aa\n...\ncommonplace-handoff-id: hof_00000000000000bb"
                )
            ],
            "hof_00000000000000bb",
        ),
        # Not on the last line: ignored.
        ([_user_line("commonplace-handoff-id: hof_00000000000000aa\nplease go on")], ""),
        # In a tool result, not the user's own message: ignored.
        (
            [
                _user_line("Fix the bug"),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "t1",
                                    "content": "commonplace-handoff-id: hof_00000000000000aa",
                                }
                            ],
                        },
                    }
                ),
            ],
            "",
        ),
        # Codex first user message.
        (
            [
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "<environment_context>x</environment_context>",
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "Go.\ncommonplace-handoff-id: hof_00000000000000cc",
                        },
                    }
                ),
            ],
            "hof_00000000000000cc",
        ),
    ],
)
def test_parent_marker_only_from_the_first_user_message(tmp_path, lines, expected):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n")
    assert hook._parent_from_transcript(path) == expected


def _codex_line(kind, payload):
    return json.dumps({"type": kind, "payload": payload})


def test_marker_found_after_codex_and_t3_preambles(tmp_path):
    lines = [
        _codex_line("session_meta", {"id": "x"}),
        _codex_line(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "# AGENTS.md instructions for /w\n..."},
                    {"type": "input_text", "text": "<environment_context>e</environment_context>"},
                ],
            },
        ),
        _codex_line(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<external_t3_fanout_mode>x"}],
            },
        ),
        _codex_line(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Continue.\ncommonplace-handoff-id: hof_00000000000000dd",
                    }
                ],
            },
        ),
        _codex_line("response_item", {"type": "reasoning", "summary": []}),
        _codex_line(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "commonplace-handoff-id: hof_00000000000000ee"}
                ],
            },
        ),
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(lines) + "\n")
    # The opening turn's marker wins; one after the first reply is ignored.
    assert hook._parent_from_transcript(path) == "hof_00000000000000dd"


@pytest.mark.parametrize(
    "line",
    [
        {"type": "user", "message": "str"},
        {"type": "user", "message": {"content": 5}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": 5}},
        {"type": "response_item", "payload": "x"},
        {"type": "event_msg", "payload": {"type": "user_message", "message": 7}},
        [1, 2],
    ],
)
def test_odd_lines_never_crash_the_marker_parser(tmp_path, line):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(line)
        + "\n"
        + _user_line("go\ncommonplace-handoff-id: hof_00000000000000ff")
        + "\n"
    )
    assert hook._parent_from_transcript(path) in ("", "hof_00000000000000ff")


def test_marker_after_a_long_first_prompt(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        _user_line("x" * 400_000 + "\ncommonplace-handoff-id: hof_00000000000000ab") + "\n"
    )
    assert hook._parent_from_transcript(path) == "hof_00000000000000ab"


def test_codex_precompact_upgrades_an_existing_mark(local, api, monkeypatch, capsys):
    _mark_codex(local, monkeypatch, capsys, event="PostToolUse")
    local.data["hook_event_name"] = "PreCompact"
    assert run_hook(local, monkeypatch, capsys, client="codex") == ("", "")
    [pending] = _pending_files(local)
    assert json.loads(pending.read_text())["event"] == "PreCompact"
    local.usage = ContextUsage(20_000, 200_000, "gpt-test")  # compacted
    local.data.update(hook_event_name="UserPromptSubmit", prompt="hi")
    out, _ = run_hook(local, monkeypatch, capsys, client="codex")
    assert json.loads(out)["decision"] == "block"


def test_hub_started_successor_blocks_even_if_journal_write_fails(local, api, monkeypatch, capsys):
    _mark_codex(local, monkeypatch, capsys)
    api.reply = {
        "action": "handoff",
        "launch": False,
        "prompt": "",
        "handoff": {
            "id": "hof_one",
            "stored_thread_id": "thr_one",
            "source_thread_id": "session-one",
            "state": "done",
            "successor": {"agent_ref": "t3:thread-b", "url": "", "thread_id": ""},
        },
    }
    real = hook._write_json

    def failing(path, value):
        if str(path).endswith(".launch.json"):
            raise OSError("disk full")
        return real(path, value)

    monkeypatch.setattr(hook, "_write_json", failing)
    local.data.update(hook_event_name="UserPromptSubmit", prompt="next")
    out, _ = run_hook(local, monkeypatch, capsys, client="codex")
    assert json.loads(out)["decision"] == "block"
