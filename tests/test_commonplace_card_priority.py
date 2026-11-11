"""Card priority: owner-set, always present, and used by every ordered read."""

import sqlite3

import pytest

from commonplace.boards import priority
from commonplace.boards.store import BoardStore
from commonplace.extensions import ToolError
from commonplace.orch import durable
from tests.test_commonplace_boards import (
    api,
    env as env,
    hub_fixture as hub_fixture,
    mcp,
    service,
)
from tests.test_orch_durable import BOARD as ORCH_BOARD, ROLE, Env as OrchEnv, at


@pytest.fixture
def orch_env(hub):
    return OrchEnv(hub)


def test_new_cards_always_get_the_default_priority(hub, env):
    token, agent, _person, cookie, owner = env
    assert owner.task_add("Owner card", board="work")["priority"] == "normal"
    assert service(hub, agent).task_add("Agent card", board="work")["priority"] == "normal"
    status, created = api(hub, token, "POST", "/api/boards/work/cards", {"title": "Over REST"})
    assert status == 201 and created["priority"] == "normal"
    ok, _text = mcp(hub, token, "task_add", title="Over MCP", board="work")
    assert ok
    assert {c["priority"] for c in owner.task_list(board="work", all_statuses=True)} == {"normal"}


def test_owner_sets_priority_on_create_and_update(hub, env):
    _token, _agent, _person, cookie, owner = env
    urgent = owner.task_add("Fire", board="work", priority="urgent")
    assert urgent["priority"] == "urgent" and urgent["can_prioritize"] is True
    plain = owner.task_add("Plain", board="work")
    version = plain["version"]
    updated = owner.task_update(plain["number"], version=version, priority="low")
    assert updated["priority"] == "low" and updated["version"] == version + 1
    shown = owner.task_show(plain["number"])
    assert shown["priority"] == "low"
    assert any(e["text"] == "Priority set to low" for e in shown["events"])
    # The app's REST route takes the same field.
    status, body = api(
        hub,
        cookie,
        "PATCH",
        f"/api/cards/{plain['number']}",
        {"version": updated["version"], "priority": "high"},
    )
    assert status == 200 and body["priority"] == "high"
    status, body = api(
        hub, cookie, "POST", "/api/boards/work/cards", {"title": "Via app", "priority": "urgent"}
    )
    assert status == 201 and body["priority"] == "urgent"


def test_setting_the_same_priority_changes_nothing(hub, env):
    _token, _agent, _person, _cookie, owner = env
    card = owner.task_add("Same", board="work", priority="high")
    again = owner.task_update(card["number"], version=card["version"], priority="high")
    assert again["version"] == card["version"]
    assert len(owner.task_show(card["number"])["events"]) == 1


@pytest.mark.parametrize("bad", ["", "critical", None, 3, ["high"]])
def test_invalid_priority_is_refused(hub, env, bad):
    _token, _agent, _person, _cookie, owner = env
    card = owner.task_add("Bad", board="work")
    with pytest.raises(ToolError, match="priority must be one of"):
        owner.task_update(card["number"], version=card["version"], priority=bad)
    with pytest.raises(ToolError, match="priority must be one of"):
        owner.task_add("Bad new", board="work", priority=bad)
    assert owner.task_show(card["number"])["priority"] == "normal"


def test_non_owner_cannot_set_priority(hub, env):
    token, agent, _person, _cookie, owner = env
    worker = service(hub, agent)
    mine = worker.task_add("Mine", board="work")
    theirs = owner.task_add("Theirs", board="work")
    with pytest.raises(ToolError, match="only a signed-in workspace owner"):
        worker.task_add("Pushy", board="work", priority="urgent")
    with pytest.raises(ToolError, match="only a signed-in workspace owner"):
        worker.task_update(mine["number"], version=mine["version"], priority="urgent")
    with pytest.raises(ToolError, match="only a signed-in workspace owner"):
        worker.task_update(theirs["number"], version=theirs["version"], priority="urgent")
    # A holder cannot raise it on a card it holds, over MCP or REST either.
    assert worker.task_take_next(task=mine["number"])["priority"] == "normal"
    ok, text = mcp(hub, token, "task_update", task=mine["number"], priority="urgent")
    assert not ok and "workspace owner" in text
    ok, text = mcp(hub, token, "task_add", title="Over MCP", board="work", priority="high")
    assert not ok and "workspace owner" in text
    status, body = api(
        hub,
        token,
        "PATCH",
        f"/api/cards/{mine['number']}",
        {"version": owner.task_show(mine["number"])["version"], "priority": "urgent"},
    )
    assert status == 403 and "workspace owner" in body["error"]
    status, _ = api(
        hub, token, "POST", "/api/boards/work/cards", {"title": "Over REST", "priority": "high"}
    )
    assert status == 403
    for number in (mine["number"], theirs["number"]):
        assert owner.task_show(number)["priority"] == "normal"
    assert worker.task_show(mine["number"])["can_prioritize"] is False


