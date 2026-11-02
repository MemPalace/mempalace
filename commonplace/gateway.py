"""The gateway: the only way into a workspace's palace.

``POST /mcp`` authenticates the bearer, resolves its connection and policy,
and then, for every JSON-RPC request:

* ``tools/list`` returns only the tools the policy lets this token call;
* ``tools/call`` refuses a hidden tool exactly as an unknown one, checks the
  call's wings against the policy, pins or forces wing arguments where the
  rule says so, and forwards to the workspace's worker with the worker's own
  token and the verified identity, which the worker stamps on every write;
* results that could carry another wing's drawers are checked once more on
  the way out, and anything outside the token's wings is dropped.

Every write and every refusal goes to the audit log (ids only, never text).
"""

from __future__ import annotations

import copy
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import sleep
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

from mempalace import provenance
from mempalace.config import normalize_wing_name
from mempalace.knowledge_graph import strictest
from mempalace.tool_capabilities import classify_tool

from . import events, extensions, plans, privacy, ratelimit, sessions, urls
from .auth import PersonalTokenAuthenticator, bearer_from_header
from .control_db import ControlDB, Principal
from .http_util import log_failure
from .policy import TOOL_RULES, Policy, canonical_wing, is_private_wing
from .workers import Worker, WorkerSupervisor

logger = logging.getLogger("commonplace.gateway")

MAX_REQUEST_BYTES = 8 * 1024 * 1024
# A whole agent transcript (context handoff); the handoff API checks it again.
HANDOFF_REQUEST_BYTES = 64 * 1024 * 1024
WORKER_TIMEOUT_S = 660.0
STARTUP_TIMEOUT_S = 0.08
MAX_FANOUT_WINGS = 64
_FORWARDED_METHODS = ("initialize", "ping")
_DROP = object()


def _with_search_facts(result, facts, facts_error=None):
    from mempalace.fact_render import format_facts

    if result.get("error"):
        return result
    if (
        not facts
        and not facts_error
        and not any(key in result for key in ("facts", "results", "cli_output"))
    ):
        return result
    out = {k: v for k, v in result.items() if k != "facts"}
    if facts or facts_error or "cli_output" not in out:
        out = {"facts": facts, **out}
    if facts_error:
        out["facts_error"] = "unavailable"
    if isinstance(out.get("cli_output"), str):
        out["cli_output"] = format_facts(facts) + out["cli_output"]
    return out


def _rpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_response(req_id, result: dict, *, is_error: bool = False) -> dict:
    body = {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}
    if is_error:
        body["isError"] = True
    return {"jsonrpc": "2.0", "id": req_id, "result": body}


