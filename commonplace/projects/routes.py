"""Person-session routes for project orchestrators and model preferences."""

from commonplace import redact
from commonplace.boards.service import BoardService, WingRefused, project_wing, shared_wing
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import path_parts, read_json, require_principal, send_json
from commonplace.projects import connect, limits, models, seats, wing
from commonplace.projects.types import ProjectDenied


def _person(gateway, h):
    principal = require_principal(gateway, h)
    if principal is not None and not principal.person:
        send_json(h, 403, {"error": "Sign in to view projects"})
        return None
    return principal


def _view(gateway, principal, board):
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    with service.store.connection() as conn:
        project_limits = limits.get(conn, board["id"])
    choices = models.get(gateway.db, principal.workspace_id, board["slug"])
    return {
        "board": board["slug"],
        "memory": {
            "wing": board["wing"],
            "shared": shared_wing(board["wing"]) and not any(c in board["wing"] for c in "*?[]"),
            "own_wing": project_wing(board["slug"]),
        },
        "name": redact.redact(board["name"])[0],
        "main": seats.main(gateway, principal, board["slug"]).public(),
        "limits": project_limits,
        "models": {
            role: choice.public() if choice is not None else None
            for role, choice in choices.items()
        },
        "can_configure": principal.person and principal.role == "owner",
    }


def install(gateway) -> None:
    def route(h):
        principal = _person(gateway, h)
        if principal is None:
            return
        parts = path_parts(h, "/api/projects/")
        viewing = h.command == "GET" and len(parts) == 1
        connecting = (
            h.command == "POST" and len(parts) == 3 and parts[1:] == ["orchestrator", "connect"]
        )
        configuring = h.command == "PUT" and len(parts) == 2 and parts[1] == "models"
        limiting = h.command == "PUT" and len(parts) == 2 and parts[1] == "limits"
        owning = h.command == "POST" and len(parts) == 2 and parts[1] == "wing"
        if not (viewing or connecting or configuring or owning or limiting):
            send_json(h, 404, {"error": "Not found"})
            return
        if not viewing and principal.role != "owner":
            send_json(h, 403, {"error": "Only workspace owners can configure projects"})
            return
        try:
            service = BoardService(ToolContext(principal=principal, gateway=gateway))
            with service.store.connection() as conn:
                board = service._board(conn, parts[0])
            slug = board["slug"]
            if not viewing:
                try:
                    body = read_json(h)
                except ValueError:
                    return
            if owning and (not isinstance(body, dict) or body):
                send_json(h, 400, {"error": "Own-wing body must be exactly {}"})
                return
            if connecting:
                if (
                    not isinstance(body, dict)
                    or set(body) - {"replace", "allow_shared"}
                    or any(type(value) is not bool for value in body.values())
                ):
                    send_json(h, 400, {"error": "Invalid project connection settings"})
                    return
            seats.ensure_main(gateway.db, principal.workspace_id, slug)
            if connecting:
                status, payload = connect.connect(
                    gateway,
                    principal,
                    slug,
                    replace=body.get("replace", False),
                    allow_shared=body.get("allow_shared", False),
                )
            elif owning:
                result = wing.own_wing(gateway, principal, slug)
                with service.store.connection() as conn:
                    board = service._board(conn, slug)
                status, payload = 200, {**_view(gateway, principal, board), **result}
            else:
                if limiting:
                    with service.transaction() as conn:
                        limits.put(conn, board["id"], body)
                if configuring:
                    models.put(gateway.db, principal, slug, body)
                status, payload = 200, _view(gateway, principal, board)
        except ProjectDenied:
            send_json(h, 403, {"error": "Project settings not allowed"})
            return
        except ValueError:
            send_json(h, 400, {"error": "Invalid project settings"})
            return
        except WingRefused as exc:
            send_json(h, 409, exc.payload)
            return
        except ToolError as exc:
            missing = str(exc).removeprefix("no: ") == "board not found"
            send_json(
                h, 404 if missing else 403, {"error": "Board not found" if missing else "Forbidden"}
            )
            return
        send_json(h, status, payload)

    for method in ("GET", "POST", "PUT"):
        gateway.add_prefix_route(method, "/api/projects/", route)
