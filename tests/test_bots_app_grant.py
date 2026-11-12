"""A person's own app uses their bots through the granted bots:chat and bots:confirm scopes."""

import json
import re
from html import unescape
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from commonplace import bots_grant, control_db, sessions
from commonplace.control_db import ControlError
from tests._bots_helpers import bots_hub_fixture  # noqa: F401
from tests.test_bots_api import routes_for
from tests.test_bots_turn import (  # noqa: F401
    Stream,
    call,
    configure,
    confirm,
    confirm_tool,
    extra_tools,
    fake,
    names,
    new_card,
    pending_turn,
    thread,
)
from tests.test_commonplace_oauth import Client

CHAT = "bots:chat"
CONFIRM = "bots:confirm"
WORDS = "zebra quartz lantern 7731"


def bearer(token):
    return {"Authorization": "Bearer " + token}


def grant(hub, cid, user, *, chat=True, confirm=False):
    hub.db.set_connection_bots(
        cid,
        chat=chat,
        confirm=confirm,
        actor=hub.person(user),
        workspace_ref=hub.workspace,
    )


def app(hub, user=None, name="phone", *, chat=True, confirm=False, **policy):
    """A person's own PAT app, optionally granted the scopes through the control layer."""
    user = user or hub.hub.admin
    policy = {
        "profile": "slim",
        "scopes": ["memory:read"],
        "read_wings": ["shared"],
        "write_wing": "inbox",
        "trust": "standard",
        **policy,
    }
    token = hub.hub.token(name, user=user, **policy)
    cid = hub.principal(token).connection_id
    if chat:
        grant(hub, cid, user, chat=True, confirm=confirm)
    return token, cid


# An app that could be made a board's coordinator (it writes the board's wing).
COORDINATOR = {
    "profile": "full",
    "scopes": ["memory:read", "memory:write"],
    "read_wings": ["*"],
    "write_wings": ["shared"],
    "write_wing": "",
    "trust": "trusted",
}


def rows(hub, **match):
    return [
        dict(r)
        for r in hub.db.audit_entries(hub.workspace, limit=500)
        if all(r[k] == v for k, v in match.items())
    ]


def every_route(bot_id):
    return [
        ("GET", "/api/bots", None),
        ("POST", "/api/bots", {"name": "App bot"}),
        *routes_for(bot_id),
        (
            "POST",
            f"/api/bots/{bot_id}/messages?stream=0",
            {"text": "hi", "request_id": "request-0001"},
        ),
        ("POST", f"/api/bots/{bot_id}/stop", {"request_id": "request-0001"}),
        ("POST", f"/api/bots/{bot_id}/confirm", {"pending": "x", "accept": True}),
    ]


def owner_bot(hub):
    return hub.by_board(hub.owner)["work"]["id"]


def set_policy_scopes(hub, cid, *scopes):
    """Store scopes the way an older build could have, bypassing every grant check."""
    with hub.db._lock:
        row = hub.db._conn.execute("SELECT policy FROM connections WHERE id=?", (cid,)).fetchone()
        policy = json.loads(row["policy"])
        policy["scopes"] = sorted(set(policy["scopes"]) | set(scopes))
        hub.db._conn.execute(
            "UPDATE connections SET policy=? WHERE id=?", (json.dumps(policy), cid)
        )


# A token without the scope is unchanged


def test_a_token_without_the_scope_gets_404_on_every_route(bots_hub):
    hub = bots_hub
    bot_id = owner_bot(hub)
    token, _cid = app(hub, chat=False)
    for method, path, body in every_route(bot_id):
        status, reply, _ = hub.request(token, method, path, body)
        assert (status, reply.get("code")) == (404, "not_found"), (method, path)
    assert hub.request(None, "GET", "/api/bots")[0] == 401
    # Another scope is not this one.
    other = hub.hub.token(
        "reader", scopes=["memory:read", "memory:write"], read_wings=["*"], trust="standard"
    )
    assert hub.request(other, "GET", "/api/bots")[0] == 404


# The chat scope acts as the granting person


