"""
oplog.py — Canonical memory op-log for MemPalace (RFC 004 layer 2, step 2a)
======================================================================

Every mutation of the palace becomes an immutable, provenance-stamped op
stored in ``oplog.sqlite3`` in the palace directory. This is the logstream
pattern (RFC 003) generalized from coordination events to memory itself:
append-only WAL SQLite, per-origin sequence counters, HLC stamps, version
vectors, and idempotent remote apply.

Step 2a ships the op-log in **dual-write shadow** mode (the RFC 004 sequencing plan): the
vector store remains the system of record; every semantic write ALSO emits
an op so divergence is detectable before cutover. In shadow posture an
emission failure must never break the user's memory operation — use
:func:`emit_op` / :func:`shadow_append`, which log loudly and return
``None`` instead of raising. At cutover the op append becomes the
authoritative write and callers switch to :meth:`OpLog.append` directly
(or ``required=True``).

Merge semantics live in RFC 004's merge table; this module only stores and ships
ops. Fold logic (applying ops to derived stores) is a separate consumer.
"""

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mempalace.oplog")

OPLOG_DB_FILENAME = "oplog.sqlite3"

# The complete op vocabulary (the RFC 004 merge table — nothing else exists). Step 2a
# emits only drawer.* and kg.*; the remaining kinds are reserved for 2b
# (registry + hallways/tunnels) so the schema never changes between steps.
OP_KINDS = frozenset(
    {
        "drawer.add",
        "drawer.revise",
        "drawer.tombstone",
        "org.file",
        "org.move",
        "org.tunnel.add",
        "org.tunnel.remove",
        "kg.assert",
        "kg.close",
        "kg.entity.upsert",
        "registry.entity.upsert",
    }
)

# Drawer content is capped at 100k characters (config.sanitize_content), so
# 4 MiB comfortably holds any payload including JSON escaping overhead —
# same ceiling as logstream artifacts.
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024

MAX_LIST_LIMIT = 1000
DEFAULT_LIST_LIMIT = 500

_MAX_AGENT_LENGTH = 256

_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_kind(value) -> str:
    if not isinstance(value, str) or value not in OP_KINDS:
        allowed = ", ".join(sorted(OP_KINDS))
        raise ValueError(f"kind={value!r} is not one of: {allowed}")
    return value


def _sanitize_agent(value, field_name: str = "author_agent") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    value = value.strip()
    if len(value) > _MAX_AGENT_LENGTH:
        raise ValueError(f"{field_name} exceeds maximum length of {_MAX_AGENT_LENGTH} characters")
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in value):
        raise ValueError(f"{field_name} contains control characters")
    return value


def _sanitize_payload(value) -> str:
    """Validate the payload dict and return its canonical JSON text."""
    if not isinstance(value, dict):
        raise ValueError("payload must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload is not JSON-serializable: {exc}") from None
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload exceeds maximum size of {MAX_PAYLOAD_BYTES} bytes")
    return encoded


