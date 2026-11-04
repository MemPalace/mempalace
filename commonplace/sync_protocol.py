"""Shared wire constants and pure native-session identity helpers."""

from __future__ import annotations

import io
import json
import re
from pathlib import PurePosixPath

from .capture_client import CaptureError, native_id
from .handoff.transcripts import detect_format

PROTOCOL_VERSION = 1
PUSH_WINDOW_SECONDS = 60
MAX_WAIT_SECONDS = 25
LIVE_TICK_SECONDS = 2
SYNC_FEED = "sync"
SYNC_KINDS = frozenset({"sync.capture", "sync.request"})
SYNC_ENVELOPE_KEYS = frozenset({"feed", "events", "seq", "more", "reset", "waited", "state"})
SYNC_EVENT_KEYS = frozenset({"seq", "at", "kind", "data"})
SYNC_STATE_KEYS = frozenset({"capture_enabled", "interval_seconds", "next_window_at"})
SYNC_RETENTION = 1000
SYNC_PAGE = 200
OP_ID_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"
WRITE_BUDGET = (120, 3600)
TRANSCRIPT_SOURCES = ("claude-code", "codex", "t3")
MEMORY_IMPORT_SOURCE = "native-memory"
MEMORY_FORMAT = "file"
CURSOR_MESSAGE = "Cursor transcripts are unsupported; choose Claude Code or Codex sources."
PROBE_KEY_ALLOWLIST = frozenset(
    {"type", "role", "message", "content", "payload", "sessionId", "agentId", "timestamp", "id"}
)
TOKEN_ENV = {
    "claude-code": "COMMONPLACE_CAPTURE_TOKEN_CLAUDE_CODE",
    "codex": "COMMONPLACE_CAPTURE_TOKEN_CODEX",
    "native-memory": "COMMONPLACE_IMPORT_TOKEN",
}
RETRY_RE = r"retry in (\d+) seconds"


def native_source(source, fmt, text) -> str | None:
    """Resolve T3's underlying native format without guessing from values."""
    if source in ("claude-code", "codex"):
        return source
    if source != "t3":
        return None
    if fmt in ("claude-code", "codex"):
        return fmt
    if fmt != "auto":
        return None
    try:
        detected = detect_format(text)
    except (ValueError, TypeError, RecursionError):
        return None
    return detected if detected in ("claude-code", "codex") else None


def native_identity(raw: bytes, native_source) -> str | None:
    """Use the capture client's bounded identity parser; invalid input is ungrouped."""
    if native_source not in ("claude-code", "codex") or not isinstance(raw, bytes):
        return None
    try:
        return native_id(io.BytesIO(raw), native_source)
    except (CaptureError, ValueError, TypeError, RecursionError):
        return None


def live_tail(text: str) -> bool:
    """Whether the last physical line is an incomplete, unterminated JSON value."""
    if not isinstance(text, str) or not text or text.endswith(("\r", "\n")):
        return False
    last = re.split(r"\r\n|\r|\n", text)[-1]
    if not last.strip():
        return False
    try:
        json.loads(last)
    except (ValueError, TypeError, RecursionError):
        return True
    return False


def memory_thread_id(label, relpath, sha256_hex) -> str:
    """Validate a portable relative memory-file identity."""
    if (
        not isinstance(label, str)
        or re.fullmatch(r"[a-z0-9-]{1,32}", label) is None
        or not isinstance(relpath, str)
        or not relpath
        or "\x00" in relpath
        or PurePosixPath(relpath).is_absolute()
        or ".." in relpath.split("/")
        or not isinstance(sha256_hex, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256_hex) is None
    ):
        raise ValueError("Invalid memory identity")
    identity = f"{label}:{relpath}:{sha256_hex}"
    if len(identity) > 512:
        raise ValueError("Invalid memory identity")
    return identity