def test_an_app_with_bots_chat_reads_writes_and_sends_as_its_person(bots_hub, fake):  # noqa: F811
    hub = bots_hub
    configure(hub)
    token, _cid = app(hub)
    listing = hub.bots(token)
    assert listing["can_configure"] is True
    assert {b["id"] for b in listing["bots"]} == {b["id"] for b in hub.bots(hub.owner)["bots"]}
    bot_id = hub.by_board(token)["work"]["id"]
    # create, rename, memory
    status, body, _ = hub.post(token, "/api/bots", {"name": "From my phone"})
    assert status == 201
    status, body, _ = hub.request(
        token, "PATCH", f"/api/bots/{body['bot']['id']}", {"name": "Renamed"}
    )
    assert (status, body["bot"]["name"]) == (200, "Renamed")
    note = hub.post(token, f"/api/bots/{bot_id}/memory", {"text": "likes tea"})[1]["memory"]
    assert [m["text"] for m in hub.get(token, f"/api/bots/{bot_id}/memory")[1]["memories"]] == [
        "likes tea"
    ]
    assert hub.request(token, "DELETE", f"/api/bots/{bot_id}/memory/{note['id']}")[0] == 200
    # send a message, streamed, and read it back in the person's own session
    fake.script = [[("text", "Hello from the bot.")]]
    stream = Stream(hub, bearer(token), bot_id, "Hello there", "request-0001")
    assert stream.status == 200
    events = stream.until_end()
    assert names(events)[0] == "ack" and names(events)[-1] == "done"
    messages = thread(hub, bot_id)["messages"]
    assert [m["text"] for m in messages] == ["Hello there", "Hello from the bot."]
    assert [m["text"] for m in thread(hub, bot_id, token)["messages"]] == [
        m["text"] for m in messages
    ]
    # stop and read
    status, body, _ = hub.post(token, f"/api/bots/{bot_id}/stop", {"request_id": "request-0001"})
    assert (status, body) == (200, {"ok": False})
    reply = messages[-1]["id"]
    assert hub.by_board(token)["work"]["unread"] == 1
    assert hub.post(token, f"/api/bots/{bot_id}/read", {"upto": reply})[1] == {"ok": True}
    assert hub.by_board(hub.owner)["work"]["unread"] == 0


def test_an_app_sees_only_its_own_persons_bots(bots_hub):
    hub = bots_hub
    owner_token, _ = app(hub, name="owner-phone")
    member_token, _ = app(hub, hub.member_user, "member-phone")
    owner_ids = {b["id"] for b in hub.bots(owner_token)["bots"]}
    member_ids = {b["id"] for b in hub.bots(member_token)["bots"]}
    assert owner_ids and member_ids and not owner_ids & member_ids
    assert member_ids == {b["id"] for b in hub.bots(hub.member)["bots"]}
    hub.store().memory_add(next(iter(owner_ids)), hub.hub.admin, "private")
    for bot_id, token in (
        (next(iter(owner_ids)), member_token),
        (next(iter(member_ids)), owner_token),
    ):
        for method, path, body in routes_for(bot_id):
            status, reply, _ = hub.request(token, method, path, body)
            assert (status, reply.get("code")) == (404, "not_found"), (method, path)
    # An app of another workspace's person is not this workspace's either.
    stranger = app_for_workspace(hub)
    assert hub.request(stranger, "GET", f"/api/bots/{next(iter(owner_ids))}/messages")[0] == 404


def app_for_workspace(hub):
    _cid, token = hub.db.create_token(
        workspace_ref=hub.other_workspace,
        user_ref=hub.other_user,
        name="stranger-phone",
        client="test",
        policy=control_db.Policy.build(scopes=["memory:read"], read_wings=["*"], trust="standard"),
        actor=hub.other_user,
    )
    cid = hub.principal(token).connection_id
    hub.db.set_connection_bots(
        cid,
        chat=True,
        confirm=False,
        actor=sessions.person_principal(hub.db, hub.other_user, hub.other_workspace),
        workspace_ref=hub.other_workspace,
    )
    return token


# Confirming needs its own scope


def test_confirm_needs_its_own_scope_and_runs_once(bots_hub, fake, confirm_tool):  # noqa: F811
    hub = bots_hub
    configure(hub)
    chat_only, _ = app(hub, name="chat-only")
    both, _ = app(hub, name="both", confirm=True)
    bot, _events, block = pending_turn(hub, fake)
    status, body, _ = confirm(hub, bot["id"], block["pending"], auth=bearer(chat_only))
    assert (status, body["code"]) == (403, "scope_required")
    assert confirm_tool == []
    assert hub.store().pending_get(bot["id"], hub.hub.admin, block["pending"])["state"] == "open"
    status, body, _ = confirm(hub, bot["id"], block["pending"], auth=bearer(both))
    assert status == 200, body
    assert len(confirm_tool) == 1
    assert confirm(hub, bot["id"], block["pending"], auth=bearer(both))[0] == 409
    assert len(confirm_tool) == 1


