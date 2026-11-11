"""Card panel Runs section: the static module renders, mounts and acts as the audit [372-A] says."""

import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "commonplace" / "web" / "static"
HARNESS = Path(__file__).with_name("cardruns.mjs")
CASES = [
    "words",
    "empty",
    "escape",
    "mount",
    "list",
    "hidden",
    "cancel",
    "refusal",
    "resolve",
    "stale",
    "panel",
]


@pytest.mark.parametrize("case", CASES)
def test_card_runs(case):
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_details_mount_is_one_small_insertion():
    source = (STATIC / "details.js").read_text()
    assert source.count("cardRunsSlot(card)") == 1
    assert source.count("attachCardRuns(") == 1
    # Logic stays in the module: the panel file does not know about jobs or states.
    assert "card-starts" not in source


def test_runs_styles_exist():
    css = (STATIC / "workbench.css").read_text()
    assert ".card-runs" in css and ".run-resolve" in css