def _tool_result(response: dict):
    """The parsed JSON result inside a tools/call response, or ``None``."""
    try:
        text = response["result"]["content"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _replace_result(response: dict, result) -> dict:
    out = copy.copy(response)
    body = dict(out["result"])
    body["content"] = [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]
    out["result"] = body
    return out


class Denied(Exception):
    """A call the policy refuses; the message goes back to the client."""


class Hidden(Exception):
    """A call on something the token can't see; answered as if it didn't exist."""

    def __init__(self, result: dict):
        super().__init__(result.get("error", ""))
        self.result = result


class WorkerUnavailable(Exception):
    pass


def _startup_pointer(principal):
    """Keep a small canonical follow-up even when optional context is unavailable."""
    policy = principal.policy
    if policy.tool_visible("session_start"):
        followup = "Automatically call session_start for full context."
    else:
        tools = [name for name in ("rules_get", "memory_search") if policy.tool_visible(name)]
        followup = (
            "Automatically call " + " and ".join(tools) + " for full context."
            if tools
            else "This connection has no context read tools."
        )
    return "Commonplace is the single source for rules and memory. " + followup


class WorkerClient:
    """Posts JSON-RPC to a worker as the gateway, on behalf of a principal."""

    def __init__(self, supervisor: WorkerSupervisor):
        self.supervisor = supervisor

    def post(self, principal: Principal, request: dict) -> Optional[dict]:
        try:
            worker = self.supervisor.get(principal.workspace_id)
        except Exception as exc:
            log_failure(logger, "worker start failed", exc)
            raise WorkerUnavailable() from exc
        return self._post(worker, principal, request)

    def _post(self, worker: Worker, principal: Principal, request: dict) -> Optional[dict]:
        from .rules.checks import remaining

        body = json.dumps(request).encode("utf-8")
        conn = None
        resp = None
        try:
            conn = http.client.HTTPConnection(
                worker.host, worker.port, timeout=remaining(WORKER_TIMEOUT_S)
            )
            conn.request(
                "POST",
                "/mcp",
                body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {worker.token}",
                    "Host": worker.host_header,
                    provenance.IDENTITY_HEADER: provenance.encode_identity(principal.identity()),
                },
            )
            # Header and body reads share the caller's deadline with connect/send.
            # Keep the socket: getresponse may detach it for Connection: close.
            sock = conn.sock
            if sock is not None:
                sock.settimeout(remaining(WORKER_TIMEOUT_S))
            resp = conn.getresponse()
            chunks = []
            while not resp.isclosed():
                if sock is not None:
                    sock.settimeout(remaining(WORKER_TIMEOUT_S))
                chunk = resp.read1(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
        except OSError as exc:
            raise WorkerUnavailable() from exc
        finally:
            if resp is not None:
                resp.close()
            if conn is not None:
                conn.close()
        if resp.status == 202:
            return None
        if resp.status != 200:
            logger.error("worker answered HTTP %s for %s", resp.status, principal.workspace_id)
            raise WorkerUnavailable()
        return json.loads(raw.decode("utf-8"))

    def call_tool(self, principal: Principal, name: str, arguments: dict):
        """Run one tool internally and return its parsed result."""
        arguments = dict(arguments)
        params = {"name": name, "arguments": arguments}
        method = "tools/call"
        if name == "write_mirror_units":
            method, params = "internal/write_mirror_units", arguments
        response = self.post(
            principal,
            {
                "jsonrpc": "2.0",
                "id": "commonplace-internal",
                "method": method,
                "params": params,
            },
        )
        if not isinstance(response, dict) or "error" in response:
            raise WorkerUnavailable()
        return _tool_result(response)


class Mediator:
    """Applies a principal's policy to one JSON-RPC request."""

    def __init__(self, db: ControlDB, workers: WorkerClient):
        self.db = db
        self.workers = workers
        self.gateway = None  # set by Gateway; local tools get it in their context

    # ── entry point ──
    def handle(self, principal: Principal, request) -> Optional[dict]:
        if not isinstance(request, dict):
            return _rpc_error(None, -32600, "Batch requests are not supported")
        req_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            return _rpc_error(req_id, -32600, "Invalid request")
        try:
            if method in _FORWARDED_METHODS or method.startswith("notifications/"):
                response = self.workers.post(principal, request)
                if (
                    method == "initialize"
                    and isinstance(response, dict)
                    and isinstance(response.get("result"), dict)
                ):
                    response = self._initialize_context(principal, response)
                return response
            if method == "tools/list":
                return self._tools_list(principal, request)
            if method == "tools/call":
                return self._tools_call(principal, request)
        except WorkerUnavailable:
            return _rpc_error(req_id, -32603, "This workspace's memory is unavailable right now")
        if req_id is None:
            return None
        return _rpc_error(req_id, -32601, f"Unknown method: {method}")

    def _initialize_context(self, principal, response):
        from .rules import checks, render
        from .rules.service import instructions_for

        previous = response["result"].get("instructions", "")
        if principal.person or not isinstance(previous, str):
            return response
        budget = render.INSTRUCTIONS_BUDGET - (render.length(previous) + 2 if previous else 0)
        pointer = _startup_pointer(principal)
        if budget < render.length(pointer):
            return response
        ctx = extensions.ToolContext(principal, self.gateway)
        rules, context = "", pointer
        try:
            with checks.deadline(STARTUP_TIMEOUT_S):
                # Whole rules have priority; reserve only the mandatory pointer.
                rules = instructions_for(
                    ctx, budget=budget - render.length(pointer) - (2 if pointer else 0)
                )
                context_budget = budget - render.length(rules) - (2 if rules else 0)
                checks.remaining(STARTUP_TIMEOUT_S)
                from .session import initialize_context

                candidate = initialize_context(ctx, budget=context_budget)
                checks.remaining(STARTUP_TIMEOUT_S)
                if (
                    isinstance(candidate, str)
                    and candidate
                    and render.length(candidate) <= context_budget
                ):
                    context = candidate
        except Exception as exc:
            log_failure(logger, "startup context unavailable", exc)
        instructions = "\n\n".join(part for part in (rules, context, previous) if part)
        if instructions:
            response = copy.copy(response)
            response["result"] = dict(response["result"])
            response["result"]["instructions"] = instructions
        return response

    # ── tools/list ──
    def _tools_list(self, principal: Principal, request: dict) -> dict:
        try:
            response = self.workers.post(principal, request)
        except WorkerUnavailable:
            response = {"jsonrpc": "2.0", "id": request.get("id"), "result": {"tools": []}}
        policy = principal.policy
        try:
            tools = response["result"]["tools"]
        except (KeyError, TypeError):
            return response
        response = copy.copy(response)
        response["result"] = dict(response["result"])
        local_names = extensions.names()
        # The gateway's own tools win over a worker tool of the same name, so a
        # gated extension tool can't slip through as a worker tool.
        visible = [
            t
            for t in tools
            if t.get("name") not in local_names and policy.tool_visible(t.get("name"))
        ]
        for name in sorted(local_names):
            tool = extensions.get(name)
            if policy.tool_visible(name) and tool.usable_by(principal, self.gateway):
                visible.append(tool.schema())
        response["result"]["tools"] = visible
        return response

    def _local_call(self, principal: Principal, tool, req_id, args: dict) -> dict:
        if not tool.usable_by(principal, self.gateway):
            self._audit(principal, tool.name, "deny", "owner only" if tool.owner_only else "off")
            return _rpc_error(req_id, -32601, f"Unknown tool: {tool.name}")
        ctx = extensions.ToolContext(principal=principal, gateway=self.gateway)
        try:
            result = tool.handler(ctx, args)
        except extensions.ToolError as exc:
            if tool.kind == "write":
                self._audit(principal, tool.name, "deny", str(exc)[:200])
            text = str(exc)
            if not text.startswith("no:"):
                text = f"no: {text}"
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": True},
            }
        if tool.kind == "write":
            self._audit(principal, tool.name, "ok")
        if isinstance(result, str):
            text = result
        else:
            text = json.dumps(result, indent=2, ensure_ascii=False)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": text}]},
        }

    # ── tools/call ──
    def _tools_call(self, principal: Principal, request: dict) -> dict:
        req_id = request.get("id")
        params = request.get("params")
        params = params if isinstance(params, dict) else {}
        name = params.get("name")
        args = params.get("arguments")
        from mempalace.stored_text import user_tool_arguments

        args = user_tool_arguments(dict(args), tool_name=name) if isinstance(args, dict) else {}
        args.pop("rules_seq", None)
        policy = principal.policy
        if not isinstance(name, str):
            return _rpc_error(req_id, -32602, "Invalid params: tool name must be a string")

        local = extensions.get(name)
        refusal = policy.refusal(name)
        if local is not None and local.available is not None:
            # A feature a workspace doesn't use is audited like a tool that
            # doesn't exist, whatever the token holds.
            if not local.usable_by(principal, self.gateway):
                refusal = "unknown tool"
        if refusal is not None:
            self._audit(principal, name, "deny", refusal)
            # A hidden tool looks exactly like a tool that doesn't exist.
            return _rpc_error(req_id, -32601, f"Unknown tool: {name}")

        if local is not None:
            return self._local_call(principal, local, req_id, args)

        rule = TOOL_RULES[name][1]
        notes: list = []
        try:
            _check_argument_types(args)
            plan = getattr(self, f"_before_{rule}")(principal, name, args, notes)
        except Hidden as exc:
            self._audit(
                principal, name, "deny", "hidden drawer", target_ids=_target_ids(args, None)
            )
            return _tool_response(req_id, exc.result)
        except Denied as exc:
            self._audit(principal, name, "deny", str(exc), wing=_wing_of(args))
            return _tool_response(req_id, {"error": str(exc), "denied": True}, is_error=True)

        if isinstance(plan, dict) and "respond" in plan:
            response = _tool_response(req_id, plan["respond"])
        else:
            forwarded = dict(request)
            forwarded["params"] = {"name": name, "arguments": args}
            response = self.workers.post(principal, forwarded)
            response = self._after(principal, name, rule, args, response)
            if isinstance(plan, dict) and "facts" in plan:
                result = _tool_result(response)
                if isinstance(result, dict):
                    response = _replace_result(
                        response, _with_search_facts(result, plan["facts"], plan.get("facts_error"))
                    )
            if isinstance(plan, dict) and plan.get("unhide"):
                unhide = plan["unhide"]
                response = _unhide(response, unhide)
                if "drawer_ids" in args:
                    args["drawer_ids"] = [unhide.get(i, i) for i in args["drawer_ids"]]
                else:
                    args["drawer_id"] = unhide.get(args.get("drawer_id"), args.get("drawer_id"))
                notes.append(f"{len(unhide)} hidden drawer(s) answered as missing")

        if classify_tool(name) != "read":
            result = _tool_result(response) if isinstance(response, dict) else None
            ok = isinstance(result, dict) and not result.get("error") and "error" not in response
            self._audit(
                principal,
                name,
                "ok" if ok else "error",
                "; ".join(notes),
                wing=_wing_of(args),
                target_ids=_target_ids(args, result),
            )
        return response

    def _audit(self, principal, tool, decision, reason="", wing=None, target_ids=()):
        self.db.audit(
            action="tool.call",
            decision=decision,
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
            connection_id=principal.connection_id,
            auth=principal.auth,
            tool=tool,
            reason=reason,
            wing=wing,
            target_ids=target_ids,
        )

    # ── per-rule checks before the call ──
    def _before_none(self, principal, name, args, notes):
        return None

    def _before_all_wings(self, principal, name, args, notes):
        return None  # Policy.refusal already required every wing.

    def _before_wing_list(self, principal, name, args, notes):
        return None

    def _before_read_wing(self, principal, name, args, notes):
        policy = principal.policy
        if policy.sees_all_wings:
            return None
        wing = args.get("wing")
        if not wing:
            wing = _only_literal_wing(policy)
            if not wing:
                raise Denied(f"pass a wing: this connection reads {_wings_text(policy)}")
            args["wing"] = wing
        if not policy.can_read_wing(wing):
            raise Denied(f"this connection can't read wing {wing!r}")
        return None

    def _before_search(self, principal, name, args, notes):
        requested = args.get("wings")
        if requested is not None and (
            not isinstance(requested, list)
            or len(requested) > 64
            or any(not isinstance(w, str) or not w for w in requested)
        ):
            raise Denied("wings must be a list of at most 64 wing names")
        policy = principal.policy
        if requested is not None:
            requested = [w for w in requested if policy.can_read_wing(w)]
            args["wings"] = requested
        wing = args.get("wing")
        if wing and not policy.sees_all_wings and not policy.can_read_wing(wing):
            raise Denied(f"this connection can't read wing {wing!r}")
        fact_result = (
            self._search_facts(principal, args)
            if args.get("include_facts", True)
            else {"facts": []}
        )
        # A worker has workspace authority. Always suppress its unscoped fact
        # search, including captured CLI output, and attach the scoped read.
        args["include_facts"] = False
        if wing or (policy.sees_all_wings and requested is None):
            if requested is not None and not any(
                canonical_wing(wing) == canonical_wing(w) for w in requested
            ):
                return {"respond": {"query": args.get("query", ""), "results": [], "facts": []}}
            return fact_result
        return {
            "respond": _with_search_facts(
                self._fan_out_search(principal, name, args),
                fact_result["facts"],
                fact_result.get("facts_error"),
            )
        }

    def _search_facts(self, principal, args):
        try:
            if not principal.policy.tool_visible("mempalace_kg_query"):
                return {"facts": []}
            call = {"entity": args.get("query", ""), "search": True, "limit": args.get("limit", 5)}
            if args.get("wing"):
                call["wings"] = [args["wing"]]
            elif args.get("wings") is not None:
                call["wings"] = args["wings"]
            self._before_kg_read(principal, "mempalace_kg_query", call, [])
            result = self.workers.call_tool(principal, "mempalace_kg_query", call)
            if (
                not isinstance(result, dict)
                or result.get("error")
                or not isinstance(result.get("facts"), list)
            ):
                raise ValueError("Invalid fact search response")
            return {
                "facts": result["facts"],
                **({"facts_error": "unavailable"} if result.get("facts_error") else {}),
            }
        except Exception:
            logger.warning("Fact search unavailable; preserving drawer search", exc_info=True)
            return {"facts": [], "facts_error": "unavailable"}

    def _before_drawer_read(self, principal, name, args, notes):
        return None  # Checked on the way out, so a hidden drawer reads as missing.

    def _before_drawer_write(self, principal, name, args, notes):
        policy = principal.policy
        ids = _drawer_ids(args)
        if ids is None:
            raise Denied("drawer_id or drawer_ids (1 to 500 ids) is required")
        wings = self._drawer_wings(principal, ids)
        unhide = {}
        for drawer_id in ids:
            wing = wings.get(drawer_id)
            if wing is None:
                continue  # Missing: the worker answers "not found".
            if not policy.can_read_wing(wing):
                # Send an id that can't exist, so the worker answers in the
                # tool's own not-found shape; the real id goes back on the way out.
                unhide[f"cmmn-hidden-{secrets.token_hex(12)}"] = drawer_id
                continue
            if not policy.can_write_wing(wing):
                raise Denied(f"this connection can't change memories in wing {wing!r}")
        new_wing = args.get("wing")
        if new_wing and not policy.can_write_wing(new_wing):
            raise Denied(f"this connection can't write to wing {new_wing!r}")
        if unhide:
            by_real = {real: fake for fake, real in unhide.items()}
            if "drawer_ids" in args:
                args["drawer_ids"] = [by_real.get(i, i) for i in args["drawer_ids"]]
            else:
                args["drawer_id"] = by_real[args["drawer_id"]]
            return {"unhide": unhide}
        return None

    def _before_add_drawer(self, principal, name, args, notes):
        self._pin_write_wing(principal.policy, args, args.get("wing"), notes)
        return None

    def _before_diary_write(self, principal, name, args, notes):
        wing = args.get("wing") or _agent_wing(args.get("agent_name"))
        self._pin_write_wing(principal.policy, args, wing, notes)
        return None

    def _before_diary_read(self, principal, name, args, notes):
        policy = principal.policy
        if policy.sees_all_wings:
            return None
        wing = args.get("wing") or _agent_wing(args.get("agent_name"))
        if not wing or not policy.can_read_wing(wing):
            raise Denied(f"this connection can't read wing {wing!r}")
        args["wing"] = wing
        return None

    def _before_tunnel_write(self, principal, name, args, notes):
        policy = principal.policy
        for key in ("source_wing", "target_wing"):
            wing = args.get(key)
            if not wing or not policy.can_write_wing(wing):
                raise Denied(f"this connection can't link wing {wing!r}")
        ids = [args[k] for k in ("source_drawer_id", "target_drawer_id") if args.get(k)]
        wings = self._drawer_wings(principal, ids) if ids else {}
        for drawer_id in ids:
            wing = wings.get(drawer_id)
            if wing is None or not policy.can_read_wing(wing):
                raise Hidden({"success": False, "error": f"Drawer not found: {drawer_id}"})
            if not policy.can_write_wing(wing):
                raise Denied(f"this connection can't link drawers in wing {wing!r}")
        return None

    def _before_kg_read(self, principal, name, args, notes):
        policy = principal.policy
        if policy.sees_all_wings:
            return None
        asked = args.get("wings")
        if asked is not None:
            for wing in asked:
                if not policy.can_read_wing(wing):
                    raise Denied(f"this connection can't read wing {wing!r}")
            asked = {canonical_wing(w) for w in asked}
        result = self.workers.call_tool(principal, "mempalace_kg_list_wings", {})
        known = result.get("wings") if isinstance(result, dict) else None
        if not isinstance(known, list):
            raise Denied("could not check which wings the facts are in")
        args["wings"] = [
            w
            for w in known
            if isinstance(w, str)
            and policy.can_read_wing(w)
            and (asked is None or canonical_wing(w) in asked)
        ]
        allowed = policy.fact_sensitivities()
        given = args.get("sensitivities")
        args["sensitivities"] = [s for s in allowed if given is None or s in given]
        return None

    def _before_kg_write(self, principal, name, args, notes):
        policy = principal.policy
        wing = args.get("wing")
        if wing or not (policy.sees_all_wings and policy.writes_all_wings):
            # Only a token that sees every wing may file a fact with no wing:
            # only such a token can read one back.
            self._pin_write_wing(policy, args, wing, notes)
        if not policy.sees_all_wings:
            # A fact write answers about what is stored (merged, replaced,
            # closed), so it needs the wing to be readable as well.
            if not policy.can_read_wing(args["wing"]):
                raise Denied(f"this connection can't read facts in wing {args['wing']!r}")
            # Only facts this token could read count as the same fact or can
            # be changed, so a write never reveals or touches a hidden one.
            args["sensitivities"] = policy.fact_sensitivities()
            drawer_id = args.get("source_drawer_id")
            if drawer_id:
                drawer_wing = self._drawer_wings(principal, [drawer_id]).get(drawer_id)
                if drawer_wing is None or not policy.can_read_wing(drawer_wing):
                    raise Hidden({"success": False, "error": f"Drawer not found: {drawer_id}"})
        if name == "mempalace_kg_invalidate":
            return None
        floor = _fact_floor(args)
        try:
            raised = strictest(args.get("sensitivity"), floor)
        except ValueError as exc:
            raise Denied(str(exc)) from None
        if args.get("sensitivity") not in (None, "", raised):
            notes.append(f"sensitivity raised to {raised!r}")
        args["sensitivity"] = raised
        return None

    def _pin_write_wing(self, policy: Policy, args: dict, wing, notes: list) -> None:
        if policy.trust == "untrusted":
            # Untrusted writes always land in the token's write wing (the inbox).
            if not policy.can_write_wing(policy.write_wing):
                raise Denied("this connection can't write")
            if wing and wing != policy.write_wing:
                notes.append(f"wing {wing!r} forced to {policy.write_wing!r}")
            args["wing"] = policy.write_wing
            return
        if not wing:
            wing = policy.write_wing
        if not wing or not policy.can_write_wing(wing):
            raise Denied(f"this connection can't write to wing {wing!r}")
        args["wing"] = wing

    # ── checks on the way out ──
    def _after(self, principal, name, rule, args, response):
        policy = principal.policy
        if not isinstance(response, dict) or "result" not in response or policy.sees_all_wings:
            return response
        if rule == "add_drawer" and not policy.can_read_wing(args.get("wing") or ""):
            # A retry into a wing the caller can't read must not show whose
            # chat the first copy came from.
            result = _tool_result(response)
            if isinstance(result, dict) and "source_kept" in result:
                result = {k: v for k, v in result.items() if k != "source_kept"}
                return _replace_result(response, result)
            return response
        if rule not in ("wing_list", "read_wing", "search", "drawer_read", "diary_read", "kg_read"):
            return response
        result = _tool_result(response)
        if result is None:
            return response
        if rule == "wing_list":
            cleaned = _filter_wing_keys(result, policy)
        elif name == "mempalace_get_drawers":
            cleaned = _filter_drawer_batch(result, policy)
        else:
            cleaned = _scrub(result, policy.can_read_wing)
            if cleaned is _DROP:
                missing = args.get("drawer_id") if rule == "drawer_read" else None
                cleaned = (
                    {"error": f"Drawer not found: {missing}"}
                    if missing
                    else {"error": "not found", "denied": True}
                )
        return _replace_result(response, cleaned)

    # ── helpers that ask the worker ──
    def _drawer_wings(self, principal, ids: list) -> dict:
        if not ids:
            return {}
        result = self.workers.call_tool(principal, "mempalace_get_drawers", {"drawer_ids": ids})
        items = result.get("results") if isinstance(result, dict) else None
        if not isinstance(items, list) or len(items) != len(ids):
            raise Denied("could not check which wings these drawers are in")
        # Results come back in input order. Match by position: a chunk id
        # resolves to a payload whose drawer_id is its group's.
        wings = {}
        for drawer_id, item in zip(ids, items):
            if isinstance(item, dict) and not item.get("error"):
                wings[drawer_id] = item.get("wing") or ""
        return wings

    def _fan_out_search(self, principal, name, args) -> dict:
        """One worker search and one ranking over the readable wing union."""
        listing = self.workers.call_tool(principal, "mempalace_list_wings", {}) or {}
        requested = args.get("wings")
        wings = sorted(
            w
            for w in (listing.get("wings") or {})
            if principal.policy.can_read_wing(w)
            and (
                requested is None or any(canonical_wing(w) == canonical_wing(r) for r in requested)
            )
        )
        if len(wings) > MAX_FANOUT_WINGS:
            raise Denied(f"pass a wing: this connection reads {len(wings)} wings")
        if not wings:
            return {"query": args.get("query", ""), "results": [], "wings_searched": []}
        result = self.workers.call_tool(principal, name, {**args, "wings": wings})
        if not isinstance(result, dict):
            raise WorkerUnavailable()
        if result.get("error"):
            return result
        result = dict(result)
        result["results"] = [
            h
            for h in result.get("results", [])
            if _scrub(h, principal.policy.can_read_wing) is not _DROP
        ]
        result["wings_searched"] = wings
        return result


