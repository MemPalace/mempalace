"""Owner-only local eval archives of stored threads (slice 1).

An eval **run** is an immutable observation of one eligible stored thread's
committed rows at one read snapshot, plus optional caller artifacts and a
manifest. Runs live in ``<workspace>/evals/runs.sqlite3``. Nothing here
scores, judges, replays, opens a network connection, starts a process or
calls a model. ``eval purge`` is the only delete, and only by run ID.

Every refusal is an :class:`EvalError` whose text is a fixed code, never
payload, key or source metadata. The CLI prints it as ``commonplace: <code>``
with exit 2.
"""

from __future__ import annotations

import hashlib
import re
import struct

from ..control_db import ControlError

SCHEMA = 1
EVAL_RUN_MAX_BYTES = 16 * 1024 * 1024
EVAL_CHUNK_BYTES = 256 * 1024
EVAL_WORKSPACE_QUOTA = 1024 * 1024 * 1024
EVAL_KEY_TOMBSTONE_RETENTION = 30 * 24 * 60 * 60
# Bounded wait for every eval lock: the eval DB, the WAL switch and the
# thread-store writer lock. A longer wait refuses with ``eval_busy``.
EVAL_LOCK_WAIT_S = 5.0

KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,64}")
RUN_ID_PATTERN = re.compile(r"run_[0-9a-f]{24}")
STORED_ID_PATTERN = re.compile(r"thr_[0-9a-fA-F]+")
_KEY_DOMAIN = b"commonplace-eval-key-v1\0"


class EvalError(ControlError):
    """An eval refusal with a fixed, metadata-free message."""


def key_digest(workspace_id: str, key: str) -> str:
    """Domain-separated, length-prefixed digest; the key itself is never stored."""
    if not isinstance(key, str) or KEY_PATTERN.fullmatch(key) is None:
        raise EvalError("eval_key_invalid")
    workspace = workspace_id.encode("utf-8")
    secret = key.encode("ascii")
    return hashlib.sha256(
        _KEY_DOMAIN
        + struct.pack(">I", len(workspace))
        + workspace
        + struct.pack(">I", len(secret))
        + secret
    ).hexdigest()
