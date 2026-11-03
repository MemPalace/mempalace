"""Fresh-workspace regressions for proposal release and work recipients."""

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import sessions
from commonplace.boards import service as board_service
from commonplace.extensions import ToolError
from commonplace.policy import Policy
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_boards import api, service


@pytest.fixture
def fresh(hub):
    email = "new-owner@example.test"
    user = hub.db.create_user(email)
    workspace = hub.db.create_workspace("fresh-authority", user)
    owner = service(hub, sessions.person_principal(hub.db, user, workspace))
    assert Path(board_service.__file__).resolve() == (
        Path(__file__).resolve().parents[1] / "commonplace/boards/service.py"
    )
    owner.board_add(slug="work", wing="shared")
    cookie = sessions.redeem_login_link(
        hub.db, sessions.create_login_link(hub.db, email, workspace)
    )

    def connect(name, *, parent=None, user_ref=None, workspace_ref=None, **overrides):
        policy = dict(
            profile="standard",
            scopes=["memory:read", "memory:write"],
            read_wings=["shared"],
            write_wing="shared",
            trust="trusted",
        )
        policy.update(overrides)
        cid, token = hub.db.create_token(
            name=name,
            client="generic",
            user_ref=user_ref or user,
            workspace_ref=workspace_ref or workspace,
            policy=Policy.build(**policy),
            actor=hub.admin if workspace_ref == hub.workspace else user,
            parent=parent,
        )
        principal = hub.db.authenticate_token(token)
        return SimpleNamespace(
            id=cid, token=token, principal=principal, work=service(hub, principal)
        )

    assert not hub.db.user(user)["is_admin"]
    return SimpleNamespace(
        user=user,
        workspace=workspace,
        owner=owner,
        cookie={"Cookie": f"{sessions.COOKIE}={cookie}"},
        connect=connect,
    )


@pytest.mark.parametrize(
    "trust,wing", [("untrusted", "inbox"), ("standard", "shared"), ("trusted", "inbox")]
)
def test_parent_approval_does_not_release_restricted_origin(fresh, trust, wing):
    parent = fresh.connect("parent", trust=trust, write_wing=wing)
    child = fresh.connect("child", parent=parent.id, trust="untrusted", write_wing="inbox")
    receiver = fresh.connect("receiver")
    proposal = child.work.task_add(board="work", title="Child follow-up")
    approved = parent.work.task_approve(proposal["number"])
    assert approved["proposal_status"] == "approved"
    held = child.work.task_take_next(task=proposal["number"])
    assert held["status"] == "doing"  # Approval still authorizes the child's own work.
    child.work.task_handoff(proposal["number"], receiver.principal.actor, "Continue this work")
    with pytest.raises(ToolError):
        receiver.work.task_take_next(task=proposal["number"])
    with pytest.raises(ToolError):
        receiver.work.task_update(proposal["number"], status="doing")
    assert receiver.work.task_take_next(board="work") is None
    assert fresh.owner.task_show(proposal["number"])["approved_by"] is None


@pytest.mark.parametrize("approver", ["owner", "trusted-parent"])
def test_authorized_approval_releases_restricted_work(fresh, approver):
    parent = fresh.connect("parent")
    child = fresh.connect("child", parent=parent.id, trust="untrusted", write_wing="inbox")
    receiver = fresh.connect("receiver")
    proposal = child.work.task_add(board="work", title="Approved follow-up")
    authority = fresh.owner if approver == "owner" else parent.work
    approved = authority.task_approve(proposal["number"])
    assert approved["approved_by"] == authority.principal.actor
    child.work.task_take_next(task=proposal["number"])
    child.work.task_handoff(proposal["number"], receiver.principal.actor, "Continue this work")
    assert receiver.work.task_take_next(task=proposal["number"])["status"] == "doing"