# ── pure helpers ──
def _fact_floor(args: dict) -> str:
    """The least sensitivity a fact may have: from its wing and its words."""
    floor = "private" if args.get("wing") and is_private_wing(args["wing"]) else "internal"
    words = " ".join(
        str(args[key])
        for key in ("subject", "predicate", "object", "old_object", "new_object")
        if args.get(key) is not None
    )
    flags = privacy.scan(words)
    if any(flag.startswith("secret:") for flag in flags):
        return "secret"
    if "health" in flags:
        return strictest(floor, "private")
    return floor


def _wing_of(args: dict) -> Optional[str]:
    wing = args.get("wing")
    return wing if isinstance(wing, str) and wing else None


def _agent_wing(agent_name) -> str:
    if not isinstance(agent_name, str) or not agent_name.strip():
        return ""
    agent = agent_name.strip().lower()
    return f"wing_{normalize_wing_name(agent) or agent}"


def _only_literal_wing(policy: Policy) -> str:
    literal = [w for w in policy.read_wings if not any(c in w for c in "*?[")]
    return literal[0] if len(literal) == 1 and len(policy.read_wings) == 1 else ""


def _wings_text(policy: Policy) -> str:
    return ", ".join(policy.read_wings) or "no wings"


_WING_ARGS = ("wing", "source_wing", "target_wing", "wing_a", "wing_b")
_ID_ARGS = ("drawer_id", "source_drawer_id", "target_drawer_id")


