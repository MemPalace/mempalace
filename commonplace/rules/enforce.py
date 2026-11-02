"""Connection-level freshness checks for client writes, over MCP and REST."""

import copy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from commonplace import events

from . import checks
from .store import rules_sequence

CHECKED_WRITES = frozenset(
    "task_add task_update task_comment task_take_next task_handoff ask space_post "
    "board_add memory_save memory_link fact_save handoff_start decide board_set rules_publish "
    "task_approve".split()
)

# Explicit names make a newly exposed write fail the public-surface contract.
EXEMPT_WRITES = {
    "task_heartbeat": "Mechanical claim renewal does not change the work or its instructions.",
    **dict.fromkeys(
        "webhook_add webhook_remove webhook_test".split(),
        "Webhook configuration and outbound automation remain open in this version.",
    ),
    **dict.fromkeys(
        "mempalace_add_drawer mempalace_update_drawer mempalace_diary_write "
        "mempalace_kg_add mempalace_kg_invalidate mempalace_kg_supersede mempalace_kg_save "
        "mempalace_create_tunnel mempalace_checkpoint mempalace_memories_filed_away".split(),
        "Hooks, miners and automatic memory/graph saves cannot supply a rules version.",
    ),
    **dict.fromkeys(
        "mempalace_delete_drawer mempalace_delete_drawers mempalace_delete_by_source "
        "mempalace_delete_tunnel mempalace_delete_hallway".split(),
        "Deletes retain their existing scope, trust and wing checks.",
    ),
    **dict.fromkeys(
        "mempalace_event_append mempalace_task_create mempalace_patch_submit "
        "mempalace_artifact_put mempalace_event_ack".split(),
        "Legacy event writes are machine traffic between workers.",
    ),
}


@dataclass(frozen=True)
class RestRoute:
    module: str
    method: str
    path: str
    tool: str
    kind: str = "checked"
    reason: str = ""


# Inventory of dispatch shapes, rather than registered prefixes. The callstack
# exception is a body restriction on task_update, not another route branch.
REST_ROUTES = (
    RestRoute("boards.rest", "PATCH", "/api/cards/<n>", "task_update"),
    RestRoute("boards.rest", "POST", "/api/cards/<n>/comments", "task_comment"),
    RestRoute("boards.rest", "POST", "/api/cards/<n>/handoff", "task_handoff"),
    RestRoute("boards.rest", "POST", "/api/cards/<n>/decide", "decide"),
    RestRoute("boards.rest", "POST", "/api/cards/<n>/approve", "task_approve"),
    RestRoute(
        "boards.rest",
        "POST",
        "/api/cards/<n>/heartbeat",
        "task_heartbeat",
        "exempt",
        "Claim renewal",
    ),
    RestRoute("boards.rest", "POST", "/api/boards/<slug>/cards", "task_add"),
    RestRoute("boards.rest", "POST", "/api/boards", "board_add"),
    RestRoute("boards.rest", "PATCH", "/api/boards/<slug>", "board_set"),
    RestRoute("boards.rest", "POST", "/api/boards/<slug>/chat", "space_post"),
    RestRoute("memory_tools", "POST", "/api/memory", "memory_save"),
    RestRoute("memory_tools", "POST", "/api/facts", "fact_save"),
    RestRoute("handoff.api", "POST", "/api/handoffs/<id>/relaunch", "relaunch"),
    RestRoute("handoff.api", "POST", "/api/handoff", "handoff_start", "exempt", "Automatic upload"),
    RestRoute(
        "handoff.api", "POST", "/api/handoffs/<id>/successor", "", "exempt", "Automatic lifecycle"
    ),
    RestRoute(
        "handoff.api",
        "POST",
        "/api/handoffs/<id>/launch-failed",
        "",
        "exempt",
        "Automatic lifecycle",
    ),
    RestRoute("handoff.api", "POST", "/api/threads/<id>/recall", "", "read", "Recall is a read"),
    RestRoute("handoff.api", "PUT", "/api/settings/handoff", "", "exempt", "Configuration"),
    RestRoute(
        "handoff.api", "DELETE", "/api/settings/handoff", "", "exempt", "Owner configuration"
    ),
    RestRoute(
        "webhooks", "POST", "/api/webhooks", "webhook_add", "exempt", "Outbound configuration"
    ),
    RestRoute(
        "webhooks",
        "DELETE",
        "/api/webhooks/<id>",
        "webhook_remove",
        "exempt",
        "Outbound configuration",
    ),
    RestRoute(
        "webhooks",
        "POST",
        "/api/webhooks/<id>/test",
        "webhook_test",
        "exempt",
        "Outbound automation",
    ),
)

RECONNECT = " If this tool's parameters do not list rules_seq, reconnect first."
NO_VALUE = " If it gives no rules_seq, call ask without one."
MESSAGES = {
    "missing": "no: this write needs rules_seq. Call rules_get format=md effective=true and pass the rules_seq it gives."
    + NO_VALUE
    + RECONNECT,
    "stale": "no: rules changed since your rules_seq. Call rules_get format=md effective=true, follow the rules, then retry with the rules_seq it gives."
    + NO_VALUE
    + RECONNECT,
    "not served": "no: this connection has not read the current rules. Call rules_get format=md effective=true, then retry with the rules_seq it gives. If it gives none, retry without it."
    + RECONNECT,
    "too_large": "no: your rule set is too large for one reply, so this write cannot be checked. Use ask to tell the owner to trim it; ask still works.",
    "unenforceable": "Commonplace: this connection cannot read rules, so its writes are not checked. Ask the owner to allow rules_get."
    + RECONNECT,
}
TOO_LARGE_NOTICE = "Commonplace: your rule set is too large for one reply, so your writes are not checked. Use ask to tell the owner to trim it."


