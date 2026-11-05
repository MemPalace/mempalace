"""Work coordination through ordinary routes in a fresh non-admin workspace.

P1-P11 are paired regressions; trusted/shared P1-P3 are both-pass controls.
P2/P8/P10 holder continuations require the first successful take on head.
Historical markers and name reuse below are explicitly test-only states.
"""

import pytest

from commonplace import sessions
from commonplace.control_db import ControlError
from commonplace.extensions import ToolError
from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    REVIEW_OPTIONS,
    REVIEW_QUESTION,
    hub_fixture,  # noqa: F401
)
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api, mcp, service

RESTRICTED = [("untrusted", "inbox"), ("trusted", "inbox")]
SHAPES = [*RESTRICTED, ("trusted", "shared")]


def allowed(fn):
    """A refused positive is an assertion failure on the pinned base."""
    try:
        return fn()
    except ToolError as exc:
        raise AssertionError(f"refused: {exc}") from exc


def agent(fresh, name, shape=("untrusted", "inbox"), **kwargs):
    trust, wing = shape
    return fresh.connect(name, trust=trust, write_wing=wing, **kwargs)


def change(hub, fresh, auth, number, method="PATCH", suffix="", **body):
    before = fresh.owner.task_show(number)
    if suffix == "/approve" and isinstance(auth, str):
        body.setdefault("reason", "synthetic reason")
    return api(
        hub,
        auth,
        method,
        f"/api/cards/{number}{suffix}",
        {"version": before["version"], **body},
    )


def take(hub, who, number=None):
    args = {"task": number} if number is not None else {"board": "work"}
    ok, reply = mcp(hub, who.token, "task_take_next", **args)
    assert ok and (f"#{number}" in reply if number is not None else "#" in reply), reply


def handoff(who, number, to="anyone"):
    return allowed(lambda: who.work.task_handoff(number, to, "Continue this work"))


def proposed(child):
    return allowed(lambda: child.work.task_add(board="work", title="Child proposal"))["number"]


def coordinator(hub, fresh, *, parent=None):
    # Standard trust, the two coordination tools/scopes, and board access.
    # Normal-route proposal creation/approval also require their named tools.
    k = fresh.connect(
        "coordinator",
        parent=parent,
        trust="standard",
        tools=["space_read", "space_post", "task_add", "task_approve"],
    )
    status, reply = api(
        hub, fresh.cookie, "PATCH", "/api/boards/work", {"coordinator": k.principal.actor}
    )
    assert status == 200, reply
    return k


def assert_nonholder_refused(hub, fresh, who, number):
    before = fresh.owner.task_show(number)
    for body in (
        {"status_line": "Unowned annotation"},
        {"link": "memory-example"},
        {"status": "backlog"},
        {"status": "doing"},
    ):
        status, reply = change(hub, fresh, who.token, number, **body)
        assert status == 403, reply
        assert fresh.owner.task_show(number) == before
    status, reply = change(
        hub, fresh, who.token, number, "POST", "/handoff", to="anyone", note="Move work"
    )
    assert status == 403, reply
    assert fresh.owner.task_show(number) == before
    ok, reply = mcp(hub, who.token, "task_heartbeat", task=number)
    assert not ok, reply
    assert fresh.owner.task_show(number) == before


@pytest.mark.parametrize("shape", SHAPES)
def test_top_level_assigns_to_another_top_level(hub, fresh, shape):
    """P1: restricted assignment is new; trusted/shared is a both-pass control."""
    a, b = agent(fresh, "a", shape), agent(fresh, "b", shape)
    status, card = api(
        hub,
        a.token,
        "POST",
        "/api/boards/work/cards",
        {"title": "Independent work", "assignee": b.principal.actor},
    )
    assert status == 201, card
    assert card["status"] == "todo" and card["assignee"] == b.principal.actor
    take(hub, b)
    assert b.work.task_show(card["number"])["status"] == "doing"
    if shape in RESTRICTED:
        foreign_child = agent(fresh, "foreign-child", parent=b.id)
        for target in (foreign_child.principal.actor, f"user:{fresh.user}"):
            status, reply = api(
                hub,
                a.token,
                "POST",
                "/api/boards/work/cards",
                {"title": "Refused assignment", "assignee": target},
            )
            assert status == 403, reply


