"""Goals tree: owner-only writes, additive schema, and the goal chain in card context."""

import sqlite3

import pytest

from commonplace.boards import goals
from commonplace.boards.store import BoardStore
from commonplace.boards.tools import show
from commonplace.extensions import ToolError
from tests.test_commonplace_boards import (
    api,
    env as env,
    hub_fixture as hub_fixture,
    mcp,
    service,
)


def chain(card):
    return [g["title"] for g in card["goals"]["chain"]]


def tree3(owner):
    top = owner.goal_set(title="Ship the product", detail="Why we exist")
    mid = owner.goal_set(title="Launch beta", parent=top["id"])
    leaf = owner.goal_set(
        title="Onboarding works", detail="First run under a minute", parent=mid["id"]
    )
    return top, mid, leaf


def test_owner_sets_and_reads_tree(hub, env):
    _, _, _, _, owner = env
    assert owner.goal_list() == []
    top, mid, leaf = tree3(owner)
    listed = owner.goal_list()
    assert [(g["id"], g["depth"]) for g in listed] == [
        (top["id"], 0),
        (mid["id"], 1),
        (leaf["id"], 2),
    ]
    changed = owner.goal_set(goal=mid["id"], title="Launch the beta", archived=True)
    assert (
        changed["title"] == "Launch the beta"
        and changed["archived"]
        and changed["parent"] == top["id"]
    )
    assert owner.goal_set(goal=leaf["id"], parent=0)["parent"] is None


def test_non_owners_are_refused_but_may_read(hub, env):
    _, agent, _, _, owner = env
    top = owner.goal_set(title="Top")
    card = owner.task_add("Work", board="work")
    worker = service(hub, agent)
    assert agent.role == "owner" and not agent.person
    for call in (
        lambda: worker.goal_set(title="Mine"),
        lambda: worker.goal_set(goal=top["id"], title="Renamed"),
        lambda: worker.card_goal(card["number"], top["id"]),
    ):
        with pytest.raises(ToolError, match="owner"):
            call()
    assert [g["title"] for g in worker.goal_list()] == ["Top"]
    assert "goals" not in owner.task_show(card["number"])


def test_card_context_has_chain_in_show_and_take_next(hub, env):
    _, agent, _, _, owner = env
    top, mid, leaf = tree3(owner)
    card = owner.task_add("Work", board="work")
    assert "goals" not in owner.card_goal(card["number"], None)
    result = owner.card_goal(card["number"], leaf["id"])
    assert chain(result) == ["Ship the product", "Launch beta", "Onboarding works"]
    shown = owner.task_show(card["number"])
    assert chain(shown)[-1] == "Onboarding works" and shown["goals"]["omitted"] == 0
    assert '- "Onboarding works": "First run under a minute"' in show(shown)
    taken = service(hub, agent).task_take_next(task=card["number"])
    assert chain(taken) == chain(shown)
    owner.card_goal(card["number"], None)
    assert "goals" not in owner.task_show(card["number"])


def test_cards_without_goal_are_unchanged(hub, env):
    _, agent, _, _, owner = env
    tree3(owner)
    card = owner.task_add("Plain", board="work")
    before = owner.task_show(card["number"])
    assert "goals" not in before and "goals" not in show(before)
    assert "goals" not in service(hub, agent).task_take_next(task=card["number"])


def test_budget_holds_for_long_tree_and_own_goal_stays(hub, env):
    _, _, _, _, owner = env
    parent = 0
    ids = []
    for level in range(goals.MAX_DEPTH):
        item = owner.goal_set(
            title=f"L{level} " + "t" * (goals.TITLE_LIMIT - 4),
            detail="d" * goals.DETAIL_LIMIT,
            parent=parent,
        )
        parent = item["id"]
        ids.append(parent)
    card = owner.task_add("Deep", board="work")
    block = owner.card_goal(card["number"], ids[-1])["goals"]
    assert len(block["text"]) <= goals.BUDGET
    assert block["chain"][-1]["id"] == ids[-1]
    assert 0 < block["omitted"] < goals.MAX_DEPTH
    # the top-level goals went first; what remains is the lowest contiguous run
    assert [g["id"] for g in block["chain"]] == ids[block["omitted"] :]
    assert f"+{block['omitted']} higher" in block["text"]
    # a small budget still keeps the card's own goal
    with BoardStore(owner.ctx.workspace_dir()).connection() as conn:
        tiny = goals.chain_block(conn, card["number"], budget=1)
    assert [g["id"] for g in tiny["chain"]] == [ids[-1]] and tiny["omitted"] == goals.MAX_DEPTH - 1


