"""Functional rules-injection tests keep their frozen deadline clock on a starved host.

``tests/_slow_host_plugin.py`` makes every read of the deadline clock burn the
whole injection budget. The functional tests freeze that clock themselves
(``frozen_deadline_clock`` in ``tests/test_commonplace_rules_inject.py``), so
the starved clock never reaches them: this test fails if one of them stops
freezing it. It does not exercise injection against a slow clock; the budget
has its own deterministic and `timing` tests.
"""

from tests._slow_host import run_under

FUNCTIONAL = [
    "tests/test_commonplace_rules_inject.py::test_authorized_text_scopes_and_owner_edits",
    "tests/test_commonplace_rules_inject.py::test_moved_rule_and_subagent_get_only_their_own_rules",
    "tests/test_commonplace_rules_inject.py::test_snapshot_served_state_and_no_binding_served_or_events",
    "tests/test_commonplace_rules_inject.py::test_lost_bound_store_has_only_header_and_pointer",
]


def test_functional_injection_tests_keep_their_frozen_clock_when_the_host_clock_is_starved():
    run_under(["tests._slow_host_plugin"], FUNCTIONAL)
