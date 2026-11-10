"""Rules injection tests give the same answers on a host too slow for the budget."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FUNCTIONAL = [
    "tests/test_commonplace_rules_inject.py::test_authorized_text_scopes_and_owner_edits",
    "tests/test_commonplace_rules_inject.py::test_moved_rule_and_subagent_get_only_their_own_rules",
    "tests/test_commonplace_rules_inject.py::test_snapshot_served_state_and_no_binding_served_or_events",
    "tests/test_commonplace_rules_inject.py::test_lost_bound_store_has_only_header_and_pointer",
]


def test_functional_injection_tests_pass_when_every_clock_read_is_slow():
    env = dict(
        os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])
    )
    run = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "tests._slow_host_plugin",
            "-p", "no:cacheprovider", "-p", "no:xdist", "-q", "--no-header",
            "-o", "addopts=-m 'not benchmark and not slow and not stress and not timing'",
            *FUNCTIONAL,
        ],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-1000:]
    assert " passed" in run.stdout and "skipped" not in run.stdout
