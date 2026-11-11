"""Per-person chat bots: storage and the read/management API."""

from commonplace.bots import routes, updates


def install(gateway):
    if getattr(gateway, "bots_installed", False):
        return
    routes.install(gateway)
    updates.install(gateway)
    gateway.bots_installed = True
