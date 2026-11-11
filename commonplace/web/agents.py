"""Human-managed agent tokens, sharing the existing connection authority."""

import logging

from commonplace import control_db, decide_grant, import_grant, urls
from commonplace.control_db import ControlError
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import (
    log_failure,
    path_parts,
    read_json,
    require_principal,
    send_json,
)
from commonplace.policy import Policy, canonical_wing, is_private_wing

TOKEN_DAYS = 90
logger = logging.getLogger(__name__)


def _read_wings(gateway, principal, data):
    if "read_wings" not in data:
        return ["*"]
    wings = data["read_wings"]
    if not isinstance(wings, list) or not 1 <= len(wings) <= 100:
        raise ValueError
    if any(
        not isinstance(wing, str)
        or not wing.strip()
        or len(wing) > 200
        or any(c in wing for c in "*?[]")
        or is_private_wing(wing)
        or not principal.policy.can_read_wing(wing)
        for wing in wings
    ):
        raise ValueError
    known = gateway.call_memory_tool(principal, "mempalace_list_wings", {}).get("wings", {})
    known = {canonical_wing(wing) for wing in known}
    if any(canonical_wing(wing) not in known for wing in wings):
        raise ValueError
    return list(dict.fromkeys(canonical_wing(wing) for wing in wings))


def _revoke_oauth(gateway, h, principal, ref):
    """Revoke an OAuth-connected client for its workspace's owner.

    The connection can only be read, rotated or edited through its own sign-in, but
    cutting it off is the owner's call. Returns False for anything that is not an
    OAuth connection of this workspace, so the caller's normal path answers it.
    """
    from commonplace.oauth import store

    try:
        row = gateway.db.connection(ref, principal.workspace_id)
    except ControlError:
        return False
    if row["auth"] != "oauth":
        return False
    try:
        store.revoke_connection(
            gateway.db, row["id"], actor_id=principal.user_id, workspace_id=principal.workspace_id
        )
    except ControlError:
        send_json(h, 403, {"error": "Sign in as a workspace owner to manage agents"})
        return True
    send_json(h, 200, {"ok": True})
    return True


def _revoke_pat(gateway, h, principal, row):
    from commonplace.control_db import RunnerLinkedError

    try:
        gateway.db.revoke(row["id"], actor=principal.user_id, workspace_ref=principal.workspace_id)
    except RunnerLinkedError as exc:
        send_json(h, 409, {"error": str(exc)})
        return
    send_json(h, 200, {"ok": True})


def coordinators(gateway, principal):
    """Board coordinator actors, or ``None`` when the boards cannot be read (fail closed)."""
    from commonplace.boards.service import BoardService

    try:
        return BoardService(ToolContext(principal=principal, gateway=gateway)).coordinator_actors()
    except Exception as exc:
        log_failure(logger, "board coordinators unavailable", exc)
        return None


def decide_view(gateway, principal):
    """What connection_view needs to offer the cards:decide grant to this viewer."""
    return {"db": gateway.db, "viewer": principal, "coordinators": coordinators(gateway, principal)}


def _expired(row):
    expires = control_db._parse(row["expires_at"])
    return expires is not None and expires <= control_db._now()


def connection_view(row, db=None, *, viewer=None, coordinators=None):
    view = {
        key: row[key]
        for key in (
            "id",
            "name",
            "client",
            "auth",
            "user_id",
            "created_at",
            "expires_at",
            "revoked_at",
            "last_used_at",
            "parent_id",
            "started_by",
        )
    }
    policy = Policy.from_json(row["policy"])
    view["read_wings"] = list(policy.read_wings)
    view["import_read"] = policy.has_scope(import_grant.READER_SCOPE)
    view["decide"] = policy.has_scope(decide_grant.DECIDE_SCOPE)
    # Offered only on the viewing owner's own active top-level PATs; never on
    # agents, runners, seats, board coordinators or other members' connections.
    view["decide_grantable"] = bool(
        db is not None
        and viewer is not None
        and coordinators is not None
        and row["user_id"] == viewer.user_id
        and row["auth"] == "pat"
        and not row["revoked_at"]
        and not _expired(row)
        and f"conn:{row['name']}" not in coordinators
        and decide_grant.refusal(db, row) is None
    )
    return view


