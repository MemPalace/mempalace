"""Person-session HTTP routes for project chat and front-agent settings."""

from commonplace import redact
from commonplace.boards.service import BoardService
from commonplace.chat import front, model, search, settings, status, store
from commonplace.chat.types import LINKED_CARDS_MAX, ChatDenied
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import path_parts, query, read_json, require_principal, send_json


def _person(gateway, h):
    principal = require_principal(gateway, h)
    if principal is not None and not principal.person:
        send_json(h, 403, {"error": "Sign in to view chat"})
        return None
    return principal


def _cursors(params):
    try:
        cursors = {
            key: int(params[key])
            for key in ("after", "last", "before", "replies_after")
            if key in params
        }
    except ValueError:
        raise ValueError("Invalid cursor") from None
    for key, value in cursors.items():
        if key == "last":
            if not 1 <= value <= 100:
                raise ValueError("Invalid cursor")
        elif not 0 <= value < 2**63:
            raise ValueError("Invalid cursor")
    if "after" in cursors and ("last" in cursors or "before" in cursors):
        raise ValueError("Invalid cursor")
    return cursors


def install(gateway) -> None:
    def listing(h):
        principal = _person(gateway, h)
        if principal is None:
            return
        service = BoardService(ToolContext(principal=principal, gateway=gateway))
        projects = []
        for board in service.board_list():
            actor = board["coordinator"]
            projects.append(
                {
                    "slug": board["slug"],
                    "name": redact.redact(board["name"])[0],
                    "orchestrator": {"name": redact.redact(actor.removeprefix("conn:"))[0]}
                    if actor is not None
                    else None,
                }
            )
        send_json(
            h,
            200,
            {
                "projects": projects,
                "default": projects[0]["slug"] if projects else None,
                "front": settings.front(gateway.db, principal.workspace_id).public(),
            },
        )

    def page(h):
        principal = _person(gateway, h)
        if principal is None:
            return
        parts = path_parts(h, "/api/chat/")
        if len(parts) not in (1, 2) or (len(parts) == 2 and parts[1] not in ("status", "search")):
            send_json(h, 404, {"error": "Not found"})
            return
        if len(parts) == 2 and parts[1] == "search":
            send_json(h, *search.handle(gateway, principal, parts[0], query(h)))
            return
        try:
            if len(parts) == 2:
                payload = status.snapshot(gateway, principal, parts[0])
            else:
                try:
                    cursors = _cursors(query(h))
                except ValueError:
                    send_json(h, 400, {"error": "Invalid cursor"})
                    return
                replies_after = cursors.pop("replies_after", 0)
                service = BoardService(ToolContext(principal=principal, gateway=gateway))
                payload = service.space_read(parts[0], **cursors)
                payload.pop("coordinator", None)
                view = front.orchestrator(gateway, principal, parts[0])
                payload["orchestrator"] = view.public() if view is not None else None
                payload["messages"] = [
                    {**message, "text": redact.redact(message["text"])[0]}
                    for message in payload["messages"]
                ]
                replies = store.open_replies(gateway, principal.workspace_id).page(
                    board=parts[0],
                    for_user=principal.user_id,
                    message_ids=[message["id"] for message in payload["messages"]],
                    after_id=replies_after,
                )
                cards = service.task_list(board=parts[0], all_statuses=True)
                payload["linked"] = [
                    {
                        "number": card["number"],
                        "message_id": card["message_id"],
                        "status": card["status"],
                        "title": redact.redact(card["title"])[0],
                    }
                    for card in sorted(
                        (c for c in cards if c.get("message_id") is not None),
                        key=lambda c: c["number"],
                        reverse=True,
                    )[:LINKED_CARDS_MAX]
                ]
                payload["me"] = {"user": principal.user_id, "role": principal.role}
                payload["replies"] = [reply.public() for reply in replies]
                payload["reply_cursor"] = max([replies_after, *(reply.id for reply in replies)])
        except ToolError as exc:
            missing = str(exc).removeprefix("no: ") in ("board not found", "card not found")
            send_json(
                h, 404 if missing else 403, {"error": "Board not found" if missing else "Forbidden"}
            )
            return
        send_json(h, 200, payload)

    gateway.add_route("GET", "/api/chat", listing)
    gateway.add_prefix_route("GET", "/api/chat/", page)
    gateway.add_route("GET", "/api/chat-settings", lambda h: configuration(gateway, h))
    gateway.add_route("PUT", "/api/chat-settings", lambda h: configuration(gateway, h))


def readable_board(gateway, principal, board):
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    try:
        service.space_read(board, last=1)
        return True
    except ToolError:
        return False


def configuration(gateway, h):
    principal = _person(gateway, h)
    if principal is None:
        return
    params = query(h)
    board = params.get("board")
    if params.get("probe") == "1" and principal.role != "owner":
        send_json(h, 403, {"error": "Only workspace owners can probe models"})
        return
    if h.command == "PUT":
        if principal.role != "owner":
            send_json(h, 403, {"error": "Only workspace owners can change chat settings"})
            return
        try:
            body = read_json(h)
        except ValueError:
            return
        if (
            "front" not in body
            or set(body) - {"front", "scope", "mode"}
            or not isinstance(body["front"], dict)
        ):
            send_json(h, 400, {"error": "Invalid chat settings"})
            return
        board = body.get("scope")
        if board is not None and not isinstance(board, str):
            send_json(h, 400, {"error": "Invalid chat settings"})
            return
        if board is not None and not readable_board(gateway, principal, board):
            send_json(h, 404, {"error": "Board not found"})
            return
        try:
            config = settings.set_front(gateway.db, principal, body)
        except ChatDenied:
            send_json(h, 403, {"error": "Chat settings not allowed"})
            return
        except settings.LocalProviderMismatch:
            send_json(h, 400, {"error": "invalid_local_provider"})
            return
        except settings.NotLoopback:
            send_json(h, 400, {"error": "not_loopback"})
            return
        except ValueError:
            send_json(h, 400, {"error": "Invalid chat settings"})
            return
    else:
        if board is not None and not readable_board(gateway, principal, board):
            send_json(h, 404, {"error": "Board not found"})
            return
        config = settings.front(gateway.db, principal.workspace_id, board)
    extra = {}
    if params.get("probe") == "1":
        from dataclasses import replace

        probe = (
            config
            if settings.is_local(config)
            else replace(
                config,
                provider="openai_compatible",
                base_url="http://127.0.0.1:11434",
                key_ref=None,
            )
        )
        local_models, reachable = model.probe_models(probe)
        usage = store.counts_today(gateway.db, principal.workspace_id, principal.user_id)
        extra = {
            "local_models": local_models,
            "local_reachable": reachable,
            "last_error": usage.get("last_error"),
            "hosted": settings.hosted_options(principal.workspace_id),
        }
    send_json(
        h,
        200,
        {
            **extra,
            "front": config.public(),
            "usage": store.counts_today(gateway.db, principal.workspace_id, principal.user_id),
        },
    )
