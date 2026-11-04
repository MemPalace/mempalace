"""Bounded, read-only board output for an agent's own person.

Only the newest 500 index entries per source are considered, even when all
500 are invisible. Unavailable reads return None so callers keep their panel;
an absent store or a revoked agent instead has an empty output history.
"""

import sqlite3
from pathlib import Path

from commonplace import redact
from commonplace.console import types
from mempalace.sqlite_crypto import connect, operational_error_type, row_factory

_REPLIES = """
SELECT id, board_id FROM (
    SELECT id, board_id FROM space_messages
    WHERE stamp_connection = ? ORDER BY id DESC LIMIT 500
)
WHERE board_id IN ({visible}) ORDER BY id DESC LIMIT 20
"""
_COMMENTS = """
SELECT e.id, e.task_id, t.board_id FROM (
    SELECT id, task_id FROM task_events
    WHERE stamp_connection = ? AND kind IN ('comment', 'handoff', 'ask')
    ORDER BY id DESC LIMIT 500
) e
JOIN tasks t ON t.id = e.task_id
WHERE t.board_id IN ({visible}) ORDER BY e.id DESC LIMIT 20
"""


def _read(gateway, agent, *, text=False):
    if not agent.own:
        return [], []
    conn = None
    try:
        principal = gateway.db.authenticate_connection(agent.id)
        if principal is None or principal.person or principal.user_id != agent.user_id:
            return [], []
        path = Path(gateway.supervisor.workspace_dir(principal.workspace_id)) / "boards.sqlite3"
        if not path.is_file():
            return [], []
        conn = connect(
            path.resolve().as_uri() + "?mode=rw",
            uri=True,
            timeout=0.2,
            isolation_level=None,
        )
        conn.row_factory = row_factory
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        visible = [
            row["id"]
            for row in conn.execute("SELECT id, slug, wing FROM boards WHERE archived=0")
            if principal.policy.can_read_wing(row["wing"])
        ]
        if not visible:
            return [], []
        placeholders = ",".join("?" for _ in visible)
        replies = conn.execute(
            _REPLIES.format(visible=placeholders), [principal.connection, *visible]
        ).fetchall()
        comments = conn.execute(
            _COMMENTS.format(visible=placeholders), [principal.connection, *visible]
        ).fetchall()
        if text:
            replies = _texts(conn, replies, reply=True)
            comments = _texts(conn, comments, reply=False)
        return replies, comments
    except (sqlite3.OperationalError, operational_error_type(conn), ValueError):
        return None
    finally:
        if conn is not None:
            conn.close()


def _texts(conn, rows, *, reply):
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in ids)
    # task_events.task_id already IS the card number. The tasks -> boards
    # joins resolve its current board slug, not a separate task number.
    source = (
        "space_messages e JOIN boards b ON b.id = e.board_id"
        if reply
        else "task_events e JOIN tasks t ON t.id = e.task_id JOIN boards b ON b.id = t.board_id"
    )
    fields = "'reply' AS kind, NULL AS card" if reply else "e.kind, e.task_id AS card"
    return conn.execute(
        f"SELECT e.id, {fields}, e.stamp_user, e.created_at, b.slug AS board, "
        "length(e.text) AS n, substr(e.text, 1, 16001) AS head "
        f"FROM {source} WHERE e.id IN ({placeholders})",
        ids,
    ).fetchall()


def _cut(text):
    end = 2000
    start = text.rfind("[", 0, end)
    if start != -1 and text.startswith(("[redacted:", "[omitted:"), start):
        closing = text.find("]", start)
        if closing == -1 or closing >= end:
            end = start
    return text[:end], len(text) > end


def _item(row):
    board, board_kinds = _redact(row["board"])
    if row["n"] is not None and row["n"] > 16_000:
        text, kinds, truncated = "[omitted:too_long]", ["too_long"], True
    else:
        text, kinds = _redact(row["head"] or "")
        text, truncated = _cut(text)
    return {
        "id": ("r" if row["kind"] == "reply" else "c") + str(row["id"]),
        "at": row["created_at"],
        "source": "output",
        "kind": row["kind"],
        "board": board,
        "card": row["card"],
        "text": text,
        "truncated": truncated,
        "redacted": sorted(set(board_kinds + kinds)),
    }


def _redact(text):
    # Do not even feed oversized board slugs to the bounded redactor.
    if len(text) > 16_000:
        return "[omitted:too_long]", ["too_long"]
    return redact.redact(text)


def latest_ids(gateway, agent: types.AgentView) -> tuple[int, int]:
    """Return visible source heads, or None when the store is unavailable."""
    rows = _read(gateway, agent)
    if rows is None:
        return None
    return tuple(max((row["id"] for row in source), default=0) for source in rows)


def recent(gateway, agent: types.AgentView) -> list[dict]:
    """Return the newest twenty items, or None to retain the caller's panel."""
    rows = _read(gateway, agent, text=True)
    if rows is None:
        return None
    ordered = sorted(
        (row for source in rows for row in source if row["stamp_user"] == agent.user_id),
        key=lambda row: (row["created_at"], row["id"], row["kind"] == "reply"),
        reverse=True,
    )
    return [_item(row) for row in ordered[:20]]
