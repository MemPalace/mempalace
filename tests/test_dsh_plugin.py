"""Run the DeepSeek Harness plugin's own Node test suite (``.dsh-plugin/test``).

The plugin is JavaScript, so its behaviour is tested with ``node --test``. This
wrapper puts that suite on the same ``pytest`` path CI already runs, so the
plugin cannot silently rot next to the Python it drives.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent / ".dsh-plugin"
NODE = shutil.which("node")


def _run_suite(**env):
    suite = sorted(str(path) for path in (PLUGIN_DIR / "test").glob("*.test.mjs"))
    assert suite, "the DSH plugin ships a Node test suite"
    return subprocess.run(
        [NODE, "--test", "--test-reporter=tap", *suite],
        cwd=PLUGIN_DIR,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_dsh_plugin_node_suite():
    # The tests that assert a wall-clock budget are skipped here and run below
    # as a `timing` test, so a loaded host cannot fail the functional suite.
    result = _run_suite(DSH_SKIP_TIMING="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# skipped 2" in result.stdout, result.stdout


@pytest.mark.timing
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_dsh_plugin_node_suite_budgets():
    result = _run_suite()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# skipped 0" in result.stdout, result.stdout
