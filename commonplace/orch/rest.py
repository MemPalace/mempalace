"""REST aliases for the orchestrator tools under ``/api/orch/``.

Each route maps to one tool of ``tools.OPERATIONS`` and runs through ``tools.dispatch``,
so arguments, authority and refusals match MCP. Agent tokens pass the same tool
visibility gate as on MCP (tool list, scope, ``trusted_only``, ``owner_only``,
``available``); signed-in people skip it as on every other REST alias, and the stores
still check ownership and wing access. POST routes run inside ``rules.enforce.rest_write``
(the rules freshness check and the audit by tool name, as on every other REST write).

Routes (``<b>`` board slug, ``<r>`` role, ``<v>`` version number, ``<k>`` step key):

    GET  /api/orch/restore                              orch_restore_status
    POST /api/orch/restore/ack                          orch_restore_ack
    GET  /api/orch/boards/<b>/configs[?channel=]        orch_config_get
    GET  /api/orch/boards/<b>/configs/<v>               orch_config_get
    POST /api/orch/boards/<b>/configs                   orch_config_propose (201)
    POST /api/orch/boards/<b>/configs/<v>/accept        orch_config_accept
    POST /api/orch/boards/<b>/configs/<v>/reject        orch_config_reject
    POST /api/orch/boards/<b>/write                     orch_config_write
    GET  /api/orch/boards/<b>/render[?version=&channel=] orch_config_render
    GET  /api/orch/boards/<b>/diff?old=&new=            orch_config_diff
    POST /api/orch/boards/<b>/experiments/<name>/retire orch_exp_retire
    GET  /api/orch/boards/<b>/roles/<r>/checkpoint      orch_checkpoint
    POST /api/orch/boards/<b>/roles/<r>/resume          orch_resume
    POST /api/orch/boards/<b>/roles/<r>/heartbeat       orch_heartbeat
    POST /api/orch/boards/<b>/roles/<r>/release         orch_release
    POST /api/orch/boards/<b>/roles/<r>/force-release   orch_force_release
    POST /api/orch/boards/<b>/roles/<r>/card-starts     orch_card_start (201 new, 200 same step)
    POST /api/orch/boards/<b>/roles/<r>/journal         orch_journal_intent
    POST /api/orch/boards/<b>/roles/<r>/journal/<k>     orch_journal_record
    POST /api/orch/boards/<b>/roles/<r>/gates           orch_gate_open
    POST /api/orch/boards/<b>/roles/<r>/gates/<k>       orch_gate_record
    POST /api/orch/boards/<b>/roles/<r>/steps/<k>/void  orch_void_step

Refusals answer ``{"error": "no: ...", "code": ..., "details": {...}}`` with the
refusal's own HTTP status (400, 403, 404 or 409).
"""

from __future__ import annotations

import re

from commonplace.boards._http import HTTPError, path_parts, query, read_json, send_json
from commonplace.extensions import ToolContext, ToolError

from . import tools

PREFIX = "/api/orch/"
_NUMBER = re.compile(r"v?([1-9][0-9]{0,8})")
_INTEGER_QUERY = ("version", "old", "new")


def _number(value: str, key: str) -> int:
    match = _NUMBER.fullmatch(value or "")
    if not match:
        raise tools.SurfaceError("invalid_argument", f"invalid {key}", 400)
    return int(match.group(1))


