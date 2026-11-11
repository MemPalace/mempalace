"""Independent origin release, fail-closed provenance, and actionable queues.

Historical rows are isolated fixtures, never a production repair mechanism.
"""

import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace.boards.display import people
from commonplace.boards.store import task_row
from commonplace.extensions import ToolError
from commonplace.policy import Policy
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api, mcp, service

TOOLS = [
    "space_read",
    "space_post",
    "task_approve",
    "needs_me",
    "task_heartbeat",
    "task_show",
    "task_list",
    "task_update",
]
SUFFIX = " Origin release does not approve the pending proposal or plan; separate review is still required."
OWNER_WHY = "Restricted-origin work has not been released. No live coordinator with all required release grants is available. You are the workspace owner: use task_approve with a reason to release its origin."
CONFIG = {
    "creator": "Restricted-origin work has not been released. The live board coordinator is this card's creator. Appoint an independent coordinator in board settings, or release it yourself from the card.",
    "child": "Restricted-origin work has not been released. The live board coordinator acts under this card's creator. Appoint an independent coordinator in board settings, or release it yourself from the card.",
    "grandchild": "Restricted-origin work has not been released. The live board coordinator acts under this card's creator. Appoint an independent coordinator in board settings, or release it yourself from the card.",
    "unsafe": "Restricted-origin work has not been released. The live board coordinator's independence from this card's creator cannot be verified. Appoint an independent coordinator in board settings, or release it yourself from the card.",
    "stamp": "Restricted-origin work has not been released. This card’s creator stamp cannot be verified. Clear the coordinator in board settings, or release it yourself from the card.",
}

NO_REVIEW = "no: only the direct parent, its top-level agent, the board coordinator or the signed-in workspace owner may approve proposals"
NO_ORIGIN = "no: restricted-origin work needs parent or workspace owner release"
LINEAGE_REFUSAL = "no: restricted-origin creator lineage cannot release this card"
NEEDS_OWNER = "no: only a workspace owner can do that"
NEEDS_ACCESS = "no: only a workspace owner or an authorized connection can read needs_me"


def edit(work, number, **changes):
    if "status" in changes and changes["status"] != "doing":
        changes.setdefault("claim_until", None)
    with work.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET " + ",".join(f"{k}=?" for k in changes) + " WHERE id=?",
            (*changes.values(), number),
        )


def raw(work, number):
    with work.store.connection() as conn:
        return dict(task_row(conn, number))


def snapshot(work, number):
    with work.store.connection() as conn:
        return (
            dict(task_row(conn, number)),
            [
                tuple(r)
                for r in conn.execute(
                    "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (number,)
                )
            ],
        )


def allowed(call):
    try:
        return call()
    except ToolError as exc:
        raise AssertionError(f"expected successful operation: {exc}") from exc


def refused(work, number, call, *, reason):
    before = snapshot(work, number)
    try:
        call()
    except ToolError as exc:
        assert str(exc) == reason
    else:
        raise AssertionError("expected refusal")
    assert snapshot(work, number) == before


def member(hub, fresh, name="coordinator", trust="standard", parent=None):
    user = hub.db.create_user(f"{name}@example.test")
    hub.db.add_member(fresh.workspace, user, "member", actor=fresh.user)
    q = fresh.connect(name, user_ref=user, trust=trust, tools=TOOLS, parent=parent)
    assert q.principal.role == "member"
    return q


def appoint(fresh, q, board="work"):
    fresh.owner.board_set(board, q.principal.actor)


def setup(
    hub,
    fresh,
    trust="standard",
    *,
    relation="independent",
    status="doing",
    review=None,
    board="work",
):
    root = fresh.connect("root")
    c = fresh.connect("creator", parent=root.id, trust="untrusted", write_wing="inbox")
    if relation in ("child", "grandchild"):
        child = member(hub, fresh, "child", trust, c.id)
        k = child if relation == "child" else member(hub, fresh, "grandchild", trust, child.id)
    elif relation == "creator":
        policy(hub, c, trust=trust, write_wing="shared")
        c.principal = hub.db.authenticate_connection(c.id)
        c.work = service(hub, c.principal)
        k = c
    elif relation in ("parent", "root"):
        if relation == "parent":
            c = fresh.connect("nested-creator", parent=c.id, trust="untrusted", write_wing="inbox")
            # Existing intermediate creator is promoted only after its restricted submission.
            k = SimpleNamespace(id=c.principal.parent_connection_id)
            k.principal = hub.db.authenticate_connection(k.id)
            policy(hub, k, trust=trust, write_wing="shared")
            k.principal = hub.db.authenticate_connection(k.id)
            k.work = service(hub, k.principal)
        else:
            k = root
    else:
        k = member(hub, fresh, trust=trust)
    n = c.work.task_add(board=board, title="Synthetic origin work")["number"]
    # Named historical held state: provenance from normal restricted submission survives.
    if status != "backlog":
        edit(
            fresh.owner,
            n,
            status=status,
            proposal_status="approved",
            assignee=k.principal.actor,
            claim_from="todo",
            claim_until="2999-01-01T00:00:00Z" if status == "doing" else None,
            heartbeat_at="2026-11-07T12:00:00Z",
        )
    if review == "plan":
        edit(fresh.owner, n, labels='["plan"]')
    if review == "pending-proposal":
        edit(
            fresh.owner, n, status="backlog", proposal_status="pending", assignee=k.principal.actor
        )
    appoint(fresh, k, board)
    return SimpleNamespace(c=c, k=k, n=n, root=root)


def policy(hub, q, **changes):
    old = json.loads(hub.db.connection(q.id, q.principal.workspace_id)["policy"])
    if isinstance(old.get("tools"), str):
        old["profile"] = old.pop("tools")
    if "write_wing" in changes:
        old["write_wings"] = []
    old.update(changes)
    hub.db._conn.execute(
        "UPDATE connections SET policy=? WHERE id=?", (Policy.build(**old).to_json(), q.id)
    )


