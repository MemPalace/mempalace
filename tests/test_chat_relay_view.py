"""B10: the runner's private answer and status label."""

import shutil
import subprocess


def test_runner_answer_label_and_status():
    result = (
        subprocess.run(["node", "--test", "tests/chat_relay_view.mjs"], capture_output=True)
        if shutil.which("node")
        else None
    )
    assert result is not None and result.returncode == 0  # RED
