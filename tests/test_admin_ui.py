"""Served Admin assets exercised with promises and explicitly fired timers."""

import json
import shutil
import subprocess
from pathlib import Path

from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_commonplace_web import request


def run_ui(hub, case):
    sources = {}
    names = (
        ["rail", "api", "palette", "board"]
        if case == "navigation"
        else ["admin", "api", "app", "routes"]
    )
    for name in names:
        status, _, body = request(hub, f"/app/{name}.js")
        assert status == 200, f"{name} asset must be served"
        sources[name] = body.decode()
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node is required for Admin checks")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("admin_ui.mjs")), case],
        input=json.dumps(sources),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode not in (0, 42):
        raise RuntimeError("Admin harness setup/runtime error: " + result.stderr[-3000:])
    assert result.returncode == 0, result.stderr


def test_owner_navigation(hub):
    run_ui(hub, "navigation")


def test_direct_admin_route_and_reload(hub):
    run_ui(hub, "route")


def test_sections_settle_independently(hub):
    run_ui(hub, "independent")


def test_retry_dispose_and_expiry(hub):
    run_ui(hub, "lifecycle")


def test_admin_to_card_route(hub):
    run_ui(hub, "card-route")


def test_admin_route_change_disposes(hub):
    run_ui(hub, "route-disposal")


def test_retry_repeats_failed_page(hub):
    run_ui(hub, "page-retry")
