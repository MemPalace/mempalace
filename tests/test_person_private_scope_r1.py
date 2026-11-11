"""Private containment also covers events, write responses and durable tokens."""

# ruff: noqa: F811

import json

import pytest

from commonplace import events, sessions
from commonplace.boards.service import BoardService
from commonplace.control_db import ControlError
from commonplace.extensions import ToolContext
from commonplace.gateway import _tool_response, _tool_result
from commonplace.oauth import store as oauth_store
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_oauth import Client
from tests.test_person_private_scope import (  # noqa: F401
    PRIVATE,
    PRIVATE_ID,
    PRIVATE_ROOM,
    SHARED,
    SHARED_ROOM,
    _payload,
    world,
)


def _set_role(w, user, role):
    # Exercise malformed legacy membership rows without weakening production
    # constraints. Restore constraint enforcement before authentication.
    with w.hub.db._lock:
        w.hub.db._conn.execute("PRAGMA ignore_check_constraints=ON")
        try:
            w.hub.db._conn.execute(
                "UPDATE memberships SET role=? WHERE workspace_id=? AND user_id=?",
                (role, w.hub.workspace, user),
            )
        finally:
            w.hub.db._conn.execute("PRAGMA ignore_check_constraints=OFF")


def test_event_feed_hides_private_events_and_cursor(world):
    w = world
    owner = sessions.person_principal(w.hub.db, w.hub.admin, w.hub.workspace)
    service = BoardService(ToolContext(principal=owner, gateway=w.hub.gateway))
    service.board_add(slug="visible", wing="shared")
    service.board_add(slug="hidden", wing="private_notes")
    shared = events.emit(
        w.hub.db, w.hub.workspace, "card.updated", data={"board": "visible", "title": SHARED}
    )
    initial = w.http(w.member, "/api/events?after=0")[1]
    epoch = initial.get("epoch", "")
    path = f"/api/events?after=0&epoch={epoch}"
    before = w.http(w.member, path)
    assert before[0] == 200 and SHARED in json.dumps(before)
    cursor = before[1]["seq"]
    w.insert_private()
    events.emit(
        w.hub.db, w.hub.workspace, "card.updated", data={"board": "hidden", "title": PRIVATE}
    )
    owner_feed = w.http(w.owner, "/api/events?after=0")
    assert PRIVATE_ID in json.dumps(owner_feed) and PRIVATE in json.dumps(owner_feed)
    after = w.http(w.member, path)
    assert after == before, "hidden events changed the member feed or cursor"
    empty = w.http(w.member, f"/api/events?after={cursor}&epoch={epoch}")
    assert empty == (200, {"events": [], "seq": cursor, "epoch": epoch, "reset": False})
    events.emit(
        w.hub.db, w.hub.workspace, "card.updated", data={"board": "visible", "title": "next shared"}
    )
    page = w.http(w.member, f"/api/events?after={cursor}&epoch={epoch}")[1]
    assert page["seq"] == cursor + 1
    assert len(page["events"]) == 1 and page["events"][0]["seq"] == cursor + 1
    assert shared["seq"] > 0


