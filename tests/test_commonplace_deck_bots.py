"""Deck bots: tiles, focused thread, draft hand-off and read-only project glance."""

import shutil
import subprocess
from pathlib import Path


def test_deck_bots():
    node = shutil.which("node")
    assert node, "Node is required for the Deck bots checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("commonplace_deck_bots.mjs"))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
