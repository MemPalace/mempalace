"""Project panel UI contract, run without a browser."""

import re
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).with_name("projectpanel.mjs")
CASES = re.findall(r"^  (?:async )?(\w+)\(\) \{", HARNESS.read_text(), re.M)


@pytest.mark.parametrize("case", CASES)
def test_project_panel_ui(case):
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=15, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_cases_were_found():
    assert len(CASES) >= 10


def test_the_board_mounts_the_panel_with_one_line():
    app = (Path(__file__).resolve().parents[1] / "commonplace/web/static/app.js").read_text()
    assert app.count("projectpanel.js") == 1
