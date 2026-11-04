"""REST aliases for requester tools, with the ordinary rules freshness gate."""

from commonplace.boards._http import (
    HTTPError,
    path_parts,
    query,
    read_json,
    require_principal,
    send_json,
)
from commonplace.extensions import ToolContext, ToolError
from commonplace.rules.enforce import RulesRefresh, rest_write

from .tools import dispatch

NOT_FOUND = {"machine not found", "start not found", "card not found"}
CONFLICTS = {
    "busy",
    "card already has an active start",
    "card was started recently",
    "request_key reused with different arguments",
    "start outcome is uncertain; a workspace owner must resolve it",
    "start is already finished",
}


def register(gateway):
    def route(handler):
        principal = require_principal(gateway, handler)
        if principal is None:
            return
        ctx = ToolContext(principal, gateway)
        parts = path_parts(handler, "/api/")
        try:
            if parts == ["card-starts"] and handler.command == "GET":
                result = dispatch(ctx, "card_start_status", query(handler))
                send_json(handler, 200, result)
                return
            if parts == ["card-starts"] and handler.command == "POST":
                name = "card_start"
                path_args = {}
            elif (
                len(parts) == 3
                and parts[0] == "card-starts"
                and parts[2] in ("cancel", "resolve")
                and handler.command == "POST"
            ):
                name = "card_start_" + parts[2]
                path_args = {"job": parts[1]}
            else:
                raise HTTPError(404, "not found")
            body = read_json(handler)
            with rest_write(ctx, handler, name, body):
                if path_args and "job" in body:
                    raise ToolError("no: unexpected arguments")
                result = dispatch(ctx, name, {**body, **path_args})
            status, payload = result if name == "card_start" else (200, result)
            send_json(handler, status, payload)
        except RulesRefresh as exc:
            send_json(handler, 428, exc.payload)
        except HTTPError as exc:
            send_json(handler, exc.status, {"error": str(exc)})
        except ToolError as exc:
            message = str(exc).removeprefix("no: ")
            status = 404 if message in NOT_FOUND else 409 if message in CONFLICTS else 403
            send_json(handler, status, {"error": str(exc)})

    gateway.add_route("POST", "/api/card-starts", route)
    gateway.add_route("GET", "/api/card-starts", route)
    gateway.add_prefix_route("POST", "/api/card-starts/", route)
