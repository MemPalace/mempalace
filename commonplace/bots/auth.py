"""Who may use ``/api/bots``: a signed-in person, or that person's own app with a granted scope.

An app is a top-level PAT or OAuth connection of the person, active, holding ``bots:chat``
(see ``commonplace.bots_grant``). It acts as that person: the principal returned is the
person's own, so every permission check downstream is theirs and it reaches only their bots.
Every other token (agents, runners, seats, coordinators, other people's apps) is answered 404,
exactly like a route that does not exist.
"""

import logging

from commonplace import bots_grant, sessions
from commonplace.boards.service import BoardService
from commonplace.control_db import ControlError
from commonplace.extensions import ToolContext
from commonplace.http_util import log_failure, send_json

logger = logging.getLogger("commonplace.bots")


def _app_person(gateway, principal):
    """The person this app token acts as, or None. Checked on every request, fails closed."""
    if not principal.policy.has_scope(bots_grant.CHAT_SCOPE) or principal.runner is not None:
        return None
    try:
        row = gateway.db.connection(principal.connection_id, principal.workspace_id)
        if row["user_id"] != principal.user_id or bots_grant.refusal(gateway.db, row):
            return None
        service = BoardService(ToolContext(principal=principal, gateway=gateway))
        if principal.actor in service.coordinator_actors():
            return None
        return sessions.person_principal(gateway.db, principal.user_id, principal.workspace_id)
    except (ControlError, LookupError) as exc:
        log_failure(logger, "bots app access unavailable", exc)
        return None
    except Exception as exc:
        log_failure(logger, "bots app access unavailable", exc)
        return None


def person(gateway, h):
    """The principal to act as, or None after the reply (401 anonymous, 404 otherwise).

    For an app token this is the granting person's own principal; ``app(h)`` then names the
    connection the request came through.
    """
    principal = gateway.principal_for(h)
    if principal is None:
        send_json(h, 401, {"error": "Sign in"})
        return None
    if principal.person:
        return principal
    acting = _app_person(gateway, principal)
    if acting is None:
        send_json(h, 404, {"error": "Not found", "code": "not_found"})
        return None
    h._bots_app = principal
    return acting


def app(h):
    """The app connection's principal when this request came through one, else None."""
    return getattr(h, "_bots_app", None)