def remove(hub, fresh, q, kind):
    if kind in ("revoked", "expired"):
        column = "revoked_at" if kind == "revoked" else "expires_at"
        hub.db._conn.execute(
            f"UPDATE connections SET {column}='2000-01-01T00:00:00Z' WHERE id=?", (q.id,)
        )
    elif kind == "disabled":
        hub.db._conn.execute(
            "UPDATE users SET disabled_at='2000' WHERE id=?", (q.principal.user_id,)
        )
    elif kind == "removed-membership":
        with closing(sqlite3.connect(hub.db.path)) as legacy:
            legacy.execute("PRAGMA foreign_keys=OFF")
            legacy.execute(
                "DELETE FROM memberships WHERE workspace_id=? AND user_id=?",
                (fresh.workspace, q.principal.user_id),
            )
            legacy.commit()
    elif kind in ("inbox", "downgraded"):
        policy(hub, q, **({"write_wing": "inbox"} if kind == "inbox" else {"trust": "untrusted"}))
    elif kind in ("no-memory-read", "no-memory-write"):
        policy(
            hub,
            q,
            scopes=[
                s
                for s in ["memory:read", "memory:write"]
                if s != kind.removeprefix("no-").replace("-", ":")
            ],
        )
    elif kind in ("no-wing-read", "no-board-read"):
        policy(hub, q, read_wings=["elsewhere"])
    elif kind in ("no-wing-write", "no-board-write"):
        policy(hub, q, write_wing="elsewhere")
    elif kind.startswith("hidden-"):
        hidden = {
            "approve": "task_approve",
            "needs_me": "needs_me",
            "space-read": "space_read",
            "space-post": "space_post",
        }[kind[7:]]
        policy(hub, q, tools=[t for t in TOOLS if t != hidden])
    elif kind == "replaced":
        appoint(fresh, fresh.connect("replacement"))
    elif kind == "foreign":
        hub.db.add_member(hub.workspace, q.principal.user_id, "member", actor=hub.admin)
        hub.db._conn.execute(
            "UPDATE connections SET workspace_id=? WHERE id=?", (hub.workspace, q.id)
        )
    else:
        raise AssertionError(kind)


def items(groups):
    return [c for v in groups.values() if isinstance(v, list) for c in v if "number" in c]


def origin(work, number, *, configuration=None, pending=False, notice=False):
    projected = work.approvals_for_me()
    assert number in {c["number"] for c in projected["items"]}, projected
    groups = allowed(lambda: work.needs_me())
    matches = [c for c in groups.get("approvals", []) if c["number"] == number]
    assert len(matches) == 1, groups
    c = matches[0]
    assert sum(i["number"] == number for i in items(groups)) == 1
    config = configuration is not None
    expected = {
        "approval_kind": "origin_configuration" if config else "origin",
        "approval_role": "workspace owner" if work.principal.person else "board coordinator",
        "can_approve": not config,
        "actions": ["configure_coordinator"] if config else ["approve"],
        "need": "you" if config else "release",
    }
    for key, value in expected.items():
        assert c.get(key) == value, (key, c)
    if config:
        why = CONFIG[configuration]
    elif work.principal.person:
        why = OWNER_WHY
    else:
        holder = (
            people(work.ctx, c["assignee"].removeprefix("conn:")) if c["assignee"] else "unassigned"
        )
        why = f"Restricted-origin work held by {holder} has not been released. You are this board's coordinator: use task_approve with a reason to release its origin."
    assert c.get("why") == why + (SUFFIX if pending else "")
    digest = allowed(lambda: work.needs_me(format="digest"))
    assert digest["count"] == groups["count"]
    d = [c for c in digest["items"] if c["number"] == number]
    assert len(d) == 1
    for key in ("approval_kind", "can_approve", "actions", "why", "need"):
        assert d[0].get(key) == c[key]
    assert "coordinator_waiting" not in digest
    if notice:
        assert groups.get("coordinator_waiting") == [
            {
                "board": c["board"],
                "count": 1,
                "message": f"1 restricted-origin card needs owner recovery ({0 if config else 1} release, {1 if config else 0} configuration); review these cards in Needs me.",
            }
        ]
    return c


def no_origin(work, n):
    try:
        groups = work.needs_me()
    except ToolError as exc:
        assert str(exc) in (NEEDS_OWNER, NEEDS_ACCESS)
        return
    assert not any(
        c["number"] == n and c.get("approval_kind", "").startswith("origin") for c in items(groups)
    )


def release(work, n):
    return allowed(lambda: work.task_approve(n, reason="Independent origin verified"))


def preserved(before, after, *, pending=False):
    ignored = {"approved_by", "version", "updated_at", "updated_by"}
    if before["status"] == "backlog" and not pending:
        ignored |= {"status", "claim_from"}
    assert {k: v for k, v in before.items() if k not in ignored} == {
        k: v for k, v in after.items() if k not in ignored
    }
    assert after["version"] == before["version"] + 1


@pytest.mark.parametrize("trust", ["standard", "trusted"])
def test_coordinator_holder_releases_then_heartbeats(hub, fresh, trust):
    s = setup(hub, fresh, trust)
    before = raw(fresh.owner, s.n)
    ok, reply = mcp(hub, s.k.token, "task_approve", task=s.n, reason="Independent origin verified")
    assert ok, reply
    after = raw(fresh.owner, s.n)
    preserved(before, after)
    assert after["approved_by"] == s.k.principal.actor
    assert snapshot(fresh.owner, s.n)[1][-1][0]  # history appended
    assert any(
        e.get("reason") == "Independent origin verified"
        for e in fresh.owner.task_show(s.n)["events"]
    )
    assert mcp(hub, s.k.token, "task_heartbeat", task=s.n)[0]


def test_coordinator_holder_marker_is_valid(hub, fresh):
    s = setup(hub, fresh)
    edit(fresh.owner, s.n, approved_by=s.k.principal.actor)
    allowed(lambda: s.k.work.task_heartbeat(s.n))


