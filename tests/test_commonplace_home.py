"""Home: the open-task list behind its table, and the three sections it renders."""

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


def test_home_renders_three_sections_from_a_seeded_payload():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed for the Home render check")
    script = Path(__file__).with_name("commonplace_home_render.mjs")
    subprocess.run([node, str(script)], check=True, timeout=10)


def test_needs_actions_behaviour():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed for the needs-actions behaviour check")
    script = Path(__file__).with_name("commonplace_needs_actions.mjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
