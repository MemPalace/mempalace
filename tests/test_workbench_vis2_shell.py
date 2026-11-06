"""Card history in Details and hidden Done cards, with isolated Node cases."""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "commonplace" / "web" / "static"
HARNESS = Path(__file__).with_name("workbench_vis2_shell.mjs")
REDS = ["history", "open-ask", "done-selected", "done-reveal", "show-more-focus"]


@pytest.mark.parametrize("case", REDS)
def test_red_vis2_shell(case):
    assert (STATIC / "details.js").is_file(), "Details module missing"
    assert HARNESS.is_file(), "Harness missing"
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_node_available():
    assert shutil.which("node") is not None
