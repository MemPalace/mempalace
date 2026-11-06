"""Board component contract, run without a browser."""

import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "commonplace/web/static"


@pytest.mark.parametrize(
    "case",
    ["ids", "blocked", "chat", "columns", "backlog", "assignee", "filters", "empty", "escaping"],
)
def test_board_contract(case):
    assert (STATIC / "board.js").is_file(), "Board module missing"
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("workbench_board.mjs")), case],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_g01_existing_api_helpers():
    source = (STATIC / "api.js").read_text()
    assert "export const escape" in source
    assert "export const actor" in source


def test_g02_harness_exists():
    assert Path(__file__).with_name("workbench_board.mjs").is_file()
