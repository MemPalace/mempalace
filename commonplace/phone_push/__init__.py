"""Opt-in phone notification extension."""

from commonplace import webhooks

from . import config, routes, schema, sender
from .dispatcher import PushDispatcher


def install(gateway):
    routes.register(gateway)
    schema.ensure(gateway.db)
    if config.load() is None:
        stop(gateway)
        return
    if getattr(gateway, "phone_push", None) is None:
        dispatcher = PushDispatcher(
            gateway.db,
            gateway=gateway,
            schedule=webhooks.BACKOFF,
            max_attempts=webhooks.MAX_ATTEMPTS,
            sweep_seconds=300,
        )
        sender.recover(gateway.db, dispatcher.max_attempts)
        gateway.phone_push = dispatcher
        try:
            dispatcher.start()
        except Exception:
            gateway.phone_push = None
            dispatcher.stop()
            raise


def stop(gateway):
    dispatcher = getattr(gateway, "phone_push", None)
    if dispatcher is not None:
        dispatcher.stop()
    gateway.phone_push = None
