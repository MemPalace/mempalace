"""Browser-free checks for the card panel's links to agent activity and transcripts."""

import re
import shutil
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "commonplace" / "web" / "static"


def test_card_links_module_and_wiring():
    node = shutil.which("node")
    assert node, "Node is required for the card link checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("card_log_links.mjs"))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_app_routes_the_console_agent_parameter():
    app = (STATIC / "app.js").read_text()
    routing = next(line for line in app.splitlines() if "state.base = start" in line)
    pattern = re.search(r"\(/(.+)/\.test\(hash\)", routing)
    assert pattern is not None
    probe = (
        f"const p = /{pattern[1]}/;"
        "for (const ok of ['#/console', '#/console?agent=abc%2F1']) if (!p.test(ok)) process.exit(1);"
        "for (const bad of ['#/console?x=1', '#/console?agent=', '#/consoles']) if (p.test(bad)) process.exit(2);"
    )
    check = subprocess.run([shutil.which("node"), "-e", probe], capture_output=True, timeout=10)
    assert check.returncode == 0, check.returncode
    # Leaving the console from an agent address must still stop its polling.
    assert 'previousBase.startsWith("#/console") && !state.base.startsWith("#/console")' in app
    assert "consoleUI.show(new URLSearchParams(" in app