def test_coordinator_releases_child_held_doing(hub, fresh):
    s = setup(hub, fresh)
    child = fresh.connect("worker", parent=s.k.id)
    edit(
        fresh.owner,
        s.n,
        assignee=child.principal.actor,
        task_type="build",
        checks="pending",
        pr_head="a" * 40,
    )
    before = raw(fresh.owner, s.n)
    release(s.k.work, s.n)
    preserved(before, raw(fresh.owner, s.n))
    allowed(lambda: child.work.task_heartbeat(s.n))


@pytest.mark.parametrize("trust", ["standard", "trusted"])
def test_origin_item_visible_to_holder_coordinator(hub, fresh, trust):
    s = setup(hub, fresh, trust)
    origin(s.k.work, s.n)
    for fmt in ("list", "digest"):
        ok, reply = mcp(hub, s.k.token, "needs_me", format=fmt)
        assert ok and (f"#{s.n}" in reply if fmt == "list" else json.loads(reply)["count"] == 1), (
            reply
        )
        assert "Restricted-origin" in reply


def test_standard_coordinator_without_decide_discovers_origin_queue(hub, fresh):
    s = setup(hub, fresh)
    names = {t["name"] for t in hub.rpc(s.k.token, "tools/list")["result"]["tools"]}
    assert "needs_me" in names
    assert "decide" not in names
    private = fresh.owner.board_add(slug="private-work", wing="private.notes")
    assert private
    fresh.owner.task_add(board="private-work", title="Private origin decoy", labels=["plan"])
    fresh.owner.task_add(board="work", title="Owner review decoy", labels=["plan"])
    origin(s.k.work, s.n)
    code, reply = api(hub, s.k.token, "GET", "/api/needs-me")
    assert code == 200 and {c["number"] for c in reply["approvals"]} == {s.n}
    assert "Private origin decoy" not in json.dumps(
        reply
    ) and "Owner review decoy" not in json.dumps(reply)


def test_origin_item_child_and_release_lifecycle(hub, fresh):
    s = setup(hub, fresh)
    child = fresh.connect("worker", parent=s.k.id)
    edit(fresh.owner, s.n, assignee=child.principal.actor)
    origin(s.k.work, s.n)
    no_origin(fresh.owner, s.n)
    assert fresh.owner.needs_me().get("coordinator_waiting") == []
    release(s.k.work, s.n)
    no_origin(s.k.work, s.n)


@pytest.mark.parametrize("review", ["plan", "pending-proposal"])
def test_self_holder_release_keeps_independent_review(hub, fresh, review):
    s = setup(hub, fresh, review=review)
    before = raw(fresh.owner, s.n)
    result = release(s.k.work, s.n)
    assert "separate review" in result.get("notice", ""), result
    preserved(before, raw(fresh.owner, s.n), pending=True)
    refused(
        fresh.owner,
        s.n,
        lambda: s.k.work.task_approve(s.n, reason="No self review"),
        reason=NO_REVIEW,
    )


@pytest.mark.parametrize("relation,trust", [("child", "standard"), ("grandchild", "trusted")])
def test_creator_descendant_pending_proposal_cannot_be_approved(hub, fresh, relation, trust):
    s = setup(hub, fresh, trust, relation=relation, status="backlog")
    refused(
        fresh.owner,
        s.n,
        lambda: s.k.work.task_approve(s.n, reason="Refused descendant"),
        reason=LINEAGE_REFUSAL,
    )


@pytest.mark.parametrize("trust", ["standard", "trusted"])
def test_creator_descendant_nonholder_marker_does_not_release(hub, fresh, trust):
    s = setup(hub, fresh, trust, relation="child")
    edit(fresh.owner, s.n, assignee=s.c.principal.actor, approved_by=s.k.principal.actor)
    refused(fresh.owner, s.n, lambda: s.c.work.task_heartbeat(s.n), reason=NO_ORIGIN)


@pytest.mark.parametrize("relation", ["child", "grandchild"])
def test_creator_descendant_cannot_release_held_doing(hub, fresh, relation):
    s = setup(hub, fresh, relation=relation)
    refused(
        fresh.owner,
        s.n,
        lambda: s.k.work.task_approve(s.n, reason="Refused descendant"),
        reason=LINEAGE_REFUSAL,
    )
    edit(fresh.owner, s.n, approved_by=s.k.principal.actor)
    refused(fresh.owner, s.n, lambda: s.k.work.task_heartbeat(s.n), reason=NO_ORIGIN)
    no_origin(s.k.work, s.n)


@pytest.mark.parametrize("relation", ["parent", "root"])
def test_creator_ancestor_remains_eligible(hub, fresh, relation):
    s = setup(hub, fresh, relation=relation)
    release(s.k.work, s.n)
    allowed(lambda: s.k.work.task_heartbeat(s.n))


RECOVERY = [
    "absent",
    "revoked",
    "expired",
    "disabled",
    "removed-membership",
    "inbox",
    "no-memory-read",
    "no-memory-write",
    "no-wing-read",
    "no-wing-write",
    "hidden-space-read",
    "hidden-space-post",
    "hidden-approve",
    "hidden-needs_me",
]


@pytest.mark.parametrize("kind", RECOVERY)
def test_owner_origin_recovery_contract(hub, fresh, kind):
    s = setup(hub, fresh)
    if kind == "absent":
        fresh.owner.board_set("work", None)
    else:
        remove(hub, fresh, s.k, kind)
    edit(fresh.owner, s.n, assignee=s.c.principal.actor)
    origin(fresh.owner, s.n, notice=True)
    before = raw(fresh.owner, s.n)
    release(fresh.owner, s.n)
    preserved(before, raw(fresh.owner, s.n))
    allowed(lambda: s.c.work.task_heartbeat(s.n))


