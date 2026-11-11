"""Re-run a functional test selection in a child pytest on a modelled slow host.

The plugins (``tests/_slow_host_plugin.py``, ``tests/_slow_hub_plugin.py``)
make the host slower than a production budget on every run, so a functional
test that leans on that budget fails here every time instead of only under
real load. Each caller's docstring says which budget its selection actually
meets.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_under(plugins, targets):
    env = dict(
        os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])
    )
    loaded = [arg for plugin in plugins for arg in ("-p", plugin)]
    run = subprocess.run(
        [
            sys.executable, "-m", "pytest", *loaded,
            "-p", "no:cacheprovider", "-p", "no:xdist", "-q", "--no-header",
            "-o", "addopts=-m 'not benchmark and not slow and not stress and not timing'",
            *targets,
        ],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
    )  # fmt: skip
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-1000:]
    assert " passed" in run.stdout and "skipped" not in run.stdout
