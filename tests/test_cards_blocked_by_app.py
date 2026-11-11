"""Create, show and clear a blocked-by link, plus the reads that must skip a blocked card."""

import json
import subprocess

import pytest

from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._split_helpers import blockers, invoke, take, world
from tests.runner_harness import http


def _ui():
    run = subprocess.run(
        ["node", "tests/blockers_ui.mjs"], capture_output=True, text=True, check=True
    )
    return json.loads(run.stdout)


def test_app_parses_card_numbers_and_edits_the_list():
    ui = _ui()
    assert ui["parsed"] == [[12], [12], [12, 14, 15], None, None, None, None, None, None]
    assert ui["add"] == [4, 5, 6] and ui["remove"] == [5]


def test_app_shows_waiting_and_done_blockers_with_clear_and_add_controls():
    ui = _ui()
    assert ui["rows"] == [
        {"number": 4, "title": "Schema <b>", "done": False},
        {"number": 5, "title": "", "done": True},
    ]
    section = ui["section"]
    assert "#4 Schema &lt;b&gt; · Waiting" in section and "#5 · Done" in section
    assert section.count('data-action="unblock"') == 2 and 'data-form="blockers"' in section
    assert "Blocks #12" in section
    assert ui["empty"] is True


def test_app_hides_blockers_when_orchestration_is_off_and_mounts_in_details():
    ui = _ui()
    assert ui["off"] == "" and ui["hiddenInDetails"] is False and ui["inDetails"] is True


def test_app_flow_create_show_clear_over_rest(hub):
    """The same PATCH the app sends: add a blocker, read it back, then clear it."""
    world(hub)
    token = hub.owner_token("blockers-app")
    made = [
        http(hub.port, "POST", "/api/boards/acme/cards", token=token, body={"title": t})
        for t in ("Prerequisite", "Dependent")
    ]
    assert [m[0] for m in made] == [201, 201]
    a, b = made[0][1]["number"], made[1][1]["number"]
    version = made[1][1]["version"]
    status, card = http(
        hub.port,
        "PATCH",
        f"/api/cards/{b}",
        token=token,
        body={"blocked_by": [a], "version": version},
    )
    assert status == 200, card
    assert card["blocked_by"] == [a]
    assert (
        card["waiting"]["code"] == "blocked_by"
        and card["waiting"]["blockers"][0]["title"] == "Prerequisite"
    )
    status, shown = http(hub.port, "GET", f"/api/cards/{a}", token=token)
    assert status == 200 and shown["blocks"] == [b]
    status, card = http(
        hub.port,
        "PATCH",
        f"/api/cards/{b}",
        token=token,
        body={"blocked_by": [], "version": card["version"]},
    )
    assert status == 200 and card["blocked_by"] == [] and card["waiting"] is None


def test_blocked_card_is_not_actionable_in_todo_take_next_or_start(hub):
    w = world(hub)
    first, dep = w.card(title="Blocked first"), w.card(title="Dependency")
    blockers(w, first, dep)
    todo = {c["number"]: c for c in w.boards.task_list(board="acme", status="todo")}
    assert todo[first]["waiting"]["code"] == "blocked_by" and todo[dep]["waiting"] is None
    status, picked = take(w)
    assert status == 200 and picked["number"] == dep
    status, refused = take(w, task=first)
    assert status >= 400 and refused["code"] == "blocked_by"
    assert w.boards.task_show(first)["waiting"]["code"] == "blocked_by"


def test_blocker_on_a_card_the_user_cannot_read_is_refused_without_leaking(hub):
    w = world(hub)
    w.boards.board_add(slug="vault", wing="vault")
    hidden, plain = w.card(board="vault", title="Hidden"), w.card()
    # PAT ``a`` reads and writes the shared wing only.
    from commonplace.boards.service import BoardService

    service = BoardService(w.ctx(w.a))
    own = service.task_add("Mine", board="acme")["number"]
    missing = max(hidden, plain, own) + 500
    seen = invoke(service.task_update, own, blocked_by=[hidden])
    absent = invoke(service.task_update, own, blocked_by=[missing])
    assert seen[0] == 400 and seen[1]["code"] == "other_board" == absent[1]["code"]
    # Apart from the caller's own number, the answer is identical to the missing-card one.
    assert seen[1]["error"].replace(f"#{hidden}", "#N") == absent[1]["error"].replace(
        f"#{missing}", "#N"
    )
    assert service.task_show(own)["blocked_by"] == []


@pytest.mark.parametrize(
    ("setup", "code", "words"),
    [
        ("self", "self", "itself"),
        ("cycle", "cycle", "each other"),
        ("far", "other_board", "this board"),
    ],
)
def test_refusals_explain_themselves_in_plain_words(hub, setup, code, words):
    w = world(hub)
    a, b = w.card(), w.card()
    blockers(w, b, a)
    target = {"self": a, "cycle": b, "far": w.card(board="plain")}[setup]
    status, result = invoke(w.boards.task_update, a, blocked_by=[target])
    assert status == 400 and result["code"] == code and words in result["error"]
