"""Web sign-in: one-time login links and session cookies.

No passwords and no email. ``commonplace login-link <email>`` prints a link
that works once within 15 minutes. Opening it sets an HttpOnly session
cookie for 30 days. Only the SHA-256 of each link token and session id is
stored. A signed-in person acts as ``user:<id>`` with the owner's full view
of their workspace (people see everything in a workspace they belong to;
scopes limit agents, not owners). Provider sign-in (:mod:`commonplace.social`)
ends in the same session through :func:`start_session`.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from .control_db import ControlDB, Principal
from .policy import SCOPES, Policy

COOKIE = "cmmn_session"
LINK_MINUTES = 15
SESSION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS login_links (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    workspace_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    workspace_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
"""

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def ensure_schema(db: ControlDB) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def person_policy() -> Policy:
    return Policy.build(
        profile="full",
        scopes=[s for s in SCOPES if s not in ("admin", "memory:import", "memory:import:read")],
        read_wings=["*"],
        write_wings=["*"],
        trust="trusted",
    )


def person_principal(db: ControlDB, user_id: str, workspace_id: str) -> Optional[Principal]:
    user = db.user(user_id)
    if user["disabled_at"]:
        return None
    role = db.role(workspace_id, user_id)
    if role is None:
        return None
    return Principal(
        user_id=user["id"],
        user_email=user["email"],
        workspace_id=workspace_id,
        connection_id="",
        connection=f"web:{user['email']}",
        client="web",
        auth="session",
        role=role,
        policy=person_policy(),
        person=True,
    )


def home_workspace(db: ControlDB, user_id: str) -> Optional[str]:
    """Where a sign-in lands: the user's personal workspace, else their first."""
    with db._lock:
        row = db._conn.execute(
            "SELECT w.id FROM workspaces w JOIN memberships m ON m.workspace_id = w.id "
            "WHERE m.user_id = ? ORDER BY w.personal_of IS ? DESC, w.created_at LIMIT 1",
            (user_id, user_id),
        ).fetchone()
    return row["id"] if row else None


def create_login_link(
    db: ControlDB, email: str, workspace_ref: Optional[str] = None, *, minutes: Optional[int] = None
) -> str:
    """A one-time login token for ``email``. Build the URL with ``/app/login?t=``."""
    ensure_schema(db)
    user = db.user(email)
    if workspace_ref:
        workspace_id = db.workspace(workspace_ref)["id"]
    else:
        workspace_id = home_workspace(db, user["id"])
        if workspace_id is None:
            raise ValueError(f"{email} is in no workspace")
    token = secrets.token_urlsafe(32)
    now = _now()
    with db._lock:
        db._conn.execute(
            "INSERT INTO login_links(token_hash, user_id, workspace_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (
                _hash(token),
                user["id"],
                workspace_id,
                now.strftime(_FMT),
                (now + timedelta(minutes=_link_minutes(minutes))).strftime(_FMT),
            ),
        )
    db.audit(
        action="login_link.create", decision="ok", workspace_id=workspace_id, user_id=user["id"]
    )
    return token


def _link_minutes(minutes: Optional[int]) -> int:
    if minutes is None:
        return LINK_MINUTES
    return max(1, min(int(minutes), 24 * 60))


def discard_login_link(db: ControlDB, token: str) -> None:
    """Delete an unused login token, e.g. when its email could not be sent."""
    ensure_schema(db)
    with db._lock:
        db._conn.execute(
            "DELETE FROM login_links WHERE token_hash = ? AND used_at IS NULL", (_hash(token),)
        )


def redeem_login_link(db: ControlDB, token: str) -> Optional[str]:
    """Use a login token once. Returns a new session id, or ``None``."""
    ensure_schema(db)
    if not token or len(token) > 200:
        return None
    now = _now()
    with db._lock:
        row = db._conn.execute(
            "SELECT * FROM login_links WHERE token_hash = ?", (_hash(token),)
        ).fetchone()
        if row is None or row["used_at"] or row["expires_at"] <= now.strftime(_FMT):
            return None
        db._conn.execute(
            "UPDATE login_links SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
            (now.strftime(_FMT), row["token_hash"]),
        )
        if db._conn.execute("SELECT changes() AS n").fetchone()["n"] != 1:
            return None
        return start_session(db, row["user_id"], row["workspace_id"])


def start_session(db: ControlDB, user_id: str, workspace_id: str, *, via: str = "") -> str:
    """A new session id for a person who just proved who they are. ``via``
    names the sign-in provider in the audit log; a login link leaves it empty."""
    session = secrets.token_urlsafe(32)
    now = _now()
    with db._lock:
        db._conn.execute(
            "INSERT INTO sessions(id_hash, user_id, workspace_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (
                _hash(session),
                user_id,
                workspace_id,
                now.strftime(_FMT),
                (now + timedelta(days=SESSION_DAYS)).strftime(_FMT),
            ),
        )
    db.audit(
        action="session.create",
        decision="ok",
        workspace_id=workspace_id,
        user_id=user_id,
        reason=via,
    )
    return session


def session_principal(db: ControlDB, session: str) -> Optional[Principal]:
    ensure_schema(db)
    if not session or len(session) > 200:
        return None
    with db._lock:
        row = db._conn.execute(
            "SELECT * FROM sessions WHERE id_hash = ?", (_hash(session),)
        ).fetchone()
    if row is None or row["revoked_at"] or row["expires_at"] <= _now().strftime(_FMT):
        return None
    return person_principal(db, row["user_id"], row["workspace_id"])


def end_session(db: ControlDB, session: str) -> None:
    ensure_schema(db)
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE id_hash = ? AND revoked_at IS NULL",
            (_now().strftime(_FMT), _hash(session)),
        )


def cookie_value(header: Optional[str], cookie: str = COOKIE) -> Optional[str]:
    if not header:
        return None
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == cookie and value:
            return value
    return None


def set_cookie(session: str, secure: bool) -> str:
    attrs = [
        f"{COOKIE}={session}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={SESSION_DAYS * 86400}",
    ]
    if secure:
        attrs.append("Secure")
    attrs.extend(_domain())
    return "; ".join(attrs)


def clear_cookie() -> str:
    return "; ".join([f"{COOKIE}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0", *_domain()])


def _domain() -> list:
    """``COMMONPLACE_COOKIE_DOMAIN=cmmn.dev`` shares the session between
    app.cmmn.dev and cmmn.dev (OAuth consent). Unset: host-only cookie."""
    domain = os.environ.get("COMMONPLACE_COOKIE_DOMAIN", "").strip().lstrip(".")
    return [f"Domain={domain}"] if domain else []
