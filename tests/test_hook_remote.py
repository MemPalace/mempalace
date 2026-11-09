"""Save hooks writing to a remote server: routing, transport and no local fallback."""

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from mempalace import hook_remote, hooks_cli
from mempalace.config import MempalaceConfig
from mempalace.hook_remote import (
    RemoteCallFailed,
    RemoteConfigError,
    RemoteTarget,
    resolve_remote_target,
)

URL = "https://memory.example.com/mcp"
TOKEN = "s3cret-test-token"
TARGET = RemoteTarget(URL, TOKEN)


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _tool_response(result: dict) -> _Response:
    text = json.dumps(result)
    return _Response(
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": text}]}}).encode()
    )


@pytest.fixture
def remote_env(monkeypatch):
    monkeypatch.setenv("MEMPALACE_HOOK_REMOTE_URL", URL)
    monkeypatch.setenv("MEMPALACE_HOOK_REMOTE_TOKEN", TOKEN)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    path = tmp_path / "hook_state"
    path.mkdir()
    monkeypatch.setattr(hooks_cli, "STATE_DIR", path)
    monkeypatch.setattr(hooks_cli, "_MINE_PID_DIR", path / "mine_pids")
    monkeypatch.setattr(hooks_cli, "_state_dir_initialized", False)
    return path


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "session.jsonl"
    lines = [json.dumps({"message": {"role": "user", "content": f"message {i}"}}) for i in range(3)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# --- URL and token validation ---


@pytest.mark.parametrize("url", ["", "   ", None])
def test_no_url_means_no_remote(url):
    assert resolve_remote_target(url, TOKEN) is None


@pytest.mark.parametrize(
    "url, token, expected",
    [
        ("https://memory.example.com", TOKEN, "https://memory.example.com/mcp"),
        ("https://memory.example.com/", TOKEN, "https://memory.example.com/mcp"),
        ("https://memory.example.com/mcp/", TOKEN, "https://memory.example.com/mcp"),
        ("https://memory.example.com/team/mcp", TOKEN, "https://memory.example.com/team/mcp"),
        ("http://127.0.0.1:8765", TOKEN, "http://127.0.0.1:8765/mcp"),
        ("http://localhost:8765/mcp", "", "http://localhost:8765/mcp"),
        ("http://[::1]:8765", "", "http://[::1]:8765/mcp"),
    ],
)
def test_usable_urls(url, token, expected):
    target = resolve_remote_target(url, token)
    assert target.url == expected
    assert target.token == token


@pytest.mark.parametrize(
    "url, token",
    [
        ("http://memory.example.com/mcp", TOKEN),  # token would travel unencrypted
        ("http://10.0.0.5:8765/mcp", TOKEN),
        ("http://127.0.0.1.example.com/mcp", TOKEN),
        ("ftp://memory.example.com/mcp", TOKEN),
        ("memory.example.com/mcp", TOKEN),
        ("https://", TOKEN),
        ("https://memory.example.com:notaport/mcp", TOKEN),
        ("https://user:pw@memory.example.com/mcp", TOKEN),
        ("https://memory.example.com/mcp?token=x", TOKEN),
        ("https://memory.example.com/mcp", ""),  # off loopback a token is required
        ("https://memory.example.com/mcp", "two words"),
        ("https://memory.example.com/mcp", "line\nbreak"),
    ],
)
def test_unusable_settings_are_refused(url, token):
    with pytest.raises(RemoteConfigError) as exc:
        resolve_remote_target(url, token)
    if token:
        assert token not in str(exc.value)


def test_token_is_not_in_repr():
    assert TOKEN not in repr(TARGET)
    routing = hooks_cli.HookWriteRouting(decision=None, source="remote", remote=TARGET)
    assert TOKEN not in repr(routing)


def test_config_env_wins_over_file(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(
        json.dumps({"hooks": {"remote_url": "https://file.example.com", "remote_token": "file"}}),
        encoding="utf-8",
    )
    config = MempalaceConfig(config_dir=str(tmp_path))
    assert config.hook_remote_url == "https://file.example.com"
    assert config.hook_remote_token == "file"
    monkeypatch.setenv("MEMPALACE_HOOK_REMOTE_URL", URL)
    monkeypatch.setenv("MEMPALACE_HOOK_REMOTE_TOKEN", TOKEN)
    assert config.hook_remote_url == URL
    assert config.hook_remote_token == TOKEN


def test_timeout_setting(monkeypatch):
    assert hook_remote.remote_timeout() == hook_remote.REMOTE_TIMEOUT_DEFAULT_S
    monkeypatch.setenv("MEMPALACE_HOOK_REMOTE_TIMEOUT", "3")
    assert hook_remote.remote_timeout() == 3.0
    monkeypatch.setenv("MEMPALACE_HOOK_REMOTE_TIMEOUT", "-1")
    assert hook_remote.remote_timeout() == hook_remote.REMOTE_TIMEOUT_DEFAULT_S


# --- Transport ---


def test_call_tool_sends_one_authenticated_request():
    with patch.object(
        hook_remote._net, "urlopen", return_value=_tool_response({"success": True})
    ) as opened:
        result = hook_remote.call_tool(TARGET, "mempalace_diary_write", {"entry": "x"})
    assert result == {"success": True}
    assert opened.call_count == 1
    request = opened.call_args.args[0]
    assert request.full_url == URL
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    body = json.loads(request.data)
    assert body["method"] == "tools/call"
    assert body["params"] == {"name": "mempalace_diary_write", "arguments": {"entry": "x"}}
    assert opened.call_args.kwargs["timeout"] == hook_remote.REMOTE_TIMEOUT_DEFAULT_S


def test_call_tool_sends_no_auth_header_without_token():
    with patch.object(
        hook_remote._net, "urlopen", return_value=_tool_response({"success": True})
    ) as opened:
        hook_remote.call_tool(RemoteTarget("http://127.0.0.1:1/mcp"), "t", {})
    assert opened.call_args.args[0].get_header("Authorization") is None


@pytest.mark.parametrize(
    "side_effect, reason",
    [
        (urllib.error.HTTPError(URL, 401, "Unauthorized", {}, io.BytesIO()), "HTTP 401"),
        (urllib.error.HTTPError(URL, 302, f"to {TOKEN}", {}, io.BytesIO()), "HTTP 302"),
        (urllib.error.URLError(f"refused {TOKEN}"), "URLError"),
        (TimeoutError(TOKEN), "TimeoutError"),
    ],
)
def test_transport_failures_are_reported_without_detail(side_effect, reason):
    with patch.object(hook_remote._net, "urlopen", side_effect=side_effect) as opened:
        with pytest.raises(RemoteCallFailed) as exc:
            hook_remote.call_tool(TARGET, "t", {})
    assert str(exc.value) == reason
    assert opened.call_count == 1


@pytest.mark.parametrize(
    "response, reason",
    [
        (_Response(b"not json"), "unreadable response"),
        (_Response(b"[]"), "unreadable response"),
        (_Response(b'{"result": {}}'), "unreadable response"),
        (_Response(b'{"error": {"message": "read only"}}'), "read only"),
        (_tool_response({"success": False, "error": "refused"}), "refused"),
        (_tool_response({"success": False}), "the tool reported failure"),
    ],
)
def test_server_refusals_raise(response, reason):
    with patch.object(hook_remote._net, "urlopen", return_value=response):
        with pytest.raises(RemoteCallFailed, match=reason):
            hook_remote.call_tool(TARGET, "t", {})


# --- Routing ---


def test_unconfigured_routing_is_unchanged(state_dir):
    routing = hooks_cli._compute_hook_write_routing()
    assert routing.use_remote is False
    assert routing.decision is not None


@pytest.mark.usefixtures("remote_env")
def test_remote_overrides_the_daemon_policy(state_dir, monkeypatch):
    monkeypatch.setenv("MEMPALACE_HOOK_WRITE_ROUTING", "require")
    with patch.object(hooks_cli, "_daemon_available") as probe:
        routing = hooks_cli._compute_hook_write_routing()
    assert routing.use_remote and not routing.blocked and not routing.use_daemon
    probe.assert_not_called()


def test_invalid_remote_blocks_the_hook(state_dir, monkeypatch):
    monkeypatch.setenv("MEMPALACE_HOOK_REMOTE_URL", "http://memory.example.com/mcp")
    monkeypatch.setenv("MEMPALACE_HOOK_REMOTE_TOKEN", TOKEN)
    routing = hooks_cli._compute_hook_write_routing()
    assert routing.blocked and not routing.use_remote
    assert "https" in routing.notice
    assert TOKEN not in routing.notice


def test_remote_url_passes_the_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks_cli, "PALACE_ROOT", tmp_path / "absent")
    monkeypatch.setattr(hooks_cli, "_config_root", lambda: tmp_path / "absent-too")
    assert hooks_cli._palace_root_exists() is False
    monkeypatch.setenv("MEMPALACE_HOOK_REMOTE_URL", URL)
    assert hooks_cli._palace_root_exists() is True


# --- The checkpoint child never writes locally in remote mode ---


def _payload(**extra):
    return {"entry": "CHECKPOINT:x", "session_id": "s", "exchange_count": 15, **extra}


@pytest.mark.usefixtures("remote_env")
def test_child_sends_remote_and_acks(state_dir):
    with (
        patch.object(
            hooks_cli, "call_tool", return_value={"success": True, "entry_id": "e1"}
        ) as call,
        patch("mempalace.hub_client.forward_tool_call") as forward,
        patch.object(hooks_cli, "_diary_write_in_process") as local,
    ):
        assert hooks_cli._apply_diary_checkpoint(_payload(remote=True)) is True
    assert call.call_args.args[1] == "mempalace_diary_write"
    forward.assert_not_called()
    local.assert_not_called()
    assert (state_dir / "s_last_save").read_text(encoding="utf-8") == "15"


@pytest.mark.usefixtures("remote_env")
def test_child_failure_does_not_write_locally(state_dir):
    with (
        patch.object(hooks_cli, "call_tool", side_effect=RemoteCallFailed("HTTP 401")),
        patch("mempalace.hub_client.forward_tool_call") as forward,
        patch.object(hooks_cli, "_diary_write_in_process") as local,
    ):
        assert hooks_cli._apply_diary_checkpoint(_payload(remote=True)) is False
    forward.assert_not_called()
    local.assert_not_called()
    assert not (state_dir / "s_last_save").exists()
    assert json.loads((state_dir / "remote_last_error").read_text())["reason"] == "HTTP 401"
    log = (state_dir / "hook.log").read_text(encoding="utf-8")
    assert "HTTP 401" in log
    assert TOKEN not in log


def test_child_with_invalid_remote_does_not_write_locally(state_dir, monkeypatch):
    monkeypatch.setenv("MEMPALACE_HOOK_REMOTE_URL", "http://memory.example.com/mcp")
    monkeypatch.setenv("MEMPALACE_HOOK_REMOTE_TOKEN", TOKEN)
    with patch.object(hooks_cli, "_diary_write_in_process") as local:
        assert hooks_cli._apply_diary_checkpoint(_payload()) is False
    local.assert_not_called()


def test_child_that_lost_its_remote_does_not_write_locally(state_dir):
    with patch.object(hooks_cli, "_diary_write_in_process") as local:
        assert hooks_cli._apply_diary_checkpoint(_payload(remote=True)) is False
    local.assert_not_called()


def test_local_path_is_unchanged_without_remote(state_dir):
    with (
        patch.object(hooks_cli, "call_tool") as call,
        patch("mempalace.hub_client.forward_tool_call", return_value=(False, None)),
        patch.object(hooks_cli, "_diary_write_in_process", return_value={"success": True}) as local,
    ):
        assert hooks_cli._apply_diary_checkpoint(_payload()) is True
    call.assert_not_called()
    local.assert_called_once()


# --- Mining and the hook handlers ---


@pytest.mark.usefixtures("remote_env")
def test_mines_are_skipped_in_remote_mode(state_dir, transcript, monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPAL_DIR", str(tmp_path))
    with (
        patch.object(hooks_cli, "_spawn_mine") as spawn,
        patch.object(hooks_cli.subprocess, "run") as run,
    ):
        hooks_cli._ingest_transcript(transcript)
        hooks_cli._maybe_auto_ingest()
        hooks_cli._mine_sync()
    spawn.assert_not_called()
    run.assert_not_called()


@pytest.mark.usefixtures("remote_env")
def test_queued_payload_holds_no_token(state_dir, transcript):
    with patch.object(hooks_cli, "_spawn_mine", return_value=True) as spawn:
        result = hooks_cli._save_diary_direct(transcript, "sess", wing="w", agent_name="claude")
    assert result["checkpoint_queued"] is True
    written = (state_dir / "diary_checkpoint_sess.json").read_text(encoding="utf-8")
    assert json.loads(written)["remote"] is True
    assert TOKEN not in written
    assert not any(TOKEN in str(arg) for arg in spawn.call_args.args[0])


@pytest.mark.usefixtures("remote_env")
def test_precompact_queues_a_diary_entry_and_does_not_mine(state_dir, transcript):
    data = {"session_id": "sess", "transcript_path": transcript}
    with (
        patch.object(hooks_cli, "_spawn_mine", return_value=True),
        patch.object(hooks_cli, "_mine_sync") as mine,
        patch.object(hooks_cli, "_ingest_transcript") as ingest,
        patch.object(hooks_cli, "_output") as output,
    ):
        hooks_cli.hook_precompact(data, "claude-code")
    assert (state_dir / "diary_checkpoint_sess.json").exists()
    mine.assert_not_called()
    ingest.assert_not_called()
    output.assert_called_once_with({})


def test_failure_notice_is_shown_one_time(state_dir):
    hooks_cli._record_remote_failure("HTTP 503")
    first = hooks_cli._with_remote_failure_notice({})
    assert "HTTP 503" in first["systemMessage"]
    assert hooks_cli._with_remote_failure_notice({}) == {}


def test_failure_notice_keeps_the_existing_message(state_dir):
    hooks_cli._record_remote_failure("HTTP 503")
    message = hooks_cli._with_remote_failure_notice({"systemMessage": "queued"})["systemMessage"]
    assert message.startswith("queued\n")


def test_no_failure_means_no_notice(state_dir):
    assert hooks_cli._with_remote_failure_notice({"a": 1}) == {"a": 1}


@pytest.mark.usefixtures("remote_env")
def test_success_clears_the_recorded_failure(state_dir):
    hooks_cli._record_remote_failure("HTTP 503")
    with patch.object(hooks_cli, "call_tool", return_value={"success": True}):
        hooks_cli._apply_diary_checkpoint(_payload(remote=True))
    assert not (state_dir / "remote_last_error").exists()