@pytest.mark.parametrize("kind", ["untrusted", "inbox", "revoked", "missing", "foreign"])
@pytest.mark.parametrize("route", ["take", "move", "finish", "queue"])
def test_legacy_approval_marker_cannot_release_or_block_safe_queue(hub, fresh, kind, route):
    creator = fresh.connect("creator", trust="untrusted", write_wing="inbox")
    receiver = fresh.connect("receiver")
    if kind == "missing":
        approver = "conn:missing"
    else:
        args = {}
        if kind == "untrusted":
            args["trust"] = "untrusted"
        elif kind == "inbox":
            args["write_wing"] = "inbox"
        elif kind == "foreign":
            args.update(user_ref=hub.admin, workspace_ref=hub.workspace)
        old = fresh.connect("old-approver", **args)
        approver = old.principal.actor
        if kind == "revoked":
            hub.db.revoke(old.id, actor=fresh.user, workspace_ref=fresh.workspace)
    legacy = creator.work.task_add(board="work", title="Legacy restricted work", claim=True)
    creator.work.task_handoff(legacy["number"], receiver.principal.actor, "Continue this work")
    # Simulate an existing marker from an older server, never a live data repair.
    with creator.work.store.transaction() as conn:
        conn.execute("UPDATE tasks SET approved_by=? WHERE id=?", (approver, legacy["number"]))
    safe = fresh.owner.task_add(board="work", title="Safe independent work")
    if route == "queue":
        assert receiver.work.task_take_next(board="work")["number"] == safe["number"]
    else:
        with pytest.raises(ToolError):
            if route == "take":
                receiver.work.task_take_next(task=legacy["number"])
            else:
                receiver.work.task_update(
                    legacy["number"], status="done" if route == "finish" else "doing"
                )


@pytest.mark.parametrize("kind", ["removed", "disabled", "foreign", "member"])
def test_legacy_person_release_requires_an_active_workspace_owner(hub, fresh, kind):
    previous = hub.db.create_user("previous-owner@example.test")
    if kind != "foreign":
        hub.db.add_member(
            fresh.workspace, previous, "member" if kind == "member" else "owner", actor=fresh.user
        )
    if kind == "removed":
        hub.db._conn.execute(
            "DELETE FROM memberships WHERE workspace_id=? AND user_id=?",
            (fresh.workspace, previous),
        )
    elif kind == "disabled":
        hub.db._conn.execute("UPDATE users SET disabled_at='2000' WHERE id=?", (previous,))
    creator = fresh.connect("creator", trust="untrusted", write_wing="inbox")
    receiver = fresh.connect("receiver")
    legacy = creator.work.task_add(board="work", title="Old release", claim=True)
    creator.work.task_handoff(legacy["number"], receiver.principal.actor, "Continue this work")
    with creator.work.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET approved_by=? WHERE id=?", (f"user:{previous}", legacy["number"])
        )
    safe = fresh.owner.task_add(board="work", title="Current authorized work")
    assert receiver.work.task_take_next(board="work")["number"] == safe["number"]


@pytest.mark.parametrize("route", ["create", "handoff"])
@pytest.mark.parametrize(
    "kind",
    ["missing", "foreign", "revoked", "expired", "disabled", "removed", "readonly", "hidden"],
)
def test_work_recipient_must_be_active_and_visible(hub, fresh, route, kind):
    sender = fresh.connect("sender")
    target = "conn:target"
    if kind != "missing":
        args = {}
        if kind == "foreign":
            args.update(user_ref=hub.admin, workspace_ref=hub.workspace)
        elif kind in ("disabled", "removed"):
            user = hub.db.create_user("recipient@example.test")
            hub.db.add_member(fresh.workspace, user, "member", actor=fresh.user)
            args["user_ref"] = user
        elif kind == "readonly":
            args["scopes"] = ["memory:read"]
        elif kind == "hidden":
            args.update(read_wings=["elsewhere"], write_wing="elsewhere")
        recipient = fresh.connect("target", **args)
        if kind == "revoked":
            hub.db.revoke(recipient.id, actor=fresh.user, workspace_ref=fresh.workspace)
        elif kind == "expired":
            hub.db._conn.execute(
                "UPDATE connections SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                (recipient.id,),
            )
        elif kind == "disabled":
            hub.db._conn.execute("UPDATE users SET disabled_at='2000' WHERE id=?", (user,))
        elif kind == "removed":
            # Exercise a legacy missing-membership row without weakening the
            # live ControlDB connection's foreign-key constraints.
            with closing(sqlite3.connect(hub.db.path)) as legacy:
                legacy.execute("PRAGMA foreign_keys=OFF")
                legacy.execute(
                    "DELETE FROM memberships WHERE workspace_id=? AND user_id=?",
                    (fresh.workspace, user),
                )
                legacy.commit()
    if route == "create":
        before = sender.work.task_list(board="work")
        with pytest.raises(ToolError, match="active.*connection|connection.*active"):
            sender.work.task_add(board="work", title="Assigned work", assignee=target)
        assert sender.work.task_list(board="work") == before
    else:
        held = sender.work.task_add(board="work", title="Held work", claim=True)
        before = sender.work.task_show(held["number"])
        with pytest.raises(ToolError, match="active.*connection|connection.*active"):
            sender.work.task_handoff(held["number"], target, "Continue this work")
        assert sender.work.task_show(held["number"]) == before


