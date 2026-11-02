"""Per-token policy: scopes, tool profiles, wing scopes and trust levels.

Everything here fails closed. A tool the gateway serves must be classified in
``mempalace.tool_capabilities`` *and* have a rule in :data:`TOOL_RULES`; a tool
missing from either is hidden from ``tools/list`` and refused in
``tools/call``. The same :func:`tool_visible` decides both, so a client never
sees a tool it would be refused.

A wing argument passes only if every spelling the worker would match for it
(``mempalace.config.wing_aliases``) passes, each compared by its slug. A grant
for ``shared`` therefore covers ``wing_shared``, and ``private.notes`` is
private because the worker also reads ``private_notes`` for it. Workstation
wings, which the worker widens to the home diary wing, need every wing.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

from mempalace.config import WORKSTATION_WINGS, normalize_wing_name, wing_aliases
from mempalace.tool_capabilities import TOOL_CAPABILITIES

# ── Scopes ────────────────────────────────────────────────────────────────
SCOPES = (
    "memory:read",
    "memory:write",
    "memory:delete",
    "skills:read",
    "kg:read",
    "kg:write",
    "events:read",
    "events:write",
    "private:read",
    "private:write",
    "threads:read",
    "handoff:write",
    "admin",
)

TRUST_LEVELS = ("trusted", "standard", "untrusted")

# ── Tool rules ────────────────────────────────────────────────────────────
# How a tool touches wings, which decides what the gateway checks:
#   none         no wing data at all
#   all_wings    reads or changes data across wings; only for a token that
#                sees every wing, private ones included
#   wing_list    lists wings; the gateway drops the ones the token can't read
#   read_wing    reads one wing named by the ``wing`` argument
#   search       ``wing`` argument, or one search per readable wing
#   drawer_read  reads drawers by id
#   drawer_write changes or deletes drawers by id
#   add_drawer   files into the ``wing`` argument
#   diary_write  files into ``wing`` or the agent's own wing
#   diary_read   reads ``wing`` or the agent's own wing
#   tunnel_write links two wings
#   kg_read      reads facts; the gateway limits them to the readable wings
#                and sensitivities
#   kg_write     adds or changes facts in the ``wing`` argument
RULES = (
    "none",
    "all_wings",
    "wing_list",
    "read_wing",
    "search",
    "drawer_read",
    "drawer_write",
    "add_drawer",
    "diary_write",
    "diary_read",
    "tunnel_write",
    "kg_read",
    "kg_write",
)

TOOL_RULES = {
    "mempalace_status": ("memory:read", "all_wings"),
    "mempalace_list_wings": ("memory:read", "wing_list"),
    "mempalace_list_rooms": ("memory:read", "read_wing"),
    "mempalace_get_taxonomy": ("memory:read", "wing_list"),
    "mempalace_get_aaak_spec": ("memory:read", "none"),
    # Facts live in wings. A read returns only facts in readable wings and of
    # allowed sensitivities; facts filed before facts had wings, and secret
    # ones, only for a token that sees every wing. A write lands in a
    # writable wing. Stats count every wing.
    "mempalace_kg_query": ("kg:read", "kg_read"),
    "mempalace_kg_add": ("kg:write", "kg_write"),
    "mempalace_kg_invalidate": ("kg:write", "kg_write"),
    "mempalace_kg_supersede": ("kg:write", "kg_write"),
    "mempalace_kg_save": ("kg:write", "kg_write"),
    "mempalace_kg_timeline": ("kg:read", "kg_read"),
    "mempalace_kg_stats": ("kg:read", "all_wings"),
    "mempalace_kg_list_wings": ("kg:read", "all_wings"),
    "mempalace_traverse": ("memory:read", "all_wings"),
    "mempalace_find_tunnels": ("memory:read", "all_wings"),
    "mempalace_graph_stats": ("memory:read", "all_wings"),
    "mempalace_mesh_peers": ("memory:read", "all_wings"),
    "mempalace_create_tunnel": ("memory:write", "tunnel_write"),
    "mempalace_list_tunnels": ("memory:read", "all_wings"),
    "mempalace_delete_tunnel": ("memory:delete", "all_wings"),
    "mempalace_list_hallways": ("memory:read", "all_wings"),
    "mempalace_delete_hallway": ("memory:delete", "all_wings"),
    "mempalace_follow_tunnels": ("memory:read", "all_wings"),
    "mempalace_search": ("memory:read", "search"),
    "mempalace_check_duplicate": ("memory:read", "all_wings"),
    "mempalace_add_drawer": ("memory:write", "add_drawer"),
    "mempalace_checkpoint": ("memory:write", "all_wings"),
    "mempalace_delete_drawer": ("memory:delete", "drawer_write"),
    "mempalace_delete_drawers": ("memory:delete", "drawer_write"),
    "mempalace_delete_by_source": ("memory:delete", "all_wings"),
    "mempalace_get_drawer": ("memory:read", "drawer_read"),
    "mempalace_get_drawers": ("memory:read", "drawer_read"),
    "mempalace_list_drawers": ("memory:read", "read_wing"),
    "mempalace_update_drawer": ("memory:write", "drawer_write"),
    "mempalace_diary_write": ("memory:write", "diary_write"),
    "mempalace_diary_read": ("memory:read", "diary_read"),
    "mempalace_memories_filed_away": ("memory:write", "all_wings"),
    "mempalace_event_append": ("events:write", "all_wings"),
    "mempalace_task_create": ("events:write", "all_wings"),
    "mempalace_event_list": ("events:read", "all_wings"),
    "mempalace_event_wait": ("events:read", "all_wings"),
    "mempalace_event_ack": ("events:write", "all_wings"),
    "mempalace_artifact_put": ("events:write", "all_wings"),
    "mempalace_artifact_get": ("events:read", "all_wings"),
    "mempalace_patch_submit": ("events:write", "all_wings"),
}

# Never served through the gateway: mine and sync read paths on the server's
# disk, hook_settings writes the server's config, reconnect drops every handle.
GATEWAY_NEVER = frozenset(
    {"mempalace_mine", "mempalace_sync", "mempalace_hook_settings", "mempalace_reconnect"}
)

# ── Profiles ──────────────────────────────────────────────────────────────
_SLIM = frozenset(
    {
        "mempalace_search",
        "mempalace_get_drawer",
        "mempalace_add_drawer",
        "mempalace_diary_write",
        "mempalace_diary_read",
        "mempalace_kg_query",
    }
)
_STANDARD = _SLIM | frozenset(
    {
        "mempalace_list_wings",
        "mempalace_list_rooms",
        "mempalace_list_drawers",
        "mempalace_list_tunnels",
        "mempalace_list_hallways",
        "mempalace_get_drawers",
        "mempalace_get_taxonomy",
        "mempalace_check_duplicate",
        "mempalace_kg_add",
        "mempalace_kg_supersede",
        "mempalace_kg_save",
        "mempalace_kg_invalidate",
        "mempalace_kg_timeline",
        "mempalace_traverse",
        "mempalace_find_tunnels",
        "mempalace_follow_tunnels",
    }
)
_FULL = frozenset(TOOL_CAPABILITIES) - GATEWAY_NEVER
PROFILES = {"slim": _SLIM, "standard": _STANDARD, "full": _FULL}

# Scopes a profile grants when a token is made without explicit scopes.
DEFAULT_SCOPES = {
    # kg:read is not a default yet: existing tokens keep their tool lists.
    "slim": ("memory:read", "memory:write"),
    "standard": ("memory:read", "memory:write", "threads:read", "handoff:write"),
    "full": (
        "memory:read",
        "memory:write",
        "memory:delete",
        "kg:write",
        "events:read",
        "events:write",
        "threads:read",
        "handoff:write",
    ),
}

# Trust caps what scopes can do, whatever the token holds.
# Untrusted writes go to the inbox for review; the graph has no inbox yet.
# Untrusted tokens never read stored agent threads nor hand off.
_UNTRUSTED_BLOCKED = frozenset(
    {"memory:delete", "private:write", "kg:write", "threads:read", "handoff:write", "admin"}
)
_STANDARD_BLOCKED = frozenset({"memory:delete", "admin"})
# Tools that change what someone else wrote. Untrusted tokens only add.
_UPDATES = frozenset(
    {
        "mempalace_update_drawer",
        "mempalace_kg_invalidate",
        "mempalace_kg_supersede",
        "mempalace_kg_save",
    }
)


class PolicyError(ValueError):
    """A policy that can't be stored: unknown scope, profile, tool or trust."""


