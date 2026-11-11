"""Structured decision blocks: an ask carries labelled options with detail and a preview."""

import shutil
import subprocess
from pathlib import Path

import pytest

from commonplace.boards import decision_blocks
from commonplace.extensions import ToolError
from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    REVIEW_OPTIONS,
    REVIEW_QUESTION,
    hub_fixture,  # noqa: F401
)
from tests.test_commonplace_board_authority import fresh as fresh_fixture
from tests.test_commonplace_boards import api, env as env_fixture, mcp

fresh = fresh_fixture
env = env_fixture

HARNESS = Path(__file__).with_name("commonplace_decision_blocks.mjs")
BLOCKS = [
    {
        "label": "Compact header",
        "detail": "One row; the search field moves into the menu.",
        "preview": "[logo]  Boards  Memory        (o)\n---------------------------------",
    },
    {"label": "Tall header", "preview": "[logo]\nBoards  Memory  [ search ]  (o)"},
    "Keep the current header",
]
LABELS = ["Compact header", "Tall header", "Keep the current header"]


def ask(caller, route="new", **kwargs):
    args = dict(question=REVIEW_QUESTION, options=BLOCKS, context=REVIEW_CONTEXT)
    args.update(kwargs)
    if route == "new":
        return caller.ask(board="work", **args)
    card = caller.task_add(board="work", title="Header layout needs a choice", claim=True)
    return caller.ask(task=card["number"], **args)


@pytest.mark.parametrize("route", ["new", "card"])
def test_structured_ask_reaches_the_person_and_the_answer_returns(fresh, route):
    worker = fresh.connect("worker")
    asked = ask(worker.work, route)
    assert asked["ask_options"] == LABELS
    assert asked["ask_blocks"] == [
        BLOCKS[0],
        BLOCKS[1],
        {"label": "Keep the current header"},
    ]
    waiting = next(c for c in fresh.owner.needs_me()["asks"] if c["number"] == asked["number"])
    assert waiting["ask_blocks"] == asked["ask_blocks"]
    assert "answer" in waiting["actions"]

    decided = fresh.owner.decide(asked["number"], choice="Tall header", version=waiting["version"])
    assert decided["status"] == "todo" and decided["assignee"] == worker.principal.actor
    assert decided["ask"] is None and decided["ask_options"] == [] and decided["ask_blocks"] == []
    # The asker picks the card back up and reads the answer on the existing return path.
    taken = worker.work.task_take_next(task=asked["number"])
    assert taken["decision"]["choice"] == "Tall header"
    assert taken["decision"]["actor"] == fresh.owner.principal.actor


def test_an_option_the_asker_did_not_offer_is_refused(fresh):
    asked = ask(fresh.connect("worker").work)
    for choice in ("Something else entirely", "tall header", BLOCKS[0]["detail"]):
        with pytest.raises(ToolError, match="choose one of the offered options"):
            fresh.owner.decide(asked["number"], choice=choice)
    still = fresh.owner.task_show(asked["number"])
    assert still["status"] == "review" and still["ask_blocks"][0] == BLOCKS[0]


def test_plain_text_asks_are_unchanged(fresh):
    worker = fresh.connect("worker")
    asked = worker.work.ask(
        board="work", question=REVIEW_QUESTION, options=REVIEW_OPTIONS, context=REVIEW_CONTEXT
    )
    assert asked["ask_options"] == REVIEW_OPTIONS and asked["ask_blocks"] == []
    # A free-text reply still answers.
    replied = fresh.owner.decide(asked["number"], reply="Ship it after the release notes.")
    assert replied["status"] == "todo"


def test_a_later_plain_ask_drops_the_earlier_blocks(fresh):
    worker = fresh.connect("worker")
    asked = ask(worker.work, "card")
    fresh.owner.decide(asked["number"], choice="Tall header")
    again = worker.work.ask(
        task=asked["number"], question=REVIEW_QUESTION, options=LABELS, context=REVIEW_CONTEXT
    )
    assert again["ask_options"] == LABELS and again["ask_blocks"] == []


