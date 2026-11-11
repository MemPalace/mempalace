"""Probe run by test_card253_s12_teardown.py: a world whose job never settles."""

import tests.test_card253_s12 as card
from tests._mirror_drain import drain_service
from tests.test_card253_s12 import worlds  # noqa: F401

# Fail the teardown drain in half a second instead of the production limit.
card.drain_service = lambda service: drain_service(service, limit=0.5)


def test_stuck_job_fails_the_drain_but_not_the_cleanup(worlds):  # noqa: F811
    world = worlds("stuck")
    world.service._index_active.add("stuck")
