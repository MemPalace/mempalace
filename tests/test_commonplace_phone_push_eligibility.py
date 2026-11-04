"""Head-only eligibility units with synthetic authority and real session queries."""

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from commonplace import sessions
from commonplace.control_db import Principal
from commonplace.phone_push import eligibility


@pytest.fixture
def owner(monkeypatch):
    principal = Principal(
        user_id="u1",
        user_email="owner@example.test",
        workspace_id="w1",
        connection_id="",
        connection="web:owner@example.test",
        client="web",
        auth="session",
        role="owner",
        policy=sessions.person_policy(),
        person=True,
    )
    db = Mock()
    db.user.return_value = {"id": "u1", "email": "owner@example.test", "disabled_at": None}
    db.role.return_value = "owner"
    monkeypatch.setattr(sessions, "home_workspace", lambda db, user: "w1")
    assert eligibility.eligible(db, principal)
    return db, principal


def test_disabled_user_ineligible(owner):
    db, principal = owner
    db.user.return_value["disabled_at"] = "2030-01-01T00:00:00Z"
    assert eligibility.owner_principal(db, "u1", "w1") is None
    assert not eligibility.eligible(db, principal)


def test_removed_member_ineligible(owner):
    db, principal = owner
    db.role.return_value = None
    assert eligibility.owner_principal(db, "u1", "w1") is None
    assert not eligibility.eligible(db, principal)


def test_demoted_owner_ineligible(owner):
    db, principal = owner
    db.role.return_value = "member"
    assert eligibility.owner_principal(db, "u1", "w1") is None
    assert not eligibility.eligible(db, principal)


def test_home_changed_ineligible(owner, monkeypatch):
    db, principal = owner
    monkeypatch.setattr(sessions, "home_workspace", lambda db, user: "w2")
    assert eligibility.owner_principal(db, "u1", "w1") is None
    assert not eligibility.eligible(db, principal)


@pytest.mark.parametrize("case", ["live", "revoked", "expired", "other_workspace", "missing"])
def test_signed_in_matches_session_principal(owner, monkeypatch, case):
    _, principal = owner
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(sessions, "_now", lambda: now)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = SimpleNamespace(_lock=RLock(), _conn=conn)
    conn.executescript(sessions.SCHEMA)
    if case != "missing":
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
            (
                sessions._hash("synthetic-session"),
                "u1",
                "w2" if case == "other_workspace" else "w1",
                "2029-12-01T00:00:00Z",
                "2030-01-01T00:00:00Z" if case == "expired" else "2030-02-01T00:00:00Z",
                "2029-12-01T00:00:00Z" if case == "revoked" else None,
            ),
        )
        conn.commit()
    # Both paths execute their real predicates against the same sessions table.
    monkeypatch.setattr(
        sessions, "person_principal", lambda db, user, ws: replace(principal, workspace_id=ws)
    )
    try:
        assert eligibility.signed_in(db, "u1", "w1") == (case == "live")
        actual = sessions.session_principal(db, "synthetic-session")
        assert (actual is not None and actual.workspace_id == "w1") == (case == "live")
    finally:
        conn.close()


def test_pause_constant_off_ignores_sessions(monkeypatch):
    signed_in = Mock(return_value=False)
    monkeypatch.setattr(eligibility, "signed_in", signed_in)
    assert eligibility.PAUSE_SIGNED_OUT is True
    assert eligibility.paused(None, "u1", "w1")
    signed_in.assert_called_once_with(None, "u1", "w1")
    signed_in.reset_mock()
    monkeypatch.setattr(eligibility, "PAUSE_SIGNED_OUT", False)
    assert not eligibility.paused(None, "u1", "w1")
    signed_in.assert_not_called()
