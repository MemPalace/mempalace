"""Person-only HTTP routes for the live console."""

from dataclasses import replace
from datetime import datetime, timezone

from commonplace import redact
from commonplace.console import authority, settings, store, stream, types
from commonplace.http_util import path_parts, query, read_json, require_principal, send_json


def _person(gateway, h):
    principal = require_principal(gateway, h)
    if principal is not None and not principal.person:
        send_json(h, 403, {"error": "Sign in to view the console"})
        return None
    return principal


def _views(gateway, principal, agents):
    own_ids = [view.id for view in agents if view.own]
    try:
        activity = store.peek(gateway, principal.workspace_id) if own_ids else None
    except ValueError:
        activity = None
    latest = activity.latest_at(own_ids) if activity is not None else {}
    now = datetime.now(timezone.utc)
    result = []
    for view in agents:
        last_seen = view.last_used_at
        if view.own and latest.get(view.id):
            last_seen = max(last_seen or "", latest[view.id])
        result.append(
            {
                "id": view.id,
                "name": redact.redact(view.name)[0],
                "client": redact.redact(view.client)[0],
                "parent_id": view.parent_id,
                "own": view.own,
                "recording": view.recording if view.own else None,
                "can_toggle": view.own,
                "status": authority.status(last_seen, now),
                "last_seen": last_seen,
            }
        )
    return result


def install(gateway) -> None:
    def listing(h):
        principal = _person(gateway, h)
        if principal is None:
            return
        views = authority.agents(gateway, principal)
        send_json(h, 200, {"role": principal.role, "agents": _views(gateway, principal, views)})

    def page(h):
        principal = _person(gateway, h)
        if principal is None:
            return
        params = query(h)
        try:
            view = authority.agent(gateway, principal, params.get("agent"))
        except LookupError:
            send_json(h, 404, {"error": "Agent not found"})
            return
        limit = None
        if "limit" in params:
            try:
                if not params["limit"].isascii() or not params["limit"].isdigit():
                    raise ValueError
                limit = int(params["limit"])
                if not 1 <= limit <= 100:
                    raise ValueError
            except ValueError:
                send_json(h, 400, {"error": "Invalid limit"})
                return
        if "after" in params:
            try:
                stream.decode_cursor(params["after"])
            except ValueError:
                send_json(h, 400, {"error": "Invalid console cursor"})
                return
        payload = stream.page(gateway, principal, view, params.get("after"), limit)
        send_json(h, 200, payload)

    def toggle(h):
        principal = _person(gateway, h)
        if principal is None:
            return
        parts = path_parts(h, "/api/console/agents/")
        try:
            if len(parts) != 1:
                raise LookupError
            view = authority.agent(gateway, principal, parts[0])
        except LookupError:
            send_json(h, 404, {"error": "Agent not found"})
            return
        denied = {"error": "Only the person this agent acts for can change this"}
        if not view.own:
            send_json(h, 403, denied)
            return
        try:
            data = read_json(h)
        except ValueError:
            return
        if set(data) != {"record"} or type(data["record"]) is not bool:
            send_json(h, 400, {"error": "Use exactly one boolean record field"})
            return
        try:
            settings.set_recording(gateway.db, principal, view.id, data["record"])
        except types.ConsoleDenied:
            send_json(h, 403, denied)
            return
        updated = replace(view, recording=data["record"])
        send_json(h, 200, _views(gateway, principal, [updated])[0])

    gateway.add_route("GET", "/api/console", listing)
    gateway.add_route("GET", "/api/console/stream", page)
    gateway.add_prefix_route("POST", "/api/console/agents/", toggle)
