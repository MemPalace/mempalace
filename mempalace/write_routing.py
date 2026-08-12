"""Shared daemon-routing policy for MemPalace write callers.

The policy is deliberately transport-agnostic. Hook and CLI consumers decide
whether a daemon is already available and whether they are allowed to start
one; this module turns those facts plus a policy into one explicit route.

This module changes no caller defaults by itself. Hook and CLI adoption are
separate follow-up PRs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class WriteRoutingError(ValueError):
    """Raised when a write-routing policy is invalid or cannot be applied."""


class WriteRoutingPolicy(str, Enum):
    """User-selected policy for routine write operations."""

    DIRECT = "direct"
    PREFER = "prefer"
    REQUIRE = "require"


class WriteRoutingTarget(str, Enum):
    """Concrete route selected for one write operation."""

    DIRECT = "direct"
    DAEMON = "daemon"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class RoutingPolicyCandidate:
    """One precedence-ordered source for a routing policy."""

    source: str
    value: Any
    legacy_boolean: bool = False


@dataclass(frozen=True)
class ResolvedWriteRoutingPolicy:
    """A normalized policy plus the source that selected it."""

    policy: WriteRoutingPolicy
    source: str


@dataclass(frozen=True)
class WriteRoutingDecision:
    """Concrete routing decision for a single operation."""

    policy: WriteRoutingPolicy
    target: WriteRoutingTarget
    auto_start_daemon: bool
    reason: str

    @property
    def use_daemon(self) -> bool:
        """Whether this operation should use the daemon."""
        return self.target is WriteRoutingTarget.DAEMON

    @property
    def blocked(self) -> bool:
        """Whether the operation must stop instead of writing directly."""
        return self.target is WriteRoutingTarget.BLOCKED


_LEGACY_TRUE = {"1", "true", "yes", "on", "daemon"}
_LEGACY_FALSE = {"0", "false", "no", "off"}


def parse_write_routing_policy(
    value: Any,
    *,
    legacy_boolean: bool = False,
) -> WriteRoutingPolicy:
    """Normalize one routing-policy value.

    New policy settings accept only ``direct``, ``prefer``, and ``require``.

    Legacy boolean settings additionally map truthy values to ``prefer`` and
    falsy values to ``direct`` so existing ``hooks.daemon`` configurations
    retain their historical behavior.
    """

    if isinstance(value, WriteRoutingPolicy):
        return value

    if legacy_boolean and isinstance(value, bool):
        return WriteRoutingPolicy.PREFER if value else WriteRoutingPolicy.DIRECT

    if legacy_boolean and isinstance(value, int) and not isinstance(value, bool):
        if value == 1:
            return WriteRoutingPolicy.PREFER
        if value == 0:
            return WriteRoutingPolicy.DIRECT

    if not isinstance(value, str):
        raise WriteRoutingError("write routing policy must be one of: direct, prefer, require")

    normalized = value.strip().lower()

    try:
        return WriteRoutingPolicy(normalized)
    except ValueError:
        pass

    if legacy_boolean:
        if normalized in _LEGACY_TRUE:
            return WriteRoutingPolicy.PREFER
        if normalized in _LEGACY_FALSE:
            return WriteRoutingPolicy.DIRECT

    raise WriteRoutingError(
        f"invalid write routing policy {value!r}; expected direct, prefer, or require"
    )


def resolve_write_routing_policy(
    candidates: Iterable[RoutingPolicyCandidate],
    *,
    default: WriteRoutingPolicy = WriteRoutingPolicy.DIRECT,
) -> ResolvedWriteRoutingPolicy:
    """Return the first configured policy from ordered policy sources."""

    for candidate in candidates:
        if candidate.value is None:
            continue

        try:
            policy = parse_write_routing_policy(
                candidate.value,
                legacy_boolean=candidate.legacy_boolean,
            )
        except WriteRoutingError as exc:
            raise WriteRoutingError(f"{candidate.source}: {exc}") from exc

        return ResolvedWriteRoutingPolicy(
            policy=policy,
            source=candidate.source,
        )

    return ResolvedWriteRoutingPolicy(
        policy=default,
        source="default",
    )


def choose_write_route(
    policy: WriteRoutingPolicy,
    *,
    daemon_available: bool,
    daemon_can_start: bool,
) -> WriteRoutingDecision:
    """Choose direct, daemon, or blocked for one routine write.

    ``daemon_can_start`` is normally false for latency-sensitive hooks and
    true for interactive CLI commands.

    The key safety guarantee is that ``require`` never degrades to a direct
    write when the daemon is unavailable.
    """

    policy = parse_write_routing_policy(policy)

    if policy is WriteRoutingPolicy.DIRECT:
        return WriteRoutingDecision(
            policy=policy,
            target=WriteRoutingTarget.DIRECT,
            auto_start_daemon=False,
            reason="policy-direct",
        )

    if daemon_available:
        return WriteRoutingDecision(
            policy=policy,
            target=WriteRoutingTarget.DAEMON,
            auto_start_daemon=False,
            reason="daemon-available",
        )

    if daemon_can_start:
        return WriteRoutingDecision(
            policy=policy,
            target=WriteRoutingTarget.DAEMON,
            auto_start_daemon=True,
            reason="daemon-auto-start",
        )

    if policy is WriteRoutingPolicy.PREFER:
        return WriteRoutingDecision(
            policy=policy,
            target=WriteRoutingTarget.DIRECT,
            auto_start_daemon=False,
            reason="daemon-unavailable-fallback",
        )

    return WriteRoutingDecision(
        policy=policy,
        target=WriteRoutingTarget.BLOCKED,
        auto_start_daemon=False,
        reason="daemon-required-unavailable",
    )