def test_worst_case_own_goal_fits_budget():
    own = {"title": '"' * goals.TITLE_LIMIT, "detail": "\\" * goals.DETAIL_LIMIT}
    assert len(goals.render([own], goals.MAX_DEPTH - 1)) <= goals.BUDGET


@pytest.mark.parametrize(
    "fields",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "x" * (goals.TITLE_LIMIT + 1)},
        {"title": "a", "detail": "x" * (goals.DETAIL_LIMIT + 1)},
        {"title": "bell\x07"},
        {"title": 5},
        {"title": "a", "parent": 99},
        {"title": "a", "parent": -1},
    ],
)
def test_invalid_goals_are_refused(hub, env, fields):
    _, _, _, _, owner = env
    with pytest.raises(ToolError):
        owner.goal_set(**fields)
    assert owner.goal_list() == []


def test_cycles_depth_and_archived_targets(hub, env):
    _, _, _, _, owner = env
    top, mid, leaf = tree3(owner)
    for bad in (leaf["id"], mid["id"], top["id"]):
        with pytest.raises(ToolError, match="itself|deep"):
            owner.goal_set(goal=top["id"], parent=bad)
    tip = leaf
    for level in range(goals.MAX_DEPTH - 3):
        tip = owner.goal_set(title=f"d{level}", parent=tip["id"])
    with pytest.raises(ToolError, match="deep"):
        owner.goal_set(title="too deep", parent=tip["id"])
    card = owner.task_add("Work", board="work")
    owner.goal_set(goal=top["id"], archived=True)
    with pytest.raises(ToolError, match="archived"):
        owner.card_goal(card["number"], top["id"])
    with pytest.raises(ToolError, match="not found"):
        owner.card_goal(card["number"], 9999)
    owner.card_goal(card["number"], leaf["id"])
    owner.goal_set(goal=leaf["id"], archived=True)  # a held goal stays in context
    assert chain(owner.task_show(card["number"]))[-1] == "Onboarding works"


def test_old_database_gains_tables_and_keeps_cards(hub, env):
    _, _, _, _, owner = env
    card = owner.task_add("Old card", board="work")
    directory = owner.ctx.workspace_dir()
    with sqlite3.connect(BoardStore(directory).path) as conn:
        conn.executescript("DROP TABLE card_goals; DROP TABLE goals;")
    store = BoardStore(directory)
    BoardStore(directory)
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0
        assert goals.chain_block(conn, card["number"]) is None
    assert owner.task_show(card["number"])["title"] == "Old card"
    owner.card_goal(card["number"], owner.goal_set(title="Back")["id"])
    assert chain(owner.task_show(card["number"])) == ["Back"]


def test_chain_without_tables_reads_as_no_goal(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    assert goals.chain_block(conn, 1) is None


def test_rest_owner_sets_and_agents_are_refused(hub, env):
    token, _, _, cookie, owner = env
    status, top = api(hub, cookie, "POST", "/api/goals", {"title": "Top", "detail": "Why"})
    assert status == 200 and top["title"] == "Top"
    card = owner.task_add("Work", board="work")
    status, result = api(
        hub, cookie, "POST", "/api/goals/card", {"task": card["number"], "goal": top["id"]}
    )
    assert status == 200 and chain(result) == ["Top"]
    status, listed = api(hub, token, "GET", "/api/goals")
    assert status == 200 and [g["title"] for g in listed["goals"]] == ["Top"]
    assert api(hub, token, "POST", "/api/goals", {"title": "Mine"})[0] == 403
    assert api(hub, token, "POST", "/api/goals/card", {"task": card["number"]})[0] == 403
    assert api(hub, cookie, "POST", "/api/goals", {"title": "x", "bogus": 1})[0] == 400
    assert api(hub, cookie, "POST", "/api/goals", {"title": ""})[0] == 400
    assert api(hub, cookie, "POST", "/api/goals", {"goal": 77, "title": "x"})[0] == 404
    assert api(hub, None, "GET", "/api/goals")[0] == 401


def test_mcp_tools_registered_and_owner_gated(hub, env):
    token, _, _, _, owner = env
    assert {"goal_list", "goal_set", "card_goal"} <= hub.tool_names(token)
    ok, text = mcp(hub, token, "goal_set", title="Nope")
    assert not ok and "owner" in text
    owner.goal_set(title="Visible")
    ok, text = mcp(hub, token, "goal_list")
    assert ok and '"Visible"' in text