@pytest.mark.parametrize("relation", ["creator", "child", "grandchild"])
def test_owner_creator_conflict_contract(hub, fresh, relation):
    # Creator submission must occur before promotion/appointment.
    s = setup(hub, fresh, relation="child" if relation == "creator" else relation)
    if relation == "creator":
        policy(hub, s.c, trust="standard", write_wing="shared")
        s.c.principal = hub.db.authenticate_connection(s.c.id)
        s.c.work = service(hub, s.c.principal)
        s.k = s.c
        appoint(fresh, s.k)
    origin(fresh.owner, s.n, configuration=relation, notice=True)
    refused(
        fresh.owner,
        s.n,
        lambda: s.k.work.task_approve(s.n, reason="Refused conflict"),
        reason=LINEAGE_REFUSAL if relation != "creator" else NO_REVIEW,
    )
    appoint(fresh, fresh.connect("independent"))
    no_origin(fresh.owner, s.n)
    release(fresh.owner, s.n)
    assert raw(fresh.owner, s.n)["approved_by"] == f"user:{fresh.user}"


@pytest.mark.parametrize("status", ["backlog", "todo"])
def test_owner_creator_conflict_inactive_contract(hub, fresh, status):
    s = setup(hub, fresh)
    policy(hub, s.c, trust="trusted", write_wing="shared")
    s.c.principal = hub.db.authenticate_connection(s.c.id)
    s.c.work = service(hub, s.c.principal)
    appoint(fresh, s.c)
    edit(fresh.owner, s.n, status=status, assignee=None)
    origin(fresh.owner, s.n, configuration="creator", notice=True)


@pytest.mark.parametrize("kind", ["all-granted", "hidden-approve", "hidden-needs_me"])
def test_owner_releases_despite_live_creator_coordinator(hub, fresh, kind):
    s = setup(hub, fresh, status="backlog")
    policy(hub, s.c, trust="trusted", write_wing="shared")
    s.c.principal = hub.db.authenticate_connection(s.c.id)
    s.c.work = service(hub, s.c.principal)
    appoint(fresh, s.c)
    if kind != "all-granted":
        remove(hub, fresh, s.c, kind)
    release(fresh.owner, s.n)
    assert raw(fresh.owner, s.n)["approved_by"] == f"user:{fresh.user}"


def test_owner_recovery_dedup_and_notice_per_board(hub, fresh):
    s = setup(hub, fresh)
    fresh.owner.board_set("work", None)
    fresh.owner.board_add(slug="other", wing="shared")
    for _ in range(2):
        n = s.c.work.task_add(board="other", title="Synthetic second board")["number"]
        edit(
            fresh.owner, n, status="doing", proposal_status="approved", assignee=s.c.principal.actor
        )
    policy(hub, s.c, trust="trusted", write_wing="shared")
    s.c.principal = hub.db.authenticate_connection(s.c.id)
    appoint(fresh, s.c, "other")
    groups = fresh.owner.needs_me()
    assert len(groups["approvals"]) == 3
    assert len(items(groups)) == groups["count"] == 3
    assert groups["coordinator_waiting"] == [
        {
            "board": "other",
            "count": 2,
            "message": "2 restricted-origin cards need owner recovery (0 release, 2 configuration); review these cards in Needs me.",
        },
        {
            "board": "work",
            "count": 1,
            "message": "1 restricted-origin card needs owner recovery (1 release, 0 configuration); review these cards in Needs me.",
        },
    ]
    assert fresh.owner.needs_me(format="digest")["count"] == 3


@pytest.mark.parametrize("state", ["active", "expired"])
def test_origin_release_does_not_renew_claim(hub, fresh, state):
    s = setup(hub, fresh)
    if state == "expired":
        edit(
            fresh.owner,
            s.n,
            claim_until="2000-01-01T00:00:00Z",
            claim_expired_at="2000-01-01T00:01:00Z",
        )
    before, history = snapshot(fresh.owner, s.n)
    release(s.k.work, s.n)
    after, events = snapshot(fresh.owner, s.n)
    preserved(before, after)
    assert len(events) == len(history) + 1


@pytest.mark.parametrize("kind", ["legacy-null", "zero-with-agent-parent"])
def test_restricted_origin_predicate_agrees(hub, fresh, kind):
    s = setup(hub, fresh)
    edit(
        fresh.owner,
        s.n,
        restricted_origin=None if kind == "legacy-null" else 0,
        agent_parent=None if kind == "legacy-null" else s.c.principal.parent_agent,
    )
    origin(s.k.work, s.n)
    refused(fresh.owner, s.n, lambda: s.k.work.task_heartbeat(s.n), reason=NO_ORIGIN)
    fresh.owner.board_set("work", None)
    origin(fresh.owner, s.n, notice=True)


@pytest.mark.parametrize("negative", ["approve-false", "escalate-true"])
def test_self_holder_origin_cannot_reject_or_escalate(hub, fresh, negative):
    s = setup(hub, fresh)
    args = {"approve": False} if negative == "approve-false" else {"escalate": True}
    refused(
        fresh.owner,
        s.n,
        lambda: s.k.work.task_approve(s.n, reason="Refused negative", **args),
        reason="no: origin release requires affirmative approval"
        if negative == "approve-false"
        else "no: origin release cannot escalate",
    )


def test_release_board_does_not_authorize_decision_ask(hub, fresh):
    s = setup(hub, fresh)
    edit(
        fresh.owner,
        s.n,
        restricted_origin=0,
        agent_parent=None,
        status="review",
        review_kind="decision",
        ask="Which route?",
        ask_to=s.k.principal.actor,
        ask_options='["A","B"]',
    )
    try:
        groups = s.k.work.needs_me()
    except ToolError as exc:
        assert str(exc) == NEEDS_OWNER
        groups = {}
    assert not any(c["number"] == s.n for c in groups.get("asks", []))
    assert not mcp(hub, s.k.token, "decide", task=s.n, choice="A")[0]


