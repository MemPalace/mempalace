"""Two counted pin failures; the browser-free Node harness checks the head."""

import os
import re
import shutil
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "commonplace" / "web" / "static"
SCRIPT = Path(__file__).with_name("commonplace_console_render.mjs")


def run_node(mode):
    result = subprocess.run(
        [shutil.which("node"), "--test", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "C184_RENDER_CASE": mode},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_console_view_renders_states_and_escapes():
    assert shutil.which("node") and (STATIC / "app.js").is_file()
    assert (STATIC / "console.js").is_file()  # RED
    run_node("view")


def test_app_reaches_console_and_isolates_its_errors():
    app = (STATIC / "app.js").read_text()
    assert 'href="#/memory"' in app
    assert app.count('href="#/console"') >= 2  # RED
    assert 'import { createConsole } from "./console.js";' in app
    routing = next(line for line in app.splitlines() if "state.base = start" in line)
    pattern = re.search(r"\(/(.+)/\.test\(hash\)", routing)
    assert pattern is not None
    # Execute the actual JS routing expression rather than translating its regex.
    check = subprocess.run(
        [shutil.which("node"), "-e", f'if (!/{pattern[1]}/.test("#/console")) process.exit(1)'],
        capture_output=True,
        timeout=10,
    )
    assert check.returncode == 0
    poll = app.split("async function poll()", 1)[1].split(
        'document.addEventListener("visibilitychange"', 1
    )[0]
    assert "console" not in poll
    assert "/app/console.css" in (STATIC / "index.html").read_text()
    assert "connection-state" not in (STATIC / "console.js").read_text()
    run_node("controller")