class OpLog:
    """Durable append-only memory op-log (the RFC 004 op envelope).

    Storage and threading mirror ``Logstream``: one SQLite file in WAL
    mode, a per-instance lock around access, ``check_same_thread=False``
    so the MCP HTTP server can call from worker threads. Multi-process
    writers (hub + CLI mine) are safe: op_id and origin_seq derive from
    the rowid inside the insert transaction, so they can never collide.
    """

    def __init__(self, db_path: str, replica_id: str = None):
        from .replica import get_replica_id

        self.db_path = str(db_path)
        db_parent = Path(self.db_path).parent
        db_parent.mkdir(parents=True, exist_ok=True)
        try:
            db_parent.chmod(0o700)
        except (OSError, NotImplementedError):
            pass
        self.replica_id = replica_id or get_replica_id(str(db_parent))
        self._connection = None
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        from .hlc import HybridLogicalClock

        conn = self._conn()
        conn.executescript("""
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS ops (
                op_id TEXT NOT NULL UNIQUE,
                origin_replica TEXT NOT NULL,
                origin_seq INTEGER,
                hlc TEXT NOT NULL,
                author_agent TEXT NOT NULL,
                authored_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS ops_origin_seq_idx
                ON ops(origin_replica, origin_seq);
            CREATE INDEX IF NOT EXISTS ops_kind_idx ON ops(kind);
            CREATE INDEX IF NOT EXISTS ops_hlc_idx ON ops(hlc);
        """)
        conn.commit()
        # Seed the HLC from the newest stamp so monotonicity survives restarts.
        row = conn.execute("SELECT max(hlc) AS last FROM ops").fetchone()
        self._clock = HybridLogicalClock(self.replica_id, last=row["last"] if row else None)

    def _conn(self):
        if self._connection is None:
            self._connection = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.row_factory = sqlite3.Row
        return self._connection

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    @staticmethod
    def _op_dict(row) -> dict:
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            payload = {}
        return {
            "op_id": row["op_id"],
            # LOCAL arrival cursor; origin_seq is the author's counter and
            # hlc is the cross-replica merge order — same contract as the
            # logstream (RFC 004 A.4).
            "seq": row["rowid"],
            "origin_replica": row["origin_replica"],
            "origin_seq": row["origin_seq"],
            "hlc": row["hlc"],
            "author_agent": row["author_agent"],
            "authored_at": row["authored_at"],
            "kind": row["kind"],
            "payload": payload,
        }

    # ── Local authorship ──────────────────────────────────────────────────

    def append(self, kind: str, payload: dict, author_agent: str = "mcp") -> dict:
        """Append one locally-authored op. Returns the stored op dict.

        The op_id is ``op_<origin_replica>_<origin_seq>`` (the RFC 004 op envelope);
        both derive from the rowid inside the insert transaction, so ids
        are unique even across concurrent writer processes.
        """
        kind = _sanitize_kind(kind)
        author_agent = _sanitize_agent(author_agent)
        payload_json = _sanitize_payload(payload)
        authored_at = _utc_now_iso()
        hlc = self._clock.tick()

        placeholder = f"pending_{secrets.token_hex(8)}"
        with self._lock:
            conn = self._conn()
            with conn:
                cursor = conn.execute(
                    "INSERT INTO ops (op_id, origin_replica, origin_seq, hlc,"
                    " author_agent, authored_at, kind, payload)"
                    " VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                    (
                        placeholder,
                        self.replica_id,
                        hlc,
                        author_agent,
                        authored_at,
                        kind,
                        payload_json,
                    ),
                )
                rowid = cursor.lastrowid
                conn.execute(
                    "UPDATE ops SET op_id = ?, origin_seq = ? WHERE rowid = ?",
                    (f"op_{self.replica_id}_{rowid}", rowid, rowid),
                )
            row = conn.execute("SELECT rowid, * FROM ops WHERE rowid = ?", (rowid,)).fetchone()
        return self._op_dict(row)

    # ── Read / replication surface (mirrors Logstream step 0) ────────────

    def count(self) -> int:
        with self._lock:
            conn = self._conn()
            return conn.execute("SELECT COUNT(*) AS cnt FROM ops").fetchone()["cnt"]

    def kind_histogram(self) -> dict:
        with self._lock:
            conn = self._conn()
            rows = conn.execute("SELECT kind, COUNT(*) AS cnt FROM ops GROUP BY kind").fetchall()
        return {row["kind"]: row["cnt"] for row in rows}

    def version_vector(self) -> dict:
        """{origin_replica: highest origin_seq applied locally}."""
        with self._lock:
            conn = self._conn()
            rows = conn.execute(
                "SELECT origin_replica, max(origin_seq) AS top FROM ops "
                "WHERE origin_seq IS NOT NULL GROUP BY origin_replica"
            ).fetchall()
        return {row["origin_replica"]: row["top"] for row in rows}

    def list_ops(self, origin: str, after_seq: int = 0, limit: int = DEFAULT_LIST_LIMIT) -> list:
        """Ops authored by ``origin`` with origin_seq > after_seq, in author
        order. The anti-entropy pull unit."""
        if not isinstance(origin, str) or not origin.strip():
            raise ValueError("origin must be a non-empty string")
        if not isinstance(after_seq, int) or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, MAX_LIST_LIMIT)
        with self._lock:
            conn = self._conn()
            rows = conn.execute(
                "SELECT rowid, * FROM ops WHERE origin_replica = ? AND origin_seq > ? "
                "ORDER BY origin_seq ASC LIMIT ?",
                (origin.strip(), after_seq, limit),
            ).fetchall()
        return [self._op_dict(row) for row in rows]

    def list_all(self, after_seq: int = 0, limit: int = DEFAULT_LIST_LIMIT, kinds=None) -> list:
        """Page every op in local arrival order (``seq`` cursor).

        ``kinds`` optionally restricts to a set of op kinds. Consumers that
        need cross-replica causal order sort the result by ``hlc``.
        """
        if not isinstance(after_seq, int) or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, MAX_LIST_LIMIT)
        where = ["rowid > ?"]
        params: list = [after_seq]
        if kinds:
            kinds = sorted(set(kinds))
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        params.append(limit)
        with self._lock:
            conn = self._conn()
            rows = conn.execute(
                f"SELECT rowid, * FROM ops WHERE {' AND '.join(where)} ORDER BY rowid ASC LIMIT ?",
                params,
            ).fetchall()
        return [self._op_dict(row) for row in rows]

    def apply_remote_op(self, op: dict) -> bool:
        """Fold one remote op into the local log, idempotently.

        Verbatim rule: the op is stored exactly as authored (op_id, hlc,
        origin stamps, authored_at untouched); only the local rowid — the
        arrival cursor — is ours. Unknown kinds are accepted verbatim
        (a newer peer may speak a newer vocabulary; fold consumers skip
        kinds they don't understand). Returns True if inserted, False if
        already present. Raises ValueError on malformed input.
        """
        for key in (
            "op_id",
            "origin_replica",
            "origin_seq",
            "hlc",
            "author_agent",
            "authored_at",
            "kind",
        ):
            if not op.get(key):
                raise ValueError(f"remote op is missing required field {key!r}")
        if not isinstance(op.get("payload"), dict):
            raise ValueError("remote op payload must be an object")
        if op["origin_replica"] == self.replica_id:
            return False  # our own op echoed back — by definition already present
        if not isinstance(op["origin_seq"], int) or op["origin_seq"] < 1:
            raise ValueError("remote op origin_seq must be a positive integer")
        if not _KIND_RE.match(op["kind"]):
            raise ValueError(f"remote op kind {op['kind']!r} is not a valid kind string")
        payload_json = _sanitize_payload(op["payload"])

        with self._lock:
            conn = self._conn()
            with conn:
                dup = conn.execute(
                    "SELECT 1 FROM ops WHERE op_id = ? OR (origin_replica = ? AND origin_seq = ?)",
                    (op["op_id"], op["origin_replica"], op["origin_seq"]),
                ).fetchone()
                if dup:
                    return False
                conn.execute(
                    "INSERT INTO ops (op_id, origin_replica, origin_seq, hlc,"
                    " author_agent, authored_at, kind, payload)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        op["op_id"],
                        op["origin_replica"],
                        op["origin_seq"],
                        op["hlc"],
                        op["author_agent"],
                        op["authored_at"],
                        op["kind"],
                        payload_json,
                    ),
                )
        # Absorb the remote instant so our next local op sorts after it.
        self._clock.observe(op["hlc"])
        return True


