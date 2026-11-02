"""Tools and routes the gateway serves itself, next to the MemPalace tools.

Boards, the Memory aliases, decisions, "needs you" and webhooks live in their
own modules. Each one registers its MCP tools here at import time and adds
its HTTP routes in ``install(gateway)``. The gateway imports every module in
:data:`MODULES` once; a module that isn't there yet is skipped.

A local tool goes through the same gate as a MemPalace tool: it must be in
the token's tool list (a profile or an explicit list), the token must hold
its scope, and ``trusted_only`` tools are refused to untrusted tokens. A tool
the token can't use is missing from ``tools/list`` and answers ``Unknown
tool``, exactly like a hidden MemPalace tool.

Handlers get a :class:`ToolContext` and the arguments, and return a string
(sent as text) or anything JSON can hold. They raise :class:`ToolError` for
a refusal the agent should read (sent as an ``isError`` result starting with
``no:``).
"""

from __future__ import annotations

import importlib
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("commonplace.extensions")

# Imported in this order by the gateway. Missing modules are skipped.
MODULES = (
    "commonplace.boards",
    "commonplace.rules",
    "commonplace.memory_tools",
    "commonplace.session",
    "commonplace.webhooks",
    "commonplace.oauth",
    "commonplace.web",
    "commonplace.voice",
    "commonplace.social",
    "commonplace.email_auth",
    "commonplace.handoff.extension",
)

ALL_PROFILES = frozenset({"slim", "standard", "full"})
WORK_PROFILES = frozenset({"standard", "full"})


class ToolError(Exception):
    """A refusal or bad input; the message goes back to the agent."""


@dataclass(frozen=True)
class LocalTool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[["ToolContext", dict], Any]
    scope: str  # one of policy.SCOPES the token must hold
    kind: str = "read"  # "read" or "write"; writes are audited
    profiles: frozenset = ALL_PROFILES
    trusted_only: bool = False  # refused to untrusted tokens
    owner_only: bool = False  # only workspace owners
    # Optional live gate, e.g. "only while this workspace uses the feature".
    # False hides the tool exactly like a tool the token can't use.
    available: Optional[Callable[[Any, Any], bool]] = None  # (principal, gateway)

    def usable_by(self, principal, gateway=None) -> bool:
        if self.owner_only and principal.role != "owner":
            return False
        if self.available is None:
            return True
        try:
            return bool(self.available(principal, gateway))
        except Exception:
            return False

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass
class ToolContext:
    """What a handler may use. ``memory`` runs a MemPalace tool through the
    caller's own policy (same checks as a direct call) and returns its result."""

    principal: Any
    gateway: Any
    extra: dict = field(default_factory=dict)

    @property
    def db(self):
        return self.gateway.db

    @property
    def data_dir(self) -> str:
        return str(self.gateway.supervisor.root.parent)

    def workspace_dir(self) -> str:
        return self.gateway.workspace_dir(self.principal.workspace_id)

    def memory(self, name: str, arguments: dict):
        return self.gateway.call_memory_tool(self.principal, name, arguments)


REGISTRY: dict = {}
_lock = threading.Lock()
_loaded = False


def register(tool: LocalTool) -> LocalTool:
    if tool.kind not in ("read", "write"):
        raise ValueError(f"{tool.name}: kind must be read or write")
    with _lock:
        if tool.name in REGISTRY and REGISTRY[tool.name] is not tool:
            raise ValueError(f"local tool {tool.name} registered twice")
        REGISTRY[tool.name] = tool
    return tool


def load_modules() -> list:
    """Import every module in :data:`MODULES` once. Returns the loaded modules."""
    global _loaded
    loaded = []
    for name in MODULES:
        try:
            loaded.append(importlib.import_module(name))
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
    _loaded = True
    return loaded


def install(gateway) -> None:
    for module in load_modules():
        hook = getattr(module, "install", None)
        if hook is not None:
            hook(gateway)


def get(name: str) -> Optional[LocalTool]:
    if not _loaded:
        load_modules()
    return REGISTRY.get(name)


def names() -> frozenset:
    if not _loaded:
        load_modules()
    return frozenset(REGISTRY)
