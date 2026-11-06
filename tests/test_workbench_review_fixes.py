"""Workbench review fixes: keyboard board, sheet modality, rail, chat times, routes, focus."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
STATIC = TESTS.parent / "commonplace" / "web" / "static"
CASES = [
    ("workbench_board.mjs", "roving"),
    ("workbench_shell_wiring.mjs", "switch-enter"),
    ("workbench_shell_wiring.mjs", "inert"),
    ("workbench_rail.mjs", "nostyle"),
    ("workbench_shell_wiring.mjs", "rail-once"),
    ("workbench_board.mjs", "chat-time"),
    ("workbench_shell_wiring.mjs", "bare-chat"),
    ("workbench_shell_wiring.mjs", "chat-key"),
    ("workbench_rail.mjs", "rules"),
    ("workbench_shell_wiring.mjs", "rules-count"),
    ("workbench_details.mjs", "refocus"),
    ("workbench_shell_wiring.mjs", "panel-focus"),
    ("workbench_board.mjs", "drag"),
    ("workbench_shell_wiring.mjs", "panel-enter"),
    ("workbench_shell_wiring.mjs", "chat-time-label"),
]


@pytest.mark.parametrize(("harness", "case"), CASES, ids=[case for _, case in CASES])
def test_review_fix(harness, case):
    assert (TESTS / harness).is_file(), f"{harness} missing"
    result = subprocess.run(
        ["node", str(TESTS / harness), case],
        capture_output=True,
        text=True,
        cwd=TESTS.parent,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_decide_phone_hides_shell_chrome():
    css = (STATIC / "decide.css").read_text()
    assert '.app[data-screen="decide"]' not in css
    chrome = re.search(r'\.shell\[data-screen="decide"\] :is\(([^)]*)\) \{ display: none; \}', css)
    assert chrome, "Decide phone rules do not target the shell"
    for part in (".head", ".m-top", ".m-tabs", "#connection-state"):
        assert part in chrome.group(1)
    assert re.search(r'\.shell\[data-screen="decide"\] #content \{ padding: 0;', css)
    # The shell's phone layout (rail hidden, phone bar and tabs shown) runs up to 699 px; decide follows it.
    queries = re.findall(r"@media \(([^{]*)\) \{", css)
    phone = css[: css.index(chrome.group(0))].rsplit("@media", 1)[1]
    assert phone.startswith(" (max-width: 699px)"), phone[:40]
    wide = [q for q in queries if "min-width" in q]
    assert wide and all(q.startswith("min-width: 700px") for q in wide), wide


def test_guard_node_available():
    assert shutil.which("node") is not None
