"""A failed drain in the worlds teardown is reported without leaking live threads."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_failed_drain_still_releases_servers_and_the_process_exits():
    env = dict(
        os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])
    )
    command = [
        sys.executable, "-m", "pytest", "tests/_card253_teardown_probe.py",
        "-p", "no:cacheprovider", "-p", "no:xdist", "-q", "--no-header",
    ]  # fmt: skip
    try:
        run = subprocess.run(
            command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired as expired:
        raise AssertionError(
            "process still alive after a failed teardown drain: a server thread leaked"
        ) from expired
    assert run.returncode != 0, run.stdout[-2000:]
    assert "did not drain" in run.stdout, run.stdout[-2000:]
    assert "2 passed, 2 errors" in run.stdout, run.stdout[-2000:]
