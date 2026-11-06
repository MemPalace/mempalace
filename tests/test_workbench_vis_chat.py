"""Chat visual fixes (header status line, quick reply, composer, no answer), without a browser."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).with_name("workbench_vis_chat.mjs")
CASES = [
    "a quick reply shows its kind and local time, never the model id",
    "card groups say what a row does and how many cards a request made",
    "the composer is the drawn box: hidden label, icon Send, Enter and Esc hints",
    "no answer: local times, the reassurance line, restart as the primary action, and the status line",
    "a connected orchestrator reads 'Orchestrator connected'",
    "styles: no header row in the docked chat, status line, composer box, stacked no-answer actions",
]


@pytest.mark.parametrize("case", CASES)
def test_vis_chat(case):
    assert HARNESS.is_file(), "chat harness missing"
    node = shutil.which("node")
    assert node, "Node is required for the chat checks"
    result = subprocess.run(
        [node, "--test", f"--test-name-pattern=^{re.escape(case)}$", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^ℹ pass 1$", result.stdout, re.M), result.stdout