def _check_argument_types(args: dict) -> None:
    """Wing and drawer arguments must already be what the policy checks.

    The worker coerces arguments (a JSON string becomes an array, a number a
    string), so a value the gateway can't read as-is must never reach it.
    ``drawer_ids`` given as a JSON string is parsed here, once, and the
    parsed list is what gets checked and forwarded.
    """
    for key in _WING_ARGS + _ID_ARGS:
        if key in args and args[key] is not None and not isinstance(args[key], str):
            raise Denied(f"'{key}' must be a string")
    for key in ("wings", "sensitivities"):
        value = args.get(key)
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(v, str) and v for v in value)
        ):
            raise Denied(f"'{key}' must be a list of names")
    if "drawer_ids" in args:
        ids = args["drawer_ids"]
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except (ValueError, RecursionError):
                ids = None
        if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
            raise Denied("'drawer_ids' must be a list of drawer id strings")
        args["drawer_ids"] = ids


def _drawer_ids(args: dict) -> Optional[list]:
    if "drawer_ids" in args:
        ids = args["drawer_ids"]
        if isinstance(ids, list) and all(isinstance(i, str) for i in ids) and 0 < len(ids) <= 500:
            return ids
        return None
    drawer_id = args.get("drawer_id")
    return [drawer_id] if isinstance(drawer_id, str) and drawer_id else None