def canonical_wing(name: str) -> str:
    """The slug a wing name or pattern is compared by (patterns keep ``*?[]``)."""
    slug = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    if slug.startswith("wing_"):
        slug = slug[len("wing_") :]
    if any(c in slug for c in "*?["):
        return slug
    return normalize_wing_name(slug)


def wing_spellings(name: str) -> set:
    """Every slug the worker may match for ``name``: its own and its aliases'."""
    return {canonical_wing(n) for n in wing_aliases(str(name).strip())} | {canonical_wing(name)}


def _private_slug(slug: str) -> bool:
    return slug == "private" or slug.startswith(("private_", "private.", "private'"))


def is_private_wing(name: str) -> bool:
    return any(_private_slug(s) for s in wing_spellings(name))


def _workstation(name: str) -> bool:
    return bool(wing_spellings(name) & WORKSTATION_WINGS)


@dataclass(frozen=True)
class Policy:
    scopes: tuple = ()
    tools: object = "slim"  # a profile name, or a tuple of tool names
    read_wings: tuple = ()
    write_wings: tuple = ()
    write_wing: str = ""
    trust: str = "untrusted"
    extra: dict = field(default_factory=dict)

    # ── construction ──
    @classmethod
    def build(
        cls,
        *,
        profile: str = "slim",
        tools: Optional[Iterable[str]] = None,
        scopes: Optional[Iterable[str]] = None,
        read_wings: Iterable[str] = (),
        write_wings: Iterable[str] = (),
        write_wing: str = "",
        trust: str = "untrusted",
    ) -> "Policy":
        if tools is not None:
            tool_spec = tuple(sorted(set(tools)))
            from . import extensions

            local = extensions.names()
            unknown = [t for t in tool_spec if t not in TOOL_RULES and t not in local]
            if unknown:
                raise PolicyError(f"unknown or ungated tools: {', '.join(unknown)}")
        else:
            if profile not in PROFILES:
                raise PolicyError(f"unknown profile {profile!r}: use {', '.join(PROFILES)}")
            tool_spec = profile
        scope_list = tuple(
            sorted(set(scopes if scopes is not None else DEFAULT_SCOPES.get(profile, ())))
        )
        bad = [s for s in scope_list if s not in SCOPES]
        if bad:
            raise PolicyError(f"unknown scopes: {', '.join(bad)}")
        if trust not in TRUST_LEVELS:
            raise PolicyError(f"unknown trust {trust!r}: use {', '.join(TRUST_LEVELS)}")
        read = tuple(dict.fromkeys(w.strip() for w in read_wings if w.strip()))
        write = tuple(dict.fromkeys(w.strip() for w in write_wings if w.strip()))
        write_wing = write_wing.strip()
        if write_wing and "*" in write_wing:
            raise PolicyError("write_wing must be one wing, not a pattern")
        if trust == "untrusted" and "memory:write" in scope_list and not write_wing:
            raise PolicyError("an untrusted token that writes needs a write_wing (e.g. inbox)")
        if write_wing and trust == "untrusted" and is_private_wing(write_wing):
            raise PolicyError("an untrusted token can't write to a private wing")
        return cls(
            scopes=scope_list,
            tools=tool_spec,
            read_wings=read,
            write_wings=write,
            write_wing=write_wing,
            trust=trust,
        )

    def to_json(self) -> str:
        data = asdict(self)
        data.pop("extra", None)
        if isinstance(self.tools, tuple):
            data["tools"] = list(self.tools)
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Policy":
        """Load a stored policy. Anything malformed loads as a policy that allows nothing."""
        try:
            data = json.loads(raw)
            tools = data.get("tools", "slim")
            return cls.build(
                profile=tools if isinstance(tools, str) else "slim",
                tools=None if isinstance(tools, str) else tools,
                scopes=data.get("scopes", ()),
                read_wings=data.get("read_wings", ()),
                write_wings=data.get("write_wings", ()),
                write_wing=data.get("write_wing", ""),
                trust=data.get("trust", "untrusted"),
            )
        except Exception:
            return DENY_ALL

    # ── scopes and trust ──
    def has_scope(self, scope: str) -> bool:
        if scope not in self.scopes:
            return False
        if self.trust == "untrusted" and scope in _UNTRUSTED_BLOCKED:
            return False
        if self.trust == "standard" and scope in _STANDARD_BLOCKED:
            return False
        return True

    # ── wings ──
    def _matches(self, wing: str, patterns) -> bool:
        spellings = wing_spellings(wing)
        if "" in spellings:
            return False
        return all(
            any(fnmatch.fnmatchcase(slug, canonical_wing(p)) for p in patterns)
            for slug in spellings
        )

    def can_read_wing(self, wing: str) -> bool:
        if not isinstance(wing, str) or not wing.strip():
            return False
        if not self.has_scope("memory:read"):
            return False
        if is_private_wing(wing) and not self.has_scope("private:read"):
            return False
        if _workstation(wing) and not self.sees_all_wings:
            return False
        return self._matches(wing, self.read_wings)

    def can_write_wing(self, wing: str) -> bool:
        if not isinstance(wing, str) or not wing.strip():
            return False
        if not self.has_scope("memory:write"):
            return False
        if is_private_wing(wing) and not self.has_scope("private:write"):
            return False
        if _workstation(wing) and not self.writes_all_wings:
            return False
        if self.trust == "untrusted":
            return bool(self.write_wing) and self._matches(wing, (self.write_wing,))
        patterns = self.write_wings + ((self.write_wing,) if self.write_wing else ())
        return self._matches(wing, patterns)

    @property
    def sees_all_wings(self) -> bool:
        """Reads every wing, private ones included: the only token cross-wing tools serve."""
        return (
            "*" in self.read_wings
            and self.has_scope("memory:read")
            and self.has_scope("private:read")
        )

    def fact_sensitivities(self) -> list:
        """Sensitivities of the facts this token may read in its readable wings."""
        if self.sees_all_wings:
            return ["public", "internal", "private", "secret"]
        allowed = ["public", "internal"]
        if self.has_scope("private:read"):
            allowed.append("private")
        return allowed

    @property
    def writes_all_wings(self) -> bool:
        return (
            self.trust != "untrusted"
            and "*" in self.write_wings
            and self.has_scope("memory:write")
            and self.has_scope("private:write")
        )

    # ── tools ──
    def tool_names(self) -> frozenset:
        if isinstance(self.tools, str):
            from . import extensions

            extensions.names()
            local = frozenset(
                t.name for t in extensions.REGISTRY.values() if self.tools in t.profiles
            )
            return PROFILES.get(self.tools, frozenset()) | local
        return frozenset(self.tools)

    def tool_visible(self, name: str) -> bool:
        """Listed and callable. One answer for tools/list and tools/call."""
        return self.refusal(name) is None

    def refusal(self, name: str) -> Optional[str]:
        """Why ``name`` is refused before its arguments are looked at, or ``None``."""
        if name in GATEWAY_NEVER:
            return "this tool is not served through the gateway"
        from . import extensions

        local = extensions.get(name)
        if local is not None:
            return self.local_refusal(local)
        if name not in TOOL_CAPABILITIES or name not in TOOL_RULES:
            return "unknown tool"
        if name not in self.tool_names():
            return "tool not allowed for this connection"
        scope, rule = TOOL_RULES[name]
        if not self.has_scope(scope):
            return f"needs scope {scope}"
        if name in _UPDATES and self.trust == "untrusted":
            return "untrusted connections can't change existing memories"
        if rule == "all_wings":
            needs_write = TOOL_CAPABILITIES[name][0] != "read"
            if not self.sees_all_wings:
                return "needs read access to every wing"
            if needs_write and scope.startswith("memory:") and not self.writes_all_wings:
                return "needs write access to every wing"
        return None

    def local_refusal(self, tool) -> Optional[str]:
        """Why a gateway-served tool (``extensions.LocalTool``) is refused, or ``None``."""
        if tool.name not in self.tool_names():
            return "tool not allowed for this connection"
        if not self.has_scope(tool.scope):
            return f"needs scope {tool.scope}"
        if tool.trusted_only and self.trust == "untrusted":
            return "untrusted connections can't use this tool"
        return None

    def visible_tools(self) -> list:
        from . import extensions

        names = set(TOOL_RULES) | set(extensions.names())
        return sorted(n for n in names if self.tool_visible(n))

    def summary(self) -> dict:
        return {
            "scopes": list(self.scopes),
            "tools": self.tools if isinstance(self.tools, str) else list(self.tools),
            "read_wings": list(self.read_wings),
            "write_wings": list(self.write_wings),
            "write_wing": self.write_wing,
            "trust": self.trust,
        }


DENY_ALL = Policy(scopes=(), tools=(), read_wings=(), write_wings=(), trust="untrusted")


def check_registry() -> None:
    """Every classified tool has a rule, and every rule names a classified tool."""
    for name, (scope, rule) in TOOL_RULES.items():
        if scope not in SCOPES or rule not in RULES:
            raise RuntimeError(f"{name}: bad rule {(scope, rule)!r}")
    missing = set(TOOL_CAPABILITIES) - set(TOOL_RULES) - GATEWAY_NEVER
    extra = set(TOOL_RULES) - set(TOOL_CAPABILITIES)
    if missing or extra:
        raise RuntimeError(
            f"tool rules out of date: missing {sorted(missing)}, extra {sorted(extra)}"
        )