@dataclass(frozen=True)
class Check:
    reason: str = ""
    notice: str = ""
    denied: bool = False


def schema(tool, mode):
    """Never mutate a registry or a worker response shared by other callers."""
    result = copy.deepcopy(tool)
    if mode not in ("warn", "enforce") or tool["name"] not in CHECKED_WRITES:
        return result
    value = result["inputSchema"]
    value.setdefault("properties", {})["rules_seq"] = {
        "oneOf": [{"type": "integer", "minimum": 0}, {"type": "string", "pattern": "^[0-9]+$"}],
        "description": "Version from rules_get format=md effective=true; read and follow the rules before writing.",
    }
    if mode == "enforce" and tool["name"] != "ask":
        value.setdefault("required", []).append("rules_seq")
    return result


def _number(value):
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        # No unbounded int conversion, including adversarial leading zeros.
        digits = value.lstrip("0") or "0"
        if len(digits) <= 9:
            return int(digits)
        return -1  # A valid decimal string, but never a current version.
    return None


def _result(reason, mode, *, ask=False):
    warn = mode != "enforce"
    audit_reason = "rules_seq " + reason + (" (warn)" if warn else "")
    if reason == "unchecked":
        return Check(audit_reason)
    if reason == "too_large" and ask and not warn:
        return Check(audit_reason)
    text = MESSAGES[reason]
    if warn:
        text = TOO_LARGE_NOTICE if reason == "too_large" else text.replace("no:", "Commonplace:", 1)
    return Check(audit_reason, text, not warn)


def check(ctx, tool, supplied):
    """The only bypass is server-owned context, never a request argument."""
    p = ctx.principal
    if p.person or checks.is_internal_call():
        return Check()
    mode = checks.mode(ctx.gateway, p)
    state = "unknown"
    try:
        directory = Path(ctx.workspace_dir())
        state = checks.guard(ctx.db, p.workspace_id, directory)
    except Exception:
        pass
    if mode == "off":
        return Check()
    if not p.policy.tool_visible("rules_get"):
        return _result("unenforceable", "warn")
    if mode == "unknown" or state == "unknown":
        return _result("unchecked", mode)
    if state == "empty":
        return Check()
    try:
        path = directory / "rules.sqlite3"
        # An old, unmigrated store is unknown; the check must not repair it and
        # silently decide that its rules are a complete current set.
        with checks.connection(path) as conn:
            conn.execute("SELECT agent FROM agent_seq LIMIT 1")
            conn.execute("SELECT agent FROM agent_served LIMIT 1")
        checks.reconcile(ctx.db, p.workspace_id, directory)
        with checks.connection(path) as conn:
            conn.execute("BEGIN")
            current = rules_sequence(conn, p.actor)
            row = conn.execute("SELECT * FROM agent_served WHERE agent=?", (p.actor,)).fetchone()
    except Exception:
        return _result("unchecked", mode)
    if row is not None and row["state"] == "too_large" and row["rules_seq"] == current:
        return _result("too_large", mode, ask=tool == "ask")
    sent = _number(supplied)
    if sent is None:
        return _result("missing", mode)
    if sent != current:
        return _result("stale", mode)
    if row is None or row["state"] != "served" or row["rules_seq"] != current:
        return _result("not served", mode)
    return Check()


def audit(db, principal, tool, decision, reason="", *, transport="mcp", **kwargs):
    """Stable reason prefixes and authenticated ids support incident counting."""
    if reason.startswith("rules_seq ") or transport == "rest":
        reason += ("; " if reason else "") + "transport=" + transport
    with db._lock:
        db.audit(
            action="tool.call",
            decision=decision,
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
            connection_id=principal.connection_id,
            auth=principal.auth,
            tool=tool,
            reason=reason,
            **kwargs,
        )
        if not reason.startswith("rules_seq unchecked"):
            return
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        count = db._conn.execute(
            "SELECT count(*) FROM audit_log WHERE workspace_id=? AND connection_id=? "
            "AND at>=? AND action='tool.call' AND reason LIKE 'rules_seq unchecked%'",
            (principal.workspace_id, principal.connection_id, since),
        ).fetchone()[0]
        alerted = db._conn.execute(
            "SELECT 1 FROM events WHERE workspace_id=? AND kind='rule.unchecked' "
            "AND subject=? AND at>=? LIMIT 1",
            (principal.workspace_id, principal.connection_id, since),
        ).fetchone()
        if count > 5 and not alerted:
            event = events.record(
                db,
                principal.workspace_id,
                "rule.unchecked",
                subject=principal.connection_id,
                actor=principal.actor,
                data={"connection_id": principal.connection_id, "count": count},
            )
        else:
            event = None
    if event is not None:
        events.notify(event)


class RulesRefresh(Exception):
    def __init__(self, text):
        self.payload = {"error": text, "rules_refresh": True}
        super().__init__(text)


@contextmanager
def rest_write(ctx, handler, tool, body):
    body.pop("rules_seq", None)
    exempt = tool in EXEMPT_WRITES or (
        tool == "task_update" and body.keys() == {"version", "callstack"}
    )
    outcome = Check() if exempt else check(ctx, tool, handler.headers.get("Commonplace-Rules-Seq"))
    if outcome.denied:
        audit(ctx.db, ctx.principal, tool, "deny", outcome.reason, transport="rest")
        raise RulesRefresh(outcome.notice)
    if outcome.notice:
        handler._rules_notice = outcome.notice
    completed = False
    try:
        yield
    except Exception:
        audit(ctx.db, ctx.principal, tool, "deny", outcome.reason, transport="rest")
        raise
    else:
        audit(ctx.db, ctx.principal, tool, "ok", outcome.reason, transport="rest")
        completed = True
    finally:
        if not completed:
            handler._rules_notice = ""