def _target_ids(args: dict, result) -> list:
    ids = []
    for key in ("drawer_id", "tunnel_id", "hallway_id", "event_id", "artifact_id"):
        if isinstance(args.get(key), str):
            ids.append(args[key])
    if isinstance(args.get("drawer_ids"), list):
        ids.extend(i for i in args["drawer_ids"] if isinstance(i, str))
    if isinstance(result, dict):
        for key in ("drawer_id", "entry_id", "triple_id", "tunnel_id", "event_id", "artifact_id"):
            value = result.get(key)
            if isinstance(value, str) and value not in ids:
                ids.append(value)
    return ids


def _scrub(node, can_read: Callable[[str], bool]):
    """``node`` without anything from an unreadable wing.

    A list drops the items that carry one. Any other object that carries one,
    itself or below it, is dropped whole, so the caller fails closed.
    """
    if isinstance(node, dict):
        wing = node.get("wing")
        if isinstance(wing, str) and wing and not can_read(wing):
            return _DROP
        out = {}
        for key, value in node.items():
            cleaned = _scrub(value, can_read)
            if cleaned is _DROP:
                return _DROP
            out[key] = cleaned
        return out
    if isinstance(node, list):
        return [c for c in (_scrub(v, can_read) for v in node) if c is not _DROP]
    return node


def _unhide(response, unhide: dict):
    """Put the caller's real ids back where the worker echoed the stand-ins."""
    result = _tool_result(response) if isinstance(response, dict) else None
    if result is None:
        return response

    def swap(node):
        if isinstance(node, str):
            for fake, real in unhide.items():
                node = node.replace(fake, real)
            return node
        if isinstance(node, dict):
            return {k: swap(v) for k, v in node.items()}
        if isinstance(node, list):
            return [swap(v) for v in node]
        return node

    return _replace_result(response, swap(result))


