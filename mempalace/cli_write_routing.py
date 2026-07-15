"""CLI adapter for the shared daemon write-routing policy.

This module keeps parser flags, configuration precedence, and route selection
out of the large CLI module. It applies only to routine CLI writes.

Maintenance operations such as repair, migration, index rebuild, closet
compression, and embedder-identity changes deliberately remain outside this
policy until an exclusive-maintenance protocol exists.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from .config import MempalaceConfig
from .write_routing import (
    ResolvedWriteRoutingPolicy,
    WriteRoutingDecision,
    WriteRoutingError,
    WriteRoutingPolicy,
    choose_write_route,
)


@dataclass(frozen=True)
class CliWriteRouting:
    """Resolved route for one routine CLI write."""

    decision: WriteRoutingDecision
    source: str
    explicit: bool

    @property
    def use_daemon(self) -> bool:
        return self.decision.use_daemon

    @property
    def use_direct(self) -> bool:
        return not self.decision.use_daemon and not self.decision.blocked


def add_cli_write_routing_flags(
    parser: argparse.ArgumentParser,
    *,
    allow_background: bool = True,
) -> None:
    """Add consistent routing flags to a routine write command."""

    routing = parser.add_mutually_exclusive_group()
    routing.add_argument(
        "--daemon",
        action="store_true",
        help=(
            "Force this operation through the local daemon, overriding "
            "the configured CLI write-routing policy"
        ),
    )
    routing.add_argument(
        "--direct",
        action="store_true",
        help=(
            "Force the legacy direct execution path, overriding "
            "the configured CLI write-routing policy"
        ),
    )

    if allow_background:
        parser.add_argument(
            "--background",
            action="store_true",
            help=(
                "Return a daemon job id immediately instead of waiting; "
                "requires a daemon-selected route"
            ),
        )


def resolve_cli_write_routing(
    args,
    *,
    operation: str,
) -> CliWriteRouting:
    """Resolve an explicit flag or the configured CLI policy.

    Interactive CLI commands are allowed to start the daemon. Therefore both
    ``prefer`` and ``require`` select the daemon when no explicit ``--direct``
    override is present.

    The daemon submission function is responsible for starting or reusing the
    daemon atomically. We intentionally do not perform a separate health probe,
    which would introduce a check-then-submit race.
    """

    force_daemon = bool(getattr(args, "daemon", False))
    force_direct = bool(getattr(args, "direct", False))
    background = bool(getattr(args, "background", False))

    if force_daemon and force_direct:
        raise WriteRoutingError(f"{operation}: --daemon and --direct are mutually exclusive")

    if force_daemon:
        resolved = ResolvedWriteRoutingPolicy(
            policy=WriteRoutingPolicy.REQUIRE,
            source="--daemon",
        )
        explicit = True
    elif force_direct:
        resolved = ResolvedWriteRoutingPolicy(
            policy=WriteRoutingPolicy.DIRECT,
            source="--direct",
        )
        explicit = True
    else:
        resolved = MempalaceConfig().resolve_write_routing("cli")
        explicit = False

    decision = choose_write_route(
        resolved.policy,
        daemon_available=False,
        daemon_can_start=True,
    )

    if background and not decision.use_daemon:
        raise WriteRoutingError(
            f"{operation}: --background requires --daemon under direct routing; "
            "--background requires a daemon route selected by --daemon "
            "or a prefer/require policy"
        )

    if decision.blocked:
        # This should be unreachable for interactive CLI commands because they
        # are allowed to auto-start the daemon. Keep the guard explicit so a
        # future policy change can never degrade to direct execution.
        raise WriteRoutingError(
            f"{operation}: the configured policy requires the daemon, "
            "but no daemon route is available"
        )

    return CliWriteRouting(
        decision=decision,
        source=resolved.source,
        explicit=explicit,
    )