def _update_decide(gateway, h, principal, row, data):
    if set(data) != {"decide"} or type(data["decide"]) is not bool:
        send_json(h, 400, {"error": "Use decide true or false"})
        return
    if data["decide"]:
        # Coordinators live in the board store; control_db cannot see them.
        current = coordinators(gateway, principal)
        if current is None:
            send_json(h, 503, {"error": "Boards are unavailable; try again"})
            return
        if f"conn:{row['name']}" in current:
            send_json(h, 409, {"error": decide_grant.AGENT_TEXT})
            return
    try:
        gateway.db.set_connection_decide(
            row["id"], data["decide"], actor=principal, workspace_ref=principal.workspace_id
        )
    except ControlError as exc:
        # Specific, owner-safe reasons: inactive, runner-linked, agent or not top-level.
        send_json(h, 409, {"error": str(exc)})
        return
    if data["decide"]:
        # Re-read after the write: a coordinator assigned meanwhile loses the grant.
        current = coordinators(gateway, principal)
        if current is None or f"conn:{row['name']}" in current:
            gateway.db.clear_decide(row["id"], reason="coordinator", user_id=principal.user_id)
            if current is None:
                send_json(h, 503, {"error": "Boards are unavailable; try again"})
            else:
                send_json(h, 409, {"error": decide_grant.AGENT_TEXT})
            return
    fresh = gateway.db.connection(row["id"], principal.workspace_id)
    send_json(h, 200, {"connection": connection_view(fresh, **decide_view(gateway, principal))})


def _update_read_wings(gateway, h, principal, row, data):
    try:
        if set(data) != {"read_wings"}:
            raise ValueError
        wings = _read_wings(gateway, principal, data)
    except ValueError:
        send_json(h, 400, {"error": "Choose existing nonprivate workspace wings without patterns"})
        return
    except ToolError:
        send_json(h, 503, {"error": "Workspace wings are unavailable; try again"})
        return
    try:
        gateway.db.set_connection_read_wings(
            row["id"], wings, actor=principal.user_id, workspace_ref=principal.workspace_id
        )
    except ControlError:
        send_json(h, 409, {"error": "Only active agent tokens can be updated"})
        return
    send_json(
        h,
        200,
        {
            "connection": connection_view(
                gateway.db.connection(row["id"], principal.workspace_id),
                **decide_view(gateway, principal),
            )
        },
    )


def _consents(data, db, principal):
    if any(
        key in data and type(data[key]) is not bool for key in ("import", "import_read", "decide")
    ):
        raise ValueError()
    grants = data.get("import", False), data.get("import_read", False)
    if any(grants) and not import_grant.eligible(db, principal.user_id, principal.workspace_id):
        raise PermissionError(import_grant.OWNER_ONLY_TEXT)
    # Only a top-level connection may answer Needs-me; create_token rechecks ownership.
    if data.get("decide", False) and data.get("parent") is not None:
        raise PermissionError(decide_grant.TOP_LEVEL_TEXT)
    return (*grants, data.get("decide", False))


def _patch(gateway, h, principal, row, data):
    if "decide" in data:
        _update_decide(gateway, h, principal, row, data)
    elif row["auth"] != "pat":
        send_json(h, 409, {"error": "Manage this connection in its sign-in client"})
    else:
        _update_read_wings(gateway, h, principal, row, data)


