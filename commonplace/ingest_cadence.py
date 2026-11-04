"""Durable account cadence shared by private imports and future live ingest."""

from __future__ import annotations

from datetime import datetime, timedelta

from . import plans

RETRY_WINDOW_SECONDS = 24 * 3600


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.execute(
            "CREATE TABLE IF NOT EXISTS ingest_cadence ("
            "account_id TEXT PRIMARY KEY REFERENCES users(id), last_admitted_at TEXT NOT NULL)"
        )
        db._conn.execute(
            "CREATE TABLE IF NOT EXISTS ingest_requests ("
            "connection_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, account_id TEXT NOT NULL, "
            "admitted_at TEXT NOT NULL, PRIMARY KEY (connection_id, request_sha256))"
        )
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(ingest_cadence)")}
        if "window_until" not in columns:
            db._conn.execute("ALTER TABLE ingest_cadence ADD COLUMN window_until TEXT")


def interval_for(db, account_id: str | None) -> int:
    if not plans.enabled():
        return 0
    plan = plans.get_plan(db, account_id) if account_id else None
    return (plan or plans.FREE).sync_interval_seconds


def account_key(db, principal) -> str:
    return plans.workspace_account(db, principal.workspace_id) or principal.user_id


def account_interval(db, principal) -> int:
    return interval_for(db, plans.workspace_account(db, principal.workspace_id))


def next_window(db, account_id, interval, now) -> datetime | None:
    """Read the next admissible account time without reserving a slot."""
    if interval <= 0:
        return None
    with db._lock:
        row = db._conn.execute(
            "SELECT last_admitted_at, window_until FROM ingest_cadence WHERE account_id=?",
            (account_id,),
        ).fetchone()
    if not row:
        return now
    last = datetime.fromisoformat(row[0])
    if row[1] and last <= now < datetime.fromisoformat(row[1]):
        return now
    return max(now, last + timedelta(seconds=interval))


def admit(
    db,
    *,
    account_id: str,
    connection_id: str,
    request_sha256: str,
    interval: int,
    now: datetime,
    window: int = 0,
    ride: bool = False,
    conn=None,
) -> int:
    """Return 0 for admission or an exact own retry, else seconds to wait.

    With conn, admission belongs to the caller's control transaction. Otherwise
    it commits independently, as private imports require.
    """
    if interval <= 0:
        return 0
    with db._lock:
        if conn is not None:
            return _admit(
                conn, account_id, connection_id, request_sha256, interval, now, window, ride
            )
        if db._conn.in_transaction:
            raise RuntimeError("ingest admission inside a control transaction")
        with db._tx() as conn:
            return _admit(
                conn, account_id, connection_id, request_sha256, interval, now, window, ride
            )


def _admit(conn, account_id, connection_id, request_sha256, interval, now, window, ride):
    conn.execute(
        "DELETE FROM ingest_requests WHERE admitted_at < ?",
        ((now - timedelta(seconds=RETRY_WINDOW_SECONDS)).isoformat(),),
    )
    if conn.execute(
        "SELECT 1 FROM ingest_requests WHERE connection_id=? AND request_sha256=? AND account_id=?",
        (connection_id, request_sha256, account_id),
    ).fetchone():
        return 0
    row = conn.execute(
        "SELECT last_admitted_at, window_until FROM ingest_cadence WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if row:
        last = datetime.fromisoformat(row[0])
        if (ride or window > 0) and row[1] and last <= now < datetime.fromisoformat(row[1]):
            return 0
        elapsed = (now - last).total_seconds()
        if elapsed < interval:
            return min(interval, int(interval - elapsed) + 1)
    conn.execute(
        "INSERT INTO ingest_cadence(account_id, last_admitted_at, window_until) VALUES (?,?,?) "
        "ON CONFLICT(account_id) DO UPDATE SET last_admitted_at=excluded.last_admitted_at, "
        "window_until=excluded.window_until",
        (
            account_id,
            now.isoformat(),
            (now + timedelta(seconds=window)).isoformat() if window else None,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO ingest_requests(connection_id, request_sha256, account_id, "
        "admitted_at) VALUES (?,?,?,?)",
        (connection_id, request_sha256, account_id, now.isoformat()),
    )
    return 0
