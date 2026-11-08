"""A1–A6, A10, A11: automatic project seats and owner Connect contracts."""

import json

import pytest

from commonplace import events
from tests._projects_helpers import projects_hub_fixture  # noqa: F401


def test_new_project_has_one_main_orchestrator(projects_hub):
    h = projects_hub
    created = h.create_board(h.owner, "alpha")
    before = [s for s in h.seats() if s["board"] == "alpha"]
    status, body = h.project(h.owner, "alpha")
    assert status == 200  # RED
    assert created[0] == 201
    assert len(before) == 1  # Created by the event, not the lazy GET.
    assert body["main"] == {
        "role": "main",
        "name": "orchestrator:alpha",
        "source": "seat",
        "connected": False,
        "status": None,
        "wakeable": False,
        "connectable": True,
        "seen": False,
    }
    assert h.coordinator("alpha") is None
    emitted = events.after(h.db, h.workspace, 0, h.seq())
    event = next(
        e for e in emitted if e["kind"] == "space_created" and e["data"]["board"] == "alpha"
    )
    h.deliver(event)
    assert len([s for s in h.seats() if s["board"] == "alpha"]) == 1
    assert h.project(h.member, "alpha")[1]["main"]["connectable"] is False


@pytest.mark.parametrize("path", ["mcp_board_add", "implicit_task_add"])
def test_every_creation_path_gets_a_seat(projects_hub, path):
    h = projects_hub
    tool = "board_add" if path == "mcp_board_add" else "task_add"
    args = (
        {"slug": "beta", "wing": "beta"}
        if path == "mcp_board_add"
        else {"board": "beta", "title": "First task"}
    )
    created = h.mcp(h.a1, tool, **args)
    before = [s for s in h.seats() if s["board"] == "beta"]
    status, body = h.project(h.owner, "beta")
    assert status == 200  # RED
    assert created[0] == 200 and created[1]["ok"]
    assert len(before) == 1
    assert body["main"]["source"] == "seat"
    assert len([s for s in h.seats() if s["board"] == "beta"]) == 1


@pytest.mark.parametrize("mode", ["lazy", "adopt"])
def test_existing_board_seat_is_lazy_and_adopts_coordinator(projects_hub, mode):
    h = projects_hub
    h.delete_seat("work")
    configured = None
    if mode == "adopt":
        configured = h.request(h.owner, "PATCH", "/api/boards/work", {"coordinator": "conn:a1"})
    status, body = h.project(h.owner, "work")
    assert status == 200  # RED
    assert len([s for s in h.seats() if s["board"] == "work"]) == 1
    if mode == "adopt":
        assert configured[0] == 200
        assert body["main"]["source"] == "coordinator"
        assert body["main"]["name"] == "a1"
        assert body["main"]["connected"] is True
    else:
        assert body["main"]["source"] == "seat"
        assert body["main"]["connected"] is False
        assert h.coordinator("work") is None


def test_connect_mints_scoped_token_once_and_sets_coordinator(projects_hub):
    h = projects_hub
    created = [h.create_board(h.owner, slug) for slug in ("alpha", "beta")]
    status, body = h.connect(h.owner, "alpha")
    assert status == 201  # RED
    assert all(r[0] == 201 for r in created)
    token, connection = body["token"], body["connection"]
    assert body["mcp_url"].endswith("/mcp") and connection["name"] == "orchestrator:alpha"
    assert body["board"] == "alpha" and body["wing"] == "alpha"
    assert body["setup"]["webhook_add"] == {
        "events": ["space_message", "card.moved"],
        "board": "alpha",
    }
    assert h.coordinator("alpha") == "conn:orchestrator:alpha"
    principal = h.gateway.authenticate("Bearer " + token)
    assert principal.policy.trust == "standard" and principal.policy.tools == "standard"
    assert h.mcp(token, "space_read", board="alpha")[1]["ok"]
    assert h.mcp(token, "space_post", board="alpha", text="Agent reply")[1]["ok"]
    denied = h.mcp(token, "space_read", board="beta")[1]
    assert denied["ok"] is False and "board not found" in denied["text"].lower()
    assert h.mcp(token, "board_add", slug="zz")[1]["ok"] is False
    view = h.project(h.owner, "alpha")
    assert view[1]["main"]["connected"] is True and view[1]["main"]["seen"] is True
    assert token not in json.dumps(view[1]) and "token" not in view[1]
    listed = h.get(h.owner, "/api/connections")
    assert listed[0] == 200
    assert any(
        c["id"] == connection["id"] and c["client"] == "commonplace-orchestrator"
        for c in listed[1]["connections"]
    )
    rotated_status, rotated = h.connect(h.owner, "alpha")
    assert rotated_status == 200 and rotated["connection"]["id"] == connection["id"]
    assert rotated["token"] != token
    assert h.mcp(token, "space_read", board="alpha")[0] == 401
    assert h.mcp(rotated["token"], "space_read", board="alpha")[1]["ok"]


