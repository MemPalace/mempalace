"""The opt-in Deck look: switch, bar, tabs, screen registry and shell wiring, run in node."""

import shutil
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "commonplace/web/static"


def test_deck_switch_bar_and_wiring():
    for name in ("deck.js", "deck.css", "fonts/inter-latin.woff2", "fonts/Inter-OFL.txt"):
        assert (STATIC / name).exists(), name
    node = shutil.which("node")
    assert node, "Node is required for the Deck checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("commonplace_deck.mjs"))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
