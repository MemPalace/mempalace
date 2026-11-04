"""Install the machine runner's routes once per gateway."""

from . import owner_api, registry, requester_api, runner_api


def install(gateway):
    if getattr(gateway, "runner_installed", False):
        return
    registry.ensure(gateway.db)
    owner_api.register(gateway)
    runner_api.register(gateway)
    requester_api.register(gateway)
    gateway.runner_installed = True
