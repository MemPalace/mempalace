"""Workbench shell integration, with isolated executable Node cases."""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).with_name("workbench_shell_wiring.mjs")
REDS = ["shell", "navigation", "board", "details", "routes", "settings", "keys", "switch"]


@pytest.mark.parametrize("case", REDS)
def test_red_shell(case):
    assert HARNESS.is_file(), "Shell harness missing"
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("case", ["anchors", "legacy"])
def test_guard_shell(case):
    assert shutil.which("node") is not None
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
