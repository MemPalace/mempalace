"""Chat review fixes: unsent sends, offline delivery and device seen ids, without a browser."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).with_name("workbench_fix5_chat.mjs")
CASES = [
    "a second offline send keeps the first message and the new draft",
    "discarding the unsent message frees the composer",
    "an offline send outlives the pane and posts once to its board",
    "a new pane on another board does not send the queued message twice",
    "an ambiguous delivery after the pane closed is kept for an explicit retry",
    "signing out drops a queued offline send",
    "a new tab after an earlier read counts the new agent message",
    "true first sight on a device still baselines to the newest ids",
    "device seen ids stay per user and board, and sign-out clears only that user",
    "a pane reopened during a background send shows it sending and cannot resend or discard it",
    "signing out during a background send keeps nothing and reads nothing more",
    "a session that expires during a background send keeps nothing and reads nothing more",
    "signing out with no board service (Settings) clears this user's seen ids only",
    "a session expiring off-board clears this user's seen ids before forgetting who it was",
    "the restart settings close button goes back to the chat",
    "board single keys wait while a project dialog is open",
    "with storage denied a new pane shows the queued message, holds the next one and sends it once",
    "with storage denied a new pane during an in-flight send cannot send it again",
]


@pytest.mark.parametrize("case", CASES)
def test_fix5_chat(case):
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