def test_explicit_person_plan_review_survives_origin_routing(hub, fresh):
    s = setup(hub, fresh, review="plan")
    edit(fresh.owner, s.n, proposed_to=f"user:{fresh.user}", assignee=s.c.principal.actor)
    for marker in (None, s.k.principal.actor):
        edit(fresh.owner, s.n, approved_by=marker)
        plans = fresh.owner.needs_me().get("plans", [])
        assert s.n in {c["number"] for c in plans}
        item = next(c for c in plans if c["number"] == s.n)
        assert item["actions"] == (["reject"] if marker is None else ["approve", "reject"])
        assert item["why"] == (
            "Awaiting plan review. You may reject it; origin release is required before approval."
            if marker is None
            else "Awaiting plan review. Approve or reject it."
        )


def test_owner_can_release_with_independent_capable_coordinator(hub, fresh):
    s = setup(hub, fresh, status="backlog")
    release(fresh.owner, s.n)
    assert raw(fresh.owner, s.n)["approved_by"] == f"user:{fresh.user}"


@pytest.mark.parametrize("kind", ["unrestricted", "already-released"])
def test_healthy_doing_has_no_origin_need(hub, fresh, kind):
    s = setup(hub, fresh)
    edit(
        fresh.owner,
        s.n,
        **(
            {"restricted_origin": 0, "agent_parent": None}
            if kind == "unrestricted"
            else {"approved_by": f"user:{fresh.user}"}
        ),
    )
    for work in (fresh.owner, s.k.work):
        no_origin(work, s.n)
    assert fresh.owner.needs_me().get("coordinator_waiting") == []


@pytest.mark.parametrize("kind", ["untrusted", "inbox"])
def test_untrusted_creator_cannot_self_release(hub, fresh, kind):
    c = fresh.connect(
        "creator", trust="untrusted" if kind == "untrusted" else "trusted", write_wing="inbox"
    )
    n = c.work.task_add(board="work", title="Restricted creator")["number"]
    for call, reason in (
        (
            lambda: fresh.owner.board_set("work", c.principal.actor),
            "no: coordinator must be an active agent with access to this board",
        ),
        (lambda: c.work.task_approve(n, reason="Refused self"), NO_REVIEW),
        (lambda: c.work.task_take_next(task=n), "no: card is not available to take"),
        (lambda: c.work.task_heartbeat(n), NO_ORIGIN),
    ):
        refused(fresh.owner, n, call, reason=reason)
    no_origin(c.work, n)


@pytest.mark.parametrize("kind", ["held", "reassigned"])
def test_creator_later_promotion_cannot_grant_new_release(hub, fresh, kind):
    s = setup(hub, fresh)
    policy(hub, s.c, trust="trusted", write_wing="shared")
    s.c.principal = hub.db.authenticate_connection(s.c.id)
    s.c.work = service(hub, s.c.principal)
    appoint(fresh, s.c)
    edit(
        fresh.owner,
        s.n,
        status="backlog",
        proposal_status="pending",
        assignee=s.c.principal.actor if kind == "held" else s.k.principal.actor,
    )
    refused(
        fresh.owner,
        s.n,
        lambda: s.c.work.task_approve(s.n, reason="No new release"),
        reason=NO_REVIEW,
    )


@pytest.mark.parametrize("kind", ["root", "holder", "sibling"])
def test_unrelated_noncoordinator_has_no_origin_authority(hub, fresh, kind):
    s = setup(hub, fresh)
    q = (
        s.root
        if kind == "root"
        else fresh.connect("other", parent=s.root.id if kind == "sibling" else None)
    )
    if kind == "holder":
        edit(fresh.owner, s.n, assignee=q.principal.actor)
    fresh.owner.board_set("work", None)
    no_origin(q.work, s.n)
    refused(
        fresh.owner,
        s.n,
        lambda: q.work.task_approve(s.n, reason="No appointment"),
        reason="no: card is not a pending proposal or a plan awaiting review"
        if kind == "root"
        else NO_REVIEW,
    )


@pytest.mark.parametrize(
    "kind",
    [
        "revoked",
        "expired",
        "disabled",
        "removed-membership",
        "downgraded",
        "inbox",
        "replaced",
        "no-board-read",
        "no-board-write",
        "no-memory-read",
        "no-memory-write",
        "hidden-space-read",
        "hidden-space-post",
        "hidden-approve",
        "hidden-needs_me",
        "foreign",
    ],
)
def test_same_holder_marker_revalidates_authority(hub, fresh, kind):
    s = setup(hub, fresh, trust="trusted")
    edit(fresh.owner, s.n, approved_by=s.k.principal.actor)
    remove(hub, fresh, s.k, kind)
    row = raw(fresh.owner, s.n)
    assert not fresh.owner._origin_released(row)
    principal = hub.db.authenticate_connection(s.k.id)
    if principal is not None and principal.workspace_id == fresh.workspace:
        work = service(hub, principal)
        refused(
            fresh.owner,
            s.n,
            lambda: work.task_heartbeat(s.n),
            reason="no: card not found"
            if kind in ("no-board-read", "no-memory-read")
            else "no: this connection cannot change this board"
            if kind == "no-memory-write"
            else NO_ORIGIN,
        )
        no_origin(work, s.n)
    code, reply = api(hub, s.k.token, "GET", "/api/needs-me")
    if principal is None:
        assert code == 401, reply
    elif principal.workspace_id != fresh.workspace:
        assert code == 200, reply
        assert s.n not in {c["number"] for c in items(reply)}
    elif kind in ("hidden-needs_me", "no-memory-read"):
        assert code == 403 and reply["error"] == "tool not allowed for this connection"
    elif code == 403:
        assert reply["error"] in (
            NEEDS_OWNER.removeprefix("no: "),
            NEEDS_ACCESS.removeprefix("no: "),
        )
    else:
        assert code == 200, reply
        assert not any(
            c.get("approval_kind", "").startswith("origin") and c["number"] == s.n
            for c in items(reply)
        )


