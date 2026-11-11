"""Install the machine runner's routes once per gateway."""

from . import chat_relay, chat_routes, ledger_api, owner_api, registry, requester_api, runner_api


def install(gateway):
    if getattr(gateway, "runner_installed", False):
        return
    registry.ensure(gateway.db)
    chat_relay.ensure_schema(gateway.db)
    owner_api.register(gateway)
    runner_api.register(gateway)
    ledger_api.register(gateway)
    requester_api.register(gateway)
    chat_routes.register(gateway)
    gateway.runner_installed = True