def _filter_wing_keys(result, policy: Policy):
    """list_wings / get_taxonomy keyed by wing: keep only the readable wings."""
    if not isinstance(result, dict):
        return {"error": "not found", "denied": True}
    for key in ("wings", "taxonomy"):
        if isinstance(result.get(key), dict):
            return {key: {w: v for w, v in result[key].items() if policy.can_read_wing(w)}}
    return {"error": "not found", "denied": True}


def _filter_drawer_batch(result, policy: Policy):
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return _scrub(result, policy.can_read_wing)
    items = []
    errors = 0
    for item in result["results"]:
        cleaned = _scrub(item, policy.can_read_wing)
        if cleaned is _DROP or (isinstance(cleaned, dict) and cleaned.get("error")):
            drawer_id = item.get("drawer_id") if isinstance(item, dict) else None
            errors += 1
            items.append({"drawer_id": drawer_id, "error": f"Drawer not found: {drawer_id}"})
        else:
            items.append(cleaned)
    return {"results": items, "count": len(items), "errors": errors}


# ── HTTP ──
RouteHandler = Callable[[BaseHTTPRequestHandler], None]


def _host_name(authority: str) -> str:
    """Normalize a Host's name, validating (but ignoring) its optional port."""
    if not authority or any(ord(c) <= 32 or ord(c) >= 127 for c in authority):
        raise ValueError("invalid host")
    parts = urlparse("//" + authority)
    if (
        parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.params
        or parts.query
        or parts.fragment
        or authority.endswith(":")
        or "?" in authority
        or "#" in authority
        or "\\" in authority
        or "%" in authority
        or (parts.port is not None and not 0 < parts.port <= 65535)
    ):
        raise ValueError("invalid host")
    host = (parts.hostname or "").lower().removesuffix(".")
    if authority.startswith("["):
        if not re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?", authority):
            raise ValueError("invalid host")
        return str(ipaddress.IPv6Address(host))
    if len(host) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")
    ):
        raise ValueError("invalid host")
    return host


def _public_route(method: str, path: str) -> bool:
    if method == "GET":
        return path in ("/app", "/app/login", "/oauth/authorize") or path.startswith(
            ("/app/", "/s/", "/.well-known/")
        )
    return method == "POST" and (
        path.startswith("/app/auth/")
        or path in ("/oauth/register", "/oauth/token", "/oauth/revoke")
    )


def _write_request(method: str, path: str, request) -> bool:
    if path != "/mcp":
        return method in ("POST", "PATCH", "DELETE")
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return False
    params = request.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    if not isinstance(name, str):
        return False
    local = extensions.get(name)
    return local.kind == "write" if local else classify_tool(name) in ("write", "maintenance")


def _rest_tool(method, path):
    """REST aliases share MCP's tool visibility gate, before route execution."""
    parts = [unquote(part) for part in path.split("/") if part]
    if parts == ["api", "boards"]:
        return {"GET": "board_list", "POST": "board_add"}.get(method)
    if len(parts) == 4 and parts[:2] == ["api", "boards"] and parts[3] == "cards":
        return {"GET": "task_list", "POST": "task_add"}.get(method)
    if parts == ["api", "needs-me"] and method == "GET":
        return "needs_me"
    if parts[:2] == ["api", "cards"]:
        if len(parts) == 3:
            return {"GET": "task_show", "PATCH": "task_update"}.get(method)
        if len(parts) == 4 and method == "POST":
            return {
                "comments": "task_comment",
                "handoff": "task_handoff",
                "decide": "decide",
                "heartbeat": "task_heartbeat",
                "approve": "task_approve",
            }.get(parts[3])
    if parts[:2] == ["api", "memory"]:
        if len(parts) == 2 and method == "POST":
            return "memory_save"
        if len(parts) == 3 and method == "GET":
            return "memory_search" if parts[2] in ("search", "map") else "memory_get"
    if parts[:2] == ["api", "webhooks"]:
        if len(parts) == 2:
            return {"GET": "webhook_list", "POST": "webhook_add"}.get(method)
        if len(parts) == 3 and method == "DELETE":
            return "webhook_remove"
        if len(parts) == 4:
            if method == "GET" and parts[3] == "deliveries":
                return "webhook_list"
            if method == "POST" and parts[3] == "test":
                return "webhook_test"
    return None


def _origin(value):
    """A strict serialized HTTP origin, normalized for default ports."""
    try:
        if not value or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
            return None
        parsed = urlparse(value)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.path
            or parsed.params
            or "?" in value
            or "#" in value
        ):
            return None
        _host_name(parsed.netloc)
        host = parsed.hostname.lower()
        return parsed.scheme, host, parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def _session_write_allowed(gateway, handler):
    media = handler.headers.get_all("Content-Type", [])
    expected = (
        "application/octet-stream"
        if handler.command == "POST" and urlparse(handler.path).path == "/api/voice/transcribe"
        else "application/json"
    )
    if len(media) != 1 or media[0].split(";", 1)[0].strip().lower() != expected:
        return False
    return _same_origin_allowed(gateway, handler)


def _same_origin_allowed(gateway, handler):
    """Validate browser Origin without imposing a request content type."""
    origins = handler.headers.get_all("Origin", [])
    if not origins:
        return True
    if len(origins) != 1 or (origin := _origin(origins[0])) is None:
        return False
    app = urlparse(urls.APP_URL)
    allowed = {_origin(app.scheme + "://" + app.netloc)}
    hosts = handler.headers.get_all("Host", [])
    if len(hosts) == 1:
        scheme = "https" if gateway.secure_cookies else "http"
        allowed.add(_origin(scheme + "://" + hosts[0]))
    return origin in allowed


