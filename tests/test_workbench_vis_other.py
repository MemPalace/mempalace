"""Visual fixes: shortcuts sheet, palette rows, memory filters and times, project settings header."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().with_name("workbench_vis_other.mjs")
CASES = [
    "shortcuts_pinned",
    "palette_rows_flat",
    "memory_time",
    "memory_chips",
    "settings_header",
    "saved_status",
]


@pytest.mark.parametrize("case", CASES)
def test_visual_fixes(case):
    assert SCRIPT.is_file()
    node = shutil.which("node")
    assert node, "Node is required for the visual fix checks"
    result = subprocess.run(
        [node, str(SCRIPT), case], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr  # RED