@pytest.mark.parametrize("kind", ["ask", "escalated", "rejected", "review", "done"])
def test_person_and_terminal_origin_guards(hub, fresh, kind):
    s = setup(hub, fresh)
    updates = (
        {"ask": "Which?", "ask_to": f"user:{fresh.user}"}
        if kind == "ask"
        else {"escalated_to": f"user:{fresh.user}"}
        if kind == "escalated"
        else {"proposal_status": "rejected"}
        if kind == "rejected"
        else {"status": kind}
    )
    if kind in ("ask", "review"):
        updates.update(
            status="review", ask="Which route?", ask_to=f"user:{fresh.user}", claim_until=None
        )
    edit(fresh.owner, s.n, **updates)
    refused(
        fresh.owner,
        s.n,
        lambda: s.k.work.task_approve(s.n, reason="No origin route"),
        reason="no: escalated to the owner, who decides" if kind == "escalated" else NO_REVIEW,
    )
    no_origin(s.k.work, s.n)


def test_noncoordinator_standard_member_is_not_widened(hub, fresh):
    setup(hub, fresh)
    other = member(hub, fresh, "other")
    assert "needs_me" not in {
        t["name"] for t in hub.rpc(other.token, "tools/list")["result"]["tools"]
    }
    assert not mcp(hub, other.token, "needs_me")[0]
    assert api(hub, other.token, "GET", "/api/needs-me")[0] == 403


def corrupt_lineage(hub, fresh, s, kind):
    p = fresh.connect("intermediate")
    q = fresh.connect(
        "upper",
        **(
            {"user_ref": hub.admin, "workspace_ref": hub.workspace}
            if kind == "cross-workspace-parent"
            else {}
        ),
    )
    # Isolated legacy fixture writer bypasses the insertion/update guards only
    # while constructing impossible historical edges, then restores the trigger.
    with closing(sqlite3.connect(hub.db.path)) as legacy:
        legacy.execute("PRAGMA foreign_keys=OFF")
        trigger = legacy.execute(
            "SELECT sql FROM sqlite_master WHERE name='connections_parent_update'"
        ).fetchone()[0]
        legacy.execute("DROP TRIGGER connections_parent_update")
        legacy.execute(
            "UPDATE connections SET parent_id=?,started_by='agent' WHERE id=?", (p.id, s.k.id)
        )
        legacy.execute(
            "UPDATE connections SET parent_id=?,started_by='agent' WHERE id=?",
            ("missing-id" if kind == "dangling-parent" else q.id, p.id),
        )
        if kind == "cycle":
            legacy.execute(
                "UPDATE connections SET parent_id=?,started_by='agent' WHERE id=?", (p.id, q.id)
            )
        legacy.execute(trigger)
        legacy.commit()
    s.k.principal = hub.db.authenticate_connection(s.k.id)
    assert s.k.principal is not None
    s.k.work = service(hub, s.k.principal)


LINEAGES = ["cycle", "dangling-parent", "cross-workspace-parent"]


@pytest.mark.parametrize("lineage", LINEAGES)
@pytest.mark.parametrize(
    "path,trust",
    [
        ("approve", "standard"),
        ("stored-marker", "standard"),
        ("stored-marker", "trusted"),
        ("same-holder", "standard"),
    ],
)
def test_lineage_fails_closed(hub, fresh, lineage, path, trust):
    s = setup(hub, fresh, trust, status="backlog" if path == "approve" else "doing")
    if path != "same-holder":
        edit(fresh.owner, s.n, assignee=s.c.principal.actor)
    if path == "stored-marker":
        edit(fresh.owner, s.n, approved_by=s.k.principal.actor)
    corrupt_lineage(hub, fresh, s, lineage)
    if path == "approve":
        refused(
            fresh.owner,
            s.n,
            lambda: s.k.work.task_approve(s.n, reason="Unsafe lineage"),
            reason=LINEAGE_REFUSAL,
        )
    elif path == "stored-marker":
        refused(fresh.owner, s.n, lambda: s.c.work.task_heartbeat(s.n), reason=NO_ORIGIN)
    else:
        refused(
            fresh.owner,
            s.n,
            lambda: s.k.work.task_approve(s.n, reason="Unsafe lineage"),
            reason=LINEAGE_REFUSAL,
        )
        edit(fresh.owner, s.n, approved_by=s.k.principal.actor)
        refused(fresh.owner, s.n, lambda: s.k.work.task_heartbeat(s.n), reason=NO_ORIGIN)
        no_origin(s.k.work, s.n)


@pytest.mark.parametrize("lineage", LINEAGES)
def test_unsafe_lineage_owner_configuration(hub, fresh, lineage):
    s = setup(hub, fresh, status="backlog")
    corrupt_lineage(hub, fresh, s, lineage)
    origin(fresh.owner, s.n, configuration="unsafe", pending=True, notice=True)
    release(fresh.owner, s.n)
    assert raw(fresh.owner, s.n)["approved_by"] == f"user:{fresh.user}"


STAMPS = ["name", "user", "created-at", "unresolvable"]


def corrupt_stamp(fresh, s, stamp):
    changes = (
        {"stamp_connection": "different-name"}
        if stamp == "name"
        else {"stamp_user": "different-user"}
        if stamp == "user"
        else {"created_at": "1900-01-01T00:00:00Z"}
        if stamp == "created-at"
        else {"created_by": "conn:missing-creator", "stamp_connection": "missing-creator"}
    )
    edit(fresh.owner, s.n, **changes)
    assert fresh.owner._creator_connection(raw(fresh.owner, s.n)) is None


@pytest.mark.parametrize("stamp", STAMPS)
@pytest.mark.parametrize("path", ["approve", "stored-marker"])
@pytest.mark.parametrize("trust", ["standard", "trusted"])
def test_unverifiable_creator_stamp_fails_closed(hub, fresh, stamp, path, trust):
    s = setup(
        hub, fresh, trust, relation="child", status="backlog" if path == "approve" else "doing"
    )
    corrupt_stamp(fresh, s, stamp)
    edit(fresh.owner, s.n, assignee=s.c.principal.actor)
    if path == "approve":
        refused(
            fresh.owner,
            s.n,
            lambda: s.k.work.task_approve(s.n, reason="Invalid stamp"),
            reason=LINEAGE_REFUSAL,
        )
    else:
        edit(fresh.owner, s.n, approved_by=s.k.principal.actor)
        refused(fresh.owner, s.n, lambda: s.c.work.task_heartbeat(s.n), reason=NO_ORIGIN)