class Gateway:
    """The HTTP front. ``routes`` is the seam OAuth plugs its endpoints into."""

    def __init__(
        self,
        db: ControlDB,
        supervisor: WorkerSupervisor,
        authenticators: Optional[list] = None,
    ):
        self.db = db
        from commonplace.rules.checks import configure

        configure(self)
        self.supervisor = supervisor
        self.authenticators: list = authenticators or [PersonalTokenAuthenticator(db)]
        self.mediator = Mediator(db, WorkerClient(supervisor))
        self.mediator.gateway = self
        self.routes: dict = {}
        self.prefix_routes: list = []
        self.rate_limits = os.environ.get("COMMONPLACE_RATE_LIMITS", "").lower() != "off"
        self.limiter = ratelimit.TokenBuckets()
        self.storage_meter = plans.StorageMeter()
        allowed = os.environ.get("COMMONPLACE_ALLOWED_HOSTS")
        self.allowed_hosts = (
            {_host_name(host.strip()) for host in allowed.split(",")} if allowed else None
        )
        app_url = os.environ.get("COMMONPLACE_APP_URL") or os.environ.get(
            "COMMONPLACE_BASE_URL", ""
        )
        self.secure_cookies = app_url.startswith("https://")
        events.ensure_schema(db)
        sessions.ensure_schema(db)
        extensions.install(self)

    def add_route(self, method: str, path: str, handler: RouteHandler) -> None:
        self.routes[(method.upper(), path)] = handler

    def add_prefix_route(self, method: str, prefix: str, handler: RouteHandler) -> None:
        """Serve every path under ``prefix`` (longest prefix wins). The handler
        gets the request handler; ``urlparse(h.path)`` gives the rest."""
        self.prefix_routes.append((method.upper(), prefix, handler))
        self.prefix_routes.sort(key=lambda r: len(r[1]), reverse=True)

    def find_route(self, method: str, path: str):
        handler = self.routes.get((method, path))
        if handler is not None:
            return handler
        for m, prefix, h in self.prefix_routes:
            if m == method and (path == prefix.rstrip("/") or path.startswith(prefix)):
                return h
        return None

    def workspace_dir(self, workspace_id: str) -> str:
        path = self.supervisor.workspace_dir(workspace_id)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def principal_for(self, handler) -> Optional[Principal]:
        """A route's caller: a bearer token, else the web session cookie."""
        if hasattr(handler, "_commonplace_principal"):
            return handler._commonplace_principal
        header = handler.headers.get("Authorization")
        if header:
            return self.authenticate(header)
        session = sessions.cookie_value(handler.headers.get("Cookie"))
        if session:
            return sessions.session_principal(self.db, session)
        return None

    def call_memory_tool(self, principal: Principal, name: str, arguments: dict):
        """Run a MemPalace tool for ``principal`` through the full policy path."""
        request = {
            "jsonrpc": "2.0",
            "id": "commonplace-local",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        try:
            from commonplace.rules.checks import internal_call

            with internal_call():
                response = self.mediator._tools_call(principal, request)
        except WorkerUnavailable:
            raise extensions.ToolError("Memory is unavailable right now") from None
        if "error" in response:
            raise extensions.ToolError(response["error"].get("message", "refused"))
        result = _tool_result(response)
        if response.get("result", {}).get("isError") or (
            isinstance(result, dict) and result.get("error")
        ):
            message = result.get("error") if isinstance(result, dict) else "refused"
            raise extensions.ToolError(str(message))
        return result

    def authenticate(self, header: Optional[str]) -> Optional[Principal]:
        bearer = bearer_from_header(header)
        if not bearer:
            return None
        for authenticator in self.authenticators:
            principal = authenticator.authenticate(bearer)
            if principal is not None:
                return principal
        return None

    def build_server(self, host: str, port: int) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer((host, port), _GatewayHandler)
        server.gateway = self
        server.daemon_threads = True
        return server

    def serve(self, host: str, port: int, idle_check_s: float = 60.0) -> None:
        from commonplace.rules.checks import deploy

        deploy(self)
        server = self.build_server(host, port)
        stop = threading.Event()

        def reap():
            while not stop.wait(idle_check_s):
                self.supervisor.stop_idle()

        threading.Thread(target=reap, daemon=True).start()
        if threading.current_thread() is threading.main_thread():
            import signal

            def _terminate(signum, frame):
                # Unwind through serve_forever so the finally block stops every worker.
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, _terminate)
        logger.info("commonplace gateway on %s:%s", host, server.server_address[1])
        try:
            server.serve_forever()
        finally:
            stop.set()
            server.server_close()
            try:
                handoff = getattr(self, "handoff", None)
                if handoff is not None:
                    handoff.close()
            finally:
                self.supervisor.close()


