"""The tools a bot may call, and what a tool may use while it runs.

A tool never acts with rights of its own: :class:`Turn` re-resolves the person for every call,
so whatever the person may not do over REST, the tool cannot do either. Free text a tool writes
must pass :meth:`Turn.words`, an exact-substring check against the person's stored message.
Kinds: ``read`` and ``routine`` run at once; ``confirm`` never runs from a tool call, the turn
stores a pending action and the person confirms it with a tap.
"""

import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

from commonplace import sessions
from commonplace.boards.service import BoardService
from commonplace.bots import store
from commonplace.extensions import ToolContext
from commonplace.rules import enforce

KINDS = ("read", "routine", "confirm")
SCOPES = ("any", "project")
NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")
MIN_SPAN = 2  # a shorter span is in almost any message, so it proves nothing


class NotYourWords(Exception):
    """A text argument is not a verbatim span of the person's current message."""


class Gone(Exception):
    """The person no longer has access to this workspace (or the project)."""


class Invalid(ValueError):
    """Arguments do not match the tool's schema. The message names the field, never a value."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict  # JSON Schema object, additionalProperties false
    kind: str  # "read" | "routine" | "confirm"
    run: Callable[["Turn", dict], "Result"]
    label: Callable[["Turn", dict], str]  # plain line for action lines and confirm chips
    scope: str = "any"  # "project": only for bots with a board (General: a `project` argument)
    available: Callable[["Turn"], bool] | None = None
    # Raises NotYourWords (or a refusal) before anything is stored or labelled; for a confirm
    # tool it runs before the pending action is created and again before `run` on /confirm.
    check: Callable[["Turn", dict], None] | None = None


@dataclass
class Result:
    ok: bool
    data: Any = None
    blocks: tuple = ()
    say: str = ""  # plain refusal sentence; on success, an optional plain outcome sentence


REGISTRY: dict[str, Tool] = {}
_lock = threading.Lock()


def register(tool: Tool) -> Tool:
    if NAME.fullmatch(tool.name) is None:
        raise ValueError("invalid tool name")
    if tool.kind not in KINDS or tool.scope not in SCOPES:
        raise ValueError(f"{tool.name}: invalid kind or scope")
    schema = tool.schema
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise ValueError(f"{tool.name}: schema must be a closed object")
    if tool.scope == "project" and "project" in schema.get("properties", {}):
        raise ValueError(f"{tool.name}: `project` is supplied by the turn")
    if tool.kind == "confirm" and tool.check is None and _free_text(schema):
        # Free text on a chip must be checked (your words) before the chip exists.
        raise ValueError(f"{tool.name}: a confirm tool with text arguments needs `check`")
    with _lock:
        if tool.name in REGISTRY and REGISTRY[tool.name] is not tool:
            raise ValueError(f"bot tool {tool.name} registered twice")
        REGISTRY[tool.name] = tool
    return tool


def _free_text(schema) -> bool:
    """True when the schema could accept a free string anywhere.

    Only an ``enum`` or ``const`` pins a value. A type list that names ``string``, any branch
    of ``anyOf`` / ``oneOf`` / ``allOf``, an untyped schema, and an open or typed
    ``additionalProperties`` of a nested object all count as free text.
    """
    if not isinstance(schema, dict):
        return schema is not False  # `true` (or anything unknown) accepts a string
    if "enum" in schema or "const" in schema:
        return False
    branches = [b for key in ("anyOf", "oneOf", "allOf") for b in schema.get(key) or ()]
    if any(_free_text(branch) for branch in branches):
        return True
    kind = schema.get("type")
    kinds = [kind] if isinstance(kind, str) else list(kind) if isinstance(kind, list) else []
    if "string" in kinds:
        return True
    if not kinds and not branches:
        return True  # no type at all: any value, a string included
    if any(_free_text(item) for item in (schema.get("properties") or {}).values()):
        return True
    extra = schema.get("additionalProperties", "object" in kinds)
    if "object" in kinds and extra is not False and _free_text(extra):
        return True
    return "array" in kinds and _free_text(schema.get("items", True))


class Turn:
    """What a tool may use: the person (fresh on every call), their boards and their message."""

    def __init__(
        self, gateway, workspace_id, user_id, bot, message, request_id, step=0, projects=None
    ):
        self.gateway = gateway
        self.db = gateway.db
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.bot = bot
        self.message = message
        self.request_id = request_id
        self.step = step
        self.projects = projects  # readable project slugs offered to a bot without a board

    def principal(self):
        principal = sessions.person_principal(self.db, self.user_id, self.workspace_id)
        if principal is None:
            raise Gone()
        return principal

    def boards(self) -> BoardService:
        return BoardService(ToolContext(principal=self.principal(), gateway=self.gateway))

    def readable(self) -> list[str]:
        return [b["slug"] for b in self.boards().board_list()]

    def board(self, args) -> str:
        """The bot's project; for a bot without one, the validated ``project`` argument."""
        board = self.bot.get("board") or args.get("project")
        if not isinstance(board, str) or board not in self.readable():
            raise Gone()
        return board

    def words(self, text) -> str:
        """``text`` trimmed, when it is a verbatim span (2+ characters) of the stored message."""
        if not isinstance(text, str):
            raise NotYourWords()
        span = text.strip()
        if len(span) < MIN_SPAN or span not in self.message["text"]:
            raise NotYourWords()
        return span

    def audit(self, tool, decision) -> None:
        """The audit row a REST write gets: ids and the decision, never text."""
        try:
            principal = self.principal()
        except Gone:
            return
        enforce.audit(self.db, principal, f"bot:{tool}", decision, "transport=bot", transport="bot")

    def store(self) -> store.BotStore:
        return store.open_bots(self.gateway, self.workspace_id)


