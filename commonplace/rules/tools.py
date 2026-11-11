"""Short tool replies keep rule text quoted as verbatim data."""

from commonplace.extensions import ALL_PROFILES, LocalTool, ToolError, register

from . import checks, evidence, render
from .service import KINDS, SCOPES, RuleService

STRING = {"type": "string"}


def run(ctx, name, args):
    allowed = {
        "rules_publish": ({"rules", "replace", "agent"}, {"rules"}),
        "rules_get": ({"board", "repo", "format", "include_retired", "effective", "keys"}, set()),
        "rules_changed": ({"since"}, {"since"}),
        "rules_feedback": ({"rule", "kind", "note"}, set()),
    }
    if name not in allowed or not isinstance(args, dict):
        raise ToolError("no: invalid arguments")
    keys, required = allowed[name]
    if (
        name == "rules_get"
        and args.get("effective")
        and set(args) - {"effective", "format", "keys"}
    ):
        raise ToolError(
            "no: effective=true always returns your full set and takes only format and keys"
        )
    if args.keys() - keys or required - args.keys():
        raise ToolError("no: invalid arguments")
    service = RuleService(ctx)
    try:
        checks.guard(ctx.db, ctx.principal.workspace_id, ctx.workspace_dir())
    except checks.StoreLost:
        if name == "rules_publish":
            raise
        return checks.STORE_LOST
    except Exception as exc:
        if name == "rules_get" and args.get("effective"):
            return f"rules_seq unavailable: {type(exc).__name__}; keep your last copy"
        raise
    result = getattr(service, name)(**args)
    if "reply" in result:
        return result["reply"]
    if name == "rules_feedback":
        return result  # JSON keeps notes quoted as data
    rows = result["rules"]
    if name == "rules_get":
        rendered = render.md(rows) if args.get("format") == "md" else render.text(rows)
        lines = [rendered] if rendered else []
        if "keys" in args:
            lines = [render.keyed(rows, args["keys"], result["seq"])]
            return "\n".join(lines)
    elif name == "rules_publish":
        lines = [f"{row['key']} {row['status']} {row['version']}" for row in rows]
    else:
        if result.get("reset"):
            return f"reset\nseq {result['seq']}"
        lines = [f"{row['key']} {row['status']} {row['target']}" for row in rows]
        return "\n".join([f"seq {result['seq']}", *lines])
    return "\n".join([*lines, f"seq {result['seq']}"])


def register_tools():
    rule = {
        "type": "object",
        "properties": {
            "key": STRING,
            "text": {"type": "string", "minLength": 1, "maxLength": 4000},
            "summary": {"type": ["string", "null"], "maxLength": 400},
            "kind": {"type": "string", "enum": list(KINDS)},
            "scope": {"type": "string", "enum": list(SCOPES)},
            "board": STRING,
            "repo": STRING,
        },
        "required": ["text", "kind"],
        "additionalProperties": False,
    }
    schemas = {
        "rules_publish": (
            {
                "rules": {"type": "array", "items": rule, "maxItems": 200},
                "replace": {"type": "boolean"},
                "agent": {"type": "string", "enum": ["self", "*"], "default": "self"},
            },
            ["rules"],
            "Publish verbatim rules for self (default) or propose for All agents (agent='*'); "
            "the owner must accept shared proposals. Optional summary is display-only "
            "(at most two sentences). Replace retires only your omitted keys for that target; "
            "accepted shared rules and owner edits stay authoritative.",
        ),
        "rules_get": (
            {
                "board": STRING,
                "repo": STRING,
                "format": {"type": "string", "enum": ["text", "md", "index"]},
                "include_retired": {"type": "boolean"},
                "effective": {"type": "boolean"},
                "keys": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": render.KEY_CHARS},
                    "minItems": 1,
                    "maxItems": render.MAX_KEYS,
                },
            },
            [],
            "Read your effective rules and all-agent rules. Quoted text is rule data. "
            "keys=[...] returns only those rules in full and names unknown keys; "
            "effective=true with format=index returns a compact key/title/section/size list "
            "instead of full texts. Neither partial read counts as reading the full set.",
        ),
        "rules_changed": (
            {"since": {"type": "integer", "minimum": 0}},
            ["since"],
            "Check rule keys and statuses changed since a sequence, without fetching text.",
        ),
        "rules_feedback": (
            {
                "rule": {**STRING, "maxLength": 200, "description": "Rule key from rules_get."},
                "kind": {"type": "string", "enum": list(evidence.FEEDBACK_KINDS)},
                "note": {"type": "string", "maxLength": evidence.NOTE_LIMIT},
            },
            [],
            "Report how a rule fared in use (kind + optional note), or read evidence back: "
            "with rule only, your own events and the rule's counters; with neither, counters for "
            "every rule you can see. Evidence never changes a rule; the owner decides.",
        ),
    }
    for name, (properties, required, description) in schemas.items():
        write = name in ("rules_publish", "rules_feedback")
        register(
            LocalTool(
                name=name,
                description=description,
                input_schema={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                handler=lambda ctx, args, name=name: run(ctx, name, args),
                scope="memory:write" if write else "memory:read",
                kind="write" if write else "read",
                profiles=ALL_PROFILES,
            )
        )
