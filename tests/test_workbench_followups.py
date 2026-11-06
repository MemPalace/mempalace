"""Workbench follow-ups: CSP-safe styles, direct decision answers, palette focus, restricted flag."""

import subprocess
from pathlib import Path

import pytest

from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api

TESTS = Path(__file__).parent
STATIC = TESTS.parent / "commonplace" / "web" / "static"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
CASES = [
    ("workbench_sheets.mjs", "nostyle"),
    ("workbench_palette.mjs", "nostyle"),
    ("workbench_shell_wiring.mjs", "shell-nostyle"),
    ("workbench_needs.mjs", "decision"),
    ("workbench_palette.mjs", "focus-race"),
    ("workbench_needs.mjs", "restricted"),
    ("workbench_needs.mjs", "zkey"),
    ("workbench_needs.mjs", "quiet"),
    ("workbench_memory.mjs", "source_author"),
]


@pytest.mark.parametrize(
    ("harness", "case"), CASES, ids=[f"{h[10:-4]}:{case}" for h, case in CASES]
)
def test_followup(harness, case):
    assert (TESTS / harness).is_file(), f"{harness} missing"
    result = subprocess.run(
        ["node", str(TESTS / harness), case],
        capture_output=True,
        text=True,
        cwd=TESTS.parent,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr  # RED


def test_static_markup_has_no_inline_styles():
    # style-src 'self' refuses inline style elements and style attributes alike.
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(STATIC.iterdir())
        if path.suffix in (".js", ".html")
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if "<style" in line or ' style="' in line
    ]
    assert not offenders, offenders  # RED


def test_card_payload_carries_restricted_flag(hub, fresh):
    parent = fresh.connect("parent")
    child = fresh.connect("child", parent=parent.id, trust="untrusted", write_wing="inbox")
    filed = child.work.task_add(board="work", title="Synthetic proposal", body="Synthetic body")
    own = fresh.owner.task_add(board="work", title="Synthetic owner card")
    status, restricted = api(hub, fresh.cookie, "GET", f"/api/cards/{filed['number']}")
    assert status == 200 and restricted.get("restricted") is True  # RED
    status, trusted = api(hub, fresh.cookie, "GET", f"/api/cards/{own['number']}")
    assert status == 200 and trusted.get("restricted") is False


def test_guard_csp_header_unchanged():
    from commonplace import web

    assert web.CSP == CSP

    class Handler:
        def __init__(self):
            self.headers = {}
            self.wfile = type("W", (), {"write": lambda self, data: None})()

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.headers[name] = value

        def end_headers(self):
            pass

    h = Handler()
    web._response(h, 200, b"<!doctype html>", "text/html; charset=utf-8")
    assert h.headers["Content-Security-Policy"] == CSP