class _GatewayHandler(BaseHTTPRequestHandler):
    server_version = "commonplace"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Targets contain login tokens and memory-search text. Do not
        # log targets, headers, or the stdlib's raw request line.
        return

    def send_response(self, code, message=None):
        self._response_started = True
        self._connection_header = False
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

    def send_header(self, keyword, value):
        if keyword.lower() == "connection":
            self._connection_header = True
        if keyword.lower() not in ("server", "x-content-type-options"):
            super().send_header(keyword, value)

    def end_headers(self):
        if self.close_connection and not getattr(self, "_connection_header", False):
            super().send_header("Connection", "close")
        super().send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self._send_json(code, {"error": "request refused"})

    def _send_json(self, status: int, payload, extra_headers=None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _body_length(self, path):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._send_json(411, {"error": "send a Content-Length, not Transfer-Encoding"})
            return None
        try:
            if len(lengths) > 1:
                raise ValueError
            text = lengths[0] if lengths else "0"
            if not text.isascii() or not text.isdecimal():
                raise ValueError
            length = int(text)
        except ValueError:
            self.close_connection = True
            self._send_json(400, {"error": "invalid request body"})
            return None
        cap = (
            MAX_REQUEST_BYTES
            if path == "/mcp"
            else HANDOFF_REQUEST_BYTES
            if path == "/api/handoff"
            else 1_920_000
            if path == "/api/voice/transcribe"
            else 65536
            if path.startswith("/oauth/")
            else 1024 * 1024
        )
        if length > cap:
            self.close_connection = True
            self._send_json(413, {"error": "request too large"})
            return None
        return length

    def _rate_limit(self, path, principal, request=None):
        if not self.server.gateway.rate_limits:
            return False
        forwarded = ",".join(self.headers.get_all("X-Forwarded-For", []))
        ip = ratelimit.client_ip(self.client_address[0], forwarded)
        policies = []
        if (
            path.startswith("/app/auth/")
            and path != "/app/auth/providers"
            or path == "/app/login"
            or self.command == "POST"
            and path in ("/oauth/token", "/oauth/register", "/oauth/authorize")
        ):
            policies.append((ratelimit.CREDENTIALS, ip))
        if principal is not None:
            if principal.person:
                session = sessions.cookie_value(self.headers.get("Cookie")) or ""
                identity = "session:" + hashlib.sha256(session.encode()).hexdigest()
            else:
                identity = "connection:" + principal.connection_id
            untrusted = principal.policy.trust == "untrusted"
            policies.append(
                (ratelimit.UNTRUSTED if untrusted else ratelimit.AUTHENTICATED, identity)
            )
            if untrusted and _write_request(self.command, path, request):
                policies.append((ratelimit.WRITES, identity))
        # Short links replace the anonymous policy with their own budget.
        if self.command == "GET" and path.startswith("/s/"):
            policies.append((ratelimit.SHORT_LINKS, ip))
        elif principal is None:
            policies.append((ratelimit.ANONYMOUS, ip))
        retry = self.server.gateway.limiter.check(policies)
        if not retry:
            return False
        self.close_connection = True
        self._send_json(429, {"error": "slow down"}, {"Retry-After": str(retry)})
        return True

    def _dispatch(self):
        self._response_started = False
        # The stdlib reuses this object for keepalive requests.
        self.__dict__.pop("_commonplace_principal", None)
        try:
            self._handle()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:
            log_failure(logger, "gateway request failed", exc)
            self.close_connection = True
            if not self._response_started:
                self._send_json(500, {"error": "internal error"})

    def _handle(self):
        path = urlparse(self.path).path
        if path in ("/health", "/healthz"):
            self.close_connection = True
            if self._body_length(path) is not None:
                self._send_json(200, {"ok": True})
            return
        if self.server.gateway.allowed_hosts is not None:
            try:
                hosts = self.headers.get_all("Host", [])
                valid = (
                    len(hosts) == 1 and _host_name(hosts[0]) in self.server.gateway.allowed_hosts
                )
            except ValueError:
                valid = False
            if not valid:
                self.close_connection = True
                self._send_json(421, {"error": "host refused"})
                return
        length = self._body_length(path)
        if length is None:
            return
        principal = self.server.gateway.principal_for(self)
        self._commonplace_principal = principal
        if (
            principal is not None
            and principal.person
            and self.command in ("POST", "PATCH", "DELETE")
            and (path.startswith("/api/") or path == "/app/logout")
            and not _session_write_allowed(self.server.gateway, self)
        ):
            self.close_connection = True
            self._send_json(403, {"error": "JSON and same-origin request required"})
            return
        handler = self.server.gateway.find_route(self.command, path)
        request, parse_error = None, None
        if path == "/mcp" and self.command == "POST" and principal is not None:
            media = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if media != "application/json":
                parse_error = (415, {"error": "POST /mcp needs application/json"})
            else:
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError
                    request = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    parse_error = (400, _rpc_error(None, -32700, "Parse error"))
        if self._rate_limit(path, principal, request):
            return
        tool = _rest_tool(self.command, path)
        if (
            principal is not None
            and not principal.person
            and tool
            and not principal.policy.tool_visible(tool)
        ):
            self.close_connection = True
            self._send_json(403, {"error": "tool not allowed for this connection"})
            return
        # OAuth POST has a browser-bound login-token stage. Its handler
        # checks sign-in before validating the authorization form.
        oauth_login = self.command == "POST" and path == "/oauth/authorize"
        if (
            principal is None
            and (handler is not None or path == "/mcp")
            and not _public_route(self.command, path)
            and not oauth_login
        ):
            self.close_connection = True
            challenge = (
                'Bearer resource_metadata="'
                + urls.AGENT_URL
                + '/.well-known/oauth-protected-resource"'
            )
            self._send_json(401, {"error": "unauthorized"}, {"WWW-Authenticate": challenge})
            return
        if principal is not None and not parse_error and plans.enabled():
            account = plans.workspace_account(self.server.gateway.db, principal.workspace_id)
            plan = plans.get_plan(self.server.gateway.db, account) if account else None
            if self._plan_limit(path, principal, request, plan):
                return
            if path == "/mcp" or path.startswith("/api/"):
                delay = plans.load_delay(plan)
                if delay:
                    sleep(delay)
        if path == "/mcp":
            if self.command != "POST":
                self.close_connection = True
                self._send_json(405, {"error": "use POST"}, {"Allow": "POST"})
            elif parse_error:
                self.close_connection = True
                self._send_json(*parse_error)
            else:
                self._mcp(principal, request)
            return
        if handler is None:
            self.close_connection = True
            self._send_json(404, {"error": "not found"})
            return
        # A route can reject a body before reading it. Close so its
        # remaining bytes cannot become another HTTP request.
        self.close_connection = True
        handler(self)

    def _plan_limit(self, path, principal, request, plan):
        if path != "/mcp" and not path.startswith("/api/"):
            return False
        if plan is None or plan.storage_bytes is None:
            return False
        if not _write_request(self.command, path, request) or self.command == "DELETE":
            return False
        if path == "/mcp":
            name = request["params"]["name"]
            if "delete" in name or "remove" in name:
                return False
        gateway = self.server.gateway
        if (
            gateway.storage_meter.used(
                principal.workspace_id, gateway.workspace_dir(principal.workspace_id)
            )
            < plan.storage_bytes
        ):
            return False
        gateway.storage_meter.audit_limit(gateway.db, principal)
        message = plans.storage_message(plan)
        self.close_connection = True
        if path == "/mcp":
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"content": [{"type": "text", "text": message}], "isError": True},
            }
            self._send_json(200, response)
        else:
            self._send_json(507, {"error": message, "limit": "storage"})
        return True

    def _mcp(self, principal, request):
        try:
            response = self.server.gateway.mediator.handle(principal, request)
        except Exception as exc:
            log_failure(logger, "gateway MCP request failed", exc)
            self.close_connection = True
            req_id = request.get("id") if isinstance(request, dict) else None
            self._send_json(500, _rpc_error(req_id, -32603, "Internal error"))
            return
        if response is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(200, response)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _dispatch
