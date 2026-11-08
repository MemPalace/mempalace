"""Owner coordinator settings and missing-coordinator notices."""

import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from commonplace import sessions
from commonplace.chat.status import snapshot
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api, service


def settings(hub, fresh):
    return api(hub, fresh.cookie, "GET", "/api/boards/work/coordinator")


def test_owner_lists_eligible_candidates(hub, fresh):
    eligible = fresh.connect("eligible")
    fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    fresh.connect("readonly", scopes=["memory:read"])
    fresh.connect("hidden", read_wings=["elsewhere"], write_wing="elsewhere")
    revoked = fresh.connect("revoked")
    hub.db.revoke(revoked.id, actor=fresh.user, workspace_ref=fresh.workspace)
    hub.owner_token("foreign")
    status, result = settings(hub, fresh)
    assert status == 200, result
    assert result == {
        "board": "work",
        "coordinator": None,
        "candidates": [{"name": "eligible", "actor": eligible.principal.actor, "current": False}],
    }


@pytest.mark.parametrize("person", [False, True])
def test_non_owner_is_refused(hub, fresh, person):
    user = hub.db.create_user("member@example.test")
    hub.db.add_member(fresh.workspace, user, "member", actor=fresh.user)
    member = fresh.connect("member", user_ref=user)
    auth = member.token
    if person:
        cookie = sessions.redeem_login_link(
            hub.db, sessions.create_login_link(hub.db, "member@example.test", fresh.workspace)
        )
        auth = {"Cookie": f"{sessions.COOKIE}={cookie}"}
    for method, path, body in (
        ("GET", "/api/boards/work/coordinator", None),
        ("PATCH", "/api/boards/work", {"coordinator": None}),
    ):
        status, result = api(hub, auth, method, path, body)
        assert status == 403, result
        assert result["error"] == "only a workspace owner can do that"


def test_owner_sets_and_clears_coordinator(hub, fresh):
    agent = fresh.connect("coordinator")
    for actor in (agent.principal.actor, None):
        status, result = api(hub, fresh.cookie, "PATCH", "/api/boards/work", {"coordinator": actor})
        assert status == 200, result
        status, result = settings(hub, fresh)
        assert status == 200, result
        assert result["coordinator"] == actor
        assert result["candidates"][0]["current"] is (actor is not None)


def test_needs_me_coordinator_waiting_bucket_is_owner_only(hub, fresh):
    assert fresh.owner.needs_me()["coordinator_waiting"] == []
    user = hub.db.create_user("member@example.test")
    hub.db.add_member(fresh.workspace, user, "member", actor=fresh.user)
    member = fresh.connect("member", user_ref=user)
    assert "coordinator_waiting" not in member.work.needs_me()


@pytest.mark.parametrize("inactive", [False, True])
def test_waiting_notice_requires_missing_live_coordinator(hub, fresh, inactive):
    assert fresh.owner.needs_me().get("coordinator_waiting", []) == []
    creator = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    coordinator = fresh.connect("coordinator")
    for title in ("First proposed work", "Second proposed work"):
        creator.work.task_add(board="work", title=title)
    if inactive:
        fresh.owner.board_set("work", coordinator.principal.actor)
        hub.db.revoke(coordinator.id, actor=fresh.user, workspace_ref=fresh.workspace)
    notices = fresh.owner.needs_me().get("coordinator_waiting", [])
    assert notices == [
        {
            "board": "work",
            "count": 2,
            "message": "2 restricted-origin cards need owner recovery (2 release, 0 configuration); review these cards in Needs me.",
        }
    ]
    live = fresh.connect("live")
    fresh.owner.board_set("work", live.principal.actor)
    assert fresh.owner.needs_me().get("coordinator_waiting") == []


def test_released_cards_do_not_wait_for_coordinator(fresh):
    parent = fresh.connect("parent")
    creator = fresh.connect("restricted", parent=parent.id, trust="untrusted", write_wing="inbox")
    proposal = creator.work.task_add(board="work", title="Released work")
    released = fresh.owner.task_approve(proposal["number"], reason="Owner released work")
    assert released["proposal_status"] == "pending"
    assert proposal["number"] in {c["number"] for c in parent.work.needs_me()["approvals"]}
    assert fresh.owner.needs_me().get("coordinator_waiting") == []


