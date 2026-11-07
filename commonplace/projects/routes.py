"""Person-session routes for project orchestrators and model preferences."""

import sqlite3

from commonplace import redact
from commonplace.boards.service import BoardService, WingRefused, project_wing, shared_wing
from commonplace.control_db import ControlError
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import path_parts, read_json, require_principal, send_json
from commonplace.projects import connect, limits, models, orchestration, seats, wing
from commonplace.projects.types import ProjectDenied
from commonplace.runner import registry


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
    result = {
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
    if orchestration.enabled(gateway.db, principal.workspace_id):
        seat = seats.ensure_main(gateway.db, principal.workspace_id, board["slug"])
        live = seats.active_connection(gateway.db, principal.workspace_id, seat.connection_id)
        from commonplace.policy import Policy

        result["orchestration"] = {
            "default_max_agents": orchestration.DEFAULT_MAX_AGENTS,
            "trusted": bool(live and Policy.from_json(live["policy"]).trust == "trusted"),
        }
        if result["can_configure"]:
            result["orchestration"]["machines"] = [
                {"id": m["id"], "label": redact.redact(m["label"])[0]}
                for m in registry.machines(gateway.db, principal.workspace_id)
                if m["enabled"] and not m["revoked_at"]
            ]
    return result


def orchestration_route(gateway, h):
    principal = _person(gateway, h)
    if principal is None:
        return
    if h.command != "GET" and principal.role != "owner":
        send_json(h, 403, {"error": "Only workspace owners can configure orchestration"})
        return
    try:
        if h.command == "GET":
            payload = {"enabled": orchestration.enabled(gateway.db, principal.workspace_id)}
        else:
            try:
                body = read_json(h)
            except ValueError:
                return
            if set(body) != {"enabled"}:
                raise ValueError("Invalid orchestration settings")
            payload = orchestration.set_enabled(gateway.db, principal, body["enabled"])
    except orchestration.AdmissionRefused as exc:
        send_json(h, exc.status, {"error": exc.code})
        return
    except ValueError:
        send_json(h, 400, {"error": "Invalid orchestration settings"})
        return
    send_json(h, 200, payload)


def install(gateway) -> None:
    for method in ("GET", "PUT"):
        gateway.add_route(
            method, "/api/settings/orchestration", lambda h: orchestration_route(gateway, h)
        )

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
        trusting = (
            h.command == "POST" and len(parts) == 3 and parts[1:] == ["orchestrator", "trust"]
        )
        owning = h.command == "POST" and len(parts) == 2 and parts[1] == "wing"
        if not (viewing or connecting or configuring or owning or limiting or trusting):
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
            elif trusting:
                status, payload = 200, connect.trust(gateway, principal, slug, body)
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
        except WingRefused as exc:
            send_json(h, 409, exc.payload)
            return
        except registry.NotFound:
            send_json(h, 404, {"error": "Machine not found"})
            return
        except sqlite3.OperationalError as exc:
            orchestration.require_lock_timeout(exc)
            send_json(h, 409, {"error": "busy", "code": "busy"})
            return
        except ControlError:
            send_json(h, 409, {"error": "Trust settings conflict"})
            return
        except orchestration.AdmissionRefused as exc:
            send_json(h, exc.status, {"error": str(exc).removeprefix("no: "), "code": exc.code})
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
