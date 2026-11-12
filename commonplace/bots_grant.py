"""The person-granted ``bots:chat`` and ``bots:confirm`` scopes: a person's own app uses their bots.

Any signed-in person grants them, for their own top-level app connection in that workspace
only. The app then acts as that person (their permissions, their bots). Agent machinery never
holds them: child agents, runner connections, project orchestrator seats and board
coordinators are refused when granting and again at use. ``bots:confirm`` (tapping a confirm
chip) is separate, explicit, and useless without ``bots:chat``.
"""

from __future__ import annotations

from commonplace import decide_grant

CHAT_SCOPE = "bots:chat"
CONFIRM_SCOPE = "bots:confirm"
SCOPES = (CHAT_SCOPE, CONFIRM_SCOPE)
# One source for the consent copy: the OAuth form and Settings → Agents show these.
CHAT_TEXT = (
    "Allow this app to use your bots as you (scope bots:chat): read your chats, send "
    "messages, stop replies and manage your bots' memory. Every change is recorded with "
    "this connection, it can reach only your own bots, and you can stop this at any time "
    "under Agents."
)
CONFIRM_TEXT = (
    "Also allow this app to confirm what a bot proposes (scope bots:confirm), the same as "
    "tapping a confirm button yourself. It needs bots:chat and you can stop it separately."
)
NOT_MEMBER_TEXT = "Only a member of this workspace can let an app use their bots."
NOT_YOURS_TEXT = "This connection belongs to someone else. Only your own apps can use your bots."
TOP_LEVEL_TEXT = "Only a top-level app connection of yours can use your bots."
AGENT_TEXT = "Agent, runner and orchestrator connections cannot use bots."
INACTIVE_TEXT = decide_grant.INACTIVE_TEXT
OAUTH_GRANT_TEXT = decide_grant.OAUTH_GRANT_TEXT
NEEDS_CHAT_TEXT = "Confirming needs the chat scope as well."
AUDIT_GRANT = "connection.bots.grant"
AUDIT_REVOKE = "connection.bots.revoke"
AUDIT_WRITE = "bots.write"  # one row per write request made through an app connection

GrantRefused = decide_grant.GrantRefused


def eligible(db, user_id, workspace_id) -> bool:
    """Whether this person may grant the scopes now: a current member, any role."""
    with db._lock:
        return db.role(workspace_id, user_id) is not None


def require_grantor(db, grantor, workspace_id, user_id) -> None:
    """Only a signed-in member, granting for themselves in this workspace."""
    if (
        grantor is None
        or not getattr(grantor, "person", False)
        or grantor.workspace_id != workspace_id
        or not eligible(db, grantor.user_id, workspace_id)
    ):
        raise GrantRefused(NOT_MEMBER_TEXT)
    if grantor.user_id != user_id:
        raise GrantRefused(NOT_YOURS_TEXT)


def refusal(db, row) -> str | None:
    """Why this stored connection cannot hold the scopes, or ``None`` (no ownership check)."""
    return {"agent": AGENT_TEXT, "top_level": TOP_LEVEL_TEXT}.get(decide_grant.machinery(db, row))
