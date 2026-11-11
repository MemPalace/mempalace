"""Shared wait for background mirror and search-index jobs in tests."""

import time

import pytest


def drain_service(service, limit=120):
    """Wait until every mirror job and search-index sweep of `service` has finished.

    `HandoffService.wait_for_mirrors` returns silently once its timeout runs out,
    so a loaded host turned one fixed budget into half-finished work and failing
    assertions. Poll the real completion condition instead; `limit` only stops a
    genuinely stuck job, and the failure says so.
    """
    end = time.monotonic() + limit
    while time.monotonic() < end:
        service.wait_for_mirrors(1)
        with service._mirror_condition:
            if not service._index_active and all(f.done() for f in service._mirror_jobs):
                return
        time.sleep(0.01)
    pytest.fail(f"background mirror jobs did not drain within {limit} s")
