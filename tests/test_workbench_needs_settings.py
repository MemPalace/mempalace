"""The shell's project-settings wiring (Needs me Back/close/save and the restart panel)."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("needs_settings_refresh.mjs")
CASES = ["panel_close", "needs_settings", "shell_sync", "restart"]


@pytest.mark.parametrize("case", CASES)
def test_settings_refresh_wiring(case):
    node = shutil.which("node")
    assert node, "Node is required for the shell wiring checks"
    result = subprocess.run(
        [node, str(SCRIPT), case], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
