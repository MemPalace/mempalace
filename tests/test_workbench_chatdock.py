"""Dock contracts run without a browser or a running service."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "commonplace/web/static"
CASES = [
    "label_plain",
    "label_unread",
    "label_missing",
    "label_project",
    "slot",
    "push",
    "sheet",
    "phone",
    "boundaries",
    "focus_wrap",
    "focus_reverse",
    "inert_regions",
    "open_focus",
    "close_focus",
    "context_focus",
    "modal",
    "desktop",
    "resize",
    "tab_trap",
    "keys",
    "typing",
    "disabled_keys",
    "nested_dialog",
    "strips",
    "details_row",
    "details_board",
    "scrim",
    "announcement",
    "quote_jump",
    "async_close",
    "remount",
    "destroy",
    "css",
]


@pytest.mark.parametrize("case", CASES)
def test_chatdock_contract(case):
    assert (STATIC / "chatdock.js").is_file(), "Chat dock module missing"  # RED
    assert (STATIC / "chatdock.css").is_file(), "Chat dock styles missing"  # RED
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("workbench_chatdock.mjs")), case],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_g01_shared_shell_layout():
    source = (STATIC / "workbench.css").read_text()
    for value in ("--surface:", "--accent:", ".shell.chat-open", "1023px", "699px"):
        assert value in source


def test_g02_details_strip_contract():
    source = (STATIC / "details.js").read_text()
    for value in ("data-back-chat", "strip.source", "strip.kind", "You picked"):
        assert value in source
