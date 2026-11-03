"""Explicit import consent and the immutable account namespace for private imports."""

from __future__ import annotations

import hashlib
import json

IMPORT_SCOPE = "memory:import"
GRANT_PREFIX = "import-private"
SCOPE_TEXT = (
    "Import your past AI conversations and memory files into this workspace, word for word. "
    "This import permission cannot read stored threads or change existing memories, rules or settings. "
    "Sending exactly the same import again adds no duplicates."
)
OWNER_ONLY_TEXT = "Importing history is available only to the owner in their personal workspace."
INELIGIBLE_TEXT = "Reconnect in your personal workspace to allow importing history."
GUIDANCE = (
    "This connection may import past conversations and memory facts word for word into your private "
    "workspace. Ask the person first. Send each conversation once with POST /api/import or Grok facts "
    "with facts_import. Items are answered accepted; repeating adds nothing. "
    "This connection's import route on Free allows one request per 15 minutes while plans are enabled. "
    "On not due, wait the given seconds and resend exactly the same request."
)


def eligible(db, user_id, workspace_id) -> bool:
    """Read current ownership by workspace ID, safely inside an existing control transaction."""
    with db._lock:
        if db.role(workspace_id, user_id) != "owner":
            return False
        row = db._conn.execute(
            "SELECT personal_of FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        return row is not None and row["personal_of"] == user_id


def grant_path(db, principal) -> bool:
    """Whether this authenticated agent can use the private import path right now."""
    return (
        not principal.person
        and principal.policy.has_scope(IMPORT_SCOPE)
        and principal.policy.has_scope("memory:write")
        and principal.policy.has_scope("memory:read")
        and eligible(db, principal.user_id, principal.workspace_id)
    )


def grant_id(user_id, source, thread_id) -> str:
    """Keep imported source identities within the immutable filing account's namespace."""
    return f"{GRANT_PREFIX}:{user_id}:{source}:{thread_id}"


def request_digest(route: str, data) -> str:
    """Hash the route and canonical request JSON without altering any string content."""
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8", errors="surrogatepass"
    )
    return hashlib.sha256(route.encode() + b"\n" + encoded).hexdigest()