def test_confirm_is_useless_without_chat_and_never_granted_alone(bots_hub):
    hub = bots_hub
    token, cid = app(hub, chat=False)
    with pytest.raises(ControlError):
        hub.db.set_connection_bots(
            cid,
            chat=False,
            confirm=True,
            actor=hub.person(hub.hub.admin),
            workspace_ref=hub.workspace,
        )
    assert CONFIRM not in hub.principal(token).policy.scopes
    set_policy_scopes(hub, cid, CONFIRM)  # a stored confirm without chat opens nothing
    assert hub.request(token, "GET", "/api/bots")[0] == 404
    grant(hub, cid, hub.hub.admin, chat=True, confirm=True)
    grant(hub, cid, hub.hub.admin, chat=False)  # withdrawing chat takes both
    scopes = hub.principal(token).policy.scopes
    assert CHAT not in scopes and CONFIRM not in scopes


# Revoking


def test_revoking_the_scope_or_the_connection_stops_access_at_once(bots_hub):
    hub = bots_hub
    token, cid = app(hub, confirm=True)
    assert hub.request(token, "GET", "/api/bots")[0] == 200
    grant(hub, cid, hub.hub.admin, chat=False)
    assert hub.request(token, "GET", "/api/bots")[0] == 404
    grant(hub, cid, hub.hub.admin, chat=True)
    assert hub.request(token, "GET", "/api/bots")[0] == 200
    hub.db.revoke(cid, actor=hub.hub.admin, workspace_ref=hub.workspace)
    assert hub.request(token, "GET", "/api/bots")[0] == 401


def test_an_expired_connection_is_refused(bots_hub):
    hub = bots_hub
    token, cid = app(hub)
    fresh, fresh_cid = app(hub, name="fresh-phone", chat=False)
    assert hub.request(token, "GET", "/api/bots")[0] == 200
    with hub.db._lock:
        hub.db._conn.execute(
            "UPDATE connections SET expires_at='2020-01-01T00:00:00Z' WHERE id IN (?, ?)",
            (cid, fresh_cid),
        )
    assert hub.request(token, "GET", "/api/bots")[0] == 401
    with pytest.raises(ControlError, match=bots_grant.INACTIVE_TEXT):  # nor granted while expired
        grant(hub, fresh_cid, hub.hub.admin)
    grant(hub, cid, hub.hub.admin, chat=False)  # but withdrawing still works


def test_a_member_losing_their_membership_ends_their_app(bots_hub):
    hub = bots_hub
    token, _ = app(hub, hub.member_user, "member-phone")
    assert hub.request(token, "GET", "/api/bots")[0] == 200
    with hub.db._lock:
        hub.db._conn.execute(
            "UPDATE users SET disabled_at='2020-01-01T00:00:00Z' WHERE id=?", (hub.member_user,)
        )
    assert hub.request(token, "GET", "/api/bots")[0] == 401


# Agent machinery


def machinery(hub):
    """Each kind of agent connection, as ``(label, token, id)``."""
    owner = hub.hub.admin
    parent_token, parent = app(hub, name="parent-app", chat=False)
    child_id, child_token = hub.db.create_token(
        workspace_ref=hub.workspace,
        user_ref=owner,
        name="child-agent",
        client="test",
        policy=control_db.Policy.build(scopes=["memory:read"], read_wings=["shared"]),
        actor=owner,
        parent=parent,
    )
    seat_token = hub.hub.token(
        "seat-app", client="commonplace-orchestrator", scopes=["memory:read"], read_wings=["shared"]
    )
    runner_token, runner = app(hub, name="runner-app", chat=False)
    with hub.db._lock:
        control_db.link_runner(hub.db._conn, runner, "mach_example", "worker")
    started_token, started = app(hub, name="started-app", chat=False)
    with hub.db._lock:
        hub.db._conn.execute("UPDATE connections SET started_by='agent' WHERE id=?", (started,))
    coordinator_token, coordinator = app(hub, name="coordinator-app", chat=False, **COORDINATOR)
    from tests.test_commonplace_boards import service

    service(hub.hub, hub.person(owner)).board_set("work", "conn:coordinator-app")
    return [
        ("child", child_token, child_id),
        ("seat", seat_token, hub.principal(seat_token).connection_id),
        ("runner", runner_token, runner),
        ("started by an agent", started_token, started),
        ("coordinator", coordinator_token, coordinator),
    ]


