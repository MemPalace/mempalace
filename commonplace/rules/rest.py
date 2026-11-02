"""Person-only rule review routes; agent publication goes through MCP."""

from commonplace.boards._http import HTTPError, path_parts, read_json, require_principal, send_json
from commonplace.extensions import ToolContext, ToolError

from . import checks
from .service import RuleService, VersionClash


def install(gateway):
    def route(h):
        principal = require_principal(gateway, h)
        if principal is None:
            return
        try:
            if not principal.person or (h.command != "GET" and principal.role != "owner"):
                raise HTTPError(403, "people only" if h.command == "GET" else "owner only")
            service = RuleService(ToolContext(principal=principal, gateway=gateway))
            status, result = dispatch(service, h, path_parts(h, "/api/"), h.command)
        except checks.StoreLost:
            send_json(h, 409, {"error": checks.LOST_ERROR, "store_lost": True})
            return
        except VersionClash as exc:
            send_json(h, 409, {"error": "rule changed", "current": exc.current})
            return
        except HTTPError as exc:
            send_json(h, exc.status, {"error": str(exc)})
            return
        except ToolError as exc:
            message = str(exc).removeprefix("no: ")
            send_json(
                h,
                404 if message in ("rule not found", "suggestion not found") else 400,
                {"error": message},
            )
            return
        except Exception:
            send_json(h, 500, {"error": "internal error"})
            return
        send_json(h, status, result)

    for method in ("GET", "POST", "PATCH"):
        gateway.add_prefix_route(method, "/api/rules/", route)


def dispatch(service, h, parts, method):
    def body(allowed, required=()):
        value = read_json(h)
        if value.keys() - set(allowed):
            raise HTTPError(400, "unknown arguments")
        for key in required:
            if key not in value:
                raise HTTPError(400, f"{key} is required")
        return value

    if parts == ["rules", "checks"] and method == "PATCH":
        return 200, service.set_check(**body(("agent", "mode"), ("agent", "mode")))
    if parts == ["rules", "store", "accept"] and method == "POST":
        body(())
        return 200, service.accept_store()
    checks.guard(service.ctx.db, service.principal.workspace_id, service.ctx.workspace_dir())
    if parts == ["rules", "count"] and method == "GET":
        return 200, service.count()
    if parts == ["rules"]:
        if method == "GET":
            return 200, service.list_rules()
        if method == "POST":
            return 201, service.create(
                **body(
                    ("agent", "text", "summary", "kind", "scope", "scope_ref"),
                    ("agent", "text", "kind"),
                )
            )
    if len(parts) == 3 and parts[:2] == ["rules", "suggestions"] and method == "POST":
        values = body(("action", "version"), ("action",))
        if values["action"] == "accept" and "version" not in values:
            raise HTTPError(400, "version is required")
        return 200, service.decide_suggestion(parts[2], **values)
    if len(parts) in (2, 3) and parts[0] == "rules":
        rule_id = parts[1]
        if len(parts) == 3 and parts[2] == "history" and method == "GET":
            return 200, service.history(rule_id)
        if len(parts) == 2 and method == "PATCH":
            return 200, service.edit(
                rule_id,
                **body(
                    ("version", "text", "summary", "kind", "scope", "scope_ref", "note"),
                    ("version",),
                ),
            )
        if len(parts) == 3 and method == "POST":
            if parts[2] == "action":
                return 200, service.action(
                    rule_id, **body(("version", "action"), ("version", "action"))
                )
            if parts[2] == "agent":
                return 200, service.set_agent(
                    rule_id,
                    **body(("version", "agent", "note", "accept"), ("version", "agent")),
                )
            if parts[2] == "suggestions":
                return 201, service.suggest(
                    rule_id, **body(("text", "from_agent", "base_version"), ("text", "from_agent"))
                )
    raise HTTPError(404, "not found")
