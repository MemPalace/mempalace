"""Card panel Reviewer row: the static module renders the owner's choice and reports it read-only to others."""

import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).with_name("card_reviewer.mjs")
CASES = ["default_words", "owner_choice", "read_only", "change", "panel", "tag"]


@pytest.mark.parametrize("case", CASES)
def test_card_reviewer_ui(case):
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr
