"""Guard units for the owner-granted cards:decide scope (new on this branch)."""

from dataclasses import replace

import pytest

from commonplace import decide_grant, sessions
from commonplace.extensions import ToolError
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_cards_decide_scope import (
    HIDDEN,
    OPTIONS,
    ask,
    delegated_audit,
    owner_app,
    rest_decide,
    service,
    world as world,
)


def granted(hub, name, **policy):
    """A top-level owner PAT, then the signed-in owner's grant, as the browser does."""
    token = hub.token(name, profile="slim", trust="untrusted", **policy)
    cid = hub.gateway.authenticate(f"Bearer {token}").connection_id
    person = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    hub.db.set_connection_decide(cid, True, actor=person, workspace_ref=hub.workspace)
    return token


def test_answers_for_requires_every_condition(world):
    token, _cid = owner_app(world)
    p = world.hub.gateway.authenticate(f"Bearer {token}")
    row = {"ask_to": f"user:{p.user_id}"}
    assert decide_grant.answers_for(p, row)
    for changed in (
        replace(p, person=True),
        replace(p, auth="web"),
        replace(p, role="member"),
        replace(p, parent_connection_id="conn_parent"),
        replace(p, runner="worker"),
        replace(p, policy=replace(p.policy, scopes=("memory:read", "memory:write"))),
    ):
        assert not decide_grant.answers_for(changed, row)
    assert not decide_grant.answers_for(p, {"ask_to": "user:someone-else"})
    assert not decide_grant.answers_for(p, {"ask_to": "conn:coordinator"})
    assert not decide_grant.answers_for(p, {"ask_to": None})


def test_delegated_decide_still_needs_board_access(world):
    w = world
    hub = w.hub
    number, version = ask(w)
    # The grant does not widen board visibility: an unreadable board stays not found.
    narrow = granted(
        hub,
        "narrow-app",
        scopes=["memory:read", "memory:write"],
        read_wings=["elsewhere"],
        write_wing="inbox",
    )
    assert rest_decide(w, narrow, number, version)[0] == 404
    # Nor does it replace memory:write.
    read_only = granted(hub, "read-only-app", scopes=["memory:read"], read_wings=["*"])
    status, refused = rest_decide(w, read_only, number, version)
    assert status == 403 and HIDDEN in refused["error"]
    p = hub.gateway.authenticate(f"Bearer {read_only}")
    with pytest.raises(ToolError, match="cannot change this board"):
        service(hub, p).decide(number, choice=OPTIONS[0], version=version)
    assert w.owner.task_show(number)["status"] == "review"
    assert delegated_audit(hub) == []


def test_delegated_app_cannot_escalate(world):
    w = world
    token, _cid = owner_app(w)
    number, version = ask(w)
    status, refused = api_decide(w, token, number, version)
    assert status == 403 and "only the addressed coordinator may escalate" in refused["error"]
    assert w.owner.task_show(number)["status"] == "review"


def api_decide(w, token, number, version):
    from tests.test_cards_decide_scope import api

    return api(
        w.hub,
        token,
        "POST",
        f"/api/cards/{number}/decide",
        {"version": version, "reply": "Over to the owner.", "then": "escalate"},
    )