# ── Shared instances + shadow-posture emission ────────────────────────────

_oplog_by_path: dict = {}
_oplog_cache_lock = threading.Lock()


def _canonical_palace_key(palace_path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(palace_path)))


def get_oplog(palace_path: str) -> OpLog:
    """Cached per-palace OpLog (one connection per process per palace)."""
    if not isinstance(palace_path, str) or not palace_path.strip():
        raise ValueError("palace_path must be a non-empty string")
    key = _canonical_palace_key(palace_path)
    oplog = _oplog_by_path.get(key)
    if oplog is not None:
        return oplog
    with _oplog_cache_lock:
        oplog = _oplog_by_path.get(key)
        if oplog is None:
            oplog = OpLog(os.path.join(os.path.expanduser(palace_path), OPLOG_DB_FILENAME))
            _oplog_by_path[key] = oplog
    return oplog


def shadow_append(oplog: Optional[OpLog], kind: str, payload: dict, author_agent: str = "mcp"):
    """Append via an already-open OpLog without ever raising.

    Shadow posture (RFC 004 sequencing): during the dual-write period the vector
    store is authoritative and op emission is bookkeeping — a failed emit
    must not break the user's write. It is logged at ERROR so the gap is
    loud, and the divergence verifier reports it.
    """
    if oplog is None:
        return None
    try:
        return oplog.append(kind, payload, author_agent=author_agent)
    except Exception:
        logger.error("op-log shadow emission failed (kind=%s)", kind, exc_info=True)
        return None


def emit_op(
    palace_path: str,
    kind: str,
    payload: dict,
    author_agent: str = "mcp",
    required: bool = False,
):
    """Emit one op for the palace at ``palace_path``.

    ``required=False`` (shadow posture) never raises; ``required=True`` is
    the post-cutover authoritative posture where a failed append must fail
    the write.
    """
    try:
        return get_oplog(palace_path).append(kind, payload, author_agent=author_agent)
    except Exception:
        if required:
            raise
        logger.error("op-log shadow emission failed (kind=%s)", kind, exc_info=True)
        return None
