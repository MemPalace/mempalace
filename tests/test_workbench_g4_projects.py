"""Project dialogs: connect, setup phases, drawn disconnect confirm, restart heading."""

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "workbench_g4_projects.mjs"
CASES = [
    "connect_dialog",
    "connect_continues",
    "setup_trap",
    "done_focus",
    "failed_focus",
    "disconnect_drawn",
    "restart_heading",
    "on_back",
    "setup_clipboard",
    "disconnect_scope",
    "models_submit_before_loads",
    "models_submit_after_reads_settle",
]


@pytest.mark.parametrize("case", CASES)
def test_project_dialogs(case):
    assert SCRIPT.is_file()
    node = shutil.which("node")
    assert node, "Node is required for project dialog checks"
    result = subprocess.run(
        [node, str(SCRIPT), case], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr  # RED