def test_agent_machinery_cannot_be_granted_and_is_refused_at_use_anyway(bots_hub):
    hub = bots_hub
    for label, token, cid in machinery(hub):
        if (
            label == "coordinator"
        ):  # only the board store knows coordinators: the REST layer refuses
            status, body, _ = hub.request(
                hub.owner, "PATCH", f"/api/connections/{cid}", {"bots_chat": True}
            )
            assert (status, body["error"]) == (409, bots_grant.AGENT_TEXT)
        else:
            with pytest.raises(control_db.ControlError):
                grant(hub, cid, hub.hub.admin)
        assert CHAT not in hub.principal(token).policy.scopes, label
        # Even a row that claims the scope (an older build, a manual edit) gets nothing.
        set_policy_scopes(hub, cid, CHAT, CONFIRM)
        assert CHAT in hub.principal(token).policy.scopes, label
        for method, path, body in every_route(owner_bot(hub)):
            status, reply, _ = hub.request(token, method, path, body)
            # A runner token is stopped earlier still, by the gateway itself.
            expected = 403 if label == "runner" else 404
            assert status == expected and "bots" not in reply, (label, method, path)
    assert hub.store().roster(hub.hub.admin)  # nothing was created by them
    assert not [r for r in rows(hub) if r["action"] == bots_grant.AUDIT_WRITE]


def test_an_app_that_becomes_a_coordinator_loses_its_grant(bots_hub):
    from tests.test_commonplace_boards import service

    hub = bots_hub
    token, cid = app(hub, name="was-an-app", confirm=True, **COORDINATOR)
    assert hub.request(token, "GET", "/api/bots")[0] == 200
    service(hub.hub, hub.person(hub.hub.admin)).board_set("work", "conn:was-an-app")
    scopes = hub.principal(token).policy.scopes
    assert CHAT not in scopes and CONFIRM not in scopes
    assert hub.request(token, "GET", "/api/bots")[0] == 404
    assert [
        r["action"] for r in rows(hub, connection_id=cid) if r["tool"] in (CHAT, CONFIRM)
    ].count(bots_grant.AUDIT_REVOKE) == 2


def test_only_the_person_themselves_can_grant_their_app(bots_hub):
    hub = bots_hub
    owner_token, owner_cid = app(hub, name="owner-phone", chat=False)
    member_token, member_cid = app(hub, hub.member_user, "member-phone", chat=False)
    member, owner = hub.person(hub.member_user), hub.person(hub.hub.admin)
    for actor, cid, text in (
        (member, owner_cid, bots_grant.NOT_YOURS_TEXT),
        (owner, member_cid, bots_grant.NOT_YOURS_TEXT),
    ):
        with pytest.raises(ControlError, match=re.escape(text)):
            hub.db.set_connection_bots(
                cid, chat=True, confirm=False, actor=actor, workspace_ref=hub.workspace
            )
    # Not an agent token, not a bare user id, not a person of another workspace.
    agent = hub.principal(owner_token)
    for actor in (agent, None, hub.hub.admin):
        with pytest.raises((ControlError, AttributeError)):
            hub.db.set_connection_bots(
                owner_cid, chat=True, confirm=False, actor=actor, workspace_ref=hub.workspace
            )
    # Over REST: an owner cannot grant another member's app, a member cannot grant the owner's.
    status, body, _ = hub.request(
        hub.owner, "PATCH", f"/api/connections/{member_cid}", {"bots_chat": True}
    )
    assert (status, body["error"]) == (409, bots_grant.NOT_YOURS_TEXT)
    status, body, _ = hub.request(
        hub.member, "PATCH", f"/api/connections/{owner_cid}", {"bots_chat": True}
    )
    assert (status, body["error"]) == (403, bots_grant.NOT_YOURS_TEXT)
    assert hub.request(owner_token, "GET", "/api/bots")[0] == 404
    assert hub.request(member_token, "GET", "/api/bots")[0] == 404
    # An agent token cannot mint a token carrying the scope either.
    with pytest.raises(ControlError):
        hub.db.create_token(
            workspace_ref=hub.workspace,
            user_ref=hub.hub.admin,
            name="sneaky",
            client="test",
            policy=control_db.Policy.build(scopes=["memory:read", CHAT], read_wings=["shared"]),
            actor=hub.hub.admin,
        )