def install(gateway):
    def manage(h):
        from commonplace.control_db import RunnerLinkedError

        principal = require_principal(gateway, h)
        if principal is None:
            return
        if not principal.person or principal.role != "owner":
            send_json(h, 403, {"error": "Sign in as a workspace owner to manage agents"})
            return
        parts = path_parts(h, "/api/connections")
        if h.command == "POST" and not parts:
            try:
                data = read_json(h)
            except ValueError:
                return
            if (
                set(data)
                - {"name", "client", "parent", "read_wings", "import", "import_read", "decide"}
                or not isinstance(data.get("name"), str)
                or not isinstance(data.get("client", "generic"), str)
                or ("parent" in data and not isinstance(data["parent"], str))
            ):
                send_json(h, 400, {"error": "Use an agent name, client and optional parent"})
                return
            try:
                grant_import, grant_read, grant_decide = _consents(data, gateway.db, principal)
            except ValueError:
                send_json(h, 400, {"error": "Import, reader and answer consent must be booleans"})
                return
            except PermissionError as exc:
                send_json(h, 403, {"error": str(exc)})
                return
            try:
                read_wings = _read_wings(gateway, principal, data)
            except ValueError:
                send_json(
                    h, 400, {"error": "Choose existing nonprivate workspace wings without patterns"}
                )
                return
            except ToolError:
                send_json(h, 503, {"error": "Workspace wings are unavailable; try again"})
                return
            try:
                parent = data.get("parent")
                if parent is not None:
                    row = gateway.db.connection(parent, principal.workspace_id)
                    if gateway.db.authenticate_connection(row["id"]) is None:
                        raise ControlError("parent connection is inactive")
                cid, token = gateway.db.create_token(
                    workspace_ref=principal.workspace_id,
                    user_ref=principal.user_id,
                    actor=principal.user_id,
                    name=data["name"],
                    client=data.get("client", "generic"),
                    parent=parent,
                    grantor=principal,
                    expires_days=TOKEN_DAYS,
                    policy=Policy.build(
                        profile="slim",
                        scopes=(
                            "memory:read",
                            "memory:write",
                            *((import_grant.IMPORT_SCOPE,) if grant_import else ()),
                            *((import_grant.READER_SCOPE,) if grant_read else ()),
                            *((decide_grant.DECIDE_SCOPE,) if grant_decide else ()),
                        ),
                        read_wings=read_wings,
                        write_wing="inbox",
                        trust="untrusted",
                    ),
                )
            except ControlError:
                send_json(h, 400, {"error": "Invalid or duplicate agent name, client or parent"})
                return
            row = gateway.db.connection(cid, principal.workspace_id)
            reveal(h, 201, row, token, principal)
            return
        if (
            h.command == "DELETE"
            and len(parts) == 1
            and _revoke_oauth(gateway, h, principal, parts[0])
        ):
            return
        if not (
            (h.command == "POST" and len(parts) == 2 and parts[1] == "rotate")
            or (h.command == "DELETE" and len(parts) == 1)
            or (h.command == "PATCH" and len(parts) == 1)
        ):
            send_json(h, 404, {"error": "Agent not found"})
            return
        try:
            row = gateway.db.connection(parts[0], principal.workspace_id)
        except ControlError:
            send_json(h, 404, {"error": "Agent not found"})
            return
        # An OAuth client's answer grant can be withdrawn here; the rest is its client's.
        if row["auth"] != "pat" and not (h.command == "PATCH" and row["auth"] == "oauth"):
            send_json(h, 409, {"error": "Manage this connection in its sign-in client"})
            return
        if h.command == "DELETE":
            _revoke_pat(gateway, h, principal, row)
            return
        try:
            data = read_json(h)
        except ValueError:
            return
        if h.command == "PATCH":
            _patch(gateway, h, principal, row, data)
            return
        if data:
            send_json(h, 400, {"error": "Rotation takes an empty object"})
            return
        try:
            token = gateway.db.rotate_token(
                row["id"],
                actor=principal.user_id,
                workspace_ref=principal.workspace_id,
                expires_days=TOKEN_DAYS,
            )
        except RunnerLinkedError as exc:
            send_json(h, 409, {"error": str(exc)})
            return
        except ControlError:
            send_json(h, 409, {"error": "Revoked agents cannot be rotated"})
            return
        reveal(h, 200, gateway.db.connection(row["id"], principal.workspace_id), token, principal)

    def reveal(h, status, row, token, principal):
        send_json(
            h,
            status,
            {
                "connection": connection_view(row, **decide_view(gateway, principal)),
                "token": token,
                "mcp_url": urls.AGENT_URL + "/mcp",
            },
        )

    gateway.add_prefix_route("POST", "/api/connections/", manage)
    gateway.add_prefix_route("DELETE", "/api/connections/", manage)
    gateway.add_prefix_route("PATCH", "/api/connections/", manage)
