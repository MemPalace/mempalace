"""Shell chat integration, exercised without a running browser or service."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).with_name("workbench_chat_wiring.mjs")
REDS = [
    "board_header",
    "needs_header",
    "memory_header",
    "styles",
    "mount",
    "reuse",
    "needs_project",
    "memory_project",
    "deep_route",
    "baseline",
    "arrivals",
    "deduplicate",
    "private_replies",
    "visibility",
    "cadence",
    "events",
    "closed_pane",
    "inflight_close",
    "visible_read",
    "details",
    "signout",
    "dispose",
    "board_switch",
]


@pytest.mark.parametrize("case", REDS)
def test_red_chat_wiring(case):
    assert HARNESS.is_file(), "Chat wiring harness missing"  # RED
    result = subprocess.run(
        ["node", str(HARNESS), case], cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr  # RED


@pytest.mark.parametrize("case", ["anchors", "board_guards"])
def test_guard_chat_wiring(case):
    result = subprocess.run(
        ["node", str(HARNESS), case], cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
