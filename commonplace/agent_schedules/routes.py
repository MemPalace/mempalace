"""``/api/agent-schedules``: owners list, create, pause, resume and cancel schedules.

    GET  /api/agent-schedules                   {schedules, limits}
    POST /api/agent-schedules                   create (201); body: agent, board, name, prompt,
                                                schedule ({every_minutes} or {cron}), timezone
    GET  /api/agent-schedules/<id>              one schedule with its recent runs
    POST /api/agent-schedules/<id>/pause|resume|cancel

Only a signed-in workspace owner may use any of it (403 otherwise, including for agent
tokens); every id is looked up inside the caller's workspace (404 otherwise).
"""

from __future__ import annotations

from commonplace import http_util, schedule_time as times
from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext, ToolError

from . import store, worker

PREFIX = "/api/agent-schedules"
ACTIONS = ("pause", "resume", "cancel")
_KEYS = {"agent", "board", "name", "prompt", "schedule", "timezone"}


def limits() -> dict:
    return {
        "max_schedules": store.MAX_PER_WORKSPACE,
        "min_interval_minutes": times.MIN_INTERVAL_MINUTES,
        "max_prompt_chars": store.MAX_PROMPT,
    }


def _text(body, key, limit):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{key} must be text of 1 to {limit} characters")
    return value.strip() if key != "prompt" else value


def create(gateway, principal, body: dict, now) -> dict:
    if set(body) - _KEYS:
        raise ValueError("unknown field")
    name, prompt = _text(body, "name", store.MAX_NAME), _text(body, "prompt", store.MAX_PROMPT)
    tz = body.get("timezone", "UTC")
    schedule = times.validate(body.get("schedule"), tz, now)
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    board = _text(body, "board", 200)
    agent = _text(body, "agent", 200)
    connection = worker.agent_connection(gateway.db, principal.workspace_id, agent, now)
    if connection is None:
        raise LookupError("agent")
    try:
        coordinator = worker.coordinator_of(service, board)
    except ToolError as exc:
        raise LookupError("board") from exc
    if coordinator != "conn:" + connection["name"]:
        raise PermissionError("not_coordinator")
    return store.create(
        gateway.db,
        workspace_id=principal.workspace_id,
        user_id=principal.user_id,
        agent_id=connection["id"],
        board=board,
        name=name,
        prompt=prompt,
        schedule=schedule,
        tz=tz,
        now=now,
    )


def install(gateway) -> None:
    def route(h):
        principal = http_util.require_principal(gateway, h)
        if principal is None:
            return

        def send(status, body):
            http_util.send_json(h, status, body)

        if not principal.person or principal.role != "owner":
            send(403, {"error": "no: only a signed-in workspace owner can manage schedules"})
            return
        parts = http_util.path_parts(h, PREFIX + "/")
        scheduler = gateway.agent_schedules
        now = worker.moment(scheduler.now())
        db, workspace = gateway.db, principal.workspace_id
        try:
            if not parts and h.command == "GET":
                send(200, {"schedules": store.listing(db, workspace), "limits": limits()})
            elif not parts:
                try:
                    body = http_util.read_json(h)
                except ValueError:
                    return
                send(201, create(gateway, principal, body, now))
            elif len(parts) == 1 and h.command == "GET":
                send(200, store.get(db, workspace, parts[0]))
            elif len(parts) == 2 and h.command == "POST" and parts[1] in ACTIONS:
                send(200, store.set_state(db, workspace, parts[0], parts[1], now))
            else:
                send(404, {"error": "Not found"})
        except store.Missing:
            send(404, {"error": "no: schedule not found", "code": "not_found"})
        except store.Limit:
            send(
                409,
                {
                    "error": f"no: at most {store.MAX_PER_WORKSPACE} schedules per workspace",
                    "code": "limit",
                },
            )
        except PermissionError:
            send(
                409,
                {
                    "error": "no: that agent is not the board's coordinator",
                    "code": "not_coordinator",
                },
            )
        except LookupError as exc:
            what = "agent" if exc.args == ("agent",) else "board"
            send(404, {"error": f"no: {what} not found", "code": f"{what}_not_found"})
        except ValueError as exc:
            send(400, {"error": f"no: {exc}", "code": "invalid"})

    gateway.add_route("GET", PREFIX, route)
    gateway.add_route("POST", PREFIX, route)
    gateway.add_prefix_route("GET", PREFIX + "/", route)
    gateway.add_prefix_route("POST", PREFIX + "/", route)
