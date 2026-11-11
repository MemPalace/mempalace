"""Card reviewer: the owner names who reviews a card's completion, and the server enforces it."""

import pytest

from commonplace import sessions
from commonplace.boards import reviewer
from commonplace.extensions import ToolError
from tests._commonplace_helpers import REVIEW_CONTEXT
from tests.test_commonplace_boards import (
    api,
    env as env,
    hub_fixture as hub_fixture,
    mcp,
    service,
)


@pytest.fixture
def members(hub, env):
    """Two more signed-in members of the workspace, with services and REST cookies."""
    result = {}
    for name in ("ada", "bob"):
        email = f"{name}@example.com"
        user = hub.db.create_user(email, name.title())
        hub.db.add_member(hub.workspace, user, "member", actor=hub.admin)
        session = sessions.redeem_login_link(hub.db, sessions.create_login_link(hub.db, email))
        principal = sessions.person_principal(hub.db, user, hub.workspace)
        result[name] = {
            "ref": f"user:{user}",
            "service": service(hub, principal),
            "cookie": {"Cookie": f"{sessions.COOKIE}={session}"},
        }
    return result


def gated_board(owner, on=True):
    with owner.store.transaction() as conn:
        conn.execute("UPDATE boards SET done_needs_review=? WHERE slug='work'", (int(on),))


def agent_card(owner, agent, hub, *, reviewer_ref=None, take=True):
    created = owner.task_add("Ship it", board="work", body=REVIEW_CONTEXT, reviewer=reviewer_ref)
    worker = service(hub, agent)
    if take:
        worker.task_take_next(task=created["number"])
    return created, worker


def test_default_without_reviewer_is_unchanged(hub, env, members):
    _token, agent, person, _cookie, owner = env
    gated_board(owner)
    created, worker = agent_card(owner, agent, hub)
    assert created["reviewer"] is None and created["can_set_reviewer"] is True
    review = worker.task_update(created["number"], status="done")
    assert review["status"] == "review" and review["review_kind"] == "completion"
    assert review["ask_to"] == f"user:{person.user_id}" and review["reviewer"] is None
    # Today only an owner answers; a plain member still cannot.
    with pytest.raises(ToolError, match="signed-in workspace owner"):
        members["ada"]["service"].decide(review["number"], choice="Accept the completed work")
    done = owner.decide(review["number"], choice="Accept the completed work", then="done")
    assert done["status"] == "done"


def test_no_policy_default_a_board_without_review_still_finishes_directly(hub, env):
    _token, agent, _person, _cookie, owner = env
    created, worker = agent_card(owner, agent, hub)
    assert worker.task_update(created["number"], status="done")["status"] == "done"


def test_owner_chooses_reviewer_on_create_and_update(hub, env, members):
    _token, _agent, _person, cookie, owner = env
    ada, bob = members["ada"], members["bob"]
    card = owner.task_add("With reviewer", board="work", reviewer=ada["ref"])
    assert card["reviewer"] == ada["ref"]
    changed = owner.task_update(card["number"], version=card["version"], reviewer=bob["ref"])
    assert changed["reviewer"] == bob["ref"] and changed["version"] == card["version"] + 1
    shown = owner.task_show(card["number"])
    assert any(e["text"] == "Reviewer set to Bob" for e in shown["events"])
    status, body = api(
        hub,
        cookie,
        "PATCH",
        f"/api/cards/{card['number']}",
        {"version": changed["version"], "reviewer": ada["ref"]},
    )
    assert status == 200 and body["reviewer"] == ada["ref"]
    status, body = api(
        hub, cookie, "POST", "/api/boards/work/cards", {"title": "REST", "reviewer": bob["ref"]}
    )
    assert status == 201 and body["reviewer"] == bob["ref"]
    current = owner.task_show(card["number"])
    cleared = owner.task_update(card["number"], version=current["version"], reviewer=None)
    assert cleared["reviewer"] is None
    assert any(e["text"] == "Reviewer cleared" for e in owner.task_show(card["number"])["events"])


