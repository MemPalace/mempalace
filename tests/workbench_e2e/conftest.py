"""Browser acceptance for the workbench: real gateway, synthetic seed, headless Chromium.

Runs only with CP_BROWSER_E2E=1; without it every test in this directory is skipped,
so ordinary pytest runs are unaffected. With it:
- a missing Playwright package or Chromium build fails the run (never a silent skip);
- a skip whose case key is not in scenarios.SKIPS is turned into a failure.

Browser evidence uses Playwright 1.64.1 and an existing Chromium cache via
PLAYWRIGHT_BROWSERS_PATH set before pytest starts. Run with --no-showlocals: the
session cookie is a credential and never appears in a URL, a log or an assertion.
Optional outputs (outside the repository): CP_E2E_OUT for the console and layout
logs, CP_E2E_SHOTS for screenshots and CP_E2E_DESIGN_SHOTS for compare.html.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.workbench_e2e.scenarios import SKIPS, append_log, out_dir

HERE = Path(__file__).parent
ENABLED = os.environ.get("CP_BROWSER_E2E") == "1"


def _ours(item):
    return Path(str(item.fspath)).parent == HERE


def pytest_collection_modifyitems(config, items):
    if ENABLED:
        return
    skip = pytest.mark.skip(reason="workbench browser acceptance runs with CP_BROWSER_E2E=1")
    for item in items:
        if _ours(item):
            item.add_marker(skip)


def case_key(item):
    spec = getattr(item, "callspec", None)
    if spec is not None:
        for name in ("key", "render"):
            if name in spec.params:
                return str(spec.params[name])
    return item.name


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not (ENABLED and _ours(item) and report.skipped):
        return
    key = case_key(item)
    if key in SKIPS:
        return
    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
    report.outcome = "failed"
    report.longrepr = f"skip of {key!r} is not in SKIPS, so it fails: {reason}"


def pytest_sessionstart(session):
    if ENABLED:
        for name in ("console.jsonl", "layout.jsonl"):
            path = out_dir(name)
            if path is not None:
                # Written even when empty: no line means no console error or warning.
                path.write_text("", encoding="utf-8")


def pytest_sessionfinish(session, exitstatus):
    if not ENABLED:
        return
    path = out_dir("layout.jsonl")
    if path is not None and path.exists():
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        issues = sum(len(r["issues"]) for r in records)
        out_dir("layout-summary.txt").write_text(
            f"{len(records)} renders, {issues} issues\n", encoding="utf-8"
        )
    from tests.workbench_e2e import shots

    shots.write_compare()


@pytest.fixture(scope="session")
def chromium():
    if not ENABLED:
        pytest.skip("CP_BROWSER_E2E is not set")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.fail(
            "CP_BROWSER_E2E=1 needs the playwright package (uv run --with playwright==1.64.1)",
            pytrace=False,
        )
    manager = sync_playwright().start()
    try:
        browser = manager.chromium.launch(headless=True)
    except Exception as exc:
        manager.stop()
        where = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "<unset>")
        detail = str(exc).splitlines()[0][:200]
        pytest.fail(
            f"CP_BROWSER_E2E=1 needs a Chromium build in the Playwright cache "
            f"(PLAYWRIGHT_BROWSERS_PATH={where}): {detail}",
            pytrace=False,
        )
    yield browser
    browser.close()
    manager.stop()


@pytest.fixture
def app(tmp_path, monkeypatch, config, kg, pytestconfig):
    """A fresh Northwind workspace behind a real gateway, per test."""
    if not ENABLED:
        pytest.skip("CP_BROWSER_E2E is not set")
    assert not pytestconfig.getoption("showlocals"), "run the browser suite with --no-showlocals"
    from tests._commonplace_helpers import Hub, InProcessLauncher
    from tests._mcp_server_helpers import _patch_mcp_server
    from tests.workbench_e2e import seed

    for name in ("COMMONPLACE_RATE_LIMITS", "COMMONPLACE_CHAT_WORKER", "COMMONPLACE_HOLD_WORKER"):
        monkeypatch.setenv(name, "off")
    monkeypatch.delenv("COMMONPLACE_APP_URL", raising=False)
    monkeypatch.delenv("COMMONPLACE_BASE_URL", raising=False)
    _patch_mcp_server(monkeypatch, config, kg)
    # tmpfs keeps the per-test seed (a few hundred SQLite commits) under a second.
    shm = Path("/dev/shm")
    data = Path(tempfile.mkdtemp(prefix="wb-e2e-", dir=shm)) if shm.is_dir() else tmp_path
    hub = Hub(data / "data", InProcessLauncher(monkeypatch))
    try:
        yield seed.build(hub)
    finally:
        hub.close()
        if data != tmp_path:
            shutil.rmtree(data, ignore_errors=True)


@pytest.fixture
def ui(chromium, app, request):
    """Open pages as Maya (or Leon) with a cookie session; collect console messages."""
    from tests.workbench_e2e.scenarios import Browser

    session = Browser(chromium, app, request.node.nodeid)
    yield session
    session.close()
    for record in session.console:
        append_log("console.jsonl", record)
