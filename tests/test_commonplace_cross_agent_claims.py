"""Fresh agent tokens distinguish available work from reservations and approvals."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import sessions
from commonplace.cli import main
from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    REVIEW_OPTIONS,
    REVIEW_QUESTION,
    hub_fixture,  # noqa: F401
)
from tests.test_commonplace_boards import api, mcp


@pytest.fixture
def fresh(hub, capsys):
    email = "new-owner@example.test"
    user = hub.db.create_user(email)
    workspace = hub.db.create_workspace("new-workspace", user, personal=True)
    assert not hub.db.user(user)["is_admin"]

    def connect(name, *, client="codex", parent=None, trust=None):
        args = [
            "--data",
            str(Path(hub.db.path).parent),
            "token",
            "add",
            name,
            "--workspace",
            workspace,
            "--client",
            client,
            "--by",
            email,
        ]
        if parent:
            args.extend(["--parent", parent])
        if trust:
            args.extend(["--trust", trust])
        assert main(args) == 0
        token = capsys.readouterr().out.strip()
        principal = hub.gateway.authenticate(f"Bearer {token}")
        assert principal.user_id == user and principal.workspace_id == workspace
        assert not principal.person and not principal.policy.has_scope("admin")
        return SimpleNamespace(token=token, principal=principal)

    first = connect("agent-a")
    second = connect("agent-b", client="claude-code")
    assert first.token != second.token and first.principal.actor != second.principal.actor
    for agent in (first, second):
        assert agent.principal.policy.trust == "trusted"
        assert agent.principal.parent_agent is None
    status, payload = api(hub, first.token, "GET", "/api/boards")
    assert status == 200 and payload["boards"] == []
    assert mcp(hub, first.token, "board_add", slug="work")[0]
    login = sessions.create_login_link(hub.db, email, workspace)
    cookie = {"Cookie": f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, login)}"}
    return SimpleNamespace(first=first, second=second, connect=connect, cookie=cookie, user=user)


def show(hub, agent, number):
    status, card = api(hub, agent.token, "GET", f"/api/cards/{number}")
    assert status == 200, card
    return card


def add(hub, agent, **args):
    ok, reply = mcp(hub, agent.token, "task_add", board="work", title="Ordinary work", **args)
    assert ok, reply
    return show(hub, agent, int(re.search(r"#(\d+)", reply)[1]))


def refused_claims(hub, agent, card):
    number = card["number"]
    before = show(hub, agent, number)
    for tool, args in (
        ("task_take_next", {"task": number}),
        ("task_update", {"task": number, "status": "doing"}),
    ):
        ok, reply = mcp(hub, agent.token, tool, **args)
        assert not ok, reply
        assert show(hub, agent, number) == before
    ok, reply = mcp(hub, agent.token, "task_take_next", board="work")
    assert ok and "nothing to take" in reply, reply
    assert show(hub, agent, number) == before


@pytest.mark.parametrize("creation", [{}, {"status": "todo", "assignee": "anyone"}])
@pytest.mark.parametrize("claim", ["explicit", "next", "update"])
def test_trusted_top_level_agent_claims_another_agents_available_todo(hub, fresh, creation, claim):
    created = add(hub, fresh.first, **creation)
    number = created["number"]
    assert created["status"] == "todo" and created["assignee"] is None
    assert created["created_by"] == fresh.first.principal.actor
    assert created["proposal_status"] is None and created["claim_until"] is None
    if claim == "update":
        ok, reply = mcp(hub, fresh.second.token, "task_update", task=number, status="doing")
    else:
        args = {"task": number} if claim == "explicit" else {"board": "work"}
        ok, reply = mcp(hub, fresh.second.token, "task_take_next", **args)
    assert ok and f"#{number}" in reply, reply
    taken = show(hub, fresh.second, number)
    assert taken["status"] == "doing" and taken["assignee"] == fresh.second.principal.actor
    assert taken["created_by"] == fresh.first.principal.actor and taken["claim_until"]
    assert taken["events"][-1]["actor"] == fresh.second.principal.actor
    assert taken["events"][-1]["kind"] == ("move" if claim == "update" else "claim")
    refused_claims(hub, fresh.first, taken)


def test_explicit_reservation_is_for_the_assignee_even_when_the_creator_is_another_agent(
    hub, fresh
):
    created = add(hub, fresh.first, assignee=fresh.second.principal.actor)
    assert created["status"] == "todo" and created["assignee"] == fresh.second.principal.actor
    refused_claims(hub, fresh.first, created)
    assert mcp(hub, fresh.second.token, "task_take_next", task=created["number"])[0]


@pytest.mark.parametrize("creation", [{"claim": True}, {"status": "doing"}, {"assignee": "self"}])
def test_create_and_claim_preserves_the_creators_ownership(hub, fresh, creation):
    created = add(hub, fresh.first, **creation)
    assert created["status"] == "doing" and created["assignee"] == fresh.first.principal.actor
    assert created["claim_until"]
    refused_claims(hub, fresh.second, created)
    assert mcp(hub, fresh.first.token, "task_heartbeat", task=created["number"])[0]
    # An explicit handoff to self retains the reservation in todo.
    assert mcp(
        hub,
        fresh.first.token,
        "task_handoff",
        task=created["number"],
        to="self",
        note="Keep this work reserved for its creator.",
    )[0]
    reserved = show(hub, fresh.first, created["number"])
    assert reserved["status"] == "todo" and reserved["assignee"] == fresh.first.principal.actor
    refused_claims(hub, fresh.second, reserved)
    assert mcp(hub, fresh.first.token, "task_take_next", task=created["number"])[0]


@pytest.mark.parametrize("trust", [None, "trusted"])
def test_default_inbox_reservation_requires_owner_release_for_other_agents(hub, fresh, trust):
    creator = fresh.connect("inbox-agent", client="chatgpt", trust=trust)
    created = add(hub, creator)
    number = created["number"]
    assert created["status"] == "todo" and created["assignee"] == creator.principal.actor
    refused_claims(hub, fresh.second, created)
    assert mcp(hub, creator.token, "task_take_next", task=number)[0]
    assert mcp(
        hub, creator.token, "task_handoff", task=number, to="anyone", note="Ready for shared work."
    )[0]
    released = show(hub, creator, number)
    assert released["status"] == "todo" and released["assignee"] is None
    refused_claims(hub, fresh.second, released)
    status, approved = api(
        hub,
        fresh.cookie,
        "POST",
        f"/api/cards/{number}/handoff",
        {"version": released["version"], "to": "anyone", "note": "Approved for shared work."},
    )
    assert status == 200 and approved["approved_by"] == f"user:{fresh.user}"
    assert mcp(hub, fresh.second.token, "task_take_next", task=number)[0]


@pytest.mark.parametrize("protection", ["person-reservation", "person-review"])
def test_person_reserved_work_and_reviews_are_not_available(hub, fresh, protection):
    if protection == "person-reservation":
        created = add(hub, fresh.first, assignee=f"user:{fresh.user}")
    else:
        created = add(hub, fresh.first)
        assert mcp(
            hub,
            fresh.first.token,
            "ask",
            task=created["number"],
            question=REVIEW_QUESTION,
            options=REVIEW_OPTIONS,
            context=REVIEW_CONTEXT,
        )[0]
        created = show(hub, fresh.first, created["number"])
        assert created["status"] == "review" and created["ask"] == REVIEW_QUESTION
    refused_claims(hub, fresh.second, created)
    refused_claims(hub, fresh.first, created)


@pytest.mark.parametrize("reject", [False, True], ids=["pending", "rejected"])
def test_child_proposals_require_the_direct_parents_approval(hub, fresh, reject):
    child = fresh.connect("child", parent=fresh.first.principal.connection_id)
    proposed = add(hub, child, claim=True)
    number = proposed["number"]
    assert proposed["status"] == "backlog" and proposed["proposal_status"] == "pending"
    assert proposed["proposed_to"] == fresh.first.principal.actor
    assert proposed["assignee"] == child.principal.actor and proposed["claim_until"] is None
    for agent in (fresh.second, child):
        assert not mcp(hub, agent.token, "task_approve", task=number)[0]
        assert show(hub, child, number) == proposed
    if reject:
        assert mcp(hub, fresh.first.token, "task_approve", task=number, approve=False)[0]
        proposed = show(hub, child, number)
        assert proposed["proposal_status"] == "rejected"
    for agent in (fresh.first, fresh.second, child):
        refused_claims(hub, agent, proposed)
    if not reject:
        assert mcp(hub, fresh.first.token, "task_approve", task=number)[0]
        approved = show(hub, child, number)
        assert approved["proposal_status"] == "approved"
        assert approved["approved_by"] == fresh.first.principal.actor
        refused_claims(hub, fresh.second, approved)
        assert mcp(hub, child.token, "task_take_next", task=number)[0]
