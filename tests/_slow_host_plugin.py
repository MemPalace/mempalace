"""Pytest plugin: make the rules deadline clock behave like a starved host.

Every read of the clock the rules deadline uses advances it by more than the
whole injection budget, so an unprotected functional test sees an expired
deadline exactly as it would under heavy CPU or IO load.
"""

from commonplace.rules import checks

STEP_SECONDS = 0.05


def pytest_configure(config):
    now = [0.0]

    def starved():
        now[0] += STEP_SECONDS
        return now[0]

    checks.monotonic = starved
