"""Orchestrator setup as code: owner-approved config versions per board, a durable
lease and journal for the orchestrator, config-driven runner starts and their triggers.

Modules: ``schema`` (TOML format), ``store`` (versions, accept, restore flag),
``durable`` (lease, journal, gates, checkpoint), ``runner_bridge`` and ``triggers``
(fenced starts, trigger sweep), ``plan_check``/``docs_check``/``cli`` (checks and the
``commonplace orch`` command), ``tools`` (MCP) and ``rest`` (``/api/orch/``).

As a gateway extension (``commonplace.extensions.MODULES``) importing this package
registers the MCP tools; ``install(gateway)`` adds the REST routes. Both stay cheap at
import time: the stores load on first use.
"""

from .tools import register_tools


def install(gateway) -> None:
    from . import rest

    rest.install(gateway)


__all__ = ["install"]

register_tools()
