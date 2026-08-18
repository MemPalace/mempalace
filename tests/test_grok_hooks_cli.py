import json
import subprocess
import sys
from pathlib import Path


def test_grok_parser_accepts_camel_case_fields():
    from mempalace.hooks_cli import _parse_harness_input

    parsed = _parse_harness_input(
        {
            "hookEventName": "Stop",
            "sessionId": "grok-session-123",
            "stopHookActive": True,
            "transcriptPath": "/tmp/grok-transcript.jsonl",
        },
        "grok",
    )

    assert parsed["session_id"] == "grok-session-123"
    assert parsed["stop_hook_active"] is True
    assert parsed["transcript_path"] == "/tmp/grok-transcript.jsonl"


def test_grok_parser_keeps_snake_case_fallback():
    from mempalace.hooks_cli import _parse_harness_input

    parsed = _parse_harness_input(
        {
            "session_id": "compat-session",
            "stop_hook_active": False,
            "transcript_path": "/tmp/compat.jsonl",
        },
        "grok",
    )

    assert parsed["session_id"] == "compat-session"
    assert parsed["stop_hook_active"] is False
    assert parsed["transcript_path"] == "/tmp/compat.jsonl"


def test_grok_harness_cli_smoke():
    payload = {
        "hookEventName": "Stop",
        "sessionId": "grok-cli-smoke",
        "stopHookActive": False,
        "transcriptPath": "",
    }

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mempalace.cli",
            "hook",
            "run",
            "--hook",
            "stop",
            "--harness",
            "grok",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        timeout=15,
        check=False,
    )

    assert "Unknown harness: grok" not in result.stderr
    assert result.returncode == 0
