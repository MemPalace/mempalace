"""Chat fixes from the browser acceptance run, checked without a browser."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).with_name("workbench_g4_chat.mjs")
CASES = [
    "request rows sit under the orchestrator's answer and name it",
    "an unanswered request keeps its rows and names the person's message",
    "a local quick reply row names the quick reply, never the model id",
    "a hosted quick reply row says hosted provider",
    "message times read local HH:MM and keep the ISO stamp",
    "closing the chat finds an opener redrawn by id",
    "closing the chat finds an opener redrawn by its data attribute",
    "focus returns to the same row after the list is rebuilt",
    "a redraw just after the close gives focus back to the opener",
    "the dock shows the chat's status line under its heading",
    "the dock hands the board's name to the chat",
    "a focused or hovered chat row outlines its board card",
    "styles: outline, in-flow docked pane, no phone key hint",
]


@pytest.mark.parametrize("case", CASES)
def test_g4_chat(case):
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