def test_valid_recipients_and_connection_ids_are_canonical(fresh):
    sender, receiver = fresh.connect("sender"), fresh.connect("receiver")
    assigned = sender.work.task_add(board="work", title="Assigned work", assignee=receiver.id)
    assert assigned["assignee"] == receiver.principal.actor
    assert receiver.work.task_take_next(task=assigned["number"])["status"] == "doing"
    handed = receiver.work.task_handoff(assigned["number"], sender.id, "Continue this work")
    assert handed["assignee"] == sender.principal.actor
    sender.work.task_take_next(task=assigned["number"])
    assert (
        sender.work.task_handoff(assigned["number"], "anyone", "Available again")["assignee"]
        is None
    )


@pytest.mark.parametrize("route", ["create", "handoff"])
def test_restricted_parent_can_assign_own_work_directly_to_child(fresh, route):
    parent = fresh.connect("parent", trust="untrusted", write_wing="inbox")
    child = fresh.connect("child", parent=parent.id, trust="untrusted", write_wing="inbox")
    if route == "create":
        assigned = parent.work.task_add(
            board="work", title="Planned child work", assignee=child.principal.actor
        )
    else:
        assigned = parent.work.task_add(board="work", title="Delegated work", claim=True)
        parent.work.task_handoff(assigned["number"], child.principal.actor, "Take this workstream")
    held = child.work.task_take_next(task=assigned["number"])
    assert held["status"] == "doing" and held["proposal_status"] is None
    assert child.work.task_update(assigned["number"], status="done")["status"] == "done"


def test_reject_is_not_an_approval_and_parent_cannot_reopen(fresh):
    parent = fresh.connect("parent")
    child = fresh.connect("child", parent=parent.id)
    proposal = child.work.task_add(board="work", title="Proposed work")
    rejected = parent.work.task_approve(proposal["number"], approve=False)
    assert rejected["proposal_status"] == "rejected" and rejected["approved_by"] is None
    with pytest.raises(ToolError):
        parent.work.task_approve(proposal["number"])
    with pytest.raises(ToolError):
        child.work.task_take_next(task=proposal["number"])
    assert fresh.owner.task_show(proposal["number"])["can_approve"]


def test_owner_can_reapprove_a_rejected_proposal(hub, fresh):
    parent = fresh.connect("parent")
    child = fresh.connect("child", parent=parent.id)
    proposal = child.work.task_add(board="work", title="Rejected work")
    rejected = parent.work.task_approve(proposal["number"], approve=False)
    status, restored = api(
        hub,
        fresh.cookie,
        "POST",
        f"/api/cards/{proposal['number']}/approve",
        {"approve": True, "version": rejected["version"]},
    )
    assert status == 200 and restored["proposal_status"] == "approved", restored
    assert child.work.task_take_next(task=proposal["number"])["status"] == "doing"


def test_owner_can_dismiss_rejected_work_without_parent_reopening(hub, fresh):
    parent = fresh.connect("parent")
    child = fresh.connect("child", parent=parent.id)
    proposal = child.work.task_add(board="work", title="Dismissed work")
    rejected = parent.work.task_approve(proposal["number"], approve=False)
    status, closed = api(
        hub,
        fresh.cookie,
        "PATCH",
        f"/api/cards/{proposal['number']}",
        {"status": "done", "version": rejected["version"]},
    )
    assert status == 200 and closed["status"] == "done", closed
    assert closed["approved_by"] is None and not closed["can_approve"]
    with pytest.raises(ToolError):
        parent.work.task_approve(proposal["number"])
    with pytest.raises(ToolError):
        child.work.task_update(proposal["number"], status="todo")
