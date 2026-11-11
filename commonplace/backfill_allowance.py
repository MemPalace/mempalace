"""Durable, bounded extra write allowance for a connection's first backfill."""

from __future__ import annotations

CONNECTION = "connection"
WORKSPACE = "workspace"


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.execute(
            "CREATE TABLE IF NOT EXISTS backfill_allowance ("
            "scope TEXT NOT NULL, scope_id TEXT NOT NULL, used INTEGER NOT NULL, "
            "started_at REAL NOT NULL, PRIMARY KEY (scope, scope_id))"
        )


def _row(conn, scope, scope_id):
    return conn.execute(
        "SELECT used, started_at FROM backfill_allowance WHERE scope=? AND scope_id=?",
        (scope, scope_id),
    ).fetchone()


def admit(db, *, workspace_id: str, connection_id: str, now: float, limits) -> bool:
    """Spend one allowance write; False when the connection or its space is spent.

    A connection's allowance is counted once for its whole life and closes
    ``window_seconds`` after first use; it is never renewed, so a client that
    starts over, or a restart, gains nothing. The workspace cap bounds new
    connections and renews only after ``space_period_seconds``.
    """
    if limits.per_connection <= 0 or limits.per_space <= 0:
        return False
    with db._lock, db._tx() as conn:
        own = _row(conn, CONNECTION, connection_id)
        if own and (own[0] >= limits.per_connection or now - own[1] >= limits.window_seconds):
            return False
        space = _row(conn, WORKSPACE, workspace_id)
        if space and now - space[1] >= limits.space_period_seconds:
            space = None
        if space and space[0] >= limits.per_space:
            return False
        conn.execute(
            "INSERT INTO backfill_allowance(scope, scope_id, used, started_at) VALUES (?,?,1,?) "
            "ON CONFLICT(scope, scope_id) DO UPDATE SET used=used+1",
            (CONNECTION, connection_id, now),
        )
        conn.execute(
            "INSERT INTO backfill_allowance(scope, scope_id, used, started_at) VALUES (?,?,1,?) "
            "ON CONFLICT(scope, scope_id) DO UPDATE SET "
            "used=CASE WHEN ?-started_at>=? THEN 1 ELSE used+1 END, "
            "started_at=CASE WHEN ?-started_at>=? THEN ? ELSE started_at END",
            (
                WORKSPACE,
                workspace_id,
                now,
                now,
                limits.space_period_seconds,
                now,
                limits.space_period_seconds,
                now,
            ),
        )
    return True
