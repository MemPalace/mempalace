"""Interchange log for live capture of any agent: one JSON object per line.

An agent without a native Claude Code or Codex transcript appends one record
per message:

    {"agent": "gemini-cli", "session_id": "s-42", "role": "user",
     "content": "exact words", "timestamp": "2026-11-11T09:30:00Z"}

``agent`` names the producing agent and ``session_id`` its own session; both are
kept verbatim in every stored record, and the capture identity is
``<agent>:<session_id>``. Parsing, privacy and idempotency are those of the
native capture sources; this module only validates and names the records.
"""

from __future__ import annotations

import json
import re
from io import StringIO

SOURCE = "agent-log"
# Native sources keep their own formats, so their names cannot label a log.
RESERVED_AGENTS = frozenset({"claude-code", "codex", "t3", SOURCE})
AGENT_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_MAX_SESSION = 400
_MAX_ID = 512


def _content(record) -> bool:
    content = record.get("content")
    return isinstance(content, (str, list)) or (
        content is None and ("tool_calls" in record or "function_call" in record)
    )


def identity(record) -> str | None:
    """The capture identity of one record, or None when it lacks valid provenance."""
    if not isinstance(record, dict):
        return None
    agent, session = record.get("agent"), record.get("session_id")
    if (
        not isinstance(agent, str)
        or AGENT_PATTERN.fullmatch(agent) is None
        or agent in RESERVED_AGENTS
        or not isinstance(session, str)
        or not session.strip()
        or len(session) > _MAX_SESSION
        or any(ord(c) < 32 for c in session)
        or not isinstance(record.get("role"), str)
        or not _content(record)
    ):
        return None
    return f"{agent}:{session}"


def valid_thread_id(thread_id) -> bool:
    agent, sep, session = thread_id.partition(":") if isinstance(thread_id, str) else ("", "", "")
    return (
        bool(sep)
        and len(thread_id) <= _MAX_ID
        and identity({"agent": agent, "session_id": session, "role": "user", "content": ""})
        == thread_id
    )


def first_identity(raw: bytes) -> str | None:
    """Identity of the first non-blank line, for choosing the stream of a local file."""
    for line in raw.splitlines():
        if line.strip():
            try:
                return identity(json.loads(line))
            except (ValueError, RecursionError):
                return None
    return None


def check(thread_id: str, text: str) -> None:
    """Every record must carry the provenance of the stream it is appended to."""
    if not valid_thread_id(thread_id):
        raise ValueError("Invalid agent-log thread_id")
    # Records end at \n, \r or \r\n only, as in the parser. str.splitlines()
    # would also cut at U+2028 or U+0085, which JSON leaves raw inside strings.
    for line in StringIO(text, newline=""):
        try:
            record = json.loads(line) if line.strip() else None
        except RecursionError:
            raise ValueError("Record is nested too deeply") from None
        if record is not None and identity(record) != thread_id:
            raise ValueError("Record provenance differs from thread_id")
