"""Compatibility helpers for legacy shell hooks.

The shell hooks intentionally stay small and portable, but parsing Claude
hook JSON and counting UTF-8 JSONL transcripts is safer in Python than in
inline shell snippets. This module centralizes that behavior for both
hooks/mempal_save_hook.sh and hooks/mempal_precompact_hook.sh.
"""

from __future__ import annotations

import json
import re
import sys


_SESSION_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")
_CONTROL_CHARS_RE = re.compile(r"[\x00\r\n]")


def sanitize_session_id(session_id: object) -> str:
    """Keep session ids safe for state-file names."""
    sanitized = _SESSION_ID_RE.sub("", str(session_id or ""))
    return sanitized or "unknown"


def normalize_transcript_path(path: object) -> str:
    r"""Normalize a hook transcript path without destroying Windows paths.

    Claude Code on Windows sends paths like:

        C:\Users\me\.claude\projects\<project>\<session>.jsonl

    The old shell sanitizer removed both the drive-letter colon and
    backslashes. That turned a valid transcript path into a nonexistent path.
    For transcript paths, we only remove control characters that would break
    newline-delimited shell parsing, and normalize backslashes to forward
    slashes so Git Bash can still address the same Windows file.
    """

    normalized = str(path or "").replace("\\", "/")
    return _CONTROL_CHARS_RE.sub("", normalized)


def _stop_hook_active(value: object) -> str:
    """Return the exact boolean string expected by the shell hook."""
    if value is True:
        return "True"
    if str(value).strip().lower() in ("true", "1", "yes"):
        return "True"
    return "False"


def parse_stop_payload(payload: dict) -> tuple[str, str, str]:
    return (
        sanitize_session_id(payload.get("session_id", "")),
        _stop_hook_active(payload.get("stop_hook_active", False)),
        normalize_transcript_path(payload.get("transcript_path", "")),
    )


def parse_precompact_payload(payload: dict) -> tuple[str, str]:
    return (
        sanitize_session_id(payload.get("session_id", "")),
        normalize_transcript_path(payload.get("transcript_path", "")),
    )


def count_human_messages(path: str) -> int:
    """Count user messages in a Claude transcript JSONL file.

    Claude transcripts are UTF-8. Windows Python defaults to cp1252 in many
    environments, so the encoding must be explicit. Invalid bytes are ignored
    to match the hooks' fail-soft behavior.
    """

    count = 0
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except Exception:
                continue

            msg = entry.get("message", {})
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue

            content = msg.get("content", "")
            if isinstance(content, str) and "<command-message>" in content:
                continue

            count += 1

    return count


def _load_stdin_json() -> dict:
    raw = sys.stdin.read()

    # Empty stdin is a legitimate hook state. Treat it as an empty payload so
    # the sentinel is printed and the shell fail-loud guard does not spam disk.
    if raw == "":
        return {}

    # For non-empty malformed input, intentionally let json.loads raise.
    # The shell hooks capture this stderr in last_python_err.log and, because
    # no sentinel is printed, write a bounded copy of the raw payload to
    # last_input.log. That fail-loud contract is pinned by
    # tests/test_hooks_bash_compat.py.
    data = json.loads(raw)

    if not isinstance(data, dict):
        raise TypeError(f"hook input must be a JSON object, got {type(data).__name__}")

    return data


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(
            "usage: python -m mempalace.hook_shell <parse-stop|parse-precompact|count-human-messages>",
            file=sys.stderr,
        )
        return 2

    command = argv[0]

    if command == "parse-stop":
        session_id, stop_hook_active, transcript_path = parse_stop_payload(_load_stdin_json())
        print("__MEMPAL_PARSE_OK__")
        print(session_id)
        print(stop_hook_active)
        print(transcript_path)
        return 0

    if command == "parse-precompact":
        session_id, transcript_path = parse_precompact_payload(_load_stdin_json())
        print("__MEMPAL_PARSE_OK__")
        print(session_id)
        print(transcript_path)
        return 0

    if command == "count-human-messages":
        if len(argv) != 2:
            print("count-human-messages requires a transcript path", file=sys.stderr)
            return 2
        try:
            print(count_human_messages(argv[1]))
        except Exception:
            print(0)
        return 0

    print(f"unknown hook_shell command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