@pytest.mark.parametrize("stamp", STAMPS)
def test_unverifiable_creator_stamp_same_holder(hub, fresh, stamp):
    s = setup(hub, fresh, relation="child")
    corrupt_stamp(fresh, s, stamp)
    refused(
        fresh.owner,
        s.n,
        lambda: s.k.work.task_approve(s.n, reason="Invalid stamp"),
        reason=LINEAGE_REFUSAL,
    )
    edit(fresh.owner, s.n, approved_by=s.k.principal.actor)
    refused(fresh.owner, s.n, lambda: s.k.work.task_heartbeat(s.n), reason=NO_ORIGIN)
    no_origin(s.k.work, s.n)


@pytest.mark.parametrize("stamp", STAMPS)
@pytest.mark.parametrize("condition", ["live-capable", "live-missing-memory-write"])
@pytest.mark.parametrize("relation", ["independent", "child"])
def test_unverifiable_creator_stamp_owner_configuration(hub, fresh, stamp, condition, relation):
    s = setup(hub, fresh, relation=relation, status="backlog")
    corrupt_stamp(fresh, s, stamp)
    if condition != "live-capable":
        remove(hub, fresh, s.k, "no-memory-write")
    origin(fresh.owner, s.n, configuration="stamp", pending=True, notice=True)
    release(fresh.owner, s.n)
    assert raw(fresh.owner, s.n)["approved_by"] == f"user:{fresh.user}"


@pytest.mark.parametrize("stamp", STAMPS)
@pytest.mark.parametrize("condition", ["absent", "revoked"])
def test_unverifiable_creator_stamp_existing_owner_fallback(hub, fresh, stamp, condition):
    s = setup(hub, fresh, relation="child", status="backlog")
    corrupt_stamp(fresh, s, stamp)
    if condition == "absent":
        fresh.owner.board_set("work", None)
    else:
        remove(hub, fresh, s.k, "revoked")
    token = fresh.connect("owner-token")
    refused(
        fresh.owner,
        s.n,
        lambda: token.work.task_approve(s.n, reason="Token is not person"),
        reason=LINEAGE_REFUSAL,
    )
    origin(fresh.owner, s.n, pending=True, notice=True)
    before = raw(fresh.owner, s.n)
    release(fresh.owner, s.n)
    preserved(before, raw(fresh.owner, s.n), pending=True)
    approved = allowed(lambda: fresh.owner.task_approve(s.n, reason="Separate proposal review"))
    assert approved["proposal_status"] == "approved" and approved["status"] == "todo"


def test_owner_origin_only_cannot_reject_pending_proposal(hub, fresh):
    s = setup(hub, fresh, status="backlog")
    fresh.owner.board_set("work", None)
    refused(
        fresh.owner,
        s.n,
        lambda: fresh.owner.task_approve(s.n, approve=False, reason="Origin has no reject"),
        reason="no: origin release requires affirmative approval",
    )


@pytest.mark.parametrize("case", ["origin-fresh", "origin-pending", "configuration"])
def test_origin_needs_action_dispatch(case):
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("workbench_origin_release.mjs")), case],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_explicit_person_backlog_proposal_survives_origin_routing(hub, fresh):
    s = setup(hub, fresh, status="backlog")
    edit(fresh.owner, s.n, proposed_to=f"user:{fresh.user}")
    for marker in (None, s.k.principal.actor):
        edit(fresh.owner, s.n, approved_by=marker)
        proposals = fresh.owner.needs_me().get("backlog_from_agents", [])
        assert s.n in {c["number"] for c in proposals}
        item = next(c for c in proposals if c["number"] == s.n)
        assert item["actions"] == (["reject"] if marker is None else ["approve", "reject"])
        assert item["why"] == (
            "creator proposed this to you. You may reject it; origin release is required before approval."
            if marker is None
            else "creator proposed this to you. Approve or reject it."
        )
        no_origin(fresh.owner, s.n)


@pytest.mark.parametrize("review", ["plan", "pending-proposal"])
def test_origin_releasers_keep_approve_action(hub, fresh, review):
    s = setup(hub, fresh, review=review)
    assert origin(s.k.work, s.n, pending=True)["actions"] == ["approve"]
    fresh.owner.board_set("work", None)
    assert origin(fresh.owner, s.n, pending=True)["actions"] == ["approve"]


@pytest.mark.parametrize("choice", ["Continue", None, "Release origin", " release origin "])
def test_origin_release_preserves_return_routed_decision(hub, fresh, choice):
    s = setup(hub, fresh, status="todo")
    # Historical restricted work awaiting a person decision before its origin release.
    edit(
        fresh.owner,
        s.n,
        status="review",
        review_kind="decision",
        ask="How should this proceed?",
        ask_to=f"user:{fresh.user}",
        return_to=s.k.principal.actor,
    )
    allowed(lambda: fresh.owner.decide(s.n, choice=choice, reply="Use the reviewed approach."))
    release(s.k.work, s.n)
    claimed = allowed(lambda: s.k.work.task_take_next(task=s.n))
    assert claimed.get("decision") == {
        "actor": f"user:{fresh.user}",
        "choice": choice,
        "reply": "Use the reviewed approach.",
    }
    assert raw(fresh.owner, s.n)["return_to"] is None