def resolve(method: str, parts: list) -> tuple[str, dict]:
    """``(tool name, path arguments)`` for a route below ``/api/orch/``; 404 otherwise."""
    get, post = method == "GET", method == "POST"
    if parts == ["restore"] and get:
        return "orch_restore_status", {}
    if parts == ["restore", "ack"] and post:
        return "orch_restore_ack", {}
    if len(parts) < 3 or parts[0] != "boards":
        raise HTTPError(404, "not found")
    board, rest = {"board": parts[1]}, parts[2:]
    if rest == ["configs"]:
        return ("orch_config_get" if get else "orch_config_propose"), board
    if len(rest) == 2 and rest[0] == "configs" and get:
        return "orch_config_get", {**board, "version": _number(rest[1], "version")}
    if len(rest) == 3 and rest[0] == "configs" and rest[2] in ("accept", "reject") and post:
        name = "orch_config_" + rest[2]
        return name, {**board, "version": _number(rest[1], "version")}
    if rest == ["write"] and post:
        return "orch_config_write", board
    if rest == ["render"] and get:
        return "orch_config_render", board
    if rest == ["diff"] and get:
        return "orch_config_diff", board
    if len(rest) == 3 and rest[0] == "experiments" and rest[2] == "retire" and post:
        return "orch_exp_retire", {**board, "name": rest[1]}
    if len(rest) >= 3 and rest[0] == "roles":
        role, tail = {**board, "role": rest[1]}, rest[2:]
        if tail == ["checkpoint"] and get:
            return "orch_checkpoint", role
        if not post:
            raise HTTPError(404, "not found")
        simple = {
            "resume": "orch_resume",
            "heartbeat": "orch_heartbeat",
            "release": "orch_release",
            "force-release": "orch_force_release",
            "card-starts": "orch_card_start",
            "journal": "orch_journal_intent",
            "gates": "orch_gate_open",
        }
        if len(tail) == 1 and tail[0] in simple:
            return simple[tail[0]], role
        if len(tail) == 2 and tail[0] == "journal":
            return "orch_journal_record", {**role, "step_key": tail[1]}
        if len(tail) == 2 and tail[0] == "gates":
            return "orch_gate_record", {**role, "step_key": tail[1]}
        if len(tail) == 3 and tail[0] == "steps" and tail[2] == "void":
            return "orch_void_step", {**role, "step_key": tail[1]}
    raise HTTPError(404, "not found")


def visible(principal, gateway, name: str) -> bool:
    """The MCP visibility gate for agent tokens; people skip it as on other REST aliases."""
    from commonplace import extensions

    if principal.person:
        return getattr(principal, "runner", None) is None
    tool = extensions.get(name)
    return principal.policy.refusal(name) is None and tool.usable_by(principal, gateway)


def _query(handler) -> dict:
    supplied = dict(query(handler))
    for key in _INTEGER_QUERY:
        if key in supplied:
            supplied[key] = _number(supplied[key], key)
    return supplied


def _merge(supplied: dict, path_args: dict) -> dict:
    if supplied.keys() & path_args.keys():
        raise tools.SurfaceError("invalid_argument", "unexpected arguments", 400)
    return {**supplied, **path_args}


def _status(name: str, result) -> tuple[int, object]:
    if name == "orch_card_start" and isinstance(result, dict):
        return result.get("status", 200), result
    if name == "orch_config_propose":
        return 201, result
    return 200, result


def _refusal(exc: ToolError) -> tuple[int, dict]:
    status = getattr(exc, "status", None)
    if not isinstance(status, int) or isinstance(status, bool):
        status = 403
    body = {"error": str(exc) if str(exc).startswith("no:") else "no: " + str(exc)}
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        body["code"] = code
    details = getattr(exc, "details", None)
    if details:
        body["details"] = details
    return status, body


def install(gateway) -> None:
    from commonplace.rules.enforce import RulesRefresh, rest_write

    def route(handler):
        principal = gateway.principal_for(handler)
        if principal is None:
            send_json(handler, 401, {"error": "unauthorized"})
            return
        ctx = ToolContext(principal, gateway)
        try:
            name, path_args = resolve(handler.command, path_parts(handler, PREFIX))
            if not visible(principal, gateway, name):
                handler.close_connection = True
                send_json(handler, 403, {"error": "tool not allowed for this connection"})
                return
            if handler.command == "GET":
                result = tools.dispatch(ctx, name, _merge(_query(handler), path_args))
            else:
                body = read_json(handler)
                # Same rules freshness check and audit as every other REST write alias.
                with rest_write(ctx, handler, name, body):
                    result = tools.dispatch(ctx, name, _merge(body, path_args))
            send_json(handler, *_status(name, result))
        except RulesRefresh as exc:
            send_json(handler, 428, exc.payload)
        except HTTPError as exc:
            send_json(handler, exc.status, {"error": str(exc)})
        except ToolError as exc:
            send_json(handler, *_refusal(exc))

    gateway.add_prefix_route("GET", PREFIX, route)
    gateway.add_prefix_route("POST", PREFIX, route)
