"""A refused Done move or handoff says why and what to do next, instead of a bare 403."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api

HARNESS = Path(__file__).with_name("owner_403_message.mjs")


@pytest.fixture
def cards(hub, fresh):
    """A restricted-origin card (unreleased, in backlog) with a live coordinator on the board."""
    coordinator = fresh.connect("orchestrator", trust="standard")
    fresh.owner.board_set("work", coordinator.principal.actor)
    creator = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    card = creator.work.task_add(board="work", title="Restricted idea")
    released = coordinator.work.task_approve(
        creator.work.task_add(board="work", title="Released idea")["number"], reason="Ready"
    )
    return card, released


def move_done(hub, fresh, card):
    version = fresh.owner.task_show(card["number"])["version"]
    return api(
        hub,
        fresh.cookie,
        "PATCH",
        f"/api/cards/{card['number']}",
        {"status": "done", "version": version},
    )


def hand_off(hub, fresh, card):
    version = fresh.owner.task_show(card["number"])["version"]
    return api(
        hub,
        fresh.cookie,
        "POST",
        f"/api/cards/{card['number']}/handoff",
        {"to": "anyone", "note": "Please take this", "version": version},
    )


def assert_explained(status, reply, *, reason):
    assert status == 403, reply
    assert reason in reply["error"], reply
    assert reply["next_step"] and reply["next_step"] != reply["error"], reply
    assert reply["code"].endswith("_refused"), reply


def test_done_on_an_unreleased_card_names_the_release_step(hub, fresh, cards):
    status, reply = move_done(hub, fresh, cards[0])
    assert_explained(status, reply, reason="has not been released")
    assert "coordinator" in reply["next_step"]
    assert "moved to Done" in reply["error"]


def test_done_on_a_released_todo_card_tells_the_owner_to_move_it_to_doing_first(hub, fresh, cards):
    status, reply = move_done(hub, fresh, cards[1])
    assert_explained(status, reply, reason="only be moved to Done from Doing")
    assert "Doing" in reply["next_step"] and "handoff" not in reply["error"]


def test_handoff_on_an_unreleased_card_names_the_release_step(hub, fresh, cards):
    status, reply = hand_off(hub, fresh, cards[0])
    assert_explained(status, reply, reason="has not been released")
    assert "handed off" in reply["error"] and "coordinator" in reply["next_step"]


def test_handoff_of_a_finished_card_says_which_cards_can_be_handed_off(hub, fresh, cards):
    card = fresh.owner.task_add(board="work", title="Plain card")
    fresh.owner.task_update(card["number"], status="doing", version=card["version"])
    done = fresh.owner.task_update(
        card["number"], status="done", version=fresh.owner.task_show(card["number"])["version"]
    )
    status, reply = hand_off(hub, fresh, done)
    assert_explained(status, reply, reason="Only cards in Todo or Doing can be handed off")


def test_other_answers_keep_their_status(hub, fresh, cards):
    card = cards[1]
    stale = fresh.owner.task_show(card["number"])["version"] - 1
    status, reply = api(
        hub,
        fresh.cookie,
        "PATCH",
        f"/api/cards/{card['number']}",
        {"status": "done", "version": stale},
    )
    assert status == 409 and reply["error"] == "card changed"
    status, reply = api(
        hub, fresh.cookie, "PATCH", "/api/cards/9999", {"status": "done", "version": 1}
    )
    assert status == 404 and "next_step" not in reply


def test_a_refused_edit_outside_done_is_not_reworded(hub, fresh, cards):
    card = cards[0]
    status, reply = api(
        hub,
        fresh.cookie,
        "PATCH",
        f"/api/cards/{card['number']}",
        {"status": "doing", "version": fresh.owner.task_show(card["number"])["version"]},
    )
    assert status == 403 and "next_step" not in reply, reply


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("case", ["explained", "bare", "json_error_only", "ok_html"])
def test_owner_ui_shows_the_reason_and_next_step(case):
    run = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=60, check=False
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_the_harness_exists():
    assert HARNESS.is_file()
