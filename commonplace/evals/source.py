"""Read-only access to the control database and the workspace thread store.

Nothing here constructs ``ControlDB``, opens the live thread store for
writing, resolves wiring or touches continuity state: the control database is
opened ``?mode=ro`` with an authorizer that denies every write, and the thread
store through ``read_store`` (``?mode=ro``, one ``BEGIN`` snapshot).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import EVAL_LOCK_WAIT_S, EvalError

EXCLUDED_PREFIXES = ("capture:", "capture-fork:", "import-private:")
SCOPES = ("threads:read", "memory:read")
PAGE = 200
UNSUPPORTED = "eval_source_gap: unsupported_in_slice1"
# The smallest possible serialized row, ``[0,"","","","","","",""]``.
_MIN_ROW_BYTES = 25
_READ_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_TRANSACTION,
        getattr(sqlite3, "SQLITE_RECURSIVE", 33),
    }
)


def deny_writes(action, *_):
    return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY


@contextmanager
def control_snapshot(data_dir):
    """One read snapshot of ``control.sqlite3``; a missing file creates nothing."""
    path = Path(data_dir) / "control.sqlite3"
    if not path.is_file():
        raise EvalError("eval_denied")
    conn = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro",
        uri=True,
        isolation_level=None,
        timeout=EVAL_LOCK_WAIT_S,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.set_authorizer(deny_writes)
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def owner_principal(conn, workspace_id, by=None):
    """The CLI actor as ``_actor`` picks it, admitted only as a live owner of W."""
    if by:
        user = conn.execute("SELECT * FROM users WHERE id = ? OR email = ?", (by, by)).fetchone()
    else:
        users = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        user = next((row for row in users if row["is_admin"]), None)
    if user is None:
        raise EvalError("eval_denied")
    row = conn.execute(
        "SELECT role FROM memberships WHERE workspace_id = ? AND user_id = ?",
        (workspace_id, user["id"]),
    ).fetchone()
    role = row["role"] if row else None
    if role != "owner":
        raise EvalError("eval_denied")
    return principal(user["id"], user["email"], workspace_id, role)


def principal(user_id, email, workspace_id, role):
    from ..control_db import Principal
    from ..policy import Policy

    return Principal(
        user_id=user_id,
        user_email=email,
        workspace_id=workspace_id,
        connection_id="",
        connection="cli",
        client="commonplace",
        auth="local",
        role=role,
        policy=Policy.build(
            profile="full", scopes=list(SCOPES), read_wings=["*"], trust="trusted"
        ).for_role(role),
    )


def storage_limit(conn, workspace_id):
    """The account plan's storage limit, read with the plan module's own SQL."""
    from .. import plans

    if not plans.enabled():
        return None
    row = conn.execute(
        "SELECT personal_of FROM workspaces WHERE id = ?", (workspace_id,)
    ).fetchone()
    if row is None or not row["personal_of"]:
        return None
    plan = conn.execute(
        "SELECT plan FROM account_plans WHERE user_id = ?", (row["personal_of"],)
    ).fetchone()
    return plans.PLANS[plan["plan"]].storage_bytes if plan else None


# Control tables that can hold a stored thread ID, and the probe for each.
# ``capture_ops.response`` is the JSON result of a capture, so the quoted ID
# is searched in it. A table counts as "no reference" only when
# ``sqlite_master`` in the same snapshot proves it absent.
_PROBES = {
    "sync_streams": (
        "SELECT 1 FROM sync_streams WHERE workspace_id=? AND own_stored_id=? UNION ALL "
        "SELECT 1 FROM sync_streams WHERE workspace_id=? AND fork_stored_id=? LIMIT 1"
    ),
    "sync_groups": "SELECT 1 FROM sync_groups WHERE workspace_id=? AND holder_stored_id=? LIMIT 1",
    "sync_backfill": "SELECT 1 FROM sync_backfill WHERE workspace_id=? AND after_id=? LIMIT 1",
    "capture_ops": (
        "SELECT 1 FROM capture_ops WHERE workspace_id=? AND instr(response, ?) > 0 LIMIT 1"
    ),
}


def related(conn, workspace_id, stored_id) -> bool:
    """Whether any continuity row references ``stored_id``; errors propagate."""
    from ..continuity import _CONTROLLED_PROBE

    present = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?, ?, ?)",
            tuple(_PROBES),
        )
    }
    if {"sync_streams", "sync_groups"} <= present:
        if conn.execute(_CONTROLLED_PROBE, (workspace_id, stored_id) * 3).fetchone():
            return True
        present -= {"sync_streams", "sync_groups"}
    for table in sorted(present):
        value = json.dumps(stored_id) if table == "capture_ops" else stored_id
        args = (workspace_id, value) * (2 if table == "sync_streams" else 1)
        if conn.execute(_PROBES[table], args).fetchone():
            return True
    return False


@contextmanager
def thread_snapshot(directory):
    """Yield a read-only snapshot store, or None when the workspace has no store."""
    from ..handoff.thread_store_migrate import read_store
    from ..workspace_store import thread_path

    path = thread_path(directory)
    other = path.with_name(
        "threads.sqlite3" if path.name == "threads.v2.sqlite3" else "threads.v2.sqlite3"
    )
    if not path.exists():
        if other.exists():
            raise EvalError("eval_source_gap: format")
        yield None
        return
    with read_store(path, compact=path.name == "threads.v2.sqlite3") as store:
        yield store


@contextmanager
def thread_lock(directory):
    """Hold the thread store's writer lock (``BEGIN IMMEDIATE``) without writing.

    Opened through ``sqlite_crypto.connect`` like the live stores, so an
    encrypted store works; ``?mode=rw`` never creates the file.
    """
    from mempalace import sqlite_crypto

    from ..workspace_store import thread_path

    path = thread_path(directory)
    if not path.exists():
        raise EvalError("eval_not_found")
    conn = sqlite_crypto.connect(
        path.resolve().as_uri() + "?mode=rw",
        uri=True,
        isolation_level=None,
        timeout=EVAL_LOCK_WAIT_S,
    )
    try:
        conn.set_authorizer(deny_writes)
        conn.execute("BEGIN IMMEDIATE")
        yield
    finally:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        finally:
            conn.close()


def eligible(stored, first, workspace_id) -> bool:
    """The slice-1 rule, evaluated on the stored row before any other lookup."""
    from ..continuity_schema import MARKER
    from ..handoff.models import THREADS_WING

    return bool(
        stored.workspace_id == workspace_id
        and stored.wing == THREADS_WING
        and not stored.source_thread_id.startswith(EXCLUDED_PREFIXES)
        and first
        and first[0].seq == 0
        and first[0].name != MARKER
    )


def check(store, conn, principal, stored_id):
    """Return ``(stored, None)`` when every check passes, else ``(None, code)``.

    Order: existence, the slice-1 rule (seq-0 marker included), the read
    policy, then the continuity probe. Lookup errors propagate to the caller,
    which refuses.
    """
    from ..continuity_read import PrivateReader
    from ..handoff import recall

    if store is None:
        return None, "eval_not_found"
    stored = store.get(stored_id)
    if stored is None:
        return None, "eval_not_found"
    if not eligible(stored, store.messages(stored.id, 0, 0), principal.workspace_id):
        return None, UNSUPPORTED
    if not (
        principal.role == "owner"
        and recall.readable(principal, stored)
        and PrivateReader(None, None).readable(principal, stored)
    ):
        return None, "eval_not_found"
    if related(conn, principal.workspace_id, stored.id):
        return None, UNSUPPORTED
    return stored, None


def row_bytes(message) -> bytes:
    value = [
        message.seq,
        message.role,
        message.kind,
        message.name,
        message.call_id,
        message.at,
        message.text,
        message.raw,
    ]
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        raise EvalError("eval_source_gap: unencodable") from None


def snapshot_rows(store, stored, budget):
    """All rows of one snapshot in pages of at most 200, with completeness proven.

    Returns ``(messages, rows_bytes)``. Refuses once the serialized rows
    exceed ``budget``, before any digest.
    """
    from ..handoff.thread_store import canonical_sha256

    count = stored.message_count
    if count < 1:
        raise EvalError("eval_source_gap: digest")
    if count * _MIN_ROW_BYTES > budget:
        raise EvalError("eval_too_large")
    messages, encoded, total = [], [], 2
    while True:
        # Pages come from the caller's one snapshot, so rows appended after it
        # never appear; the completeness proof below refuses any mismatch.
        start = len(messages)
        page = store.messages(stored.id, start, start + PAGE - 1)
        for message in page:
            item = row_bytes(message)
            total += len(item) + bool(encoded)
            if total > budget:
                raise EvalError("eval_too_large")
            messages.append(message)
            encoded.append(item)
        if len(page) < PAGE:
            break
    if (
        len(messages) != count
        or any(message.seq != seq for seq, message in enumerate(messages))
        or canonical_sha256(messages) != stored.sha256
    ):
        raise EvalError("eval_source_gap: digest")
    return messages, b"[" + b",".join(encoded) + b"]"


def as_lists(messages):
    return [[m.seq, m.role, m.kind, m.name, m.call_id, m.at, m.text, m.raw] for m in messages]
