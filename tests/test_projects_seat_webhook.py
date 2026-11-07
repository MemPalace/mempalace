"""Replay worker completion through the project seat's recommended subscription."""

import pytest

from commonplace import events, webhooks
from commonplace.boards.service import BoardService, needs_me
from commonplace.extensions import ToolContext
from tests._projects_helpers import projects_hub_fixture  # noqa: F401


@pytest.mark.parametrize("orchestration_enabled", [False, True], ids=["off", "on"])
def test_worker_done_reaches_recommended_seat_webhook(projects_hub, orchestration_enabled):
    h = projects_hub
    if orchestration_enabled:
        assert h.request(h.owner, "PUT", "/api/settings/orchestration", {"enabled": True})[0] == 200
    status, connected = h.connect(h.owner, "work")
    assert status == 201
    token = connected["token"]
    setup = connected["setup"]["webhook_add"]
    if orchestration_enabled:
        assert "card.moved" in setup["events"], "Orchestration must retain completion notifications"
    assert h.mcp(token, "webhook_add", url="http://127.0.0.1:9/hook", **setup)[1]["ok"]
    with h.db._lock:
        subscription = dict(
            h.db._conn.execute(
                "SELECT workspace_id, kinds, board, waiting_on, owner_actor FROM webhooks "
                "WHERE connection_id=?",
                (connected["connection"]["id"],),
            ).fetchone()
        )
    orchestrator = BoardService(
        ToolContext(principal=h.gateway.authenticate("Bearer " + token), gateway=h.gateway)
    )
    worker = BoardService(
        ToolContext(principal=h.gateway.authenticate("Bearer " + h.a1), gateway=h.gateway)
    )
    before_creation = h.seq()
    card = orchestrator.task_add(board="work", title="Some work")
    created = next(
        e
        for e in events.after(h.db, h.workspace, before_creation, h.seq())
        if e["kind"] == "card.created" and e["data"]["number"] == card["number"]
    )
    assert webhooks._matches(subscription, created) is orchestration_enabled
    worker.task_take_next(task=card["number"])
    before = h.seq()
    worker.task_update(card["number"], status="done")
    moved = next(
        e
        for e in events.after(h.db, h.workspace, before, h.seq())
        if e["kind"] == "card.moved" and e["data"]["number"] == card["number"]
    )
    assert moved["data"]["to"] == "done"
    assert webhooks._matches(subscription, moved), "Worker completion must wake its orchestrator"
    groups = needs_me(orchestrator.store, orchestrator.principal)
    assert card["number"] not in {
        c["number"] for key, cards in groups.items() if key != "count" for c in cards
    }
    assert not webhooks._matches(subscription, {**moved, "workspace_id": "other-workspace"})
    assert not webhooks._matches(
        subscription, {**moved, "data": {**moved["data"], "board": "another-board"}}
    )
    assert h.request(h.owner, "POST", "/api/boards/work/chat", {"text": "Please check"})[0] == 201
    message = next(
        e for e in events.after(h.db, h.workspace, before, h.seq()) if e["kind"] == "space_message"
    )
    assert webhooks._matches(subscription, message)
    assert not webhooks._matches(subscription, {**message, "kind": "space_replied"})


@pytest.mark.parametrize("orchestration_enabled", [False, True], ids=["off", "on"])
def test_reconnect_leaves_existing_webhook_subscription_unchanged(
    projects_hub, orchestration_enabled
):
    h = projects_hub
    if orchestration_enabled:
        assert h.request(h.owner, "PUT", "/api/settings/orchestration", {"enabled": True})[0] == 200
    status, connected = h.connect(h.owner, "work")
    assert status == 201
    principal = h.gateway.authenticate("Bearer " + connected["token"])
    webhooks.add(h.db, principal, "http://127.0.0.1:9/hook", kinds=["space_message"], board="work")
    before = webhooks.list_webhooks(h.db, principal)
    status, reconnected = h.connect(h.owner, "work")
    assert status == 200
    assert reconnected["connection"]["id"] == connected["connection"]["id"]
    assert webhooks.list_webhooks(h.db, principal) == before
    assert before[0]["kinds"] == ["space_message"]
