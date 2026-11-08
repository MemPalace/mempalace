"""Needs me replaces Home in the shell; the open-card API behind the old table stays."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    REVIEW_OPTIONS,
    hub_fixture,  # noqa: F401
)
from tests.test_commonplace_boards import api, env, service  # noqa: F401


def test_open_cards_are_scoped_and_carry_need(hub, env):  # noqa: F811
    token, agent, person, cookie, owner = env
    worker = service(hub, agent)
    asked = worker.ask(
        'Ship <b>"now"</b>? Which approach should we take?',
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    todo = owner.task_add(title="Later", board="work")
    owner.task_add(title="Someday", board="work", status="backlog")
    owner.seed_card(board="work", title="Finished", status="done")
    owner.board_add(slug="secret", wing="private.secret")
    secret = owner.seed_card(board="secret", title="SECRET CANARY", status="todo")

    status, payload = api(hub, cookie, "GET", "/api/cards")
    assert status == 200 and payload["total"] == 3
    needs = {c["number"]: c["need"] for c in payload["cards"]}
    assert needs == {asked["number"]: "you", todo["number"]: "idle", secret["number"]: "idle"}
    first = payload["cards"][0]
    assert (
        first["title"] == 'Ship <b>"now"</b>? Which approach should we take?'
        and first["ask"] == 'Ship <b>"now"</b>? Which approach should we take?'
    )
    # Slim: no body, and no share link minted per row.
    assert all("body" not in c and "url" not in c for c in payload["cards"])

    scoped = hub.token("home-reader", profile="full", scopes=["memory:read"], read_wings=["shared"])
    status, payload = api(hub, scoped, "GET", "/api/cards")
    assert status == 200
    assert {c["number"] for c in payload["cards"]} == {asked["number"], todo["number"]}
    assert "SECRET CANARY" not in json.dumps(payload)
    assert api(hub, None, "GET", "/api/cards")[0] == 401
    assert api(hub, cookie, "POST", "/api/cards", {})[0] == 404
    for bad in ("0", "501", "x"):
        assert api(hub, cookie, "GET", f"/api/cards?limit={bad}")[0] == 400


@pytest.mark.timing
def test_open_cards_are_capped_most_urgent_first_and_fast(hub, env):  # noqa: F811
    token, agent, person, cookie, owner = env
    for n in range(300):
        owner.seed_card(board="work", title=f"Card {n}", status="todo")
    asked = service(hub, agent).ask(
        "Last but urgent? Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    start = time.perf_counter()
    status, payload = api(hub, cookie, "GET", "/api/cards")
    elapsed = time.perf_counter() - start
    assert status == 200 and payload["total"] == 301 and len(payload["cards"]) == 200
    assert payload["cards"][0]["number"] == asked["number"]  # Needs you survives the cap.
    assert elapsed < 3, elapsed
    status, payload = api(hub, cookie, "GET", "/api/cards?limit=500")
    assert status == 200 and len(payload["cards"]) == 301 and payload["total"] == 301


STATIC = Path(__file__).resolve().parents[1] / "commonplace/web/static"


def _render(case):
    node = shutil.which("node")
    assert node, "node is needed for the Needs me render checks"
    script = Path(__file__).with_name("commonplace_home_render.mjs")
    result = subprocess.run([node, str(script), case], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr  # RED


def test_needs_view_renders_needs_me():
    assert (STATIC / "needs.js").is_file(), "needs.js missing"
    _render("needs_view")


def test_home_view_tiles_table_and_stylesheet_removed():
    assert (STATIC / "home.js").is_file(), "home.js missing"
    _render("home_removed")


def test_guard_need_items_order_kept():
    _render("need_items")


def test_owner_origin_survives_shell_refresh():
    _render("origin_shell")


def test_needs_actions_behaviour():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed for the needs-actions behaviour check")
    script = Path(__file__).with_name("commonplace_needs_actions.mjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
