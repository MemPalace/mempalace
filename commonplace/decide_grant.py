"""The owner-granted ``cards:decide`` scope: an owner's own app answers that owner's asks.

Only the signed-in owner grants it, and only to their own top-level app
connections. Agent machinery never holds it: child agents, runner connections,
project orchestrator seats and board coordinators are refused when granting and
again when answering.
"""

from __future__ import annotations

DECIDE_SCOPE = "cards:decide"
# The one source for the consent copy: the OAuth form and Settings → Agents show it.
SCOPE_TEXT = (
    "Allow this app to answer your Needs-me questions as you (scope cards:decide). "
    "Every answer is recorded with this connection, and you can stop this at any time "
    "under Agents. It can answer only questions addressed to you, never another "
    "person's, and only on boards you can write to."
)
OWNER_ONLY_TEXT = "Only the workspace owner can let an app answer Needs-me questions."
NOT_YOURS_TEXT = (
    "This connection belongs to another member. Only your own apps can answer your "
    "Needs-me questions."
)
TOP_LEVEL_TEXT = "Only a top-level connection of the workspace owner can answer Needs-me questions."
AGENT_TEXT = "Agent, runner and orchestrator connections cannot answer Needs-me questions."
INACTIVE_TEXT = "Only an active connection can change; this one is revoked or expired."
OAUTH_GRANT_TEXT = "An OAuth app gets this only through a fresh sign-in consent."
AUDIT_ACTION = "card.decide.delegated"
ROLLBACK_ACTION = "card.decide.rolled_back"  # cancels an AUDIT_ACTION row by audit_seq
SEAT_CLIENT = "commonplace-orchestrator"  # projects.types.SEAT_CLIENT; asserted in tests


class GrantRefused(ValueError):
    """A refused grant; the message is safe to show to the signed-in owner."""


def eligible(db, user_id, workspace_id) -> bool:
    """Whether this person may grant the scope now: a current workspace owner."""
    with db._lock:
        return db.role(workspace_id, user_id) == "owner"


def require_grantor(db, grantor, workspace_id, user_id) -> None:
    """Only the signed-in owner, granting for themselves in this workspace."""
    if (
        grantor is None
        or not getattr(grantor, "person", False)
        or grantor.workspace_id != workspace_id
        or not eligible(db, grantor.user_id, workspace_id)
    ):
        raise GrantRefused(OWNER_ONLY_TEXT)
    if grantor.user_id != user_id:
        raise GrantRefused(NOT_YOURS_TEXT)


def is_seat(db, connection_id, client) -> bool:
    """A project orchestrator seat, by client or by its seat record."""
    if client == SEAT_CLIENT:
        return True
    with db._lock:
        table = db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_orchestrators'"
        ).fetchone()
        return bool(
            table
            and db._conn.execute(
                "SELECT 1 FROM project_orchestrators WHERE connection_id=?", (connection_id,)
            ).fetchone()
        )


def is_runner(db, connection_id) -> bool:
    with db._lock:
        return bool(
            db._conn.execute(
                "SELECT 1 FROM runner_connections WHERE connection_id=?", (connection_id,)
            ).fetchone()
        )


def refusal(db, row) -> str | None:
    """Why this stored connection cannot hold the scope, or ``None`` (no ownership check)."""
    if row["auth"] not in ("pat", "oauth"):
        return TOP_LEVEL_TEXT
    if (
        is_runner(db, row["id"])
        or is_seat(db, row["id"], row["client"])
        or row["started_by"] == "agent"
    ):
        return AGENT_TEXT
    if row["parent_id"] is not None:
        return TOP_LEVEL_TEXT
    return None


def answers_for(principal, row) -> bool:
    """Whether this connection may answer this ask on behalf of its own person.

    Fails closed: only a live owner's top-level PAT or OAuth connection holding
    the scope, and only for an ask addressed to that same person. Seats, board
    coordinators and write access are checked by the caller.
    """
    return bool(
        not principal.person
        and principal.auth in ("pat", "oauth")
        and principal.role == "owner"
        and principal.parent_connection_id is None
        and principal.runner is None
        and principal.policy.has_scope(DECIDE_SCOPE)
        and row["ask_to"] == f"user:{principal.user_id}"
    )