def test_a_member_app_stays_bound_by_the_members_permissions(bots_hub, fake, extra_tools):  # noqa: F811
    from commonplace.bots import tools
    from commonplace.extensions import ToolError

    hub = bots_hub
    configure(hub)
    token, cid = app(hub, hub.member_user, "member-phone", chat=False)
    status, body, _ = hub.request(
        hub.member, "PATCH", f"/api/connections/{cid}", {"bots_chat": True}
    )
    assert status == 200 and body["connection"]["bots_chat"] is True, body
    owner_token, _ = app(hub, name="owner-phone")
    assert hub.bots(token)["can_configure"] is False
    assert hub.bots(owner_token)["can_configure"] is True

    def owner_only(turn, args):
        try:
            turn.boards().goal_set(title="A goal")
        except ToolError:
            return tools.Result(False, say="Only an owner can do that.")
        return tools.Result(True, {})

    extra_tools(
        tools.Tool(
            name="owner_op",
            description="An owner-only operation",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            kind="routine",
            run=owner_only,
            label=lambda turn, args: "Owner operation",
        )
    )
    outcomes = {}
    for who, auth, board in (("member", token, "work"), ("owner", owner_token, "work")):
        fake.script = [[call("owner_op")], [("text", "Done.")]]
        bot = hub.by_board(auth)[board]
        events = Stream(hub, bearer(auth), bot["id"], "Please do it", "request-0001").until_end()
        outcomes[who] = [d["state"] for e, d in events if e == "action" and d["state"] != "running"]
    assert outcomes == {"member": ["refused"], "owner": ["ok"]}


# Audit


def test_granting_revoking_and_every_write_are_audited_without_text(bots_hub, fake):  # noqa: F811
    hub = bots_hub
    configure(hub)
    token, cid = app(hub, confirm=True)
    grants = rows(hub, connection_id=cid, action=bots_grant.AUDIT_GRANT)
    assert sorted(r["tool"] for r in grants) == [CHAT, CONFIRM]
    assert all(r["user_id"] == hub.hub.admin and r["decision"] == "ok" for r in grants)
    bot_id = hub.by_board(token)["work"]["id"]
    hub.get(token, f"/api/bots/{bot_id}/messages")  # reads are not writes
    hub.post(token, "/api/bots", {"name": WORDS})
    hub.post(token, f"/api/bots/{bot_id}/memory", {"text": WORDS})
    hub.post(token, f"/api/bots/{bot_id}/read", {"upto": 0})
    fake.script = [[("text", WORDS)]]
    Stream(hub, bearer(token), bot_id, WORDS, "request-0001").until_end()
    hub.post(token, f"/api/bots/{bot_id}/stop", {"request_id": "request-0001"})
    hub.post(token, f"/api/bots/{bot_id}/memory", {"text": ""})  # refused, still recorded
    writes = list(reversed(rows(hub, action=bots_grant.AUDIT_WRITE)))
    assert [r["tool"] for r in writes] == [
        "POST bots",
        "POST memory",
        "POST read",
        "POST messages",
        "POST stop",
        "POST memory",
    ]
    assert [r["decision"] for r in writes] == ["ok"] * 5 + ["refused"]
    assert all(
        r["connection_id"] == cid and r["user_id"] == hub.hub.admin and r["auth"] == "pat"
        for r in writes
    )
    dump = json.dumps([dict(r) for r in hub.db.audit_entries(hub.workspace, limit=500)])
    assert WORDS not in dump and "zebra" not in dump
    # A person's own session writes are not app writes.
    before = len(rows(hub, action=bots_grant.AUDIT_WRITE))
    hub.post(hub.owner, "/api/bots", {"name": "Session bot"})
    assert len(rows(hub, action=bots_grant.AUDIT_WRITE)) == before
    grant(hub, cid, hub.hub.admin, chat=False)
    revoked = rows(hub, connection_id=cid, action=bots_grant.AUDIT_REVOKE)
    assert sorted(r["tool"] for r in revoked) == [CHAT, CONFIRM]


