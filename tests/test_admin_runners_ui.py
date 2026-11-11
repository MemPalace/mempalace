"""Owner runner block: enroll, attest and wings edit through a fake DOM, no browser."""

import json
import shutil
import subprocess
from pathlib import Path

from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_commonplace_web import request


def _served(hub, name):
    status, _, body = request(hub, f"/app/{name}.js")
    assert status == 200, f"{name} asset must be served"
    return body.decode()


def test_runner_block_routes_and_one_time_tokens(hub):
    node = shutil.which("node")
    assert node, "Node is required for the runner block checks"
    sources = {name: _served(hub, name) for name in ("adminrunners", "admin")}
    result = subprocess.run(
        [node, str(Path(__file__).with_name("admin_runners_ui.mjs"))],
        input=json.dumps(sources),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr[-3000:]


def test_admin_screen_wires_the_runner_block(hub):
    app = _served(hub, "app")
    assert 'from "./adminrunners.js"' in app
    assert "runners: createRunnerBlock({ api, e })" in app