# Arguments


def _check(schema, value, path) -> None:
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise Invalid(f"{path or 'arguments'} must be an object")
        properties = schema.get("properties", {})
        extra = set(value) - set(properties)
        if extra and schema.get("additionalProperties", True) is False:
            raise Invalid(f"{path or 'arguments'} has unknown fields")
        for name in schema.get("required", ()):
            if name not in value:
                raise Invalid(f"{(path + '.' if path else '') + name} is required")
        for name, item in value.items():
            if name in properties:
                _check(properties[name], item, (path + "." if path else "") + name)
        return
    if kind == "string":
        ok = isinstance(value, str)
        if ok and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 8000):
            raise Invalid(f"{path} has the wrong length")
    elif kind == "integer":
        ok = type(value) is int
        if ok and not schema.get("minimum", -(2**63)) <= value <= schema.get("maximum", 2**63 - 1):
            raise Invalid(f"{path} is out of range")
    elif kind == "boolean":
        ok = type(value) is bool
    elif kind == "array":
        ok = isinstance(value, list) and len(value) <= schema.get("maxItems", 50)
        if ok:
            for index, item in enumerate(value):
                _check(schema.get("items", {}), item, f"{path}[{index}]")
    else:
        raise Invalid(f"{path} has an unsupported type")
    if not ok:
        raise Invalid(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise Invalid(f"{path} is not one of the allowed values")


def validate(schema, args) -> dict:
    """``args`` when they match the schema (types, required, enum, length, no extra fields)."""
    _check(schema, args, "")
    return args


def schema_for(tool: Tool, turn: Turn) -> dict:
    """The schema the model sees: a bot without a board names the project for project tools."""
    if tool.scope != "project" or turn.bot.get("board"):
        return tool.schema
    properties = {
        "project": {
            "type": "string",
            "enum": list(turn.projects or ()),
            "description": "Which project",
        },
        **tool.schema.get("properties", {}),
    }
    return {
        **tool.schema,
        "properties": properties,
        "required": ["project", *tool.schema.get("required", ())],
    }


def offered(turn: Turn) -> list[Tool]:
    """The tools this turn may call, in registration order."""
    out = []
    for tool in list(REGISTRY.values()):
        if tool.scope == "project" and not turn.bot.get("board") and not turn.projects:
            continue
        if tool.available is not None:
            try:
                if not tool.available(turn):
                    continue
            except Exception:
                continue
        out.append(tool)
    return out


# Built-in tools: the bot's own memory


def _remember(turn, args):
    text = turn.words(args["text"])
    try:
        note = turn.store().memory_add(turn.bot["id"], turn.user_id, text)
    except store.Invalid:
        return Result(False, say="That note is too long to remember.")
    return Result(True, {"id": note["id"]})


def _forget(turn, args):
    if not turn.store().memory_remove(turn.bot["id"], turn.user_id, args["id"]):
        return Result(False, say="There is no such note.")
    return Result(True, {"id": args["id"]})


register(
    Tool(
        name="remember",
        description=(
            "Save a note to this bot's memory. `text` must be copied verbatim from the person's "
            "current message. Only when the person asks you to remember something."
        ),
        schema={
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 2000}},
            "required": ["text"],
            "additionalProperties": False,
        },
        kind="routine",
        run=_remember,
        label=lambda turn, args: "Remembered a note",
    )
)
register(
    Tool(
        name="forget",
        description="Remove one note from this bot's memory, by its id.",
        schema={
            "type": "object",
            "properties": {"id": {"type": "integer", "minimum": 1}},
            "required": ["id"],
            "additionalProperties": False,
        },
        kind="routine",
        run=_forget,
        label=lambda turn, args: "Forgot a note",
    )
)
