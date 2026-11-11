"""Mirror and search-index tests give the same answers on a host too slow for a fixed wait."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELECTION = [
    "tests/test_search_index_sweep.py::test_u3_a_coalescing_and_enrolment",
    "tests/test_search_index_sweep.py::test_u3_b_job_bounds_and_draining",
    "tests/test_search_index_sweep.py::test_u3_c_crash_retry_idempotence",
    "tests/test_search_index_sweep.py::test_u3_d_quota_defers_between_batches",
    "tests/test_search_index_sweep.py::test_u3_f_principals_and_import_groups",
    "tests/test_card253_s12.py::test_card253_s12_progress",
    "tests/test_card253_s12.py::test_card253_s12_all_readable_and_reauthorize_units",
    "tests/test_card253_s12.py::test_card253_s12_readable_running_and_precedence",
    "tests/test_card253_s12.py::test_card253_s12_global_quota_causality",
    "tests/test_commonplace_import_reader.py::test_g04_fail_closed",
    "tests/test_commonplace_import_reader.py::test_r07_range_gate",
    "tests/test_handoff_adversarial_idempotency.py::test_append_failure_rolls_back_then_retry_mirrors_new_tail",
    "tests/test_handoff_adversarial_security.py::test_normal_memory_tools_cannot_bypass_thread_read_scope",
    "tests/test_imported_history_search.py::test_imported_search_r01_acl_import_http_search",
    "tests/test_imported_history_search.py::test_imported_search_r05_acl_import_mcp_memory_search",
    "tests/test_imported_history_search.py::test_imported_search_r07_acl_import_startup_injection",
]


def test_mirror_tests_pass_when_waits_give_up_and_jobs_start_late():
    env = dict(
        os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])
    )
    command = [
        sys.executable, "-m", "pytest", "-p", "tests._slow_sweep_plugin",
        "-p", "no:cacheprovider", "-p", "no:xdist", "-q", "--no-header", "--maxfail=1",
        "-o", "addopts=-m 'not benchmark and not slow and not stress and not timing'",
        *SELECTION,
    ]  # fmt: skip
    try:
        run = subprocess.run(
            command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=600
        )
    except subprocess.TimeoutExpired as expired:
        output = (expired.stdout or b"") + (expired.stderr or b"")
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        raise AssertionError("slow-host selection timed out:\n" + output[-3000:]) from expired
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-1000:]
    assert " passed" in run.stdout and "skipped" not in run.stdout
