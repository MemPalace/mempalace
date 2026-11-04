"""Owner eligibility and sign-in policy."""

from commonplace import sessions
from commonplace.control_db import ControlError, Principal

PAUSE_SIGNED_OUT: bool = True


def owner_principal(db, user_id, ws) -> Principal | None:
    try:
        owner = sessions.person_principal(db, user_id, ws)
    except ControlError:
        return None
    if owner is None or owner.role != "owner" or ws != sessions.home_workspace(db, user_id):
        return None
    return owner


def eligible(db, principal) -> bool:
    return (
        principal.person
        and owner_principal(db, principal.user_id, principal.workspace_id) is not None
    )


def signed_in(db, user_id, ws) -> bool:
    sessions.ensure_schema(db)
    with db._lock:
        row = db._conn.execute(
            "SELECT 1 FROM sessions WHERE user_id = ? AND workspace_id = ? "
            "AND revoked_at IS NULL AND expires_at > ? LIMIT 1",
            (user_id, ws, sessions._now().strftime(sessions._FMT)),
        ).fetchone()
    return row is not None


def paused(db, user_id, ws) -> bool:
    return PAUSE_SIGNED_OUT and not signed_in(db, user_id, ws)
