"""Deck Today: real screen controller and markup with a fake API and minimal DOM."""

import shutil
import subprocess
from pathlib import Path


def test_deck_today():
    node = shutil.which("node")
    assert node, "Node is required for the Deck Today checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("commonplace_deck_today.mjs"))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-10000:] + result.stderr[-2000:]
