"""Card panel Done and Pocket section: the static module renders and builds its requests."""

import re
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).with_name("lifecycle_ui.mjs")
CASES = re.findall(r"^  (\w+)\(\) \{", HARNESS.read_text(), re.M)


@pytest.mark.parametrize("case", CASES)
def test_card_lifecycle_ui(case):
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_details_calls_the_after_draw_hook_only_on_the_details_tab():
    source = (HARNESS.parent.parent / "commonplace/web/static/details.js").read_text()
    assert 'if (options.tab === "details") afterDraw(mount, current);' in source
    app = (HARNESS.parent.parent / "commonplace/web/static/app.js").read_text()
    assert "afterDraw: placeLifecycle" in app