def test_connect_permissions_and_replace(projects_hub):
    h = projects_hub
    created = h.create_board(h.owner, "alpha")
    status, _ = h.connect(h.member, "alpha")
    assert status == 403  # RED
    assert created[0] == 201
    assert h.connect(h.a1, "alpha")[0] == 403
    assert h.connect(h.other, "alpha")[0] == 404
    assert h.connect(None, "alpha")[0] == 401
    assert h.request(h.owner, "PATCH", "/api/boards/work", {"coordinator": "conn:a1"})[0] == 200
    assert h.connect(h.owner, "work")[0] == 409
    assert h.connect(h.owner, "work", {"replace": True})[0] == 201
    assert h.coordinator("work") == "conn:orchestrator:work"
    assert h.create_board(h.owner, "shared-board", wing="shared")[0] == 201
    code, shared = h.connect(h.owner, "shared-board")
    assert code == 409 and shared["error"] == "shared_wing"
    assert (
        shared["message"]
        == "This project uses the shared memory wing. Connecting gives the agent read and write access to all shared memory."
    )
    assert h.connect(h.owner, "shared-board", {"allow_shared": True})[0] == 201
    assert h.connect(h.owner, "alpha", {"allow_shared": "yes"})[0] == 400
    assert h.connect(h.owner, "alpha", {"unknown": True})[0] == 400


def test_chat_reaches_connected_main_orchestrator(projects_hub):
    h = projects_hub
    status, body = h.connect(h.owner, "work")
    assert status == 201  # RED
    token = body["token"]
    assert h.mcp(
        token, "webhook_add", url="http://127.0.0.1:9/hook", events=["space_message"], board="work"
    )[1]["ok"]
    text = "Please check this exact text: café —  two spaces."
    posted = h.request(h.owner, "POST", "/api/boards/work/chat", {"text": text})
    assert posted[0] == 201
    page = h.get(h.owner, "/api/chat/work")
    assert page[0] == 200
    receipt = next(
        r
        for r in page[1]["replies"]
        if r["message_id"] == posted[1]["id"] and r["kind"] == "receipt"
    )
    assert receipt["code"] == "forwarded" and "orchestrator:work" in receipt["text"]
    response = h.request(token, "GET", f"/api/boards/work/chat?after={posted[1]['id'] - 1}")
    assert response[0] == 200 and response[1]["messages"][0]["text"] == text
    assert h.mcp(token, "space_read", board="work", after=posted[1]["id"] - 1)[1]["ok"]


def test_unseen_seat_keeps_owner_release(projects_hub):
    h = projects_hub
    created = h.create_board(h.owner, "alpha")
    status, body = h.connect(h.owner, "alpha")
    assert status == 201  # RED
    assert created[0] == 201
    view = h.project(h.owner, "alpha")[1]["main"]
    assert view["connected"] is True and view["seen"] is False
    proposed_status, proposal = h.propose("alpha")
    assert proposed_status == 201
    code, queue, _ = h.get(h.owner, "/api/needs-me")
    assert code == 200
    assert [c["number"] for c in queue["approvals"]] == [proposal["number"]]
    origin = queue["approvals"][0]
    assert origin["approval_kind"] == "origin"
    assert origin["approval_role"] == "workspace owner"
    assert origin["need"] == "release"
    assert origin["can_approve"] is True and origin["actions"] == ["approve"]
    assert queue["backlog_from_agents"] == []
    assert h.mcp(body["token"], "space_read", board="alpha")[1]["ok"]
    code, queue, _ = h.get(h.owner, "/api/needs-me")
    assert code == 200 and queue["approvals"] == []
    assert queue["backlog_from_agents"] == []
    assert h.project(h.owner, "alpha")[1]["main"]["seen"] is True


def test_reconnect_after_revoke_mints_fresh_connection(projects_hub):
    h = projects_hub
    created = h.create_board(h.owner, "alpha")
    status, first = h.connect(h.owner, "alpha")
    assert status == 201  # RED
    assert created[0] == 201
    assert h.request(h.owner, "DELETE", "/api/connections/" + first["connection"]["id"])[0] == 200
    assert h.project(h.owner, "alpha")[1]["main"]["connected"] is False
    status, second = h.connect(h.owner, "alpha")
    assert status == 201
    assert second["connection"]["id"] != first["connection"]["id"]
    assert second["connection"]["name"] != first["connection"]["name"]
    assert h.project(h.owner, "alpha")[1]["main"]["connected"] is True
    rows = h.get(h.owner, "/api/connections")[1]["connections"]
    live = [
        c for c in rows if c["client"] == "commonplace-orchestrator" and c["revoked_at"] is None
    ]
    assert len(live) == 1 and live[0]["id"] == second["connection"]["id"]
    assert h.coordinator("alpha") == "conn:" + second["connection"]["name"]