@pytest.mark.parametrize("kind", ["expired", "disabled", "removed", "readonly", "hidden"])
def test_ineligible_coordinator_is_neither_listed_nor_settable(hub, fresh, kind):
    user = hub.db.create_user("agent-owner@example.test")
    hub.db.add_member(fresh.workspace, user, "owner", actor=fresh.user)
    args = {}
    if kind == "readonly":
        args["scopes"] = ["memory:read"]
    elif kind == "hidden":
        args.update(read_wings=["elsewhere"], write_wing="elsewhere")
    agent = fresh.connect("candidate", user_ref=user, **args)
    if kind == "expired":
        hub.db._conn.execute(
            "UPDATE connections SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (agent.id,)
        )
    elif kind == "disabled":
        hub.db._conn.execute("UPDATE users SET disabled_at='2000' WHERE id=?", (user,))
    elif kind == "removed":
        # Simulate a legacy missing-membership row without relaxing live constraints.
        with closing(sqlite3.connect(hub.db.path)) as legacy:
            legacy.execute(
                "DELETE FROM memberships WHERE workspace_id=? AND user_id=?",
                (fresh.workspace, user),
            )
            legacy.commit()
    status, result = settings(hub, fresh)
    assert status == 200, result
    assert result["candidates"] == []
    status, result = api(
        hub, fresh.cookie, "PATCH", "/api/boards/work", {"coordinator": agent.principal.actor}
    )
    assert status == 403, result


def test_settings_respect_tool_visibility(hub, fresh):
    agent = fresh.connect("limited", tools=["space_read", "space_post"])
    for method, path, body in (
        ("GET", "/api/boards/work/coordinator", None),
        ("PATCH", "/api/boards/work", {"coordinator": None}),
    ):
        status, result = api(hub, agent.token, method, path, body)
        assert status == 403, result
        assert result["error"] == "tool not allowed for this connection"


def test_workspaces_keep_settings_and_waiting_separate(hub, fresh):
    foreign = service(hub, sessions.person_principal(hub.db, hub.admin, hub.workspace))
    foreign.board_add(slug="work", wing="shared")
    foreign_agent = fresh.connect("coordinator", user_ref=hub.admin, workspace_ref=hub.workspace)
    foreign.board_set("work", foreign_agent.principal.actor)
    creator = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    creator.work.task_add(board="work", title="Local waiting work")
    status, result = settings(hub, fresh)
    assert status == 200, result
    assert result["coordinator"] is None
    assert result["candidates"] == []
    assert fresh.owner.needs_me().get("coordinator_waiting", [{}])[0].get("count") == 1
    assert foreign.needs_me().get("coordinator_waiting") == []
    local = fresh.connect("coordinator")
    fresh.owner.board_set("work", local.principal.actor)
    assert fresh.owner.needs_me().get("coordinator_waiting") == []
    assert foreign.board_list()[0]["coordinator"] == foreign_agent.principal.actor


@pytest.mark.parametrize(
    "kind", ["done", "doing", "review", "rejected", "archived", "hidden", "ordinary"]
)
def test_non_waiting_or_invisible_cards_do_not_raise_notice(hub, fresh, kind):
    owner = fresh.owner
    creator = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    work = fresh.owner if kind == "ordinary" else creator.work
    card = work.task_add(board="work", title="Work without a coordinator notice")
    # Synthetic historical states isolate the projection from mutation permissions.
    with fresh.owner.store.transaction() as conn:
        if kind in ("done", "doing"):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (kind, card["number"]))
            if kind == "doing":
                conn.execute(
                    "UPDATE tasks SET approved_by=?,proposal_status='approved' WHERE id=?",
                    (f"user:{fresh.user}", card["number"]),
                )
        elif kind == "review":
            conn.execute(
                "UPDATE tasks SET status='review',ask='Synthetic question' WHERE id=?",
                (card["number"],),
            )
        elif kind == "rejected":
            conn.execute(
                "UPDATE tasks SET proposal_status='rejected' WHERE id=?", (card["number"],)
            )
        elif kind == "archived":
            conn.execute("UPDATE boards SET archived=1 WHERE slug='work'")
        elif kind == "hidden":
            conn.execute("UPDATE boards SET wing='private' WHERE slug='work'")
    if kind == "hidden":
        owner = service(
            hub,
            replace(
                fresh.owner.principal,
                policy=replace(fresh.owner.principal.policy, read_wings=("shared",)),
            ),
        )
    assert owner.needs_me().get("coordinator_waiting") == []


def test_waiting_is_grouped_by_board_and_only_shown_to_people(hub, fresh):
    fresh.owner.board_add(slug="second", wing="shared")
    creator = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    for board in ("work", "second", "second"):
        creator.work.task_add(board=board, title="Waiting work")
    notices = fresh.owner.needs_me().get("coordinator_waiting", [])
    assert [(n["board"], n["count"]) for n in notices] == [("second", 2), ("work", 1)]
    # The owner origin-recovery item counts once; its board notice adds no card.
    assert snapshot(hub.gateway, fresh.owner.principal, "work")["needs_you"] == 1
    agent = fresh.connect("trusted")
    assert agent.work.needs_me().get("coordinator_waiting") == []
    assert "text" in fresh.owner.needs_me(format="digest")
