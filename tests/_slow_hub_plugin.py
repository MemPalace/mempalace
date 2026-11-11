"""Pytest plugin: make every gateway POST slower than the startup hook allows.

Each POST to the gateway takes longer than the 0.4 s a native startup hook
gives the hub (``commonplace.client_hooks.HUB_TIMEOUT_S``), so a functional
test that leans on the hub being quick fails here every time, as it does on a
loaded host. Other verbs are not slowed.
"""

import time

DELAY_SECONDS = 0.6


def pytest_configure(config):
    from commonplace import gateway

    original = gateway._GatewayHandler.do_POST

    def slow(self):
        time.sleep(DELAY_SECONDS)
        return original(self)

    gateway._GatewayHandler.do_POST = slow
