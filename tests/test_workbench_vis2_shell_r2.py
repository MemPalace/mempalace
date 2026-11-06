"""Visual recheck round 2: phone headings, Memory subtitle and filters, Hand off placement."""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "commonplace" / "web" / "static"
HARNESS = Path(__file__).with_name("workbench_vis2_shell_r2.mjs")
REDS = ["route-heading", "memory-subtitle", "memory-chips", "handoff"]


@pytest.mark.parametrize("case", REDS)
def test_red_vis2_shell_r2(case):
    assert (STATIC / "app.js").is_file(), "App shell missing"
    assert HARNESS.is_file(), "Harness missing"
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_node_available():
    assert shutil.which("node") is not None