@pytest.mark.parametrize(
    "options",
    [
        [{"label": "A", "detail": "x"}] * 2 + [{"label": "B"}],
        [{"label": "Compact header"}, {"label": "compact header ", "detail": "x"}],
        [{"label": "A", "detail": "x"}, {"label": "B", "image": "https://example.com/a.png"}],
        [{"detail": "no label"}, "B"],
        [{"label": "A", "detail": ""}, "B"],
        [{"label": "A", "detail": 7}, "B"],
        [{"label": "A" * 121, "detail": "x"}, "B"],
        [{"label": "A", "detail": "x" * (decision_blocks.DETAIL_LIMIT + 1)}, "B"],
        [{"label": "A", "preview": "x" * (decision_blocks.PREVIEW_LIMIT + 1)}, "B"],
        [{"label": "A", "preview": "\n".join("x" * (decision_blocks.PREVIEW_LINES + 1))}, "B"],
        [{"label": "A", "preview": "bell\x07"}, "B"],
        [{"label": "A", "detail": "x"}],
        [{"label": f"Option {i}", "detail": "x"} for i in range(7)],
        [{"label": "release origin", "detail": "x"}, "B"],
        [["A"], "B"],
        {"label": "A"},
    ],
)
def test_malformed_or_oversized_blocks_are_refused(fresh, options):
    worker = fresh.connect("worker")
    with pytest.raises(ToolError, match="^no: "):
        ask(worker.work, options=options)
    assert fresh.owner.needs_me()["asks"] == []


def test_parse_keeps_plain_lists_plain_and_bounds_structured_ones():
    assert decision_blocks.parse(None) == (None, None)
    assert decision_blocks.parse(["A", "B"]) == (["A", "B"], None)
    largest = [
        {
            "label": f"{i}" * 120,
            "detail": "d" * decision_blocks.DETAIL_LIMIT,
            "preview": "p" * decision_blocks.PREVIEW_LIMIT,
        }
        for i in range(6)
    ]
    labels, stored = decision_blocks.parse(largest)
    assert labels == [item["label"] for item in largest]
    assert decision_blocks.public({"ask_options": '["x"]', "ask_blocks": stored}) == []


def test_mcp_ask_accepts_blocks_and_rest_decide_answers(hub, env):
    token, _principal, _person, cookie, _owner = env
    ok, output = mcp(
        hub,
        token,
        "ask",
        board="work",
        question=REVIEW_QUESTION,
        options=BLOCKS,
        context=REVIEW_CONTEXT,
    )
    assert ok, output
    number = int(output.split()[0].lstrip("#"))
    ok, shown = mcp(hub, token, "task_show", task=number)
    assert ok and "Compact header: One row; the search field moves into the menu." in shown
    assert "[logo]  Boards  Memory        (o)" in shown

    code, card = api(hub, cookie, "GET", f"/api/cards/{number}")
    assert code == 200 and card["ask_blocks"][1] == BLOCKS[1]
    code, refused = api(
        hub,
        cookie,
        "POST",
        f"/api/cards/{number}/decide",
        {"choice": "Not offered", "version": card["version"]},
    )
    assert code >= 400 and "choose one of the offered options" in refused["error"]
    code, decided = api(
        hub,
        cookie,
        "POST",
        f"/api/cards/{number}/decide",
        {"choice": "Compact header", "version": card["version"]},
    )
    assert code == 200 and decided["status"] == "todo" and decided["ask_blocks"] == []

    ok, output = mcp(
        hub,
        token,
        "ask",
        board="work",
        question=REVIEW_QUESTION,
        options=[{"label": "A", "image": "x"}, "B"],
    )
    assert not ok and output.startswith("no: ")


@pytest.mark.parametrize("case", ["render", "plain", "escape", "answer", "stale"])
def test_decision_blocks_ui(case):
    assert shutil.which("node") is not None
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr
