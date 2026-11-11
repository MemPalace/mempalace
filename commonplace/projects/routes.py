"""Person-session routes for project orchestrators and model preferences."""

import sqlite3

from commonplace import redact
from commonplace.boards.service import BoardService, WingRefused, project_wing, shared_wing
from commonplace.boards.store import CardRefused
from commonplace.control_db import ControlError
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import path_parts, read_json, require_principal, send_json
from commonplace.projects import connect, limits, models, orchestration, seats, wing
from commonplace.projects.types import ProjectDenied
from commonplace.rules.enforce import RulesRefresh, rest_write
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
        "model_catalog": [
            choice.public()
            for choice in models.catalog(gateway.db, principal.workspace_id, board["slug"])
        ],
        "can_configure": principal.person and principal.role == "owner",
    }
    if orchestration.enabled(gateway.db, principal.workspace_id):
        seat = seats.ensure_main(gateway.db, principal.workspace_id, board["slug"])
        live = seats.active_connection(gateway.db, principal.workspace_id, seat.connection_id)
        from commonplace.policy import Policy

        result["setup"] = {
            "webhook_add": {"events": "space_message,card.created", "board": board["slug"]}
        }
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


def _split_route(gateway, h, principal, parts, *, inboxing):
    from commonplace.projects import split

    try:
        if inboxing:
            payload = split.inbox(gateway, principal, parts[0])
        else:
            body = read_json(h)
            body.pop("rules_seq", None)
            if (
                set(body) != {"task", "state"}
                or type(body["task"]) is not int
                or not 1 <= body["task"] < 2**63
            ):
                raise CardRefused("invalid_arguments", status=400)
            # Bind the task reference to this authorized board before mutation.
            split.inbox(gateway, principal, parts[0])
            with orchestration.board_read(gateway.workspace_dir(principal.workspace_id)) as conn:
                row = conn.execute(
                    "SELECT b.slug FROM tasks t JOIN boards b ON b.id=t.board_id WHERE t.id=?",
                    (body["task"],),
                ).fetchone()
                if row is None or row[0] != parts[0]:
                    raise CardRefused("card_not_found", status=404)
            with rest_write(ToolContext(principal, gateway), h, "card_split_done", body):
                payload = split.mark(gateway, principal, body["task"], body["state"])
    except sqlite3.OperationalError as exc:
        orchestration.require_lock_timeout(exc)
        send_json(h, 409, {"error": "busy", "code": "busy"})
        return
    except RulesRefresh as exc:
        send_json(h, 428, exc.payload)
        return
    except CardRefused as exc:
        send_json(
            h, exc.status, {"error": str(exc).removeprefix("no: "), "code": exc.code, **exc.details}
        )
        return
    except (ValueError, TypeError):
        send_json(h, 400, {"error": "invalid_arguments"})
        return
    except orchestration.SettingsUnavailable as exc:
        send_json(h, exc.status, {"error": exc.code, "code": exc.code})
        return
    except ToolError:
        send_json(h, 403, {"error": "not_allowed"})
        return
    send_json(h, 200, payload)


def _project_action(method, parts):
    if len(parts) == 1 and method == "GET":
        return "view"
    routes = {
        ("POST", ("orchestrator", "connect")): "connect",
        ("POST", ("orchestrator", "trust")): "trust",
        ("GET", ("orchestrator", "inbox")): "inbox",
        ("POST", ("orchestrator", "split")): "split",
        ("PUT", ("models",)): "models",
        ("POST", ("models", "catalog")): "model_catalog",
        ("PUT", ("limits",)): "limits",
        ("POST", ("wing",)): "wing",
    }
    return routes.get((method, tuple(parts[1:])))


def _valid_project_body(h, action, body):
    if action == "wing" and (not isinstance(body, dict) or body):
        send_json(h, 400, {"error": "Own-wing body must be exactly {}"})
        return False
    if action == "connect" and (
        not isinstance(body, dict)
        or set(body) - {"replace", "allow_shared"}
        or any(type(value) is not bool for value in body.values())
    ):
        send_json(h, 400, {"error": "Invalid project connection settings"})
        return False
    return True


def _project_result(gateway, principal, service, board, action, body):
    slug = board["slug"]
    seats.ensure_main(gateway.db, principal.workspace_id, slug)
    if action == "connect":
        return connect.connect(
            gateway,
            principal,
            slug,
            replace=body.get("replace", False),
            allow_shared=body.get("allow_shared", False),
        )
    if action == "trust":
        return 200, connect.trust(gateway, principal, slug, body)
    if action == "wing":
        result = wing.own_wing(gateway, principal, slug)
        with service.store.connection() as conn:
            board = service._board(conn, slug)
        return 200, {**_view(gateway, principal, board), **result}
    if action == "limits":
        with service.transaction() as conn:
            limits.put(conn, board["id"], body)
    if action == "models":
        models.put(gateway.db, principal, slug, body)
    if action == "model_catalog":
        models.catalog_add(gateway.db, principal, slug, body)
    return 200, _view(gateway, principal, board)


def _project_route(gateway, h, principal, parts, action):
    if action is None:
        send_json(h, 404, {"error": "Not found"})
        return
    if action != "view" and principal.role != "owner":
        send_json(h, 403, {"error": "Only workspace owners can configure projects"})
        return
    try:
        service = BoardService(ToolContext(principal=principal, gateway=gateway))
        with service.store.connection() as conn:
            board = service._board(conn, parts[0])
        body = None
        if action != "view":
            try:
                body = read_json(h)
            except ValueError:
                return
            if not _valid_project_body(h, action, body):
                return
        status, payload = _project_result(gateway, principal, service, board, action, body)
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
    except models.UnknownModel:
        send_json(
            h,
            400,
            {
                "error": "That model is not in this workspace's list. Pick one from the list "
                "or add it first.",
                "code": "unknown_model",
            },
        )
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


def _route(gateway, h):
    parts = path_parts(h, "/api/projects/")
    action = _project_action(h.command, parts)
    splitting = action in ("inbox", "split")
    principal = require_principal(gateway, h) if splitting else _person(gateway, h)
    if principal is None:
        return
    if splitting:
        _split_route(gateway, h, principal, parts, inboxing=action == "inbox")
    else:
        _project_route(gateway, h, principal, parts, action)


def install(gateway) -> None:
    for method in ("GET", "PUT"):
        gateway.add_route(
            method, "/api/settings/orchestration", lambda h: orchestration_route(gateway, h)
        )
    for method in ("GET", "POST", "PUT"):
        gateway.add_prefix_route(method, "/api/projects/", lambda h: _route(gateway, h))
