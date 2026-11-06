"""Board, Details and Needs me markup from the visual check, with isolated Node cases."""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "commonplace" / "web" / "static"
HARNESS = Path(__file__).with_name("workbench_vis_shell.mjs")
REDS = ["empty-board", "columns", "composer", "activity", "needs-meta"]


@pytest.mark.parametrize("case", REDS)
def test_red_vis_shell(case):
    assert (STATIC / "board.js").is_file(), "Board module missing"
    assert HARNESS.is_file(), "Harness missing"
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_node_available():
    assert shutil.which("node") is not None
