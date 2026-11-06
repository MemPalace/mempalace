"""Human-managed agent tokens, sharing the existing connection authority."""

from commonplace import import_grant, urls
from commonplace.control_db import ControlError
from commonplace.extensions import ToolError
from commonplace.http_util import path_parts, read_json, require_principal, send_json
from commonplace.policy import Policy, canonical_wing, is_private_wing

TOKEN_DAYS = 90


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


def connection_view(row):
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
    view["read_wings"] = list(Policy.from_json(row["policy"]).read_wings)
    return view


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
        {"connection": connection_view(gateway.db.connection(row["id"], principal.workspace_id))},
    )


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
                set(data) - {"name", "client", "parent", "read_wings", "import"}
                or not isinstance(data.get("name"), str)
                or not isinstance(data.get("client", "generic"), str)
                or ("parent" in data and not isinstance(data["parent"], str))
            ):
                send_json(h, 400, {"error": "Use an agent name, client and optional parent"})
                return
            if "import" in data and type(data["import"]) is not bool:
                send_json(h, 400, {"error": "Import consent must be a boolean"})
                return
            grant_import = data.get("import", False)
            if grant_import and not import_grant.eligible(
                gateway.db, principal.user_id, principal.workspace_id
            ):
                send_json(h, 403, {"error": import_grant.OWNER_ONLY_TEXT})
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
                    expires_days=TOKEN_DAYS,
                    policy=Policy.build(
                        profile="slim",
                        scopes=("memory:read", "memory:write", import_grant.IMPORT_SCOPE)
                        if grant_import
                        else None,
                        read_wings=read_wings,
                        write_wing="inbox",
                        trust="untrusted",
                    ),
                )
            except ControlError:
                send_json(h, 400, {"error": "Invalid or duplicate agent name, client or parent"})
                return
            row = gateway.db.connection(cid, principal.workspace_id)
            reveal(h, 201, row, token)
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
        if row["auth"] != "pat":
            send_json(h, 409, {"error": "Manage this connection in its sign-in client"})
            return
        if h.command == "DELETE":
            try:
                gateway.db.revoke(
                    row["id"], actor=principal.user_id, workspace_ref=principal.workspace_id
                )
            except RunnerLinkedError as exc:
                send_json(h, 409, {"error": str(exc)})
                return
            send_json(h, 200, {"ok": True})
            return
        try:
            data = read_json(h)
        except ValueError:
            return
        if h.command == "PATCH":
            _update_read_wings(gateway, h, principal, row, data)
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
        reveal(h, 200, gateway.db.connection(row["id"], principal.workspace_id), token)

    def reveal(h, status, row, token):
        send_json(
            h,
            status,
            {
                "connection": connection_view(row),
                "token": token,
                "mcp_url": urls.AGENT_URL + "/mcp",
            },
        )

    gateway.add_prefix_route("POST", "/api/connections/", manage)
    gateway.add_prefix_route("DELETE", "/api/connections/", manage)
    gateway.add_prefix_route("PATCH", "/api/connections/", manage)
