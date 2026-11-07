"""Production refresh arbitration with deferred API reads in the Node harness."""

import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "case",
    [
        "foreground_only",
        "background_first",
        "foreground_first",
        "navigate_background_first",
        "navigate_foreground_first",
        "navigate_during_settings",
        "background_during_settings",
        "settled_background",
        "console_background_first",
    ],
)
def test_refresh_first_render(case):
    harness = Path(__file__).with_name("commonplace_refresh_render.mjs")
    result = subprocess.run(
        ["node", str(harness), case], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
