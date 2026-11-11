"""Contract REST routes; all policy and state changes run in BoardService."""

import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from urllib.parse import quote

from commonplace.extensions import ToolContext, ToolError
from commonplace.rules.enforce import RulesRefresh, rest_write

from ._http import (
    HTTPError,
    path_parts,
    query,
    read_json as _read_json,
    require_principal,
    send_json,
    send_text,
)
from .digest import ssml
from .display import NEEDS, with_urls
from .priority import rank
from .refusals import explained
from .service import BoardService, ParentError, SpaceError, VersionClash
from .store import CardRefused

OPEN_LIMIT, OPEN_LIMIT_MAX = 200, 500


def read_json(h):
    body = _read_json(h)
    body.pop("rules_seq", None)
    return body


@dataclass
class SpaceResponse:
    status: int
    payload: dict
    headers: dict | None = None


def space_projection(service, row):
    return {
        **{k: row[k] for k in ("slug", "name", "wing", "coordinator", "counts")},
        "can_configure": service.principal.person and service.principal.role == "owner",
        "open_url": "/app/#/board/" + quote(row["slug"], safe=""),
        "version": 1,
        "deleted": False,
    }


def install(gateway):
    def route(h):
        principal = require_principal(gateway, h)
        if principal is None:
            return
        parts = path_parts(h, "/api/")
        method = h.command
        if parts and parts[0] == "spaces":
            try:
                service = BoardService(ToolContext(principal=principal, gateway=gateway))
                response = dispatch(service, h, parts, method)
            except RulesRefresh as exc:
                send_json(h, 428, exc.payload)
            except SpaceError as exc:
                send_json(h, exc.status, {"error": exc.code})
            except HTTPError as exc:
                send_json(h, exc.status, {"error": str(exc)})
            else:
                send_json(h, response.status, response.payload, response.headers)
            return
        try:
            service = BoardService(ToolContext(principal=principal, gateway=gateway))
            status, result = dispatch(service, h, parts, method)
        except sqlite3.OperationalError as exc:
            from commonplace.projects import orchestration

            orchestration.require_lock_timeout(exc)
            send_json(h, 409, {"error": "busy", "code": "busy"})
            return
        except CardRefused as exc:
            send_json(
                h,
                exc.status,
                {"error": str(exc).removeprefix("no: "), "code": exc.code, **exc.details},
            )
            return
        except RulesRefresh as exc:
            send_json(h, 428, exc.payload)
            return
        except VersionClash as exc:
            send_json(
                h, 409, {"error": "card changed", "current": with_urls(service.ctx, exc.current)}
            )
            return
        except HTTPError as exc:
            send_json(h, exc.status, {"error": str(exc)})
            return
        except ParentError as exc:
            send_json(h, 400, {"error": str(exc).removeprefix("no: ")})
            return
        except ToolError as exc:
            message = str(exc).removeprefix("no: ")
            send_json(
                h,
                404 if message in ("card not found", "board not found") else 403,
                {"error": message},
            )
            return
        except TypeError:
            send_json(h, 400, {"error": "invalid arguments"})
            return
        format = query(h).get("format")
        if parts == ["cards"]:
            send_json(h, status, result)  # No share link per card: rows link to #/card/N.
        elif parts == ["needs-me", "digest"] and format in ("text", "ssml"):
            text = ssml(result) if format == "ssml" else result["text"]
            content_type = "application/ssml+xml" if format == "ssml" else "text/plain"
            send_text(h, status, text, f"{content_type}; charset=utf-8")
        else:
            send_json(h, status, with_urls(service.ctx, result))

    for method in ("GET", "POST", "PATCH", "DELETE"):
        for prefix in ("/api/boards/", "/api/cards/", "/api/needs-me/", "/api/spaces/"):
            gateway.add_prefix_route(method, prefix, route)


def open_cards(cards, limit=None):
    """Home's task table: slim cards, the most urgent first, capped, with the full count."""
    try:
        limit = OPEN_LIMIT if limit in (None, "") else int(limit)
    except ValueError:
        raise HTTPError(400, "limit must be a number") from None
    if not 1 <= limit <= OPEN_LIMIT_MAX:
        raise HTTPError(400, f"limit must be 1 to {OPEN_LIMIT_MAX}")
    cards = sorted(cards, key=lambda c: (NEEDS.index(c["need"]), rank(c["priority"]), c["number"]))
    return {
        "cards": [{k: v for k, v in c.items() if k != "body"} for c in cards[:limit]],
        "total": len(cards),
    }


def _space_route(service, h, parts, method):
    """Space chat and board settings routes; None when the path is not one of them."""
    if len(parts) == 3 and parts[2] == "coordinator" and method == "GET":
        p = service.principal
        if not p.person and not p.policy.tool_visible("board_set"):
            raise HTTPError(403, "tool not allowed for this connection")
        return 200, service.board_coordinator(parts[1])
    tool = None
    if len(parts) == 2 and method == "PATCH":
        tool = "board_set"
    elif len(parts) == 3 and parts[2] == "chat":
        tool = {"GET": "space_read", "POST": "space_post"}.get(method)
    if tool:
        # These new aliases enforce MCP visibility here, keeping gateway/handoff untouched.
        p = service.principal
        if not p.person and not p.policy.tool_visible(tool):
            raise HTTPError(403, "tool not allowed for this connection")
        if tool == "space_read":
            try:
                cursors = {
                    key: int(value)
                    for key, value in query(h).items()
                    if key in ("after", "last", "before")
                }
            except ValueError:
                raise HTTPError(400, "invalid cursor") from None
            return 200, service.space_read(parts[1], **cursors)
        body = read_json(h)
        with rest_write(service.ctx, h, tool, body):
            body.pop("board", None)
            if tool == "board_set":
                return 200, service.board_set(parts[1], **body)
            return 201, service.space_post(parts[1], **body)
    return None


