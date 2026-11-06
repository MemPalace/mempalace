"""First signup lands on an owned space through ordinary public auth routes."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial

import pytest

from commonplace import sessions
from commonplace.boards.service import BoardService
from commonplace.boards.store import BoardStore
from commonplace.cli import main
from commonplace.control_db import ControlDB
from tests.test_commonplace_email_auth import fresh as fresh, refused as email_refused
from tests.test_commonplace_social import (
    enable,
    finish,
    refused as social_refused,
    remote as remote,
    start,
)

LANDING = "/app/#/board/commonplace"


def signin(
    client, mailbox, remote, channel, *, email="new@example.test", subject=123, next_path=""
):
    if channel == "email":
        assert client.ask(email, next=next_path)[0] == 202
        return client.verify(mailbox.link())
    remote.github.update(id=subject, name="New Person")
    remote.emails = [{"email": email, "verified": True, "primary": True}]
    return finish(client, start(client, remote, "github", next_path=next_path))


@pytest.fixture
def signup(fresh, remote, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "open")
    enable(monkeypatch, "github")
    client, mailbox = fresh()

    def run(channel="email", **kwargs):
        status, headers, body = signin(client, mailbox, remote, channel, **kwargs)
        assert status == 303 and not body and headers["Set-Cookie"], headers["Location"]
        return headers["Set-Cookie"].split(";", 1)[0], headers["Location"]

    return client, mailbox, run


def api(client, cookie, path="/api/boards", method="GET", data=None, *, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    status, _, body = client.request(path, method, cookie=cookie, data=data, headers=headers)
    return status, json.loads(body) if body else None


@pytest.mark.parametrize("channel", ["github", "email"])
def test_fresh_signup_creates_one_owned_space_and_lands_in_board(signup, channel):
    client, _, run = signup
    assert client.db.users() == client.db.workspaces() == []
    cookie, landing = run(channel)
    assert landing == LANDING
    me = client.me(cookie)
    assert client.db.user(me["user"])["is_admin"] == 0
    assert client.db.workspace(me["workspace"])["personal_of"] == me["user"]
    assert client.db.role(me["workspace"], me["user"]) == "owner"
    status, data = api(client, cookie)
    assert status == 200
    [board] = data["boards"]
    assert (board["slug"], board["name"], board["wing"]) == (
        "commonplace",
        "Commonplace",
        "shared",
    )
    assert board["can_configure"] and not any(board["counts"].values())
    store = BoardStore(client.gateway.workspace_dir(me["workspace"]))
    with store.connection() as conn:
        [row] = conn.execute("SELECT * FROM boards").fetchall()
        assert row["stamp_user"] == me["user"]
        assert row["stamp_connection"] is None and row["stamp_trust"] == "trusted"
    assert client.request(landing.split("#")[0], cookie=cookie)[0] == 200
    assert api(client, cookie, "/api/boards/commonplace/cards") == (
        200,
        {"cards": [], "linked": [], "children": []},
    )


@pytest.mark.parametrize("channel", ["github", "email"])
def test_returning_signup_preserves_spaces_and_home_or_explicit_route(signup, channel):
    client, _, run = signup
    cookie, _ = run(channel)
    me = client.me(cookie)
    assert (
        api(client, cookie, method="POST", data={"slug": "field-notes", "name": "Field notes"})[0]
        == 201
    )
    before = api(client, cookie)[1]
    assert api(client, cookie, "/app/logout", "POST", {})[0] == 204
    for next_path, expected in [
        ("", "/app/"),
        ("/app/#/board/field-notes", "/app/#/board/field-notes"),
    ]:
        cookie, landing = run(channel, next_path=next_path)
        assert landing == expected
        assert client.me(cookie)["workspace"] == me["workspace"]
        assert api(client, cookie)[1] == before
    assert len(client.db.users()) == len(client.db.workspaces()) == 1


@pytest.mark.parametrize("channel", ["github", "email"])
@pytest.mark.parametrize("destination", ["/app/#/settings", "/oauth/authorize?client_id=test"])
def test_new_signup_provisions_space_without_overriding_destination(signup, channel, destination):
    client, _, run = signup
    cookie, landing = run(channel, next_path=destination)
    assert landing.endswith(destination)
    assert len(api(client, cookie)[1]["boards"]) == 1


@pytest.mark.parametrize("channel", ["github", "email"])
def test_two_signup_workspaces_keep_boards_and_cards_private(signup, channel):
    client, _, run = signup
    first, _ = run(channel)
    assert (
        api(
            client,
            first,
            method="POST",
            data={"slug": "private-project", "name": "Private project"},
        )[0]
        == 201
    )
    status, card = api(
        client, first, "/api/boards/private-project/cards", "POST", {"title": "PRIVATE_CARD"}
    )
    assert status == 201
    second, landing = run(channel, email="second@example.test", subject=456)
    assert landing == LANDING
    assert client.me(first)["workspace"] != client.me(second)["workspace"]
    status, data = api(client, second)
    assert status == 200 and [b["slug"] for b in data["boards"]] == ["commonplace"]
    assert api(client, second, "/api/boards/private-project/cards")[0] == 404
    assert api(client, second, f"/api/cards/{card['number']}")[0] == 404
    assert "PRIVATE_CARD" not in str(api(client, second, "/api/cards"))
    assert api(client, first, f"/api/cards/{card['number']}")[1]["title"] == "PRIVATE_CARD"


def test_linking_another_provider_preserves_existing_spaces(signup):
    client, _, run = signup
    cookie, _ = run("email")
    user = client.me(cookie)["user"]
    before = api(client, cookie)[1]
    cookie, landing = run("github")
    assert landing == "/app/" and client.me(cookie)["user"] == user
    assert api(client, cookie)[1] == before
    assert len(client.db.users()) == len(client.db.workspaces()) == 1


def test_concurrent_first_challenge_is_consumed_once_and_creates_one_space(signup):
    client, mailbox, _ = signup
    assert client.ask("new@example.test")[0] == 202
    link = mailbox.link()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.verify(link), range(2)))
    assert sorted(response[0] for response in responses) == [303, 400]
    success = next(response for response in responses if response[0] == 303)
    assert success[1]["Location"] == LANDING
    cookie = success[1]["Set-Cookie"].split(";", 1)[0]
    assert len(client.db.users()) == len(client.db.workspaces()) == 1
    assert len(api(client, cookie)[1]["boards"]) == 1


def test_agents_keep_space_creation_authority_and_child_approval(signup):
    client, _, run = signup
    cookie, _ = run()
    tokens = []
    for data in [{"name": "parent"}, {"name": "child", "parent": "parent"}]:
        status, created = api(client, cookie, "/api/connections", "POST", data)
        assert status == 201
        tokens.append(created["token"])
    parent, child = tokens
    assert api(client, None, method="POST", data={"slug": "agent-space"}, token=parent)[0] == 403
    assert (
        api(client, cookie, method="POST", data={"slug": "owner-space", "name": "Work"})[0] == 201
    )
    status, proposal = api(
        client,
        None,
        "/api/boards/owner-space/cards",
        "POST",
        {"title": "Child proposal"},
        token=child,
    )
    assert status == 201 and proposal["proposal_status"] == "pending"
    path = f"/api/cards/{proposal['number']}"
    patch = {"status": "doing", "version": proposal["version"]}
    assert api(client, None, path, "PATCH", patch, token=child)[0] == 403
    assert api(client, None, path + "/approve", "POST", {}, token=child)[0] == 403
    status, approved = api(
        client, None, path + "/approve", "POST", {"reason": "synthetic reason"}, token=parent
    )
    assert status == 200 and approved["proposal_status"] == "approved"
    # The owning top-level agent releases its child's work (owner decision 3 Nov 14:17).
    assert approved["approved_by"] == "conn:parent"
    status, claimed = api(
        client,
        None,
        path,
        "PATCH",
        {"status": "doing", "version": approved["version"]},
        token=child,
    )
    assert status == 200 and claimed["assignee"] == "conn:child"


def ordinary_proof(client, mailbox, remote, channel):
    if channel == "email":
        assert client.ask("new@example.test")[0] == 202
        return partial(client.verify, mailbox.link())
    remote.github.update(id=123, name="New Person")
    remote.emails = [{"email": "new@example.test", "verified": True, "primary": True}]
    return partial(finish, client, start(client, remote, "github"))


def board_rows(client, workspace):
    store = BoardStore(client.gateway.workspace_dir(workspace))
    with store.connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM boards ORDER BY id")]


def inject_signup_fault(patch, client, fault):
    if fault == "before_board_commit":
        original = BoardService._add_board

        def before_commit(service, conn, **kwargs):
            original(service, conn, **kwargs)
            assert conn.in_transaction
            assert conn.execute("SELECT count(*) FROM boards").fetchone()[0] == 1
            raise sqlite3.OperationalError("synthetic board transaction failure")

        patch.setattr(BoardService, "_add_board", before_commit)
    elif fault == "after_board_commit":
        original = BoardService.board_add

        def after_commit(service, **kwargs):
            result = original(service, **kwargs)
            assert board_rows(client, service.principal.workspace_id)[0]["slug"] == result["slug"]
            raise sqlite3.OperationalError("synthetic failure after board commit")

        patch.setattr(BoardService, "board_add", after_commit)
    elif fault == "principal":
        original = sessions.person_principal

        def invalid_owner(db, uid, workspace):
            return replace(original(db, uid, workspace), role="member")

        patch.setattr(sessions, "person_principal", invalid_owner)
    else:
        assert fault == "session"
        original = sessions.start_session

        def after_session_insert(db, uid, workspace, **kwargs):
            original(db, uid, workspace, **kwargs)
            assert db._conn.in_transaction
            assert db._conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
            assert (
                db._conn.execute(
                    "SELECT count(*) FROM audit_log WHERE action = 'session.create'"
                ).fetchone()[0]
                == 1
            )
            raise sqlite3.OperationalError("synthetic failure after session issuance")

        patch.setattr(sessions, "start_session", after_session_insert)


@pytest.mark.parametrize("channel", ["github", "email"])
@pytest.mark.parametrize(
    "fault", ["before_board_commit", "after_board_commit", "principal", "session"]
)
def test_signup_failure_keeps_durable_marker_and_ordinary_retry_finishes_once(
    signup, remote, monkeypatch, channel, fault
):
    client, mailbox, _ = signup
    original_tx = client.db._tx
    original_start = sessions.start_session
    original_add = BoardService.board_add

    def checked_tx():
        assert not client.db._conn.in_transaction
        return original_tx()

    def checked_start(db, uid, workspace, **kwargs):
        assert db._conn.in_transaction
        assert workspace == sessions.home_workspace(db, uid)
        assert db.workspace(workspace)["initial_space_pending"] == 1
        return original_start(db, uid, workspace, **kwargs)

    def checked_add(service, **kwargs):
        principal = service.principal
        assert not client.db._conn.in_transaction
        assert principal.person and principal.role == "owner"
        assert principal.user_id == client.db.user("new@example.test")["id"]
        assert client.db.workspace(principal.workspace_id)["personal_of"] == principal.user_id
        assert client.db.user(principal.user_id)["is_admin"] == 0
        return original_add(service, **kwargs)

    monkeypatch.setattr(client.db, "_tx", checked_tx)
    monkeypatch.setattr(sessions, "start_session", checked_start)
    monkeypatch.setattr(BoardService, "board_add", checked_add)
    proof = ordinary_proof(client, mailbox, remote, channel)
    with monkeypatch.context() as patch:
        inject_signup_fault(patch, client, fault)
        response = proof()
    if channel == "email":
        email_refused(response)
        email_refused(proof())
    else:
        social_refused(response, "failed")
        social_refused(proof(), "expired")
    with client.db._lock:
        assert not client.db._conn.in_transaction
        assert client.db._conn.execute("SELECT count(*) FROM email_challenges").fetchone()[0] == 0
        assert client.db._conn.execute("SELECT count(*) FROM signin_flows").fetchone()[0] == 0
        assert client.db._conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        assert (
            client.db._conn.execute(
                "SELECT count(*) FROM audit_log WHERE action = 'session.create'"
            ).fetchone()[0]
            == 0
        )
        assert client.db._conn.execute("SELECT count(*) FROM user_identities").fetchone()[0] == 1
    [user], [workspace] = client.db.users(), client.db.workspaces()
    uid, wid = user["id"], workspace["id"]
    assert user["is_admin"] == 0 and workspace["personal_of"] == uid
    assert workspace["initial_space_pending"] == 1
    assert client.db.role(wid, uid) == "owner"
    # A fresh control connection sees the durable marker, not uncommitted state.
    reopened = ControlDB(client.db.path)
    try:
        assert reopened.users() == [user] and reopened.workspaces() == [workspace]
        assert reopened.role(wid, uid) == "owner"
    finally:
        reopened.close()
    before = board_rows(client, wid)
    assert len(before) == (1 if fault in ("after_board_commit", "session") else 0)
    if before:
        store = BoardStore(client.gateway.workspace_dir(wid))
        with store.transaction() as conn:
            conn.execute("UPDATE boards SET name = 'Kept display name' WHERE slug = 'commonplace'")
        before = board_rows(client, wid)

    # Returning-account recovery must also work after new signups close.
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "closed")
    client.gateway.sign_in.signup = False
    status, headers, body = ordinary_proof(client, mailbox, remote, channel)()
    assert status == 303 and headers["Location"] == LANDING and not body
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    assert cookie.startswith(sessions.COOKIE + "=")
    for attribute in ("HttpOnly", "SameSite=Lax", "Path=/"):
        assert attribute in headers["Set-Cookie"]
    me = client.me(cookie)
    assert me["user"] == uid and me["workspace"] == wid
    assert client.db.users() == [user]
    assert [dict(row) for row in client.db.workspaces()] == [
        {**workspace, "initial_space_pending": 0}
    ]
    after = board_rows(client, wid)
    [board] = after
    assert board["slug"] == "commonplace" and board["stamp_user"] == uid
    assert board["stamp_connection"] is None and board["stamp_trust"] == "trusted"
    if before:
        assert after == before
    else:
        assert board["name"] == "Commonplace"
    assert api(client, cookie, "/api/boards/commonplace/cards") == (
        200,
        {"cards": [], "linked": [], "children": []},
    )
    with client.db._lock:
        [issued] = client.db._conn.execute("SELECT * FROM sessions").fetchall()
        assert issued["id_hash"] == sessions._hash(cookie.split("=", 1)[1])
        assert issued["user_id"] == uid and issued["workspace_id"] == wid
        assert issued["revoked_at"] is None
        assert client.db._conn.execute("SELECT count(*) FROM user_identities").fetchone()[0] == 1
        assert (
            client.db._conn.execute(
                "SELECT count(*) FROM audit_log WHERE action = 'session.create'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("channel", ["github", "email"])
def test_returning_empty_workspace_is_not_recreated(signup, channel):
    client, _, run = signup
    cookie, _ = run(channel)
    me = client.me(cookie)
    workspace = client.db.workspace(me["workspace"])
    assert workspace["initial_space_pending"] == 0
    store = BoardStore(client.gateway.workspace_dir(me["workspace"]))
    with store.transaction() as conn:
        conn.execute("DELETE FROM boards")
    for next_path, expected in [("", "/app/"), ("/app/#/settings", "/app/#/settings")]:
        cookie, landing = run(channel, next_path=next_path)
        assert landing == expected and client.me(cookie) == me
        assert api(client, cookie) == (200, {"boards": []})
        assert client.db.workspace(me["workspace"]) == workspace
    assert len(client.db.users()) == len(client.db.workspaces()) == 1


def test_legacy_workspace_migration_preserves_data_and_defaults_to_not_pending(tmp_path):
    path = str(tmp_path / "control.sqlite3")
    db = ControlDB(path)
    uid = db.create_user("legacy@example.test", "Legacy person")
    wid = db.create_workspace("Existing home", uid, personal=True)
    user, workspace = dict(db.user(uid)), dict(db.workspace(wid))
    db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE workspaces DROP COLUMN initial_space_pending")
        assert "initial_space_pending" not in {
            r[1] for r in conn.execute("PRAGMA table_info(workspaces)")
        }
    for _ in range(2):
        db = ControlDB(path)
        try:
            assert dict(db.user(uid)) == user and dict(db.workspace(wid)) == workspace
            assert workspace["initial_space_pending"] == 0
            assert db.role(wid, uid) == "owner"
            for invalid in (None, 2):
                with pytest.raises(sqlite3.IntegrityError):
                    db._conn.execute(
                        "UPDATE workspaces SET initial_space_pending = ? WHERE id = ?",
                        (invalid, wid),
                    )
            assert dict(db.workspace(wid)) == workspace
        finally:
            db.close()


def test_plain_and_cli_workspace_creation_default_to_not_pending(tmp_path, capsys):
    assert main(["--data", str(tmp_path), "user", "add", "cli@example.test"]) == 0
    capsys.readouterr()
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    try:
        [user], [workspace] = db.users(), db.workspaces()
        assert user["is_admin"] == 0 and workspace["personal_of"] == user["id"]
        assert workspace["initial_space_pending"] == 0
        plain = db.create_workspace("Plain workspace", user["id"])
        assert db.workspace(plain)["initial_space_pending"] == 0
        pending = db.create_workspace("Pending workspace", user["id"], initial_space_pending=True)
        assert db.workspace(pending)["initial_space_pending"] == 1
        assert all(db.role(w["id"], user["id"]) == "owner" for w in db.workspaces())
    finally:
        db.close()
