"""Automatic project orchestrator seats and model preferences."""

from commonplace.chat import settings
from commonplace.projects import orchestration, routes, seats


def install(gateway):
    if getattr(gateway, "projects_installed", False):
        return
    orchestration.ensure_schema(gateway.db)
    seats.ensure_schema(gateway.db)
    settings.ensure_schema(gateway.db)
    routes.install(gateway)
    seats.install_listener(gateway)
    gateway.projects_installed = True