@pytest.mark.parametrize("chunked", [False, True])
def test_unreadable_drawer_retry_has_fresh_write_shape(world, chunked):
    w = world
    content = PRIVATE * (w.mcp._config.chunk_size // len(PRIVATE) + 2) if chunked else PRIVATE
    args = {"wing": "private_notes", "room": PRIVATE_ROOM, "content": content}
    fresh = _payload(w.rpc(w.member, "mempalace_add_drawer", args))
    retry = _payload(w.rpc(w.member, "mempalace_add_drawer", args))
    assert retry == fresh, "unreadable write response distinguishes existing content"
    assert fresh["success"] and fresh["wing"] == "private_notes"
    assert fresh["chunks"] > 1 if chunked else fresh["chunks"] == 1
    owner_retry = _payload(w.rpc(w.owner, "mempalace_add_drawer", args))
    assert owner_retry["reason"] == "already_exists"
    assert (
        content
        in _payload(w.rpc(w.owner, "mempalace_get_drawer", {"drawer_id": fresh["drawer_id"]}))[
            "content"
        ]
    )


@pytest.mark.parametrize("private_endpoint", ["source", "target"])
def test_tunnel_write_requires_readable_endpoints(world, private_endpoint):
    w = world
    args = {
        "source_wing": "shared",
        "source_room": SHARED_ROOM,
        "target_wing": "project_shared",
        "target_room": SHARED_ROOM,
        "label": "shared control",
    }
    assert _payload(w.rpc(w.member, "mempalace_create_tunnel", args))
    # Tunnels need real endpoint rooms, so the private room holds a drawer;
    # no tunnel touches it yet.
    status, saved = w.http(
        w.owner, "/api/memory", {"text": PRIVATE, "theme": "private_notes", "topic": PRIVATE_ROOM}
    )
    assert status == 201 and saved["id"] == PRIVATE_ID, saved
    args[private_endpoint + "_wing"] = "private_notes"
    args[private_endpoint + "_room"] = PRIVATE_ROOM
    absent = w.rpc(w.member, "mempalace_create_tunnel", args)
    created = _payload(w.rpc(w.owner, "mempalace_create_tunnel", {**args, "label": PRIVATE}))
    assert "error" not in created, created
    before = _payload(w.rpc(w.owner, "mempalace_list_tunnels"))
    hidden = next(row for row in before if row.get("label") == PRIVATE)
    present = w.rpc(w.member, "mempalace_create_tunnel", {**args, "label": "overwrite attempt"})
    after = _payload(w.rpc(w.owner, "mempalace_list_tunnels"))
    assert after == before, "member overwrote a private tunnel"
    assert present == absent, "hidden tunnel response differs from absent"
    assert hidden["created_at"] not in json.dumps(present)
    assert present[1]["result"]["isError"]


def test_tunnel_write_response_is_scrubbed(world):
    w = world
    member = sessions.person_principal(w.hub.db, w.member_id, w.hub.workspace)
    result = {
        "success": True,
        "tunnel": {
            "source": {"wing": "shared", "room": SHARED_ROOM},
            "target": {"wing": "private_notes", "room": PRIVATE_ROOM},
            "created_at": PRIVATE,
        },
    }
    response = w.hub.gateway.mediator._after(
        member, "mempalace_create_tunnel", "tunnel_write", {}, _tool_response(1, result)
    )
    assert PRIVATE not in json.dumps(_tool_result(response)), (
        "tunnel write bypasses output wing filtering"
    )


@pytest.mark.parametrize("role", ["member", "unexpected", ""])
def test_member_token_creation_refuses_private_read(world, role):
    w = world
    _set_role(w, w.member_id, role)
    denied = False
    try:
        w.hub.token(
            "private-holder",
            user=w.member_id,
            profile="full",
            scopes=["memory:read", "private:read"],
            read_wings=["*"],
            trust="trusted",
        )
    except ControlError:
        denied = True
    assert denied, "non-owner holder received private read scope"


@pytest.mark.parametrize("auth", ["pat", "oauth"])
@pytest.mark.parametrize("role", ["member", "unexpected", ""])
def test_loaded_tokens_drop_owner_scopes_after_role_change(world, auth, role):
    w = world
    policy = sessions.person_principal(w.hub.db, w.hub.admin, w.hub.workspace).policy
    # Include admin in a synthetic legacy policy to prove both caps are live.
    from dataclasses import replace

    policy = replace(policy, scopes=(*policy.scopes, "admin"))
    if auth == "pat":
        cid, token = w.hub.db.create_token(
            workspace_ref=w.hub.workspace,
            user_ref=w.hub.admin,
            name="owner-before-change",
            client="test",
            policy=policy,
            actor=w.hub.admin,
        )
        authenticate = w.hub.db.authenticate_token
    else:
        client = Client(w.hub)
        grant = client.grant(client.register())
        token = grant["access_token"]
        with w.hub.db._lock:
            cid = w.hub.db._conn.execute(
                "SELECT connection_id FROM oauth_tokens WHERE token_hash=?",
                (oauth_store.digest(token),),
            ).fetchone()[0]
            # Public issuance never requests these scopes. Seed legacy stored
            # authority to exercise the authenticator independently of issuance.
            w.hub.db._conn.execute(
                "UPDATE connections SET policy=? WHERE id=?", (policy.to_json(), cid)
            )
            w.hub.db._conn.execute(
                "UPDATE oauth_tokens SET scope=? WHERE connection_id=?",
                (" ".join(policy.scopes), cid),
            )
        authenticate = oauth_store.OAuthAuthenticator(w.hub.db).authenticate
    owner = authenticate(token)
    assert owner.policy.has_scope("private:read") and owner.policy.has_scope("admin")
    w.insert_private()
    assert PRIVATE in json.dumps(
        w.rpc(
            {"Authorization": f"Bearer {token}"},
            "mempalace_search",
            {"query": "CANARY", "limit": 100},
        )
    )
    _set_role(w, w.hub.admin, role)
    changed = authenticate(token)
    assert changed is not None and not {"private:read", "admin"}.intersection(
        changed.policy.scopes
    ), "loaded policy retains owner authority"
    shared = w.rpc(
        {"Authorization": f"Bearer {token}"}, "mempalace_search", {"query": "CANARY", "limit": 100}
    )
    assert PRIVATE not in json.dumps(shared) and SHARED in json.dumps(shared)
    if auth == "pat":
        assert not w.hub.db.authenticate_connection(cid).policy.has_scope("private:read")
    # Simulate a legacy orphan without changing production FK enforcement.
    with w.hub.db._lock:
        w.hub.db._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            w.hub.db._conn.execute(
                "DELETE FROM memberships WHERE workspace_id=? AND user_id=?",
                (w.hub.workspace, w.hub.admin),
            )
        finally:
            w.hub.db._conn.execute("PRAGMA foreign_keys=ON")
    assert authenticate(token) is None


@pytest.mark.parametrize("surface", ["http", "mcp"])
def test_private_health_save_returns_unreadable_notice(world, surface):
    w = world
    args = {"text": "HEALTH_CANARY hospital visit", "theme": "shared", "topic": "member_topic"}
    if surface == "http":
        status, saved = w.http(w.member, "/api/memory", args)
        assert status == 201
        notice = saved.get("notice", "")
        drawer_id = saved["id"]
    else:
        status, saved = w.rpc(w.member, "memory_save", args)
        assert status == 200 and not saved["result"].get("isError")
        notice = saved["result"]["content"][0]["text"]
        drawer_id = notice.split()[1]
    assert (
        "saved privately" in notice.lower()
        and "cannot read" in notice.lower()
        and "for now" in notice.lower()
    ), "private save silently disappears from author recall"
    assert drawer_id.startswith("drawer_private_")
    assert (
        "HEALTH_CANARY"
        in _payload(w.rpc(w.owner, "mempalace_get_drawer", {"drawer_id": drawer_id}))["content"]
    )
    member_read = w.rpc(w.member, "mempalace_get_drawer", {"drawer_id": drawer_id})
    assert "HEALTH_CANARY" not in json.dumps(member_read)
    shared_status, plain = w.http(
        w.member, "/api/memory", {"text": SHARED + " plain", "theme": "shared"}
    )
    assert shared_status == 201 and "notice" not in plain
    assert SHARED in json.dumps(w.rpc(w.member, "memory_get", {"id": plain["id"]}))

    # A configured inbox writer may request another wing; the gateway pins
    # the actual destination. Its readable inbox must not be called private.
    token = w.hub.token("inbox-writer", read_wings=["inbox"], write_wing="inbox", trust="untrusted")
    pinned = w.rpc(
        {"Authorization": f"Bearer {token}"},
        "memory_save",
        {"text": "pinned shared control", "theme": "shared"},
    )
    message = pinned[1]["result"]["content"][0]["text"]
    assert not pinned[1]["result"].get("isError")
    assert "saved privately" not in message.lower() and "cannot read" not in message.lower()
