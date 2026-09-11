"""Side-effect-free write-ahead log for MemPalace write operations.

This lives in its own module so callers that only need WAL audit logging — the
CLI ``sync`` path and the daemon's ``service`` layer — can obtain ``_wal_log``
without importing :mod:`mempalace.mcp_server`. Importing ``mcp_server`` runs its
module-level stdio protection (``os.dup2(2, 1)`` and ``sys.stdout = sys.stderr``,
required so the MCP stdio JSON stream isn't corrupted by C-level library
banners). In a non-MCP process — e.g. the daemon worker or ``mempalace sync`` —
that redirect is an unwanted import side effect that misroutes operator output,
so the WAL machinery is kept here, free of any such side effects.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_WAL_FILE = Path(os.path.expanduser("~/.mempalace/wal")) / "write_log.jsonl"
_WAL_INITIALIZED_DIR = None

# Keys whose values should be redacted in WAL entries to avoid logging sensitive content
_WAL_REDACT_KEYS = frozenset(
    {"content", "content_preview", "document", "entry", "entry_preview", "query", "text"}
)


def _ensure_wal() -> None:
    """Create (and re-harden) the WAL directory lazily, on the first write.

    This must NOT run at import time: a user who removed ``~/.mempalace`` has
    engaged the documented kill-switch (``hooks_cli._palace_root_exists()``,
    #1305), and recreating the directory just by importing this module would
    silently re-arm the autosave/mining hooks they disabled (#1676). Creating
    it on the first real write keeps the kill-switch contract intact.

    It is deliberately not gated on ``_palace_root_exists()``: by the time a
    write reaches here the palace is already being recreated by the ChromaDB/KG
    layer regardless, so gating would only drop audit records, not prevent
    recreation. Runtime kill-switch enforcement for MCP writes is the broader
    question tracked in #504.

    Hardening is attempted once per directory and the path cached in
    ``_WAL_INITIALIZED_DIR`` regardless of outcome (keyed on the path, so a
    test repointing ``_WAL_FILE`` re-initialises), so a persistent failure on a
    restricted filesystem does not retry on every write. ``mkdir`` runs only
    when the initial ``chmod`` raises ``FileNotFoundError`` (EAFP). The parent
    ``~/.mempalace`` keeps its umask mode, like the other palace directories;
    the WAL file is created atomically with mode 0o600 by ``_wal_log``.
    """
    global _WAL_INITIALIZED_DIR
    wal_dir = _WAL_FILE.parent
    if _WAL_INITIALIZED_DIR == wal_dir:
        return
    try:
        wal_dir.chmod(0o700)
    except FileNotFoundError:
        try:
            wal_dir.mkdir(parents=True, exist_ok=True)
            wal_dir.chmod(0o700)
        except (OSError, NotImplementedError):
            pass
    except (OSError, NotImplementedError):
        pass
    # Cache regardless of outcome: one attempt per directory, so a persistent
    # chmod/mkdir failure (restricted FS) is not retried on every write.
    _WAL_INITIALIZED_DIR = wal_dir


def _wal_log(operation: str, params: dict, result: dict = None):
    """Append a write operation to the write-ahead log.

    Preserves the existing contract: callers write *before* the backend
    mutation so the intent entry survives a crash in the mutation itself. The
    ``result`` argument is kept for back-compat but is not expected to be
    supplied at the call site — tools that want to record the write outcome
    should follow up with :func:`_wal_result`, which appends a second entry
    carrying the actual outcome for that operation (success/error and any ids
    it produced). A WAL for a successful write therefore reads as two lines:
    the pre-mutation intent (``result: null``) and the post-mutation outcome
    (``result: {...success...}``). Consumers that select by
    ``result is None`` naturally pick up the intent entry; consumers that
    select by ``result == {"success": True, ...}`` pick up the outcome. This
    keeps the existing 8 call sites unchanged and adds the observation the
    issue #538 reporter was missing.
    """
    # Redact sensitive content from params before logging
    safe_params = {}
    for k, v in params.items():
        if k in _WAL_REDACT_KEYS:
            safe_params[k] = f"[REDACTED {len(v)} chars]" if isinstance(v, str) else "[REDACTED]"
        else:
            safe_params[k] = v
    entry = {
        "timestamp": datetime.now().isoformat(),
        "operation": operation,
        "params": safe_params,
        "result": result,
    }
    try:
        # Dir setup shares the append's exception handler below: any WAL
        # failure is logged and non-fatal, never crashing the tool call.
        _ensure_wal()
        fd = os.open(str(_WAL_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.error(f"WAL write failed: {e}")


def _wal_result(operation: str, result: dict):
    """Append an outcome entry to the write-ahead log for a prior intent.

    Called by write tools after the backend mutation completes — either on
    success or on the failure paths they catch. The resulting WAL line for a
    single logical write is then two entries: the pre-mutation intent from
    :func:`_wal_log` (``result: null``) followed by this outcome (``result``
    populated). This is the "result is structurally always null" gap closed by
    issue #538 — the WAL now answers "did this land?" with the tool's actual
    return value, so a replay script can distinguish completed writes (skip
    them) from pending ones (redo them) using the intent entry as the key.

    The entry deliberately carries no ``params`` (the intent line owns those);
    this line is outcome-only: timestamp, operation, result. Same file, same
    append-only discipline, same redaction of sensitive keys, same non-fatal
    error contract.
    """
    entry = {
        "timestamp": datetime.now().isoformat(),
        "operation": operation,
        "result": result,
    }
    try:
        _ensure_wal()
        fd = os.open(str(_WAL_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.error(f"WAL result write failed: {e}")
