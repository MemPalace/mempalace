"""Account cap and waitlist: unlimited default, cap, races, admit, never locking anyone out."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from commonplace import capacity
from commonplace.cli import main
from commonplace.control_db import ControlDB, ControlError
from commonplace.social import accounts
from commonplace.social.providers import Profile, SignInError
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_admin_api import clock, cookie
from tests.test_commonplace_signup_space import (
    fresh as fresh,
    remote as remote,
    signin,
    signup as signup,
)
from tests.test_commonplace_social import refused as social_refused
from tests.test_commonplace_web import request


@pytest.fixture(autouse=True)
def cap_off(monkeypatch):
    monkeypatch.delenv("COMMONPLACE_MAX_ACCOUNTS", raising=False)
    monkeypatch.setattr(capacity, "_warned_invalid", False)


@pytest.fixture
def db(tmp_path):
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    accounts.ensure_schema(db)
    yield db
    db.close()


def person(n):
    return Profile(f"subject-{n}", f"Person {n}", (f"person{n}@example.test",))


def join(db, n):
    return accounts.resolve(db, "github", person(n), signup=True)


def waiting(db):
    return [entry["email"] for entry in capacity.waitlist(db) if entry["status"] == "waiting"]


def test_unlimited_by_default(db):
    assert capacity.max_accounts() is None
    for n in range(12):
        join(db, n)
    assert len(db.users()) == 12
    assert capacity.waitlist(db) == []


@pytest.mark.parametrize("value", ["", "  ", "many", "-1", "1.5", "٣"])
def test_unset_or_invalid_cap_means_unlimited(db, monkeypatch, caplog, value):
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", value)
    assert capacity.max_accounts() is None
    for n in range(3):
        join(db, n)
    assert len(db.users()) == 3
    assert (value.strip() in ("",)) == (not caplog.records)


def test_under_the_cap_signs_up_normally(db, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "3")
    for n in range(2):
        join(db, n)
    assert len(db.users()) == 2 and capacity.waitlist(db) == []
    assert capacity.status(db) == {"max_accounts": 3, "accounts": 2, "waiting": 0}


def test_at_the_cap_new_signup_joins_the_waitlist_and_creates_nothing(db, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "2")
    for n in range(2):
        join(db, n)
    workspaces = len(db.workspaces())
    with pytest.raises(SignInError) as refusal:
        join(db, 9)
    assert refusal.value.code == "waitlist"
    assert len(db.users()) == 2 and len(db.workspaces()) == workspaces
    assert waiting(db) == ["person9@example.test"]
    with db._lock:
        assert db._conn.execute("SELECT COUNT(*) FROM user_identities").fetchone()[0] == 2
    # Asking again keeps one place in line.
    with pytest.raises(SignInError):
        join(db, 9)
    assert waiting(db) == ["person9@example.test"]


def test_existing_and_invited_people_are_never_locked_out(db, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "1")
    first = join(db, 0)
    invited = db.create_user("invited@example.test")  # over the cap, added by an admin
    assert capacity.status(db)["accounts"] == 2
    assert join(db, 0) == first
    assert (
        accounts.resolve(
            db, "google", Profile("g-1", "Invited", ("INVITED@example.test",)), signup=True
        )
        == invited
    )
    assert capacity.waitlist(db) == []


def test_cap_zero_waitlists_everyone_new(db, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "0")
    with pytest.raises(SignInError):
        join(db, 1)
    assert db.users() == [] and waiting(db) == ["person1@example.test"]


def test_disabling_an_account_frees_its_seat(db, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "1")
    uid = join(db, 0)
    with pytest.raises(SignInError):
        join(db, 1)
    with db._lock, db._tx() as conn:
        conn.execute("UPDATE users SET disabled_at = '2026-11-11T00:00:00Z' WHERE id = ?", (uid,))
    join(db, 1)
    assert len(db.users()) == 2


def test_waitlist_admit_by_email_lets_that_person_in_over_the_cap(db, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "1")
    join(db, 0)
    for n in (1, 2):
        with pytest.raises(SignInError):
            join(db, n)
    assert capacity.admit(db, actor="usr_admin", email="PERSON2@example.test") == [
        "person2@example.test"
    ]
    # Only the admitted person gets in; the other keeps waiting.
    with pytest.raises(SignInError):
        join(db, 1)
    join(db, 2)
    assert len(db.users()) == 2
    assert waiting(db) == ["person1@example.test"]
    assert [e["email"] for e in capacity.waitlist(db)] == ["person1@example.test"]
    with pytest.raises(SignInError):
        join(db, 3)
    assert next(a for a in db.audit_entries() if a["action"] == "waitlist.admit")["reason"] == (
        "person2@example.test"
    )


def test_admit_next_takes_the_oldest_first(db, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "0")
    for n in (1, 2, 3):
        with pytest.raises(SignInError):
            join(db, n)
    assert capacity.admit(db, actor="usr_admin", next_n=2) == [
        "person1@example.test",
        "person2@example.test",
    ]
    assert waiting(db) == ["person3@example.test"]
    assert capacity.admit(db, actor="usr_admin", next_n=5) == ["person3@example.test"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"email": "x@example.test", "next_n": 1},
        {"next_n": -1},
        {"email": "nobody@example.test"},
    ],
)
def test_admit_refuses_bad_requests(db, arguments):
    with pytest.raises(ControlError):
        capacity.admit(db, actor="usr_admin", **arguments)


def test_concurrent_signups_cannot_overshoot_the_cap(db, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "3")
    start = threading.Barrier(16)

    def attempt(n):
        start.wait()
        try:
            join(db, n)
            return "in"
        except SignInError as exc:
            return exc.code

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(attempt, range(16)))
    assert results.count("in") == 3 and results.count("waitlist") == 13
    assert len(db.users()) == 3 and len(waiting(db)) == 13


def test_concurrent_signups_through_separate_connections_cannot_overshoot(tmp_path, monkeypatch):
    """Two server processes share one control file: BEGIN IMMEDIATE serialises them."""
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "4")
    path = str(tmp_path / "control.sqlite3")
    dbs = [ControlDB(path) for _ in range(2)]
    try:
        accounts.ensure_schema(dbs[0])
        start = threading.Barrier(12)

        def attempt(n):
            start.wait()
            try:
                join(dbs[n % 2], n)
                return "in"
            except SignInError as exc:
                return exc.code

        with ThreadPoolExecutor(12) as pool:
            results = list(pool.map(attempt, range(12)))
        assert results.count("in") == 4 and results.count("waitlist") == 8
        assert len(dbs[0].users()) == 4
    finally:
        for db in dbs:
            db.close()


def test_schema_is_additive_and_idempotent(db):
    capacity.ensure_schema(db)
    capacity.ensure_schema(db)
    with db._lock:
        columns = [r["name"] for r in db._conn.execute("PRAGMA table_info(signup_waitlist)")]
    assert columns == [
        "email",
        "name",
        "provider",
        "created_at",
        "admitted_at",
        "admitted_by",
        "joined_at",
    ]


def test_waitlist_survives_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "0")
    path = str(tmp_path / "control.sqlite3")
    first = ControlDB(path)
    accounts.ensure_schema(first)
    with pytest.raises(SignInError):
        join(first, 1)
    first.close()
    again = ControlDB(path)
    try:
        assert waiting(again) == ["person1@example.test"]
    finally:
        again.close()


# ── the real gateway: email and provider sign-in ──


@pytest.mark.parametrize("channel", ["github", "email"])
def test_full_server_tells_the_person_plainly_and_admit_lets_them_in(
    signup, remote, monkeypatch, channel
):
    client, mailbox, run = signup
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "1")
    run(channel, email="first@example.test", subject=1)
    status, headers, body = signin(
        client, mailbox, remote, channel, email="late@example.test", subject=2
    )
    if channel == "email":
        assert status == 200 and b"waitlist" in body.lower()
        assert "Set-Cookie" not in headers
    else:
        social_refused((status, headers, body), "waitlist")
    assert len(client.db.users()) == 1
    assert waiting(client.db) == ["late@example.test"]
    capacity.admit(client.db, actor="usr_admin", email="late@example.test")
    cookie_value, _ = run(channel, email="late@example.test", subject=2)
    assert cookie_value and len(client.db.users()) == 2
    assert capacity.waitlist(client.db) == []


# ── instance admin REST + CLI ──


def admin_post(hub, headers, data):
    return request(
        hub,
        "/api/admin/instance/waitlist",
        "POST",
        body=json.dumps(data),
        headers={**headers, "Content-Type": "application/json"},
    )


def test_instance_admin_sees_and_admits_the_waitlist(hub, monkeypatch):
    clock(hub)
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "1")
    for n in (1, 2):
        with pytest.raises(SignInError):
            join(hub.db, n)
    owner = cookie(hub)
    status, _, body = request(hub, "/api/admin/instance/waitlist", headers=owner)
    listing = json.loads(body)
    assert status == 200
    assert listing["max_accounts"] == 1 and listing["waiting"] == 2
    assert [e["email"] for e in listing["waitlist"]] == [
        "person1@example.test",
        "person2@example.test",
    ]
    status, _, body = admin_post(hub, owner, {"email": "person2@example.test"})
    assert (status, json.loads(body)) == (200, {"admitted": ["person2@example.test"]})
    status, _, body = admin_post(hub, owner, {"next": 1})
    assert json.loads(body) == {"admitted": ["person1@example.test"]}
    assert admin_post(hub, owner, {})[0] == 400
    assert admin_post(hub, owner, {"email": 5})[0] == 400
    assert admin_post(hub, owner, {"email": "nobody@example.test"})[0] == 400
    assert admin_post(hub, owner, {"next": "1"})[0] == 400


def test_waitlist_admin_routes_require_an_instance_admin(hub):
    clock(hub)
    member = hub.db.create_user("member@example.com")
    hub.db.add_member(hub.workspace, member, "owner", actor=hub.admin)
    plain = cookie(hub, "member@example.com")
    path = "/api/admin/instance/waitlist"
    assert request(hub, path)[0] == 401
    assert (
        request(hub, path, "POST", body="{}", headers={"Content-Type": "application/json"})[0]
        == 401
    )
    assert request(hub, path, headers=plain)[0] == 403
    assert admin_post(hub, plain, {"next": 1})[0] == 403
    assert [e for e in capacity.waitlist(hub.db)] == []


def test_cli_lists_and_admits(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COMMONPLACE_MAX_ACCOUNTS", "1")
    data = tmp_path / "data"
    assert main(["--data", str(data), "init", "--admin-email", "admin@example.test"]) == 0
    db = ControlDB(str(data / "control.sqlite3"))
    accounts.ensure_schema(db)
    try:
        for n in (1, 2):
            with pytest.raises(SignInError):
                join(db, n)
    finally:
        db.close()
    capsys.readouterr()
    assert main(["--data", str(data), "waitlist", "list"]) == 0
    out = capsys.readouterr().out
    assert "1 of 1" in out and "person1@example.test" in out and "waiting" in out
    assert main(["--data", str(data), "waitlist", "admit", "--next", "1"]) == 0
    assert "admitted  person1@example.test" in capsys.readouterr().out
    assert main(["--data", str(data), "waitlist", "admit", "nobody@example.test"]) != 0
