"""Onboarding acceptance gives the same answers when every gateway POST is slow.

Every POST to the gateway takes 0.6 s (``tests/_slow_hub_plugin.py``); these
tests reach the hub only through POST. That is longer than the startup hook's
production 0.4 s allowance, but the acceptance test runs the hook with that
allowance lifted to a hang guard (``_without_hub_budget``), so this covers hub
latency, not the 0.4 s budget, which ``tests/test_commonplace_client_hooks.py``
pins. The deadline clock is not starved here: these tests freeze it
(``frozen_deadline_clock``), so a starved clock could not reach them.
"""

from tests._slow_host import run_under

ACCEPTANCE = [
    "tests/test_commonplace_agent_defaults.py::"
    "test_default_cli_onboarding_installs_working_native_startup_and_warns_on_local_edits",
    "tests/test_commonplace_agent_defaults.py::"
    "test_onboarding_import_save_and_owner_approval_remain_one_canonical_flow",
]


def test_onboarding_acceptance_passes_when_every_gateway_post_is_slow():
    run_under(["tests._slow_hub_plugin"], ACCEPTANCE)