def test_setting_the_same_reviewer_changes_nothing(hub, env, members):
    _token, _agent, _person, _cookie, owner = env
    card = owner.task_add("Same", board="work", reviewer=members["ada"]["ref"])
    again = owner.task_update(
        card["number"], version=card["version"], reviewer=members["ada"]["ref"]
    )
    assert again["version"] == card["version"]


@pytest.mark.parametrize("bad", ["ada", "user:nobody", "conn:abc", 7, ["x"]])
def test_reviewer_must_be_a_workspace_member(hub, env, bad):
    _token, _agent, _person, _cookie, owner = env
    card = owner.task_add("Bad", board="work")
    with pytest.raises(ToolError, match="must be a member"):
        owner.task_update(card["number"], version=card["version"], reviewer=bad)
    with pytest.raises(ToolError, match="must be a member"):
        owner.task_add("Bad new", board="work", reviewer=bad)
    assert owner.task_show(card["number"])["reviewer"] is None


def test_only_the_owner_chooses_a_reviewer(hub, env, members):
    token, agent, _person, _cookie, owner = env
    ada = members["ada"]
    mine = service(hub, agent).task_add("Mine", board="work")
    theirs = owner.task_add("Theirs", board="work")
    for who in (service(hub, agent), ada["service"]):
        with pytest.raises(ToolError, match="signed-in workspace owner"):
            who.task_add("Pushy", board="work", reviewer=ada["ref"])
        for card in (mine, theirs):
            with pytest.raises(ToolError, match="signed-in workspace owner"):
                who.task_update(card["number"], version=card["version"], reviewer=ada["ref"])
    ok, text = mcp(hub, token, "task_update", task=mine["number"], reviewer=ada["ref"])
    assert not ok and "workspace owner" in text
    status, body = api(
        hub,
        ada["cookie"],
        "PATCH",
        f"/api/cards/{theirs['number']}",
        {"version": theirs["version"], "reviewer": ada["ref"]},
    )
    assert status == 403 and "workspace owner" in body["error"]
    assert owner.task_show(theirs["number"])["reviewer"] is None
    assert ada["service"].task_show(theirs["number"])["can_set_reviewer"] is False


def test_completion_review_goes_to_the_chosen_reviewer_only(hub, env, members):
    token, agent, person, _cookie, owner = env
    ada, bob = members["ada"], members["bob"]
    gated_board(owner)
    created, worker = agent_card(owner, agent, hub, reviewer_ref=ada["ref"])
    review = worker.task_update(created["number"], status="done")
    assert review["status"] == "review" and review["ask_to"] == ada["ref"]
    assert review["reviewer"] == ada["ref"] and review["review_kind"] == "completion"
    # The owner who set it up, another member and the agent cannot answer.
    for other in (owner, bob["service"]):
        with pytest.raises(ToolError, match="only the chosen reviewer"):
            other.decide(review["number"], choice="Accept the completed work", then="done")
    with pytest.raises(ToolError):
        worker.decide(review["number"], choice="Accept the completed work", then="done")
    status, body = api(
        hub,
        bob["cookie"],
        "POST",
        f"/api/cards/{review['number']}/decide",
        {"version": review["version"], "choice": "Accept the completed work", "then": "done"},
    )
    assert status == 403 and "chosen reviewer" in body["error"]
    assert owner.task_show(review["number"])["status"] == "review"
    done = ada["service"].decide(review["number"], choice="Accept the completed work", then="done")
    assert done["status"] == "done" and done["ask_to"] is None


def test_chosen_reviewer_applies_even_on_a_board_without_review_gate(hub, env, members):
    _token, agent, _person, _cookie, owner = env
    created, worker = agent_card(owner, agent, hub, reviewer_ref=members["ada"]["ref"])
    review = worker.task_update(created["number"], status="done")
    assert review["status"] == "review" and review["ask_to"] == members["ada"]["ref"]


