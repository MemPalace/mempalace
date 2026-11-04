"""Person-session routes for project orchestrators and model preferences."""

from commonplace import redact
from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import path_parts, read_json, require_principal, send_json
from commonplace.projects import connect, models, seats
from commonplace.projects.types import ProjectDenied


def _person(gateway, h):
    principal = require_principal(gateway, h)
    if principal is not None and not principal.person:
        send_json(h, 403, {"error": "Sign in to view projects"})
        return None
    return principal


def _view(gateway, principal, board):
    choices = models.get(gateway.db, principal.workspace_id, board["slug"])
    return {
        "board": board["slug"],
        "name": redact.redact(board["name"])[0],
        "main": seats.main(gateway, principal, board["slug"]).public(),
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
        if not (viewing or connecting or configuring):
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
            else:
                if configuring:
                    models.put(gateway.db, principal, slug, body)
                status, payload = 200, _view(gateway, principal, board)
        except ProjectDenied:
            send_json(h, 403, {"error": "Project settings not allowed"})
            return
        except ValueError:
            send_json(h, 400, {"error": "Invalid project settings"})
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
