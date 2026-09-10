"""Generic harness adapters for mempalace.hooks_cli.

The save policy (interval, silent diary, SessionEnd flush) is shared.
Each harness only supplies payload aliases, transcript location, and
user-turn counting. Grok and Copilot are the first non-Claude/Codex adapters.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest

import mempalace.hooks_cli as hooks_cli_mod
from mempalace.hooks_cli import (
    SAVE_INTERVAL,
    _count_human_messages,
    _diary_agent_for_harness,
    _extract_recent_messages,
    _parse_harness_input,
    hook_stop,
)
from tests.test_hooks_cli import _capture_hook_output, _write_transcript


@pytest.fixture(autouse=True)
def _isolated_palace_root(monkeypatch, tmp_path):
    root = tmp_path / ".mempalace"
    state_dir = root / "hook_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hooks_cli_mod, "PALACE_ROOT", root)
    monkeypatch.setattr(hooks_cli_mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(hooks_cli_mod, "_MINE_PID_DIR", state_dir / "mine_pids")
    monkeypatch.setattr(hooks_cli_mod, "_state_dir_initialized", False)
    return root


def _grok_user(text: str, prompt_index: int | None = None, synthetic: str | None = None) -> dict:
    rec: dict = {"type": "user", "content": [{"type": "text", "text": text}]}
    if prompt_index is not None:
        rec["prompt_index"] = prompt_index
    if synthetic is not None:
        rec["synthetic_reason"] = synthetic
    return rec


def _write_grok_history(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


# --- parse aliases ---


def test_parse_grok_camelcase_envelope():
    result = _parse_harness_input(
        {
            "hookEventName": "stop",
            "sessionId": "sid-1",
            "cwd": "/Users/me/Projects/Engram",
            "reason": "end_turn",
            "subagentType": None,
        },
        "grok",
    )
    assert result["session_id"] == "sid-1"
    assert result["cwd"] == "/Users/me/Projects/Engram"
    assert result["stop_reason"] == "end_turn"
    assert result["transcript_path"] == ""
    assert not result["subagent_type"]


def test_parse_grok_env_fallbacks():
    result = _parse_harness_input(
        {},
        "grok",
        env={
            "GROK_SESSION_ID": "sid-2",
            "GROK_WORKSPACE_ROOT": "/tmp/proj",
        },
    )
    assert result["session_id"] == "sid-2"
    assert result["cwd"] == "/tmp/proj"


def test_parse_claude_snake_case_unchanged():
    result = _parse_harness_input(
        {
            "session_id": "abc-123",
            "stop_hook_active": True,
            "transcript_path": "/tmp/t.jsonl",
        },
        "claude-code",
    )
    assert result["session_id"] == "abc-123"
    assert result["stop_hook_active"] is True
    assert result["transcript_path"] == "/tmp/t.jsonl"


def test_parse_unknown_harness_uses_generic_aliases():
    """A new harness must parse, not sys.exit, so wrappers can pass any token."""
    result = _parse_harness_input(
        {"sessionId": "new-1", "transcriptPath": "/tmp/x.jsonl", "cwd": "/tmp/app"},
        "gemini",
    )
    assert result["session_id"] == "new-1"
    assert result["transcript_path"] == "/tmp/x.jsonl"
    assert result["cwd"] == "/tmp/app"


def test_parse_cursor_conversation_id_as_session():
    result = _parse_harness_input(
        {
            "conversation_id": "conv-9",
            "transcript_path": "/tmp/cursor.jsonl",
            "workspace_roots": ["/tmp/ws"],
        },
        "cursor",
    )
    assert result["session_id"] == "conv-9"
    assert result["cwd"] == "/tmp/ws"


def test_parse_auto_detects_grok_from_env():
    result = _parse_harness_input(
        {"sessionId": "auto-sid", "cwd": "/tmp/proj"},
        "auto",
        env={"GROK_HOOK_EVENT": "stop", "GROK_SESSION_ID": "auto-sid"},
    )
    assert result["session_id"] == "auto-sid"
    assert (
        hooks_cli_mod._detect_harness(
            {"sessionId": "auto-sid"},
            {"GROK_HOOK_EVENT": "stop"},
        )
        == "grok"
    )


# --- detect ---


def test_detect_harness_prefers_explicit_env():
    assert (
        hooks_cli_mod._detect_harness(
            {}, {"MEMPALACE_HOOK_HARNESS": "codex", "GROK_SESSION_ID": "x"}
        )
        == "codex"
    )


def test_detect_harness_grok_from_session_env():
    assert hooks_cli_mod._detect_harness({}, {"GROK_SESSION_ID": "s"}) == "grok"


def test_detect_harness_defaults_to_claude_code():
    assert (
        hooks_cli_mod._detect_harness({"session_id": "s", "transcript_path": "/t.jsonl"}, {})
        == "claude-code"
    )


# --- locate ---


def test_locate_grok_chat_history_urlencoded_cwd(tmp_path):
    cwd = "/Users/vijay/Projects/Engram"
    sid = "01abc"
    dest = tmp_path / quote(cwd, safe="") / sid
    history = dest / "chat_history.jsonl"
    _write_grok_history(history, [_grok_user("<user_query>\nhi\n</user_query>", prompt_index=0)])
    located = hooks_cli_mod._locate_transcript(
        "grok",
        {"session_id": sid, "cwd": cwd, "transcript_path": ""},
        sessions_root=tmp_path,
    )
    assert located == str(history.resolve())


def test_locate_grok_falls_back_to_session_glob(tmp_path):
    sid = "sess-glob"
    dest = tmp_path / "slug-and-hash" / sid
    history = dest / "chat_history.jsonl"
    _write_grok_history(history, [_grok_user("x", prompt_index=0)])
    located = hooks_cli_mod._locate_transcript(
        "grok",
        {"session_id": sid, "cwd": "/does/not/match", "transcript_path": ""},
        sessions_root=tmp_path,
    )
    assert located == str(history.resolve())


def test_locate_keeps_explicit_transcript_path(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [{"message": {"role": "user", "content": "hi"}}])
    located = hooks_cli_mod._locate_transcript(
        "claude-code",
        {"session_id": "s", "cwd": "", "transcript_path": str(transcript)},
    )
    assert Path(located).resolve() == transcript.resolve()


# --- count / extract ---


def test_count_grok_prompt_index_turns_skips_synthetic(tmp_path):
    transcript = tmp_path / "chat_history.jsonl"
    _write_grok_history(
        transcript,
        [
            _grok_user("<user_info>\nWorkspace Path: /tmp/proj\n</user_info>"),
            _grok_user("<system-reminder>skills</system-reminder>", synthetic="system_reminder"),
            _grok_user("<user_query>\nfirst\n</user_query>", prompt_index=0),
            {"type": "assistant", "content": "ok"},
            _grok_user("<user_query>\nsecond\n</user_query>", prompt_index=1),
        ],
    )
    assert _count_human_messages(str(transcript)) == 2


def test_extract_grok_strips_user_query_tags(tmp_path):
    transcript = tmp_path / "chat_history.jsonl"
    _write_grok_history(
        transcript,
        [
            _grok_user("<user_info>meta</user_info>"),
            _grok_user("<user_query>\nHow does storage work?\n</user_query>", prompt_index=0),
            _grok_user("<user_query>\nwire the hook\n</user_query>", prompt_index=1),
        ],
    )
    assert _extract_recent_messages(str(transcript)) == [
        "How does storage work?",
        "wire the hook",
    ]


def test_count_claude_transcript_still_counts_role_user(tmp_path):
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


# --- wing / diary agent ---


def test_wing_from_cwd_uses_leaf():
    assert hooks_cli_mod._wing_from_cwd("/Users/vijay/Projects/Engram") == "wing_engram"


def test_diary_agent_for_grok():
    assert _diary_agent_for_harness("grok") == "grok"


# --- stop hook ---


def test_stop_hook_grok_saves_at_interval(tmp_path):
    cwd = "/Users/vijay/Projects/Engram"
    sid = "grok-sess"
    dest = tmp_path / "sessions" / quote(cwd, safe="") / sid
    records = [
        _grok_user(f"<user_query>\nq{i}\n</user_query>", prompt_index=i)
        for i in range(SAVE_INTERVAL)
    ]
    _write_grok_history(dest / "chat_history.jsonl", records)

    save_result = {"count": 15, "themes": ["hooks"]}
    with patch("mempalace.hooks_cli._save_diary_direct", return_value=save_result) as mock_save:
        with patch.object(hooks_cli_mod, "_grok_sessions_root", return_value=tmp_path / "sessions"):
            result = _capture_hook_output(
                hook_stop,
                {
                    "sessionId": sid,
                    "cwd": cwd,
                    "reason": "end_turn",
                },
                harness="grok",
                state_dir=tmp_path,
            )
    assert "systemMessage" in result
    kwargs = mock_save.call_args.kwargs
    assert kwargs["agent_name"] == "grok"
    assert kwargs["wing"] == "wing_engram"


def test_stop_hook_grok_skips_subagent(tmp_path):
    with patch("mempalace.hooks_cli._save_diary_direct") as mock_save:
        result = _capture_hook_output(
            hook_stop,
            {
                "sessionId": "sid",
                "cwd": "/tmp/proj",
                "reason": "end_turn",
                "subagentType": "explore",
            },
            harness="grok",
            state_dir=tmp_path,
        )
    assert result == {}
    mock_save.assert_not_called()


def test_stop_hook_grok_skips_session_teardown_stop(tmp_path):
    with patch("mempalace.hooks_cli._save_diary_direct") as mock_save:
        result = _capture_hook_output(
            hook_stop,
            {
                "sessionId": "sid",
                "cwd": "/tmp/proj",
                "reason": "shutdown",
            },
            harness="grok",
            state_dir=tmp_path,
        )
    assert result == {}
    mock_save.assert_not_called()


# --- copilot ---


def _copilot_user(text: str) -> dict:
    return {
        "type": "user.message",
        "data": {"content": text, "turnId": "0"},
        "id": "evt",
        "timestamp": "2026-09-10T00:00:00Z",
    }


def _write_copilot_events(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_parse_copilot_camelcase_agent_stop():
    result = _parse_harness_input(
        {
            "sessionId": "copilot-sid",
            "cwd": "/Users/me/app",
            "transcriptPath": "/tmp/events.jsonl",
            "stopReason": "end_turn",
            "stop_hook_active": False,
        },
        "copilot",
    )
    assert result["session_id"] == "copilot-sid"
    assert result["cwd"] == "/Users/me/app"
    assert result["transcript_path"] == "/tmp/events.jsonl"
    assert result["stop_reason"] == "end_turn"
    assert result["harness"] == "copilot"


def test_parse_copilot_vscode_compatible_snake_case():
    result = _parse_harness_input(
        {
            "hook_event_name": "Stop",
            "session_id": "sid-vs",
            "cwd": "/tmp/ws",
            "transcript_path": "/tmp/ws/events.jsonl",
            "stop_reason": "end_turn",
        },
        "copilot",
    )
    assert result["session_id"] == "sid-vs"
    assert result["stop_reason"] == "end_turn"
    assert result["transcript_path"] == "/tmp/ws/events.jsonl"


def test_parse_copilot_subagent_uses_agent_type():
    result = _parse_harness_input(
        {
            "sessionId": "sid",
            "cwd": "/tmp/ws",
            "transcriptPath": "/tmp/events.jsonl",
            "agentType": "explore",
            "stopReason": "end_turn",
        },
        "copilot",
    )
    assert result["subagent_type"] == "explore"


def test_detect_harness_copilot_from_stop_reason_and_transcript():
    assert (
        hooks_cli_mod._detect_harness(
            {
                "sessionId": "s",
                "transcriptPath": "/Users/me/.copilot/session-state/s/events.jsonl",
                "stopReason": "end_turn",
            },
            {},
        )
        == "copilot"
    )


def test_detect_harness_copilot_from_home_env():
    assert hooks_cli_mod._detect_harness({}, {"COPILOT_HOME": "/tmp/copilot-home"}) == "copilot"


def test_locate_copilot_events_jsonl(tmp_path):
    sid = "aaaa-bbbb"
    history = tmp_path / "session-state" / sid / "events.jsonl"
    _write_copilot_events(history, [_copilot_user("hello")])
    located = hooks_cli_mod._locate_transcript(
        "copilot",
        {"session_id": sid, "cwd": "/tmp/app", "transcript_path": ""},
        sessions_root=tmp_path / "session-state",
    )
    assert located == str(history.resolve())


def test_count_copilot_user_message_events(tmp_path):
    transcript = tmp_path / "events.jsonl"
    _write_copilot_events(
        transcript,
        [
            {"type": "session.start", "data": {"sessionId": "s"}},
            _copilot_user("first"),
            {"type": "system.message", "data": {"content": "You are Copilot"}},
            _copilot_user("second"),
            {"type": "assistant.turn_end", "data": {"turnId": "1"}},
        ],
    )
    assert _count_human_messages(str(transcript)) == 2


def test_extract_copilot_user_message_content(tmp_path):
    transcript = tmp_path / "events.jsonl"
    _write_copilot_events(
        transcript,
        [_copilot_user("How does storage work?"), _copilot_user("wire the hook")],
    )
    assert _extract_recent_messages(str(transcript)) == [
        "How does storage work?",
        "wire the hook",
    ]


def test_diary_agent_for_copilot():
    assert _diary_agent_for_harness("copilot") == "copilot"


def test_stop_hook_copilot_saves_at_interval(tmp_path):
    sid = "copilot-sess"
    dest = tmp_path / "session-state" / sid
    records = [_copilot_user(f"q{i}") for i in range(SAVE_INTERVAL)]
    _write_copilot_events(dest / "events.jsonl", records)

    save_result = {"count": 15, "themes": ["hooks"]}
    with patch("mempalace.hooks_cli._save_diary_direct", return_value=save_result) as mock_save:
        with patch.object(
            hooks_cli_mod, "_copilot_sessions_root", return_value=tmp_path / "session-state"
        ):
            result = _capture_hook_output(
                hook_stop,
                {
                    "sessionId": sid,
                    "cwd": "/Users/vijay/Projects/Engram",
                    "stopReason": "end_turn",
                },
                harness="copilot",
                state_dir=tmp_path,
            )
    assert "systemMessage" in result
    kwargs = mock_save.call_args.kwargs
    assert kwargs["agent_name"] == "copilot"
    assert kwargs["wing"] == "wing_engram"


def test_stop_hook_copilot_skips_non_end_turn_reason(tmp_path):
    with patch("mempalace.hooks_cli._save_diary_direct") as mock_save:
        result = _capture_hook_output(
            hook_stop,
            {
                "sessionId": "sid",
                "cwd": "/tmp/proj",
                "stopReason": "abort",
            },
            harness="copilot",
            state_dir=tmp_path,
        )
    assert result == {}
    mock_save.assert_not_called()