def test_task_list_orders_by_priority_then_position(hub, env):
    _token, _agent, _person, _cookie, owner = env
    first = owner.task_add("First normal", board="work")
    low = owner.task_add("Low", board="work", priority="low")
    high_a = owner.task_add("High A", board="work", priority="high")
    urgent = owner.task_add("Urgent", board="work", priority="urgent")
    high_b = owner.task_add("High B", board="work", priority="high")
    order = [c["number"] for c in owner.task_list(board="work")]
    assert order == [
        urgent["number"],
        high_a["number"],
        high_b["number"],
        first["number"],
        low["number"],
    ]
    # Backlog reads order the same way.
    backlog = [
        owner.task_add(title, board="work", status="backlog", priority=name)["number"]
        for title, name in (("b-low", "low"), ("b-urgent", "urgent"), ("b-normal", "normal"))
    ]
    listed = [c["number"] for c in owner.task_list(board="work", status="backlog")]
    assert listed == [backlog[1], backlog[2], backlog[0]]
    # Re-prioritising moves the card without touching position.
    owner.task_update(first["number"], version=first["version"], priority="urgent")
    assert [c["number"] for c in owner.task_list(board="work")][:2] == [
        first["number"],
        urgent["number"],
    ]


def test_task_take_next_takes_the_highest_priority_first(hub, env):
    _token, agent, _person, _cookie, owner = env
    worker = service(hub, agent)
    owner.task_add("Early normal", board="work")
    owner.task_add("Low", board="work", priority="low")
    urgent = owner.task_add("Urgent", board="work", priority="urgent")
    owner.task_add("High", board="work", priority="high")
    assert worker.task_take_next(board="work")["number"] == urgent["number"]


def test_orchestrator_listing_shows_priority(hub, env):
    token, _agent, _person, _cookie, owner = env
    card = owner.task_add("Fire", board="work", priority="urgent")
    ok, text = mcp(hub, token, "task_list", board="work")
    assert ok and f"#{card['number']} todo" in text and "priority:urgent" in text
    ok, text = mcp(hub, token, "task_show", task=card["number"])
    assert ok and "priority:urgent" in text


def test_home_task_table_orders_by_priority_within_need(hub, env):
    _token, _agent, _person, cookie, owner = env
    plain = owner.task_add("Plain", board="work")
    urgent = owner.task_add("Urgent", board="work", priority="urgent")
    status, body = api(hub, cookie, "GET", "/api/cards")
    assert status == 200
    assert [c["number"] for c in body["cards"]] == [urgent["number"], plain["number"]]


def test_checkpoint_lists_unstarted_todo_by_priority(orch_env):
    env = orch_env
    plain = env.card("plain")
    low = env.owner_board.task_add("low", board=ORCH_BOARD, priority="low")["number"]
    urgent = env.owner_board.task_add("urgent", board=ORCH_BOARD, priority="urgent")["number"]
    high = env.owner_board.task_add("high", board=ORCH_BOARD, priority="high")["number"]
    cp = durable.checkpoint(env.ctx(env.a), ORCH_BOARD, ROLE, now=at(10_000))
    assert [c["card"] for c in cp["unstarted"]] == [urgent, high, plain, low]
    assert [c["priority"] for c in cp["unstarted"]] == ["urgent", "high", "normal", "low"]


def test_priority_scale_helpers():
    assert priority.name(0) == "urgent" and priority.name(3) == "low"
    assert priority.name(None) == priority.name(99) == priority.DEFAULT == "normal"
    assert [priority.rank(n) for n in priority.PRIORITIES] == [0, 1, 2, 3]


def test_old_shaped_database_gains_priority_additively(hub, env):
    _token, _agent, _person, _cookie, owner = env
    cards = [owner.task_add(f"Old {i}", board="work") for i in range(3)]
    directory = owner.ctx.workspace_dir()
    with owner.store.transaction() as conn:
        conn.execute("ALTER TABLE tasks DROP COLUMN priority")
        assert "priority" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    before = _rows(owner.store)
    store = BoardStore(directory)
    with store.connection() as conn:
        columns = {r[1]: r for r in conn.execute("PRAGMA table_info(tasks)")}
        assert columns["priority"]["notnull"] == 1
        assert {r["priority"] for r in conn.execute("SELECT priority FROM tasks")} == {2}
    # Only the new column is added: every existing value is untouched.
    assert _rows(store, skip="priority") == before
    # Opening again is a no-op, and the migrated card reads as normal.
    BoardStore(directory)
    assert _rows(store, skip="priority") == before
    assert owner.task_show(cards[0]["number"])["priority"] == "normal"
    migrated = owner.task_update(cards[0]["number"], version=cards[0]["version"], priority="high")
    assert migrated["priority"] == "high"
    # The stored rank is constrained to the scale.
    with store.connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tasks SET priority=9 WHERE id=?", (cards[1]["number"],))


def _rows(store, skip=None):
    with store.connection() as conn:
        return [
            {k: v for k, v in dict(r).items() if k != skip}
            for r in conn.execute("SELECT * FROM tasks ORDER BY id")
        ]
