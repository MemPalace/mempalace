"""Opt-in per-wing access control for ``mempalace serve`` (see docs/acl.md).

The caller's scope is a ``ContextVar`` set around each HTTP request. A thread that
never set one is ADMIN while ACL is off and DENIED once on: losing it only denies.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    subject: str
    source: str  # "legacy" (the single bearer token) or "grants"


@dataclass(frozen=True)
class Scope:
    # admin and internal bypass every check

    kind: str  # admin | locked | internal | denied
    subject: str = ""
    read: frozenset = frozenset()  # normalized wing names
    write: frozenset = frozenset()

    @property
    def bypass(self) -> bool:
        return self.kind in ("admin", "internal")


ADMIN, INTERNAL, DENIED = Scope("admin"), Scope("internal"), Scope("denied")
GENERIC_ERROR = {"success": False, "error": "Request failed"}

_on = [False]  # process-wide: set once by the HTTP server when a grants file is loaded
_scope_var: contextvars.ContextVar = contextvars.ContextVar("mempalace_acl_scope", default=None)


def enable(on: bool = True) -> None:
    _on[0] = on


def disable() -> None:
    enable(False)


def current_scope() -> Scope:
    return _scope_var.get() or (DENIED if _on[0] else ADMIN)


@contextlib.contextmanager
def request_scope(scope):
    """Run a request as ``scope``. ``None`` (ACL off) changes nothing."""
    if scope is not None and scope.kind == "internal":
        raise ValueError("INTERNAL scope is for server-started code only")
    token = _scope_var.set(scope) if scope else None
    try:
        yield
    finally:
        if token:
            _scope_var.reset(token)


def run_internal(fn):
    """Run a server-started thread target as INTERNAL (no request path may call this)."""

    def runner(*args, **kwargs):
        token = _scope_var.set(INTERNAL)
        try:
            return fn(*args, **kwargs)
        finally:
            _scope_var.reset(token)

    return runner


def denies_tool(tool: str) -> bool:
    """Whether the current caller must see ``tool`` as nonexistent."""
    scope = current_scope()
    if scope.bypass:
        return False
    from ..tool_capabilities import ACL_PENDING, ACL_POLICY

    kind = ACL_POLICY.get(tool, ("deny", None))[0]
    return scope.kind != "locked" or kind == "deny" or tool in ACL_PENDING


def scoped_args(scope: Scope, tool: str, args: dict):
    """``args`` for a locked caller, or ``None`` when its wing is not granted.

    Checked before any database access, alike for a forbidden and a nonexistent
    wing. Drops ``source_file`` and forces ``added_by`` / ``agent_name`` to the key.
    """
    from ..config import normalize_wing_name
    from ..tool_capabilities import ACL_POLICY, classify_tool

    args = {k: v for k, v in args.items() if k != "source_file"}
    if tool == "mempalace_add_drawer":
        args["added_by"] = scope.subject
    elif tool == "mempalace_diary_write":
        args["agent_name"] = scope.subject
    wing_arg = ACL_POLICY.get(tool, ("deny", None))[1]
    if wing_arg is None:
        return args
    raw = args.get(wing_arg)
    wing = normalize_wing_name(raw) if isinstance(raw, str) else ""
    allowed = scope.read if classify_tool(tool) == "read" else scope.write
    return {**args, wing_arg: wing} if wing in allowed and wing else None


def scrub_result(result):
    """One generic error in place of any failed handler result (text may name paths or wings)."""
    failed = isinstance(result, dict) and ("error" in result or result.get("success") is False)
    return dict(GENERIC_ERROR) if failed else result


def input_problem(tool: str, args: dict):
    """The validation message for a granted caller's own bad input, or ``None``.

    The text echoes only what the caller sent; every other handler error stays
    generic. Runs after the wing check, so a forbidden wing never reaches it.
    """
    from ..config import sanitize_content, sanitize_name

    fields = {"mempalace_add_drawer": ("room", "content")}.get(
        tool, ("agent_name", "topic", "entry") if tool == "mempalace_diary_write" else ()
    )
    try:
        for field in fields:
            value = args.get(field, args.get("content") if field == "entry" else None)
            if field in ("content", "entry") and value is not None:
                sanitize_content(value)
            elif value is not None:
                sanitize_name(value, field)
    except ValueError as exc:
        return str(exc)
    return None