def test_a_refused_confirm_is_audited_too(bots_hub):
    hub = bots_hub
    token, cid = app(hub)  # chat only
    bot_id = owner_bot(hub)
    status, body, _ = hub.post(
        token, f"/api/bots/{bot_id}/confirm", {"pending": WORDS, "accept": True}
    )
    assert (status, body["code"]) == (403, "scope_required")
    [row] = rows(hub, action=bots_grant.AUDIT_WRITE)
    assert (row["tool"], row["decision"], row["connection_id"], row["user_id"]) == (
        "POST confirm",
        "refused",
        cid,
        hub.hub.admin,
    )
    assert WORDS not in json.dumps([dict(r) for r in hub.db.audit_entries(hub.workspace)])


def test_a_write_that_dies_before_answering_is_not_audited_as_ok(bots_hub, monkeypatch):
    from commonplace.bots import routes

    hub = bots_hub
    token, cid = app(hub)
    bot_id = owner_bot(hub)

    def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setitem(routes.HANDLERS, ("POST", "read"), broken)
    status, _, _ = hub.post(token, f"/api/bots/{bot_id}/read", {"upto": 0})
    assert status == 500
    [row] = rows(hub, action=bots_grant.AUDIT_WRITE)
    assert (row["tool"], row["decision"], row["connection_id"]) == ("POST read", "error", cid)


# Settings > Agents


def test_settings_agents_grant_and_withdraw_with_one_consent_text(bots_hub):
    hub = bots_hub
    token, cid = app(hub, chat=False)
    status, listing, _ = hub.get(hub.owner, "/api/connections")
    row = next(c for c in listing["connections"] if c["id"] == cid)
    assert (row["bots_chat"], row["bots_confirm"], row["bots_grantable"]) == (False, False, True)
    assert listing["bots_consent"] == {
        "chat": bots_grant.CHAT_TEXT,
        "confirm": bots_grant.CONFIRM_TEXT,
    }
    path = f"/api/connections/{cid}"
    status, body, _ = hub.request(hub.owner, "PATCH", path, {"bots_confirm": True})
    assert (status, body["error"]) == (409, bots_grant.NEEDS_CHAT_TEXT)
    status, body, _ = hub.request(hub.owner, "PATCH", path, {"bots_chat": True})
    assert (
        status == 200 and body["connection"]["bots_chat"] and not body["connection"]["bots_confirm"]
    )
    assert hub.request(token, "GET", "/api/bots")[0] == 200
    status, body, _ = hub.request(hub.owner, "PATCH", path, {"bots_confirm": True})
    assert status == 200 and body["connection"]["bots_confirm"] is True
    for bad in ({"bots_chat": "yes"}, {"bots_chat": True, "name": "x"}, {}):
        assert hub.request(hub.owner, "PATCH", path, bad)[0] == 400
    status, body, _ = hub.request(hub.owner, "PATCH", path, {"bots_chat": False})
    assert (
        status == 200
        and not body["connection"]["bots_chat"]
        and not body["connection"]["bots_confirm"]
    )
    assert hub.request(token, "GET", "/api/bots")[0] == 404
    # A member sees nothing of other people's connections and manages only their own scopes.
    assert hub.request(hub.member, "PATCH", path, {"name": "x"})[0] == 403
    assert hub.request(hub.member, "DELETE", path)[0] == 403
    assert hub.get(hub.member, "/api/connections")[0] == 403


def test_the_app_script_offers_the_buttons_and_the_server_consent(bots_hub):
    source = open("commonplace/web/static/agents.js").read()
    assert 'data-action="agent-bots"' in source and "bots_grantable" in source
    assert "data-bots-chat-consent" in source and "bots_consent" in source


# OAuth consent


