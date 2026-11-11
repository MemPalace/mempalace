"""Pytest plugin: make mirror and search-index jobs behave as on a starved host.

Every background job starts late and `wait_for_mirrors` gives up at once. A test
that trusts one fixed wait budget sees unfinished jobs, exactly as it would when
the host is heavily loaded. Subprocess use only: the patches last until
`pytest_unconfigure` restores them.
"""

import time

from commonplace.handoff import palace_mirror, search_index
from commonplace.handoff.service import HandoffService

JOB_DELAY_SECONDS = 0.1
_originals = {}


def _delayed(function):
    def slow(*args, **kwargs):
        time.sleep(JOB_DELAY_SECONDS)
        return function(*args, **kwargs)

    return slow


def pytest_configure(config):
    _originals.update(
        sweep=search_index.sweep,
        mirror_pending=palace_mirror.mirror_pending,
        wait=HandoffService.wait_for_mirrors,
    )
    search_index.sweep = _delayed(search_index.sweep)
    palace_mirror.mirror_pending = _delayed(palace_mirror.mirror_pending)
    HandoffService.wait_for_mirrors = lambda self, timeout=None: None


def pytest_unconfigure(config):
    if _originals:
        search_index.sweep = _originals["sweep"]
        palace_mirror.mirror_pending = _originals["mirror_pending"]
        HandoffService.wait_for_mirrors = _originals["wait"]
        _originals.clear()
