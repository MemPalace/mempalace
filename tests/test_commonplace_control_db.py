"""Control DB: accounts, roles, tokens stored as hashes, append-only audit."""

import sqlite3
import threading

import pytest

from commonplace import tokens
from commonplace.control_db import ControlDB, ControlError
from commonplace.policy import Policy

POLICY = Policy.build(
    profile="slim", scopes=["memory:read"], read_wings=["shared"], trust="standard"
)


@pytest.fixture
def db(tmp_path):
    d = ControlDB(str(tmp_path / "control.sqlite3"))
    yield d
    d.close()


def test_pre_close_callback_runs_once_without_sql_lock_and_refuses_late_registration(db):
    checked = threading.Event()
    calls = []

    def check_connection():
        with db._lock:
            db._conn.execute("SELECT 1")
            checked.set()

    def stop_resource():
        calls.append("stop")
        worker = threading.Thread(target=check_connection, daemon=True)
        worker.start()
        worker.join(timeout=3)
        assert not worker.is_alive() and checked.is_set()

    db.add_pre_close_callback(stop_resource)
    db.close()
    db.close()
    assert calls == ["stop"]
    with pytest.raises(sqlite3.ProgrammingError):
        db._conn.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="closed"):
        db.add_pre_close_callback(lambda: None)


def test_concurrent_close_serializes_callbacks(db):
    entered, release = threading.Event(), threading.Event()
    calls, errors = [], []

    def stop_resource():
        calls.append("stop")
        entered.set()
        assert release.wait(3)

    def close():
        try:
            db.close()
        except BaseException as exc:
            errors.append(exc)

    db.add_pre_close_callback(stop_resource)
    first = threading.Thread(target=close, daemon=True)
    second = threading.Thread(target=close, daemon=True)
    first.start()
    try:
        assert entered.wait(3)
        second.start()
    finally:
        release.set()
        first.join(timeout=3)
        if second.ident is not None:
            second.join(timeout=3)
    assert not first.is_alive() and not second.is_alive()
    assert calls == ["stop"] and not errors


def test_callback_failure_still_stops_other_resources_and_closes_sqlite(db):
    calls = []

    def fail():
        raise RuntimeError("stop failed")

    db.add_pre_close_callback(fail)
    db.add_pre_close_callback(lambda: calls.append(db._conn.execute("SELECT 1").fetchone()[0]))
    with pytest.raises(RuntimeError, match="stop failed"):
        db.close()
    assert calls == [1]
    with pytest.raises(sqlite3.ProgrammingError):
        db._conn.execute("SELECT 1")
    db.close()


def _setup(db):
    owner = db.create_user("owner@example.com", is_admin=True)
    member = db.create_user("member@example.com")
    ws = db.create_workspace("home", owner, personal=True)
    db.add_member(ws, member, "member", actor=owner)
    return owner, member, ws


def test_token_format():
    t = tokens.new_token()
    assert t.startswith("cmmn_") and tokens.looks_like_token(t)
    assert len(tokens.token_hash(t)) == 64 and tokens.token_hint(t).endswith(t[-4:])
    assert tokens.new_token() != t
    for bad in ("", "cmmn_", "cpl_" + t[5:], t + "x", t[:-1] + "!", None):
        assert not tokens.looks_like_token(bad)


def test_token_is_stored_only_as_a_hash(db, tmp_path):
    owner, _member, ws = _setup(db)
    _cid, token = db.create_token(
        workspace_ref=ws, user_ref=owner, name="bot", client="c", policy=POLICY, actor=owner
    )
    row = db.connection("bot")
    assert row["token_hash"] == tokens.token_hash(token)
    db._conn.execute("PRAGMA wal_checkpoint(FULL)")
    for path in tmp_path.iterdir():
        assert token.encode() not in path.read_bytes(), path


def test_authenticate_resolves_the_principal(db):
    owner, member, ws = _setup(db)
    _cid, token = db.create_token(
        workspace_ref=ws,
        user_ref=member,
        name="codex:vm",
        client="codex",
        policy=POLICY,
        actor=owner,
    )
    p = db.authenticate_token(token)
    assert (p.user_id, p.workspace_id, p.connection, p.client, p.auth, p.role) == (
        member,
        ws,
        "codex:vm",
        "codex",
        "pat",
        "member",
    )
    assert p.actor == "conn:codex:vm"
    assert p.identity()["trust"] == "standard"
    assert db.authenticate_token(token[:-1] + ("A" if token[-1] != "A" else "B")) is None


