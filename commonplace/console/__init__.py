"""Live console extension."""

from commonplace.console import routes, settings


def install(gateway):
    settings.ensure_schema(gateway.db)
    routes.install(gateway)