def oauth_app(
    hub, *, email="owner@example.com", scope=f"memory:read memory:write {CHAT} {CONFIRM}", ticks=()
):
    client = Client(hub.hub)
    client.login(email)
    client_id = client.register(client_name="My Phone")
    path = client.authorize_path(client_id, scope=scope)
    status, _, page = client.request("GET", path)
    assert status == 200, page
    request = unescape(re.search(r'name="request" value="([^"]+)"', page)[1])
    data = {"request": request, "decision": "allow", **{t: "1" for t in ticks}}
    status, headers, _ = client.request("POST", path, data)
    assert status == 303
    query = parse_qs(urlsplit(headers["Location"]).query)
    if "code" not in query:
        return SimpleNamespace(page=page, error=query.get("error"), issued=None, token=None)
    status, _, raw = client.exchange(client_id, query["code"][0])
    assert status == 200, raw
    issued = json.loads(raw)
    return SimpleNamespace(page=page, error=None, issued=issued, token=issued["access_token"])


def test_oauth_consent_grants_only_what_was_ticked(bots_hub):
    hub = bots_hub
    ticked = oauth_app(hub, ticks=["grant_bots_chat"])
    page = unescape(ticked.page)
    assert bots_grant.CHAT_TEXT in page and bots_grant.CONFIRM_TEXT in page
    assert sorted(ticked.issued["scope"].split()) == ["bots:chat", "memory:read", "memory:write"]
    assert hub.request(ticked.token, "GET", "/api/bots")[0] == 200
    assert (
        hub.request(
            ticked.token,
            "POST",
            f"/api/bots/{owner_bot(hub)}/confirm",
            {"pending": "x", "accept": True},
        )[0]
        == 403
    )
    both = oauth_app(hub, ticks=["grant_bots_chat", "grant_bots_confirm"])
    assert {CHAT, CONFIRM} <= set(both.issued["scope"].split())
    none = oauth_app(hub)
    assert not {CHAT, CONFIRM} & set(none.issued["scope"].split())
    assert hub.request(none.token, "GET", "/api/bots")[0] == 404
    # Confirm ticked without chat grants neither.
    lone = oauth_app(hub, ticks=["grant_bots_confirm"])
    assert not {CHAT, CONFIRM} & set(lone.issued["scope"].split())
    # Asking for confirm without chat is refused outright; a forged tick on a scope not asked for adds nothing.
    assert oauth_app(
        hub, scope=f"memory:read memory:write {CONFIRM}", ticks=["grant_bots_confirm"]
    ).error == ["invalid_scope"]
    plain = oauth_app(
        hub, scope="memory:read memory:write", ticks=["grant_bots_chat", "grant_bots_confirm"]
    )
    assert 'name="grant_bots_chat"' not in plain.page
    assert not {CHAT, CONFIRM} & set(plain.issued["scope"].split())


def test_oauth_consent_works_for_a_member_and_stays_member_bound(bots_hub):
    hub = bots_hub
    granted = oauth_app(hub, email="member@example.com", ticks=["grant_bots_chat"])
    assert CHAT in granted.issued["scope"].split()
    body = hub.bots(granted.token)
    assert body["can_configure"] is False
    assert {b["id"] for b in body["bots"]} == {b["id"] for b in hub.bots(hub.member)["bots"]}
    # The stored grant is withdrawn from Settings > Agents; granting again needs fresh consent.
    cid = hub.principal(granted.token).connection_id
    status, body, _ = hub.request(
        hub.member, "PATCH", f"/api/connections/{cid}", {"bots_chat": False}
    )
    assert status == 200
    assert hub.request(granted.token, "GET", "/api/bots")[0] == 404
    status, body, _ = hub.request(
        hub.member, "PATCH", f"/api/connections/{cid}", {"bots_chat": True}
    )
    assert (status, body["error"]) == (409, bots_grant.OAUTH_GRANT_TEXT)


@pytest.mark.parametrize("scope", [CHAT, CONFIRM])
def test_the_cli_refuses_the_scopes(monkeypatch, scope):
    from commonplace import cli

    monkeypatch.setattr(cli, "_db", lambda args: None)
    args = SimpleNamespace(
        scopes=f"memory:read,{scope}",
        client="generic",
        **dict.fromkeys(("profile", "tools", "read_wings", "write_wings", "write_wing", "trust")),
    )
    with pytest.raises(ValueError, match="own sign-in consent"):
        cli.cmd_token_add(args)


def test_person_sessions_do_not_carry_the_scopes():
    assert CHAT not in sessions.person_policy("owner").scopes
    assert CONFIRM not in sessions.person_policy("member").scopes