def test_only_owners_make_tokens_and_add_members(db):
    owner, member, ws = _setup(db)
    with pytest.raises(ControlError):
        db.create_token(
            workspace_ref=ws, user_ref=member, name="x", client="c", policy=POLICY, actor=member
        )
    outsider = db.create_user("out@example.com")
    with pytest.raises(ControlError):
        db.create_token(
            workspace_ref=ws, user_ref=outsider, name="x", client="c", policy=POLICY, actor=owner
        )
    with pytest.raises(ControlError):
        db.add_member(ws, outsider, "member", actor=member)
    with pytest.raises(ControlError):
        db.add_member(ws, outsider, "admin", actor=owner)
    admin_scope = Policy.build(profile="slim", scopes=["admin", "memory:read"], trust="trusted")
    with pytest.raises(ControlError):
        db.create_token(
            workspace_ref=ws, user_ref=member, name="x", client="c", policy=admin_scope, actor=owner
        )


def test_connection_names(db):
    owner, _member, ws = _setup(db)
    db.create_token(
        workspace_ref=ws, user_ref=owner, name="grok:cos", client="grok", policy=POLICY, actor=owner
    )
    with pytest.raises(ControlError):
        db.create_token(
            workspace_ref=ws,
            user_ref=owner,
            name="grok:cos",
            client="grok",
            policy=POLICY,
            actor=owner,
        )
    for bad in ("", "has space", "x" * 121, ":lead"):
        with pytest.raises(ControlError):
            db.create_token(
                workspace_ref=ws, user_ref=owner, name=bad, client="c", policy=POLICY, actor=owner
            )


def test_revoke_expiry_disabled_and_left_member(db):
    owner, member, ws = _setup(db)
    make = lambda name, **kw: db.create_token(  # noqa: E731
        workspace_ref=ws, user_ref=member, name=name, client="c", policy=POLICY, actor=owner, **kw
    )[1]
    revoked, expired, disabled, left = make("a"), make("b"), make("c"), make("d")
    assert db.connection("a")["expires_at"]  # default expiry exists
    db.revoke("a", actor=member)  # a holder may revoke their own
    db._conn.execute("UPDATE connections SET expires_at = '2001-01-01T00:00:00Z' WHERE name = 'b'")
    assert db.authenticate_token(revoked) is None and db.authenticate_token(expired) is None
    db._conn.execute("UPDATE users SET disabled_at = 'x' WHERE id = ?", (member,))
    assert db.authenticate_token(disabled) is None
    db._conn.execute("UPDATE users SET disabled_at = NULL WHERE id = ?", (member,))
    assert db.authenticate_token(left) is not None
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM memberships WHERE user_id = ?", (member,))
    assert db.authenticate_token(left) is None
    no_expiry = db.create_token(
        workspace_ref=ws,
        user_ref=owner,
        name="e",
        client="c",
        policy=POLICY,
        actor=owner,
        expires_days=None,
    )
    assert db.connection("e")["expires_at"] is None and no_expiry


def test_member_cannot_revoke_someone_elses_token(db):
    owner, member, ws = _setup(db)
    db.create_token(
        workspace_ref=ws, user_ref=owner, name="o", client="c", policy=POLICY, actor=owner
    )
    with pytest.raises(ControlError):
        db.revoke("o", actor=member)


def test_audit_log_is_append_only(db):
    owner, _member, ws = _setup(db)
    db.audit(action="tool.call", decision="ok", workspace_id=ws, tool="t", target_ids=["d1"])
    assert db.audit_entries(ws)[0]["target_ids"] == '["d1"]'
    with pytest.raises(sqlite3.DatabaseError):
        db._conn.execute("UPDATE audit_log SET decision = 'x'")
    with pytest.raises(sqlite3.DatabaseError):
        db._conn.execute("DELETE FROM audit_log")
    seqs = [e["seq"] for e in db.audit_entries()]
    assert seqs == sorted(seqs, reverse=True)


def test_lookups(db):
    owner, member, ws = _setup(db)
    assert db.user("owner@example.com")["id"] == owner
    assert db.workspace("home")["id"] == ws
    assert {m["email"] for m in db.members(ws)} == {"owner@example.com", "member@example.com"}
    for bad in (
        lambda: db.user("nobody"),
        lambda: db.workspace("nowhere"),
        lambda: db.connection("none"),
    ):
        with pytest.raises(ControlError):
            bad()
    with pytest.raises(ControlError):
        db.create_user("not-an-email")
    with pytest.raises(ControlError):
        db.create_user("OWNER@example.com")
    with pytest.raises(ControlError):
        db.create_workspace(" ", owner)
