"""Optional account cap per server, with a waitlist for new sign-ups when it is full.

``COMMONPLACE_MAX_ACCOUNTS`` unset (or empty) means unlimited: nothing changes. The cap only
gates the creation of a *new* account through sign-up. Existing accounts, invited members and
anyone an instance admin admitted are never refused. The check and the insert share one
``BEGIN IMMEDIATE`` transaction, so concurrent sign-ups cannot overshoot the cap.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

from .control_db import ControlError

logger = logging.getLogger("commonplace.capacity")
_warned_invalid = False
_warn_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS signup_waitlist (
    email TEXT PRIMARY KEY COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL,
    created_at TEXT NOT NULL,
    admitted_at TEXT,
    admitted_by TEXT,
    joined_at TEXT
);
"""

MESSAGE = (
    "Deze server zit op dit moment vol. Je staat op de wachtlijst; probeer het later opnieuw. / "
    "This server is full right now. You're on the waitlist; please try again later."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def max_accounts() -> int | None:
    """The account cap, or None for unlimited (the default, and for an invalid value)."""
    global _warned_invalid
    value = os.environ.get("COMMONPLACE_MAX_ACCOUNTS", "").strip()
    if not value:
        return None
    if value.isascii() and value.isdigit():
        return int(value)
    with _warn_lock:
        if not _warned_invalid:
            logger.warning("Invalid COMMONPLACE_MAX_ACCOUNTS value; accounts are unlimited")
            _warned_invalid = True
    return None


def account_count(conn) -> int:
    """Accounts that can sign in; disabling one frees its seat."""
    return conn.execute("SELECT COUNT(*) FROM users WHERE disabled_at IS NULL").fetchone()[0]


def admit_new_account(conn, emails, name: str, provider: str) -> bool:
    """Decide, inside the caller's write transaction, whether a new account may be created.

    When the server is full the person is recorded on the waitlist (they are not refused
    silently) and False is returned; the caller must commit before reporting that.
    """
    limit = max_accounts()
    if limit is None or account_count(conn) < limit:
        return True
    for email in emails:
        row = conn.execute(
            "SELECT 1 FROM signup_waitlist WHERE email = ? AND admitted_at IS NOT NULL "
            "AND joined_at IS NULL",
            (email,),
        ).fetchone()
        if row is not None:
            return True
    conn.execute(
        "INSERT INTO signup_waitlist(email, name, provider, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(email) DO NOTHING",
        (emails[0], name, provider, _now()),
    )
    return False


def mark_joined(conn, emails) -> None:
    """An admitted person who now has an account no longer needs their waitlist spot."""
    for email in emails:
        conn.execute(
            "UPDATE signup_waitlist SET joined_at = ? WHERE email = ? AND joined_at IS NULL",
            (_now(), email),
        )


def waitlist(db) -> list[dict]:
    """People still waiting or admitted but not yet signed up, oldest first."""
    with db._lock:
        rows = db._conn.execute(
            "SELECT email, name, provider, created_at, admitted_at, admitted_by "
            "FROM signup_waitlist WHERE joined_at IS NULL ORDER BY created_at, email"
        ).fetchall()
    return [
        {
            "email": row["email"],
            "name": row["name"],
            "provider": row["provider"],
            "created_at": row["created_at"],
            "status": "admitted" if row["admitted_at"] else "waiting",
            "admitted_at": row["admitted_at"],
            "admitted_by": row["admitted_by"],
        }
        for row in rows
    ]


def status(db) -> dict:
    with db._lock:
        used = account_count(db._conn)
    pending = sum(1 for entry in waitlist(db) if entry["status"] == "waiting")
    return {"max_accounts": max_accounts(), "accounts": used, "waiting": pending}


def admit(db, *, actor: str, email: str | None = None, next_n: int = 0) -> list[str]:
    """Admit one waiting person, or the oldest ``next_n``. They then sign up normally.

    An admitted person may exceed the cap: admitting is the admin's explicit decision.
    """
    if (email is None) == (next_n <= 0):
        raise ControlError("name an email or ask for the next N people")
    with db._lock, db._tx() as conn:
        if email is not None:
            rows = conn.execute(
                "SELECT email FROM signup_waitlist WHERE email = ? AND joined_at IS NULL",
                (email.strip(),),
            ).fetchall()
            if not rows:
                raise ControlError("no such person on the waitlist")
        else:
            rows = conn.execute(
                "SELECT email FROM signup_waitlist WHERE admitted_at IS NULL AND joined_at IS NULL "
                "ORDER BY created_at, email LIMIT ?",
                (next_n,),
            ).fetchall()
        admitted = []
        for row in rows:
            conn.execute(
                "UPDATE signup_waitlist SET admitted_at = COALESCE(admitted_at, ?), "
                "admitted_by = COALESCE(admitted_by, ?) WHERE email = ?",
                (_now(), actor, row["email"]),
            )
            db.audit(action="waitlist.admit", decision="ok", user_id=actor, reason=row["email"])
            admitted.append(row["email"])
    return admitted


def install(gateway) -> None:
    """Instance-admin REST: GET lists the waitlist, POST admits by email or the next N."""
    from .http_util import log_failure, read_json, require_principal, send_json
    from .web import admin

    ensure_schema(gateway.db)
    path = "/api/admin/instance/waitlist"

    def authorized(h):
        principal = require_principal(gateway, h)
        if principal is None:
            return None
        try:
            ok = admin.instance_authorized(
                gateway, principal, admin.ReadContext(gateway.admin_clock)
            )
        except Exception as exc:
            log_failure(logger, "Waitlist authority unavailable", exc)
            send_json(h, 503, {"error": "Waitlist unavailable; try again"})
            return None
        if not ok:
            send_json(h, 403, {"error": admin.INSTANCE_DENIAL})
            return None
        return principal

    def read(h):
        if authorized(h) is None:
            return
        try:
            send_json(h, 200, {**status(gateway.db), "waitlist": waitlist(gateway.db)})
        except Exception as exc:
            log_failure(logger, "Waitlist unavailable", exc)
            send_json(h, 503, {"error": "Waitlist unavailable; try again"})

    def write(h):
        principal = authorized(h)
        if principal is None:
            return
        try:
            data = read_json(h)
        except ValueError:
            return
        email, count = data.get("email"), data.get("next", 0)
        if (email is not None and not isinstance(email, str)) or type(count) is not int:
            send_json(h, 400, {"error": "Send an email or a positive number as next"})
            return
        try:
            admitted = admit(gateway.db, actor=principal.user_id, email=email, next_n=count)
        except ControlError as exc:
            send_json(h, 400, {"error": str(exc)})
            return
        except Exception as exc:
            log_failure(logger, "Waitlist admit failed", exc)
            send_json(h, 503, {"error": "Waitlist unavailable; try again"})
            return
        send_json(h, 200, {"admitted": admitted})

    gateway.add_route("GET", path, read)
    gateway.add_route("POST", path, write)
