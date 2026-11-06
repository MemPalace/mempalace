"""Chat fix-round contracts, run browser-free through the existing node harnesses."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).parent
VIEW = [
    "ask_keeps_draft",
    "unchanged_poll_keeps_focus",
    "reopen_moves_divider",
    "sending_is_pending",
    "destroy_removes_listeners",
    "slow_endpoints_every_15s",
    "open_reads_after_seen",
    "open_pages_full",
    "closed_keeps_divider",
    "reopen_before_first_page",
    "reopen_after_failed_first_page",
    "reopen_catches_up",
]
CASES = [
    *(("commonplace_chat_view.mjs", case) for case in VIEW),
    ("workbench_chatstate.mjs", "receipt_newest"),
    ("workbench_chatstate.mjs", "peek"),
    ("workbench_chat_wiring.mjs", "seen_pages"),
    ("workbench_chat_wiring.mjs", "chat_destroy"),
    ("workbench_chatdock.mjs", "reopen_baseline"),
    ("workbench_chatdock.mjs", "open_promise"),
    ("workbench_chatdock.mjs", "open_state"),
]


@pytest.mark.parametrize(("harness", "case"), CASES, ids=[case for _, case in CASES])
def test_red_chat_fix(harness, case):
    script = TESTS / harness
    assert script.is_file(), f"{harness} missing"
    node = shutil.which("node")
    assert node, "Node is required for the chat checks"
    if harness == "commonplace_chat_view.mjs":
        command = [node, "--test", f"--test-name-pattern=^RED fix {case}$", str(script)]
    else:
        command = [node, str(script), case]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr  # RED
    if harness == "commonplace_chat_view.mjs":
        assert re.search(r"\bpass 1\b", result.stdout), result.stdout