@pytest.mark.parametrize("shape", SHAPES)
def test_handoff_receiver_claims_and_keeps_working(hub, fresh, shape):
    """P2: the first take is paired red; subsequent checks are head continuations."""
    a, b = agent(fresh, "a", shape), agent(fresh, "b", shape)
    c, outsider = agent(fresh, "c", shape), fresh.connect("outsider")
    card = a.work.task_add(board="work", title="Shared workstream", claim=True)
    number = card["number"]
    handoff(a, number, b.principal.actor)
    take(hub, b, number)  # First regression assertion, before any new holder path.
    # doing->doing is an existing no-op; todo->doing is guarded below on
    # the reserved card, rather than being treated as a fresh claim here.
    if shape in RESTRICTED:
        before = fresh.owner.task_show(number)
        assert change(hub, fresh, b.token, number, status="todo")[0] == 403
        assert fresh.owner.task_show(number) == before
    assert_nonholder_refused(hub, fresh, outsider, number)
    assert change(hub, fresh, b.token, number, status_line="Implementation underway")[0] == 200
    ok, reply = mcp(hub, b.token, "task_heartbeat", task=number)
    assert ok and f"#{number}" in reply, reply
    # Expiry retains the holder; renewal must keep working on the new path.
    with b.work.store.transaction() as conn:
        conn.execute("UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (number,))
    expired = fresh.owner.task_show(number)
    assert expired["status"] == "doing" and expired["assignee"] == b.principal.actor
    assert expired["claim_expired"]
    assert not mcp(hub, outsider.token, "task_take_next", task=number)[0]
    assert mcp(hub, b.token, "task_heartbeat", task=number)[0]
    assert not fresh.owner.task_show(number)["claim_expired"]
    assert (
        change(
            hub, fresh, b.token, number, "POST", "/handoff", to=c.principal.actor, note="Next step"
        )[0]
        == 200
    )
    # Reserved own-card annotation is deliberately a head continuation.
    assert change(hub, fresh, c.token, number, status_line="Reserved recipient notes")[0] == 200
    if shape in RESTRICTED:
        before = fresh.owner.task_show(number)
        assert change(hub, fresh, c.token, number, status="doing")[0] == 403
        assert fresh.owner.task_show(number) == before
    take(hub, c, number)
    assert change(hub, fresh, c.token, number, status="done")[0] == 200


@pytest.mark.parametrize("shape", SHAPES)
def test_unreserved_top_level_card_in_shared_queue(hub, fresh, shape):
    """P3: shared queue and the claim-time wing/trust bypass."""
    a, b = agent(fresh, "a", shape), agent(fresh, "b", shape)
    number = a.work.task_add(board="work", title="Available work", claim=True)["number"]
    handoff(a, number)
    ok, reply = mcp(hub, b.token, "task_take_next", board="work")
    assert ok and f"#{number}" in reply, reply
    assert b.work.task_show(number)["assignee"] == b.principal.actor


@pytest.mark.parametrize("shape", RESTRICTED)
def test_owning_root_approves_and_rejects_descendant_proposals(hub, fresh, shape):
    """P4: direct-root release, descendant approval/rejection, owner re-approval."""
    root = agent(fresh, "root", shape)
    child = agent(fresh, "child", parent=root.id)
    grandchild = agent(fresh, "grandchild", parent=child.id)
    receiver = agent(fresh, "receiver")
    number = proposed(child)
    status, card = change(hub, fresh, root.token, number, "POST", "/approve")
    assert status == 200, card
    assert card["approved_by"] == root.principal.actor
    nested = proposed(grandchild)
    status, card = change(hub, fresh, root.token, nested, "POST", "/approve")
    assert status == 200 and card["approved_by"] == root.principal.actor, card
    take(hub, child, number)
    handoff(child, number, receiver.principal.actor)
    take(hub, receiver, number)
    rejected = proposed(grandchild)
    status, card = change(hub, fresh, root.token, rejected, "POST", "/approve", approve=False)
    assert status == 200 and card["proposal_status"] == "rejected", card
    before = fresh.owner.task_show(rejected)
    status, reply = change(hub, fresh, root.token, rejected, "POST", "/approve")
    assert status == 403 and "not a pending proposal" in reply["error"], reply
    assert fresh.owner.task_show(rejected) == before
    assert change(hub, fresh, fresh.cookie, rejected, "POST", "/approve")[0] == 200


@pytest.mark.parametrize("subagent", [False, True])
def test_board_coordinator_approves_and_rejects(hub, fresh, subagent):
    """P5: owner-appointed standard coordinators may release another creator."""
    root = fresh.connect("root")
    k = coordinator(hub, fresh, parent=root.id if subagent else None)
    unrelated = fresh.connect("unrelated")
    child = agent(fresh, "child", parent=unrelated.id)
    receiver = agent(fresh, "receiver")
    number = proposed(child)
    status, card = change(hub, fresh, k.token, number, "POST", "/approve")
    assert status == 200, card
    assert card["approved_by"] == k.principal.actor
    take(hub, child, number)
    handoff(child, number, receiver.principal.actor)
    take(hub, receiver, number)
    rejected = proposed(child)
    status, card = change(hub, fresh, k.token, rejected, "POST", "/approve", approve=False)
    assert status == 200 and card["proposal_status"] == "rejected", card
    before = fresh.owner.task_show(rejected)
    status, reply = change(hub, fresh, k.token, rejected, "POST", "/approve")
    assert status == 403 and "not a pending proposal" in reply["error"], reply
    assert fresh.owner.task_show(rejected) == before


def test_coordinator_cannot_approve_own_proposal(hub, fresh):
    """Both-pass normal-route control for the parent's least-privilege decision."""
    root = fresh.connect("root")
    k = coordinator(hub, fresh, parent=root.id)
    status, card = api(
        hub, k.token, "POST", "/api/boards/work/cards", {"title": "Coordinator's own proposal"}
    )
    assert status == 201 and card["proposal_status"] == "pending", card
    number = card["number"]
    before = fresh.owner.task_show(number)
    for approve in (True, False):
        status, reply = change(hub, fresh, k.token, number, "POST", "/approve", approve=approve)
        assert status == 403 and "direct parent" in reply["error"], reply
        assert fresh.owner.task_show(number) == before


@pytest.mark.parametrize("shape", SHAPES)
def test_top_level_explicit_parent_links(hub, fresh, shape):
    """P6: top-level callers can explicitly link their own parent card."""
    a = agent(fresh, "a", shape)
    parent = a.work.task_add(board="work", title="Parent work")
    status, card = api(
        hub,
        a.token,
        "POST",
        "/api/boards/work/cards",
        {"title": "Linked work", "parent": parent["number"]},
    )
    assert status == 201, card
    assert card["parent"] == parent["number"]


@pytest.mark.parametrize("shape", RESTRICTED)
def test_restricted_top_level_takes_other_readable_work(hub, fresh, shape):
    """P7: trusted-origin work also participates in the readable queue."""
    b = agent(fresh, "b", shape)
    number = fresh.owner.task_add(board="work", title="Owner work")["number"]
    ok, reply = mcp(hub, b.token, "task_take_next", board="work")
    assert ok and f"#{number}" in reply, reply
    assert change(hub, fresh, b.token, number, status_line="Working on owner work")[0] == 200


@pytest.mark.parametrize("shape", RESTRICTED)
def test_holder_fails_closed_when_creator_revoked(hub, fresh, shape):
    """P8: paired red at first take; revocation/recovery are head continuations."""
    a, b = agent(fresh, "a", shape), agent(fresh, "b", shape)
    number = a.work.task_add(board="work", title="Revocable work", claim=True)["number"]
    handoff(a, number, b.principal.actor)
    take(hub, b, number)
    hub.db.revoke(a.id, actor=fresh.user, workspace_ref=fresh.workspace)
    before = fresh.owner.task_show(number)
    with pytest.raises(ToolError, match="restricted-origin"):
        b.work.task_update(number, status_line="Must fail closed")
    assert fresh.owner.task_show(number) == before
    status, released = change(
        hub,
        fresh,
        fresh.cookie,
        number,
        "POST",
        "/handoff",
        to=b.principal.actor,
        note="Owner recovery",
    )
    assert status == 200 and released["approved_by"] == f"user:{fresh.user}", released
    take(hub, b, number)
    assert change(hub, fresh, b.token, number, status_line="Recovered work")[0] == 200


def test_restricted_child_handoff_of_top_level_created_work(hub, fresh):
    """P9: a child's handoff does not turn top-level-created work into a proposal."""
    a = agent(fresh, "a")
    child = agent(fresh, "child", parent=a.id)
    receiver = agent(fresh, "receiver")
    card = a.work.task_add(board="work", title="Delegated work", assignee=child.principal.actor)
    take(hub, child, card["number"])
    handoff(child, card["number"])
    take(hub, receiver, card["number"])


def assert_person_review_protected(hub, fresh, who, number):
    before = fresh.owner.task_show(number)
    for body in (
        *({"status": status} for status in ("done", "todo", "doing", "backlog")),
        {"question": "Replace the pending question?"},
        {"options": ["Replace", "Keep"]},
        {"ask_to": who.principal.actor},
        {"return_to": who.principal.actor},
        {"assignee": who.principal.actor},
    ):
        status, reply = change(hub, fresh, who.token, number, **body)
        assert status in (400, 403), reply
        assert fresh.owner.task_show(number) == before
    for tool, args in (
        ("task_handoff", {"to": "anyone", "note": "Agent cannot decide"}),
        ("task_take_next", {}),
        ("decide", {"choice": REVIEW_OPTIONS[0]}),
    ):
        ok, reply = mcp(hub, who.token, tool, task=number, **args)
        assert not ok, reply
        assert fresh.owner.task_show(number) == before


def person_review(who, number):
    return allowed(
        lambda: who.work.task_update(
            number,
            status="review",
            question=REVIEW_QUESTION,
            options=REVIEW_OPTIONS,
            context=REVIEW_CONTEXT,
        )
    )


def test_person_review_holder_feedback_without_decision_rights(hub, fresh):
    """P10: first take is old-red; feedback is a head-only continuation."""
    a, b = agent(fresh, "a"), agent(fresh, "b")
    number = a.work.task_add(board="work", title="Reviewable work", claim=True)["number"]
    handoff(a, number, b.principal.actor)
    take(hub, b, number)
    reviewed = person_review(b, number)
    assert reviewed["return_to"] == b.principal.actor
    status, card = change(
        hub,
        fresh,
        b.token,
        number,
        title="Updated review title",
        body=REVIEW_CONTEXT,
        checks="passing",
        labels=["reviewed"],
    )
    assert status == 200 and card["ask"] == REVIEW_QUESTION, card
    drawer = allowed(
        lambda: fresh.owner.ctx.memory(
            "mempalace_add_drawer",
            {"content": "Review evidence", "wing": "shared", "room": "general"},
        )
    )["drawer_id"]
    status, reply = change(hub, fresh, b.token, number, link=drawer)
    assert status == 200, reply
    assert change(hub, fresh, b.token, number, unlink=drawer)[0] == 200
    assert_person_review_protected(hub, fresh, b, number)


def test_root_release_keeps_existing_nonholder_annotations(hub, fresh):
    """P11: base proceeds past direct-parent approval to the annotation assertion."""
    root = agent(fresh, "root")
    child = agent(fresh, "child", parent=root.id)
    other = fresh.connect("other")
    number = proposed(child)
    status, reply = change(hub, fresh, root.token, number, "POST", "/approve")
    assert status == 200, reply
    take(hub, child, number)
    handoff(child, number)
    status, reply = change(hub, fresh, other.token, number, status_line="Released work annotation")
    assert status == 200, reply


def test_unreleased_nonholder_edits_and_member_claim_are_refused(hub, fresh):
    """N1 pre-take and N2: both-pass controls on unreleased work."""
    a = agent(fresh, "a")
    other = fresh.connect("other")
    number = a.work.task_add(board="work", title="Protected work", claim=True)["number"]
    handoff(a, number)
    assert_nonholder_refused(hub, fresh, other, number)
    member = hub.db.create_user("member@example.test")
    hub.db.add_member(fresh.workspace, member, "member", actor=fresh.user)
    person = service(hub, sessions.person_principal(hub.db, member, fresh.workspace))
    before = fresh.owner.task_show(number)
    with pytest.raises(ToolError, match="owner release"):
        person.task_update(number, status="doing")
    assert fresh.owner.task_show(number) == before


def test_creator_self_marker_does_not_release_own_origin(hub, fresh):
    """A historical self marker grants no owning-root release."""
    a = agent(fresh, "a")
    number = a.work.task_add(board="work", title="Self marker", claim=True)["number"]
    handoff(a, number)
    # Test-only historical marker; ordinary approval refuses creator self-release.
    with a.work.store.transaction() as conn:
        conn.execute("UPDATE tasks SET approved_by=? WHERE id=?", (a.principal.actor, number))
    other = fresh.connect("other")
    before = fresh.owner.task_show(number)
    assert change(hub, fresh, other.token, number, status_line="Self release is invalid")[0] == 403
    assert fresh.owner.task_show(number) == before


@pytest.mark.parametrize("shape", [("untrusted", "inbox"), ("trusted", "shared")])
def test_subagent_takers_gain_no_top_level_authority(hub, fresh, shape):
    """N3: restricted and trusted subagents retain the original origin boundary."""
    a, root = agent(fresh, "a"), fresh.connect("root")
    child = agent(fresh, "child", shape, parent=root.id)
    number = a.work.task_add(board="work", title="Top-level work", claim=True)["number"]
    handoff(a, number)
    before = fresh.owner.task_show(number)
    with pytest.raises(ToolError):
        child.work.task_take_next(task=number)
    assert child.work.task_take_next(board="work") is None
    with pytest.raises(ToolError):
        child.work.task_update(number, status="doing")
    assert fresh.owner.task_show(number) == before


def test_unreadable_board_stays_hidden(fresh):
    """N4: read_wings can grant ops explicitly; another agent cannot discover it."""
    fresh.owner.board_add(slug="ops", wing="ops")
    a = agent(fresh, "a", read_wings=["shared", "ops"])
    b = agent(fresh, "b")
    number = a.work.task_add(board="ops", title="Separate work")["number"]
    before = fresh.owner.task_show(number)
    for args in ({"task": number}, {"board": "ops"}):
        with pytest.raises(ToolError, match="not found"):
            b.work.task_take_next(**args)
    assert fresh.owner.task_show(number) == before


def test_foreign_workspace_has_no_authority(hub, fresh):
    """N5: lookups, approval, and parent linking stay workspace-scoped."""
    a = agent(fresh, "a")
    number = a.work.task_add(board="work", title="Local work")["number"]
    foreign = fresh.connect("foreign", user_ref=hub.admin, workspace_ref=hub.workspace)
    foreign.work.board_add(slug="work", wing="shared")
    before = fresh.owner.task_show(number)
    assert not mcp(hub, foreign.token, "task_take_next", task=number)[0]
    assert api(hub, foreign.token, "POST", f"/api/cards/{number}/approve", {})[0] == 404
    status, reply = api(
        hub, foreign.token, "POST", "/api/boards/work/cards", {"title": "Refused", "parent": number}
    )
    assert status == 400, reply
    assert fresh.owner.task_show(number) == before


def test_unrelated_release_and_cross_root_parent_are_refused(hub, fresh):
    """N6: an intermediate restricted parent and siblings cannot release work."""
    root = fresh.connect("root")
    mid = agent(fresh, "mid", parent=root.id)
    child = agent(fresh, "child", parent=mid.id)
    sibling = agent(fresh, "sibling", parent=mid.id)
    unrelated = fresh.connect("unrelated")
    number = proposed(child)
    before = fresh.owner.task_show(number)
    assert change(hub, fresh, unrelated.token, number, "POST", "/approve")[0] == 403
    assert fresh.owner.task_show(number) == before
    approved = allowed(lambda: mid.work.task_approve(number, reason="synthetic reason"))
    assert approved["approved_by"] is None
    take(hub, child, number)
    handoff(child, number)
    before = fresh.owner.task_show(number)
    for who in (unrelated, sibling):
        with pytest.raises(ToolError):
            who.work.task_take_next(task=number)
        assert fresh.owner.task_show(number) == before
    other_card = unrelated.work.task_add(board="work", title="Another root")
    status, reply = api(
        hub,
        child.token,
        "POST",
        "/api/boards/work/cards",
        {"title": "Refused", "parent": other_card["number"]},
    )
    assert status == 400 and "top-level actor" in reply["error"], reply


@pytest.mark.parametrize("authority", ["root", "coordinator"])
def test_pending_release_lapses_live(hub, fresh, authority):
    """N7: normal parent revocation; test-only historical coordinator marker."""
    root = fresh.connect("root")
    child = agent(fresh, "child", parent=root.id)
    receiver = agent(fresh, "receiver")
    number = proposed(child)
    allowed(lambda: root.work.task_approve(number, reason="synthetic reason"))
    take(hub, child, number)
    handoff(child, number)
    if authority == "root":
        hub.db.revoke(root.id, actor=fresh.user, workspace_ref=fresh.workspace)
    else:
        k = coordinator(hub, fresh)
        # Test-only historical marker, not a normal-route release on the base.
        with child.work.store.transaction() as conn:
            conn.execute("UPDATE tasks SET approved_by=? WHERE id=?", (k.principal.actor, number))
        status, reply = api(hub, fresh.cookie, "PATCH", "/api/boards/work", {"coordinator": None})
        assert status == 200, reply
    before = fresh.owner.task_show(number)
    with pytest.raises(ToolError):
        receiver.work.task_take_next(task=number)
    assert receiver.work.task_take_next(board="work") is None
    assert fresh.owner.task_show(number) == before


@pytest.mark.parametrize("reuse", ["none", "later", "different-user"])
def test_creator_revocation_and_historical_name_reuse_fail_closed(hub, fresh, reuse):
    """N8: base-reachable reservations, real duplicate refusal, labelled history."""
    a, b = agent(fresh, "a"), agent(fresh, "b")
    numbers = []
    for target in ("anyone", b.principal.actor):
        number = a.work.task_add(board="work", title="Old work", claim=True)["number"]
        handoff(a, number, target)
        numbers.append(number)
    hub.db.revoke(a.id, actor=fresh.user, workspace_ref=fresh.workspace)
    with pytest.raises(ControlError, match="already exists"):
        fresh.connect("a")
    if reuse != "none":
        # Test-only: simulates historical name reuse; unreachable through the API:
        # names are unique even when revoked.
        hub.db._conn.execute("UPDATE connections SET name='a-retired' WHERE id=?", (a.id,))
        kwargs = {}
        if reuse == "different-user":
            member = hub.db.create_user("replacement@example.test")
            hub.db.add_member(fresh.workspace, member, "member", actor=fresh.user)
            kwargs["user_ref"] = member
        replacement = agent(fresh, "a", **kwargs)
        if reuse == "later":
            hub.db._conn.execute(
                "UPDATE connections SET created_at='2999-01-01T00:00:00Z' WHERE id=?",
                (replacement.id,),
            )
    for number in numbers:
        before = fresh.owner.task_show(number)
        with pytest.raises(ToolError):
            b.work.task_take_next(task=number)
        assert fresh.owner.task_show(number) == before
    assert b.work.task_take_next(board="work") is None


@pytest.mark.parametrize("kind", ["ask", "handoff", "completion"])
def test_person_decisions_stay_protected_on_base_reachable_cards(hub, fresh, kind):
    """N9: all decision negatives run without a head-only holder setup."""
    a, b = agent(fresh, "a"), agent(fresh, "b")
    if kind == "ask":
        card = allowed(
            lambda: a.work.ask(
                REVIEW_QUESTION, REVIEW_OPTIONS, board="work", context=REVIEW_CONTEXT
            )
        )
        number = card["number"]
    else:
        number = a.work.task_add(board="work", title="Decision work", claim=True)["number"]
        if kind == "handoff":
            allowed(
                lambda: a.work.task_handoff(
                    number,
                    f"user:{fresh.user}",
                    REVIEW_QUESTION,
                    REVIEW_OPTIONS,
                    context=REVIEW_CONTEXT,
                )
            )
        else:
            person_review(a, number)
    for who in (a, b):
        assert_person_review_protected(hub, fresh, who, number)
    child = agent(fresh, "child", parent=a.id)
    # task_add has no ask argument on either revision. Exercise its ordinary
    # rejection as text, plus the actual child-origin ask guard via ask().
    assert not mcp(
        hub, child.token, "task_add", board="work", title="Refused ask", ask=REVIEW_QUESTION
    )[0]
    with pytest.raises(ToolError, match="proposal"):
        child.work.ask(REVIEW_QUESTION, REVIEW_OPTIONS, board="work", context=REVIEW_CONTEXT)
    proposal = child.work.task_add(board="work", title="Still a proposal")
    assert proposal["status"] == "backlog" and proposal["proposal_status"] == "pending"


def test_backlog_stays_person_triage(fresh):
    """N10: test-only legacy lane shape; backlog gains no claim authority."""
    a, b = agent(fresh, "a"), agent(fresh, "b")
    number = a.work.task_add(board="work", title="Legacy backlog")["number"]
    # Test-only historical lane, matching the existing backlog regression.
    with a.work.store.transaction() as conn:
        conn.execute("UPDATE tasks SET status='backlog',assignee=NULL WHERE id=?", (number,))
    before = fresh.owner.task_show(number)
    with pytest.raises(ToolError):
        b.work.task_take_next(task=number)
    with pytest.raises(ToolError):
        b.work.task_update(number, status="todo")
    assert b.work.task_take_next(board="work") is None
    assert fresh.owner.task_show(number) == before


def test_restricted_subagent_write_branch_is_unchanged(fresh):
    """N11: restricted children still hit the existing explicit-take write gate."""
    root = fresh.connect("root")
    child = agent(fresh, "child", parent=root.id)
    number = fresh.owner.task_add(board="work", title="Owner work")["number"]
    before = fresh.owner.task_show(number)
    with pytest.raises(ToolError, match="can only add cards and comments"):
        child.work.task_take_next(task=number)
    assert fresh.owner.task_show(number) == before


@pytest.mark.parametrize("authority", ["root", "coordinator"])
def test_rejected_reapproval_stays_owner_only(hub, fresh, authority):
    """N12: both refuse; only the root's exact error is shared with base."""
    root = fresh.connect("root")
    child = agent(fresh, "child", parent=root.id)
    k = coordinator(hub, fresh)
    number = proposed(child)
    allowed(lambda: root.work.task_approve(number, approve=False, reason="synthetic reason"))
    before = fresh.owner.task_show(number)
    who = root if authority == "root" else k
    status, reply = change(hub, fresh, who.token, number, "POST", "/approve")
    assert status in (400, 403), reply
    if authority == "root":
        assert "not a pending proposal" in reply["error"]
    assert fresh.owner.task_show(number) == before


@pytest.mark.parametrize("shape", RESTRICTED)
def test_reserved_restricted_update_names_claim_route(hub, fresh, shape):
    a, b = agent(fresh, "a", shape), agent(fresh, "b", shape)
    number = a.work.task_add(board="work", title="Reserved work", claim=True)["number"]
    handoff(a, number, b.principal.actor)
    before = fresh.owner.task_show(number)
    with pytest.raises(ToolError) as exc:
        b.work.task_update(number, status="doing")
    assert type(exc.value) is ToolError
    assert "task_take_next" in str(exc.value)
    assert fresh.owner.task_show(number) == before
    status, reply = change(hub, fresh, b.token, number, status="doing")
    assert status == 403, reply
    assert "task_take_next" in reply["error"]
    assert fresh.owner.task_show(number) == before
    take(hub, b, number)
    claimed = fresh.owner.task_show(number)
    assert claimed["status"] == "doing"
    assert claimed["assignee"] == b.principal.actor
    assert claimed["claim_until"] and not claimed["claim_expired"]


@pytest.mark.parametrize("kind", ["other", "subagent", "waiting"])
def test_restricted_update_keeps_release_message(hub, fresh, kind):
    a, b = agent(fresh, "a"), agent(fresh, "b")
    who = agent(fresh, "child", parent=b.id) if kind == "subagent" else b
    number = a.work.task_add(board="work", title="Protected work", claim=True)["number"]
    handoff(a, number, a.principal.actor if kind == "other" else who.principal.actor)
    if kind == "waiting":
        # Test-only legacy todo reservation waiting on a person.
        with a.work.store.transaction() as conn:
            conn.execute("UPDATE tasks SET ask_to=? WHERE id=?", (f"user:{fresh.user}", number))
    before = fresh.owner.task_show(number)
    with pytest.raises(
        ToolError, match="restricted-origin work needs parent or workspace owner release"
    ):
        who.work.task_update(number, status="doing")
    assert fresh.owner.task_show(number) == before
    with pytest.raises(ToolError, match="card is not available to take"):
        who.work.task_take_next(task=number)
    assert fresh.owner.task_show(number) == before
