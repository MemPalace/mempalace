"""Only a live coordinator lease of exactly this board makes a seat the board coordinator."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext, ToolError
from commonplace.orch import durable, schema
from commonplace.projects import split
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_origin_holder_release import TOOLS, edit, policy, raw, snapshot
from tests.test_orch_durable import BOARD, MACHINE, ROLE, T0, Env, at, config_text

REASON = "Independent origin checked"
PAST = "2020-01-01T00:00:00Z"


@pytest.fixture
def env(hub):
    return Env(hub)


def work_for(env, principal):
    return BoardService(ToolContext(principal=principal, gateway=env.hub.gateway))


def creator_card(env, board=BOARD):
    token = env.hub.token(
        "creator",
        profile="standard",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wing="inbox",
        trust="untrusted",
    )
    creator = work_for(env, env.hub.gateway.authenticate(f"Bearer {token}"))
    return creator, creator.task_add(board=board, title="Restricted work")["number"]


def restricted_card(env, board=BOARD):
    return creator_card(env, board)[1]


def refuse(env, work, number, call, match="only the direct parent|not available to take"):
    before = snapshot(env.owner_board, number)
    with pytest.raises(ToolError, match=match):
        call()
    assert snapshot(env.owner_board, number) == before


def approve(work, number):
    return work.task_approve(number, approve=True, reason=REASON)


def test_the_live_coordinator_lease_holder_releases_the_card(hub, env):
    number = restricted_card(env)
    seat = work_for(env, env.a)
    refuse(env, seat, number, lambda: approve(seat, number))
    env.resume(env.a)
    card = approve(seat, number)
    assert card["status"] == "todo"
    assert raw(env.owner_board, number)["approved_by"] == env.a.actor


def test_a_replaced_epoch_is_refused_and_the_new_holder_is_accepted(hub, env):
    number = restricted_card(env)
    first, second = work_for(env, env.a), work_for(env, env.b)
    assert env.resume(env.a)["epoch"] == 1
    assert env.resume(env.b, force=False, now=at(durable.TTL_SECONDS + 1, T0))["epoch"] == 2
    refuse(env, first, number, lambda: approve(first, number))
    assert approve(second, number)["status"] == "todo"


def test_an_expired_lease_is_refused(hub, env):
    number = restricted_card(env)
    seat = work_for(env, env.a)
    env.resume(env.a, now=PAST)
    refuse(env, seat, number, lambda: approve(seat, number))


def test_a_released_lease_is_refused(hub, env):
    number = restricted_card(env)
    seat = work_for(env, env.a)
    claim = env.resume(env.a)
    durable.release(env.ctx(env.a), BOARD, ROLE, claim["epoch"], claim["token"], now=T0)
    refuse(env, seat, number, lambda: approve(seat, number))


def test_a_lease_for_another_board_is_refused(hub, env):
    number = restricted_card(env, board="plain")
    seat = work_for(env, env.a)
    env.resume(env.a)
    refuse(env, seat, number, lambda: approve(seat, number))


def test_a_seat_without_the_lease_is_refused_while_another_holds_it(hub, env):
    number = restricted_card(env)
    other = work_for(env, env.b)
    env.resume(env.a)
    refuse(env, other, number, lambda: approve(other, number))


def test_a_lease_holder_that_is_the_cards_creator_cannot_take_over_its_reservation(hub, env):
    number = restricted_card(env)
    env.owner_board.task_approve(number, reason=REASON)
    env.resume(env.a)
    edit(env.owner_board, number, created_by=env.a.actor, assignee=env.b.actor)
    seat = work_for(env, env.a)
    refuse(
        env,
        seat,
        number,
        lambda: seat.task_take_next(task=number),
        match="no: card is not available to take",
    )


NOTE = "Handing this to the orchestrator"


def numbers(listing):
    return [item["number"] for item in listing["items"]]


def test_a_lease_only_holder_sees_the_card_it_can_release(hub, env):
    number = restricted_card(env)
    principal = env.a
    seat = work_for(env, principal)
    other = work_for(env, env.b)
    assert numbers(seat.approvals_for_me()) == []
    env.resume(principal)
    assert seat.release_boards()
    assert number in numbers(seat.approvals_for_me())
    assert number not in numbers(env.owner_board.approvals_for_me())
    assert not other.release_boards()
    assert numbers(other.approvals_for_me()) == []


def test_a_lease_only_holder_sees_a_pending_proposal(hub, env):
    number = env.owner_board.task_add(board=BOARD, title="Plain work", status="backlog")["number"]
    edit(env.owner_board, number, proposal_status="pending", proposed_to=None)
    seat = work_for(env, env.a)
    assert number not in numbers(seat.approvals_for_me())
    env.resume(env.a)
    assert number in numbers(seat.approvals_for_me())


def test_the_owner_keeps_the_recovery_item_while_the_lease_holder_cannot_release(hub, env):
    number = restricted_card(env)
    env.resume(env.a)
    hidden = [t for t in TOOLS if t != "needs_me"]
    policy(hub, SimpleNamespace(id=env.a.connection_id, principal=env.a), tools=hidden)
    seat = work_for(env, hub.db.authenticate_connection(env.a.connection_id))
    assert number not in numbers(seat.approvals_for_me())
    assert number in numbers(env.owner_board.approvals_for_me())


def test_a_lease_only_holder_is_an_approver_candidate_and_a_supervisor(hub, env):
    number = restricted_card(env)
    env.resume(env.a)
    edit(env.owner_board, number, proposal_status="pending", status="backlog")
    row = raw(env.owner_board, number)
    assert dict(env.owner_board._live_approvers(row)).get(env.a.actor) == "board coordinator"
    row = raw(env.owner_board, number)
    row["assignee"] = "conn:creator"
    assert dict(env.owner_board._supervisors(row)).get(env.a.actor) == "board coordinator"


def test_the_orchestrator_alias_reaches_the_lease_holder(hub, env):
    creator, number = creator_card(env)
    env.owner_board.task_approve(number, reason=REASON)
    assert creator.task_take_next(task=number)["status"] == "doing"
    with pytest.raises(ToolError, match="no board coordinator"):
        creator.task_handoff(number, "orchestrator", NOTE)
    env.resume(env.a)
    card = creator.task_handoff(number, "orchestrator", NOTE)
    assert card["assignee"] == env.a.actor


def test_a_locked_runner_store_fails_closed(hub, env, monkeypatch):
    number = restricted_card(env)
    seat = work_for(env, env.a)
    env.resume(env.a)

    def locked(directory):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(split, "runner_read", locked)
    assert env.owner_board.task_show(number)["awaiting_release"] is True
    refuse(env, seat, number, lambda: approve(seat, number))


def test_the_lease_read_fails_closed_on_every_unusable_state(hub, env):
    env.resume(env.a)
    expected = env.a.actor
    with env.runner_db() as conn:
        assert durable.coordinator_holder_in(conn, BOARD) == expected
        assert durable.coordinator_holders_in(conn) == {BOARD: expected}
        assert durable.coordinator_holder_in(conn, "plain") is None
        conn.execute(
            "INSERT INTO orch_leases(board,role,epoch,holder,holder_actor,token_hash,claimed_at,"
            "expires_at) VALUES ('plain',?,1,'x',?,'h','2030-01-01T00:00:00Z','2031-01-01T00:00:00Z')",
            (ROLE, expected),
        )
        assert durable.coordinator_holder_in(conn, "plain") is None
        conn.execute("UPDATE orch_configs SET body='not a config [' WHERE board=?", (BOARD,))
        assert durable.coordinator_holder_in(conn, BOARD) is None
        assert durable.coordinator_holders_in(conn) == {}
    assert durable.coordinator_holder_in(None, BOARD) is None
    assert durable.coordinator_holders_in(None) == {}
    empty = sqlite3.connect(":memory:")
    assert durable.coordinator_holder_in(empty, BOARD) is None
    assert durable.coordinator_holders_in(empty) == {}


def test_a_pending_restore_names_nobody(hub, env):
    env.resume(env.a)
    with env.runner_db() as conn:
        assert durable.coordinator_holder_in(conn, BOARD) == env.a.actor
        conn.execute("UPDATE orch_state SET restore_flag=1 WHERE id=1")
        assert durable.coordinator_holder_in(conn, BOARD) is None


def found(obj, number):
    if isinstance(obj, dict):
        if obj.get("number") == number:
            return obj
        obj = list(obj.values())
    if isinstance(obj, list):
        for value in obj:
            hit = found(value, number)
            if hit is not None:
                return hit
    return None


def pending_plain_card(env):
    number = env.owner_board.task_add(board=BOARD, title="Plain work", status="backlog")["number"]
    edit(env.owner_board, number, proposal_status="pending", proposed_to=None)
    return number


@pytest.mark.parametrize("sees", [True, False])
@pytest.mark.parametrize("route", ["lease", "configured"])
def test_the_owner_item_for_a_pending_proposal_follows_whether_a_candidate_sees_it(
    hub, env, route, sees
):
    number = pending_plain_card(env)
    if route == "lease":
        env.resume(env.a)
    else:
        env.owner_board.board_set(BOARD, env.a.actor)
    if not sees:
        hidden = [t for t in TOOLS if t != "needs_me"]
        policy(hub, SimpleNamespace(id=env.a.connection_id, principal=env.a), tools=hidden)
    seat = work_for(env, hub.db.authenticate_connection(env.a.connection_id))
    assert (number in numbers(seat.approvals_for_me())) is sees
    assert (found(env.owner_board.needs_me(), number) is None) is sees


@pytest.mark.parametrize("trust_after", ["trusted", "standard"])
def test_a_member_role_lease_holder_sees_its_card(hub, env, trust_after):
    number = restricted_card(env)
    user = hub.db.create_user(f"member-{trust_after}@example.test")
    hub.db.add_member(env.ws, user, "member", actor=hub.admin)
    token = hub.token(
        f"member-{trust_after}",
        user=user,
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wings=["shared"],
        trust="trusted",
    )
    member = hub.gateway.authenticate(f"Bearer {token}")
    assert member.role == "member"
    requesters = [env.a, env.b, env.seat, member]
    with hub.db._lock, hub.db._tx() as conn:
        conn.execute(
            "UPDATE runner_machines SET requesters=? WHERE id=?",
            (json.dumps([p.connection_id for p in requesters]), MACHINE),
        )
    env.resume(member)
    if trust_after != "trusted":
        policy(hub, SimpleNamespace(id=member.connection_id, principal=member), trust=trust_after)
    seat = work_for(env, hub.db.authenticate_connection(member.connection_id))
    assert seat.principal.policy.trust == trust_after
    assert seat.release_boards()
    assert number in numbers(seat.approvals_for_me())
    assert number not in numbers(env.owner_board.approvals_for_me())


@pytest.mark.parametrize("roles", ["none", "two"])
def test_the_lease_read_needs_exactly_one_orchestrator_role(hub, env, roles):
    env.resume(env.a)
    body = config_text()
    if roles == "none":
        body = body.replace('[roles.main]\nkind = "orchestrator"', '[roles.main]\nkind = "builder"')
    else:
        body = body.replace(
            '[roles.builder]\nkind = "builder"\nparent = "main"',
            '[roles.builder]\nkind = "orchestrator"',
        )
    kinds = [role.kind for role in schema.parse(body).roles.values()]
    assert kinds.count("orchestrator") == (0 if roles == "none" else 2)
    with env.runner_db() as conn:
        assert durable.coordinator_holder_in(conn, BOARD) == env.a.actor
        conn.execute(
            "UPDATE orch_configs SET body=? WHERE board=? AND status='accepted'", (body, BOARD)
        )
        assert durable.coordinator_holder_in(conn, BOARD) is None
        assert durable.coordinator_holders_in(conn) == {}