def test_reserved_origin_choice_cannot_swallow_owner_decision(hub, fresh):
    s = setup(hub, fresh, status="todo")
    edit(
        fresh.owner,
        s.n,
        status="review",
        review_kind="decision",
        ask="How should this proceed?",
        ask_options=None,
        ask_to=f"user:{fresh.user}",
        return_to=s.k.principal.actor,
    )
    refused(
        fresh.owner,
        s.n,
        lambda: fresh.owner.decide(s.n, choice="release origin", reply="Stop; do not start."),
        reason="no: 'release origin' is a reserved choice",
    )
    allowed(lambda: fresh.owner.decide(s.n, choice="Stop", reply="Stop; do not start."))
    release(s.k.work, s.n)
    claimed = allowed(lambda: s.k.work.task_take_next(task=s.n))
    assert claimed.get("decision") == {
        "actor": f"user:{fresh.user}",
        "choice": "Stop",
        "reply": "Stop; do not start.",
    }
    assert raw(fresh.owner, s.n)["return_to"] is None


@pytest.mark.parametrize("existing", [False, True])
def test_reserved_origin_choice_cannot_be_offered(hub, fresh, existing):
    s = setup(hub, fresh)
    if existing:
        release(s.k.work, s.n)
    before = fresh.owner.task_list(board="work")
    refused(
        fresh.owner,
        s.n,
        lambda: fresh.owner.ask(
            "Which approach should we use for this work?",
            options=["Continue", "release origin"],
            board="work",
            task=s.n if existing else None,
            context="Synthetic context for the owner's decision.",
        ),
        reason="no: 'release origin' is a reserved choice",
    )
    assert fresh.owner.task_list(board="work") == before


@pytest.mark.parametrize("configuration", ["creator", "child", "unsafe", "stamp"])
def test_owner_rejects_pending_proposal_during_origin_configuration(hub, fresh, configuration):
    relation = configuration if configuration in ("creator", "child") else "independent"
    s = setup(hub, fresh, relation=relation, status="backlog")
    if configuration == "unsafe":
        corrupt_lineage(hub, fresh, s, "cycle")
    elif configuration == "stamp":
        corrupt_stamp(fresh, s, "name")
    origin(fresh.owner, s.n, configuration=configuration, pending=True)
    before = raw(fresh.owner, s.n)
    rejected = allowed(
        lambda: fresh.owner.task_approve(s.n, approve=False, reason="Proposal declined")
    )
    assert rejected["proposal_status"] == "rejected"
    after = raw(fresh.owner, s.n)
    for field in ("approved_by", "restricted_origin", "agent_parent", "created_by"):
        assert after[field] == before[field]
    assert not fresh.owner._origin_released(after)


def test_owner_rejects_person_targeted_proposal_with_healthy_coordinator(hub, fresh):
    s = setup(hub, fresh, status="backlog")
    edit(fresh.owner, s.n, proposed_to=f"user:{fresh.user}")
    before = raw(fresh.owner, s.n)
    assert before["proposal_status"] == "pending"
    assert fresh.owner._origin_authority(before)[0] == "coordinator"
    rejected = allowed(
        lambda: fresh.owner.task_approve(s.n, approve=False, reason="Proposal declined")
    )
    assert rejected["proposal_status"] == "rejected"
    after = raw(fresh.owner, s.n)
    for field in ("approved_by", "restricted_origin", "agent_parent", "created_by"):
        assert after[field] == before[field]
    assert not fresh.owner._origin_released(after)


def test_descendant_coordinator_content_exception_is_removed(hub, fresh):
    s = setup(hub, fresh, relation="child", status="todo")
    edit(fresh.owner, s.n, assignee=None)
    refused(
        fresh.owner,
        s.n,
        lambda: s.k.work.task_update(s.n, title="Disallowed edit", body="Disallowed body"),
        reason=NO_ORIGIN,
    )


def test_owner_role_token_has_no_person_origin_fallback(hub, fresh):
    s = setup(hub, fresh)
    fresh.owner.board_set("work", None)
    token = fresh.connect("owner-token")
    refused(
        fresh.owner,
        s.n,
        lambda: token.work.task_approve(s.n, reason="Token is not person"),
        reason=NO_REVIEW,
    )
    no_origin(token.work, s.n)


def test_release_only_standard_coordinator_queue_is_narrow(hub, fresh):
    s = setup(hub, fresh)
    decoys = []
    for pending in (True, False):
        n = s.c.work.task_add(board="work", title="Ordinary review decoy")["number"]
        edit(
            fresh.owner,
            n,
            approved_by=f"user:{fresh.user}",
            **(
                {}
                if pending
                else {"status": "todo", "proposal_status": "approved", "labels": '["plan"]'}
            ),
        )
        decoys.append(n)
    for fmt in ("list", "digest"):
        groups = allowed(lambda fmt=fmt: s.k.work.needs_me(format=fmt))
        shown = groups["items"] if fmt == "digest" else items(groups)
        assert not set(decoys) & {c["number"] for c in shown}
    assert not set(decoys) & {c["number"] for c in s.k.work.approvals_for_me()["items"]}
    for fmt in ("list", "digest"):
        ok, text = mcp(hub, s.k.token, "needs_me", format=fmt)
        assert ok, text
        assert "Ordinary review decoy" not in text
    held = s.k.work.task_add(board="work", title="Independent heartbeat", claim=True)
    ok, text = mcp(hub, s.k.token, "task_heartbeat", task=held["number"])
    assert ok and "Ordinary review decoy" not in text
    code, reply = api(hub, s.k.token, "GET", "/api/needs-me")
    assert code == 200, reply
    assert not set(decoys) & {c["number"] for c in items(reply)}


def test_owner_origin_overflow_never_becomes_ordinary_proposal(hub, fresh):
    s = setup(hub, fresh, status="backlog")
    for i in range(12):
        s.c.work.task_add(board="work", title=f"Origin overflow {i}")
    fresh.owner.board_set("work", None)
    groups = fresh.owner.needs_me()
    assert len(groups["approvals"]) == 10
    assert groups["approvals_truncated"]
    assert not groups["backlog_from_agents"]
    assert groups["count"] == 10
    assert all(c.get("approval_kind") == "origin" for c in groups["approvals"])
    digest = fresh.owner.needs_me(format="digest")
    assert digest["count"] == 10 and digest["remaining"] == 5
    assert groups["coordinator_waiting"][0]["count"] == 13
