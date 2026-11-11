"""Task types in the app: the card details field and the per-agent allowlist."""

import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "commonplace" / "web" / "static"
HARNESS = Path(__file__).with_name("task_types_ui.mjs")


@pytest.mark.parametrize("case", ["parse", "card", "save", "agents"])
def test_task_types_ui(case):
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_agents_settings_mounts_the_control():
    source = (STATIC / "agents.js").read_text()
    assert "agentTypesControl(c)" in source
    assert "cardTypeRows(card)" in (STATIC / "details.js").read_text()