def _explained(kind, service):
    """Refusals of a Done move or a handoff carry a reason and a next step; other routes are as is."""
    return explained(kind, person=service.principal.person) if kind else nullcontext()


def _card_route(service, h, parts, method):
    task = parts[1]
    if len(parts) == 2:
        if method == "GET":
            return 200, service.task_show(task)
        if method == "PATCH":
            body = read_json(h)
            with _explained("done" if body.get("status") == "done" else None, service):
                with rest_write(service.ctx, h, "task_update", body):
                    if "version" not in body or body["version"] is None:
                        raise HTTPError(400, "version is required")
                    body.pop("task", None)
                    return 200, service.task_update(task, **body)
    if len(parts) == 3 and method == "POST":
        handlers = {
            "comments": service.task_comment,
            "handoff": service.task_handoff,
            "decide": service.decide,
            "heartbeat": service.task_heartbeat,
            "approve": service.task_approve,
        }
        if parts[2] in handlers:
            body = read_json(h)
            tool = {
                "comments": "task_comment",
                "handoff": "task_handoff",
                "decide": "decide",
                "heartbeat": "task_heartbeat",
                "approve": "task_approve",
            }[parts[2]]
            with _explained("handoff" if parts[2] == "handoff" else None, service):
                with rest_write(service.ctx, h, tool, body):
                    # A decision answers the ask the person saw; a newer ask is a 409.
                    if parts[2] == "decide" and body.get("version") is None:
                        raise HTTPError(400, "version is required")
                    body.pop("task", None)
                    return 200, handlers[parts[2]](task, **body)
    raise HTTPError(404, "not found")


def dispatch(service, h, parts, method):
    if parts and parts[0] == "spaces":
        if len(parts) not in (1, 2):
            raise HTTPError(404, "not_found")
        collection = len(parts) == 1
        if (collection and method not in ("GET", "POST")) or (not collection and method != "GET"):
            return SpaceResponse(
                405,
                {"error": "method_not_allowed"},
                {"Allow": "GET, POST" if collection else "GET"},
            )
        if "Idempotency-Key" in h.headers:
            raise HTTPError(400, "unsupported_idempotency")
        if "include_deleted" in query(h):
            raise HTTPError(400, "unsupported_include_deleted")
        if method == "POST":
            try:
                body = _read_json(h)
            except HTTPError as exc:
                if exc.status == 400:
                    raise HTTPError(400, "invalid_arguments") from exc
                raise
            if (
                set(body) - {"slug", "name", "wing", "repo"}
                or not isinstance(body.get("slug"), str)
                or any(value is not None and not isinstance(value, str) for value in body.values())
            ):
                raise HTTPError(400, "invalid_arguments")
            with rest_write(service.ctx, h, "board_add", body):
                return SpaceResponse(201, space_projection(service, service.board_add(**body)))
        if collection:
            return SpaceResponse(
                200,
                {
                    "spaces": [
                        space_projection(service, row) for row in service.board_list(top_level=True)
                    ]
                },
            )
        return SpaceResponse(200, space_projection(service, service.space_metadata(parts[1])))
    if len(parts) in (2, 3) and parts[0] == "boards":
        routed = _space_route(service, h, parts, method)
        if routed:
            return routed
    if parts == ["boards"]:
        if method == "GET":
            return 200, {"boards": service.board_list(top_level=True)}
        if method == "POST":
            body = read_json(h)
            with rest_write(service.ctx, h, "board_add", body):
                return 201, service.board_add(**body)
    if len(parts) == 3 and parts[0] == "boards" and parts[2] == "cards":
        if method == "GET":
            cards = service.task_list(
                board=parts[1], status=query(h).get("status") or None, all_statuses=True
            )
            return 200, {
                "cards": [c for c in cards if c["parent"] is None],
                "linked": [c for c in cards if c["message_id"] is not None],
                "children": [c for c in cards if c["parent"] is not None],
            }
        if method == "POST":
            body = read_json(h)
            with rest_write(service.ctx, h, "task_add", body):
                body.pop("board", None)
                return 201, service.task_add(board=parts[1], **body)
    if parts == ["cards"] and method == "GET":
        return 200, open_cards(service.task_list(top_level=True), query(h).get("limit"))
    if parts == ["needs-me"] and method == "GET":
        return 200, service.needs_me()
    if parts == ["needs-me", "digest"] and method == "GET":
        if query(h).get("format", "json") not in ("json", "text", "ssml"):
            raise HTTPError(400, "format must be json, text or ssml")
        return 200, service.needs_me(format="digest")
    if len(parts) in (2, 3) and parts[0] == "cards":
        return _card_route(service, h, parts, method)
    raise HTTPError(404, "not found")
