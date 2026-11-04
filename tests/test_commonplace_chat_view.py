"""A14: browser-free renderer and controller contracts."""

import shutil
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "commonplace/web/static"


def test_chat_view_and_controller():
    assert (STATIC / "chat.js").exists()  # RED
    node = shutil.which("node")
    assert node, "Node is required for the chat view checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("commonplace_chat_view.mjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
