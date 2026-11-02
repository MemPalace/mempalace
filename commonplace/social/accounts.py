"""Link provider subjects to people through verified email only."""

from __future__ import annotations

import os

from .. import plans
from ..control_db import ControlError
from ..oauth.store import iso
from .providers import Profile, SignInError

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_identities (
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY(provider, subject)
);
CREATE INDEX IF NOT EXISTS user_identities_user ON user_identities(user_id);
"""


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def signup_enabled() -> bool:
    """New accounts require an explicit opt-in; sign-in remains available."""
    return os.environ.get("COMMONPLACE_SIGNUP", "").strip().lower() == "open"


def resolve(db, provider: str, profile: Profile, *, signup: bool) -> str:
    with db._lock:
        known = db._conn.execute(
            "SELECT u.* FROM user_identities i JOIN users u ON u.id = i.user_id "
            "WHERE i.provider = ? AND i.subject = ?",
            (provider, profile.subject),
        ).fetchone()
        if known is not None:
            if known["disabled_at"]:
                raise SignInError("disabled")
            db._conn.execute(
                "UPDATE user_identities SET last_used_at = ? WHERE provider = ? AND subject = ?",
                (iso(), provider, profile.subject),
            )
            return known["id"]
        if not profile.emails:
            raise SignInError("email")
        user = None
        for email in profile.emails:
            user = db._conn.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            if user is not None:
                break
        if user is not None:
            if user["disabled_at"]:
                raise SignInError("disabled")
            uid = user["id"]
        else:
            if not signup:
                raise SignInError("account")
            try:
                uid = db.create_user(profile.emails[0], profile.name)
                workspace_id = db.create_workspace(profile.emails[0], uid, personal=True)
                plans.assign_signup_plan(db, uid, workspace_id, via=provider)
            except ControlError:
                raise SignInError("failed") from None
            db.audit(action="user.create", decision="ok", user_id=uid, reason=provider)
        now = iso()
        db._conn.execute(
            "INSERT INTO user_identities(provider, subject, user_id, created_at, last_used_at) VALUES (?,?,?,?,?)",
            (provider, profile.subject, uid, now, now),
        )
        db.audit(action="identity.link", decision="ok", user_id=uid, reason=provider)
        return uid
