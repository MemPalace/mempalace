"""Workspace chat front agent extension."""

import os
import time

from commonplace.chat import front, handoff, routes, settings, store, worker


def install(gateway):
    if getattr(gateway, "chat_installed", False):
        return
    db = gateway.db
    settings.ensure_schema(db)
    store.ensure_schema(db)
    handoff.ensure_schema(db)
    now = time.time()
    store.recover(db, now)
    handoff.recover(db, gateway, now)
    routes.install(gateway)
    front.install_listener(gateway)
    gateway.chat_worker = worker.FrontWorker(
        gateway, start=os.environ.get("COMMONPLACE_CHAT_WORKER", "on") != "off"
    )
    db.add_pre_close_callback(gateway.chat_worker.stop)
    gateway.chat_installed = True
