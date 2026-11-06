"""Workbench shell fixes from the browser acceptance run (board data and app shell)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_actors import actor
from tests.test_commonplace_boards import api, env, service  # noqa: F401

STATIC = Path(__file__).resolve().parents[1] / "commonplace" / "web" / "static"
HARNESS = Path(__file__).with_name("workbench_g4_shell.mjs")
REDS = [
    "details-button",
    "add-sheet",
    "board-strip",
    "row-src",
    "ask-strip",
    "restart-link",
    "direct-link",
    "needs-settings",
]


def test_board_cards_include_child_cards(hub, env):  # noqa: F811
    _token, principal, _person, cookie, _owner = env
    top = service(hub, principal)
    parent = top.task_add(title="Approval with linked work", board="work")
    _, _, sub = actor(hub, "worker", parent=principal.connection_id)
    child = sub.task_add(title="Linked child card", board="work")
    status, body = api(hub, cookie, "GET", "/api/boards/work/cards")
    assert status == 200
    assert "children" in body, "the board never receives child cards"
    assert [c["number"] for c in body["children"]] == [child["number"]]
    assert body["children"][0]["parent"] == parent["number"]
    # The legacy keys keep their meaning: top-level cards only, chat-linked cards.
    assert [c["number"] for c in body["cards"]] == [parent["number"]]
    assert body["linked"] == []


def test_me_names_the_workspace(hub, env):  # noqa: F811
    _token, _principal, _person, cookie, _owner = env
    status, body = api(hub, cookie, "GET", "/api/me")
    assert status == 200
    assert "workspace_name" in body, "the rail cannot name the workspace"
    assert body["workspace_name"] == hub.db.workspace(hub.workspace)["name"]


@pytest.mark.parametrize("case", REDS)
def test_red_g4_shell(case):
    assert (STATIC / "app.js").is_file(), "App shell missing"
    assert HARNESS.is_file(), "Shell harness missing"
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_node_available():
    assert shutil.which("node") is not None