def test_assignee_cannot_review_own_work_unless_owner_chose_them(hub, env, members):
    _token, _agent, _person, _cookie, owner = env
    ada, bob = members["ada"], members["bob"]
    card = owner.task_add("Ada builds", board="work", body=REVIEW_CONTEXT, reviewer=bob["ref"])
    ada["service"].task_update(card["number"], version=card["version"], status="doing")
    review = ada["service"].task_update(card["number"], status="done")
    assert review["status"] == "review" and review["ask_to"] == bob["ref"]
    assert review["return_to"] == ada["ref"]
    with pytest.raises(ToolError, match="only the chosen reviewer"):
        ada["service"].decide(review["number"], choice="Accept the completed work", then="done")
    # The owner explicitly naming the assignee is the one way to self-review.
    own = owner.task_add(
        "Bob builds and reviews", board="work", body=REVIEW_CONTEXT, reviewer=bob["ref"]
    )
    bob["service"].task_update(own["number"], version=own["version"], status="doing")
    assert bob["service"].task_update(own["number"], status="done")["status"] == "done"


def test_a_person_without_a_reviewer_still_finishes_directly(hub, env, members):
    _token, _agent, _person, _cookie, owner = env
    card = owner.task_add("Plain", board="work")
    members["ada"]["service"].task_update(card["number"], version=card["version"], status="doing")
    assert members["ada"]["service"].task_update(card["number"], status="done")["status"] == "done"


def test_owner_cannot_skip_the_chosen_reviewer_by_moving_to_done(hub, env, members):
    _token, _agent, _person, _cookie, owner = env
    card = owner.task_add(
        "Owner moves", board="work", body=REVIEW_CONTEXT, reviewer=members["ada"]["ref"]
    )
    moved = owner.task_update(card["number"], version=card["version"], status="doing")
    review = owner.task_update(moved["number"], status="done")
    assert review["status"] == "review" and review["ask_to"] == members["ada"]["ref"]


def test_changing_the_reviewer_readdresses_an_open_completion_review(hub, env, members):
    _token, agent, person, _cookie, owner = env
    ada, bob = members["ada"], members["bob"]
    created, worker = agent_card(owner, agent, hub, reviewer_ref=ada["ref"])
    review = worker.task_update(created["number"], status="done")
    moved = owner.task_update(review["number"], version=review["version"], reviewer=bob["ref"])
    assert moved["status"] == "review" and moved["ask_to"] == bob["ref"]
    with pytest.raises(ToolError, match="only the chosen reviewer"):
        ada["service"].decide(review["number"], choice="Accept the completed work", then="done")
    back = owner.task_update(review["number"], version=moved["version"], reviewer=None)
    assert back["ask_to"] == f"user:{person.user_id}" and back["reviewer"] is None
    done = owner.decide(review["number"], choice="Accept the completed work", then="done")
    assert done["status"] == "done"


def test_needs_me_shows_the_review_to_the_chosen_reviewer_only(hub, env, members):
    _token, agent, _person, _cookie, owner = env
    ada, bob = members["ada"], members["bob"]
    created, worker = agent_card(owner, agent, hub, reviewer_ref=ada["ref"])
    number = worker.task_update(created["number"], status="done")["number"]
    waiting = ada["service"].needs_me()
    assert [c["number"] for c in waiting["done_review"]] == [number]
    assert owner.needs_me()["done_review"] == []
    with pytest.raises(ToolError):
        bob["service"].needs_me()


def test_reviewer_who_left_the_workspace_falls_back_to_the_owner():
    row = {"reviewer": "user:gone"}
    assert reviewer.effective(row, [{"id": "kept"}]) is None
    assert reviewer.effective(row, [{"id": "gone"}]) == "user:gone"
    assert not reviewer.gates_done(row, [{"id": "kept"}], "user:kept")
