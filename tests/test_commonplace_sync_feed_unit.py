"""Synthetic sync-feed units; cross-unit acceptance runs at integration."""

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from commonplace import capture, continuity_schema, events, ingest_cadence, plans, sync_feed, web
from commonplace.control_db import ControlDB, Principal
from commonplace.policy import Policy
from commonplace.sessions import person_policy
from commonplace.sync_protocol import SYNC_ENVELOPE_KEYS, SYNC_EVENT_KEYS

NOW = datetime(2026, 1, 2, 12, 30, 45, 123456, tzinfo=timezone.utc)


class Handler:
    def __init__(self, path="/api/events?feed=sync", *, cookie=None, body=None):
        self.path = path
        self.headers = Message()
        if cookie:
            self.headers["Cookie"] = cookie
        self.wfile = io.BytesIO()
        self.response_headers = {}
        self.command = "GET" if body is None else "POST"
        payload = json.dumps(body).encode() if body is not None else b""
        self.rfile = io.BytesIO(payload)
        self.headers["Content-Length"] = str(len(payload))
        self.headers["Content-Type"] = "application/json"

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass

    def _send_json(self, status, result, headers=None):
        self.status = status
        self.response_headers.update(headers or {})
        self.wfile.write(json.dumps(result).encode())

    @property
    def json(self):
        return json.loads(self.wfile.getvalue())


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    monkeypatch.setattr(plans, "_now", lambda: NOW)
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    user = db.create_user("synthetic@example.test")
    ws = db.create_workspace("synthetic", user, personal=True)
    continuity_schema.ensure_schema(db)
    ingest_cadence.ensure_schema(db)
    db._conn.execute(
        "CREATE TABLE capture_settings (workspace_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL)"
    )
    db._conn.execute("INSERT INTO capture_settings VALUES (?,1)", (ws,))
    principal = Principal(
        user_id=user,
        user_email="synthetic@example.test",
        workspace_id=ws,
        connection_id="conn_synthetic",
        connection="synthetic",
        client="claude-code",
        auth="pat",
        role="owner",
        policy=Policy.build(write_wing="inbox"),
    )
    # These frozen cadence helpers land in the cadence unit's branch. Stubs
    # keep these tests independent of that implementation before integration.
    monkeypatch.setattr(
        ingest_cadence,
        "account_interval",
        lambda db, p: ingest_cadence.interval_for(db, plans.workspace_account(db, p.workspace_id)),
        raising=False,
    )
    monkeypatch.setattr(
        ingest_cadence,
        "account_key",
        lambda db, p: plans.workspace_account(db, p.workspace_id) or p.user_id,
        raising=False,
    )
    next_window = Mock(return_value=None)
    monkeypatch.setattr(ingest_cadence, "next_window", next_window, raising=False)
    a = SimpleNamespace(db=db, ws=ws, user=user, principal=principal, next_window=next_window)
    try:
        yield a
        assert sync_feed.waiter_count() == 0
    finally:
        db.close()


def emit(a, kind="sync.request", data=None, *, db=None, ws=None):
    db = db or a.db
    with db._lock, db._tx() as conn:
        seq = sync_feed.record(conn, ws or a.ws, kind, {} if data is None else data)
    sync_feed.notify(ws or a.ws)
    return seq


def request(a, path="/api/events?feed=sync", *, principal=None, cookie=None):
    h = Handler(path, cookie=cookie)
    sync_feed.handle(SimpleNamespace(db=a.db), h, principal or a.principal)
    return h


def watch_wait(monkeypatch):
    registered = threading.Event()
    original = sync_feed._condition.wait

    def wait(timeout=None):
        registered.set()
        return original(timeout)

    monkeypatch.setattr(sync_feed._condition, "wait", wait)
    return registered


@pytest.mark.parametrize(
    "person,client,grant,writable,personal,owner,allowed",
    [
        (True, "web", False, True, True, True, True),
        (True, "web", False, False, True, True, False),
        (True, "web", False, True, False, False, True),
        (False, "claude-code", False, True, True, True, True),
        (False, "codex", False, True, False, False, True),
        (False, "claude-code", False, False, True, True, False),
        (False, "generic", True, True, True, True, True),
        (False, "other", True, True, True, True, True),
        (False, "generic", True, True, False, True, False),
        (False, "generic", True, True, True, False, False),
        (False, "generic", True, False, True, True, False),
        (False, "generic", False, True, True, True, False),
        (False, "chatgpt", False, True, True, True, False),
        (False, "t3", False, True, True, True, False),
    ],
)
def test_authorized_matrix(account, person, client, grant, writable, personal, owner, allowed):
    a = account
    ws = a.ws if personal else a.db.create_workspace("shared", a.user)
    user = a.user
    if not owner:
        user = a.db.create_user("member@example.test")
        a.db.add_member(ws, user, "member", actor=a.user)
    scopes = ["memory:read"] + (["memory:write"] if writable else [])
    if grant:
        scopes.append("memory:import")
    p = replace(
        a.principal,
        person=person,
        client=client,
        workspace_id=ws,
        user_id=user,
        role="owner" if owner else "member",
        policy=Policy.build(scopes=scopes, write_wing="inbox"),
    )
    assert sync_feed.authorized(a.db, p) is allowed


def test_sequence_is_per_workspace_and_independent_of_hub(account):
    a = account
    events.ensure_schema(a.db)
    assert emit(a) == 1
    assert emit(a, ws="ws_other") == 1
    events.emit(a.db, a.ws, "card.created", data={"title": "synthetic"})
    assert emit(a, "sync.capture", {"enabled": False}) == 2
    batch, seq, more, reset = sync_feed.page(a.db, a.ws, 0)
    assert (seq, more, reset) == (2, False, False)
    assert batch == [
        {"seq": 1, "at": "2026-01-02T12:30:45Z", "kind": "sync.request", "data": {}},
        {
            "seq": 2,
            "at": "2026-01-02T12:30:45Z",
            "kind": "sync.capture",
            "data": {"enabled": False},
        },
    ]
    assert all(set(e) == SYNC_EVENT_KEYS for e in batch)
    assert [e["kind"] for e in events.after(a.db, a.ws)] == ["card.created"]


@pytest.mark.parametrize(
    "kind,data",
    [
        ("memory.saved", {}),
        ("sync.request", {"text": "synthetic"}),
        ("sync.capture", {}),
        ("sync.capture", {"enabled": 1}),
        ("sync.capture", {"enabled": True, "text": "synthetic"}),
    ],
)
def test_record_rejects_content_and_invalid_shapes(account, kind, data):
    with account.db._lock, account.db._tx() as conn:
        with pytest.raises(ValueError, match="Invalid sync signal"):
            sync_feed.record(conn, account.ws, kind, data)
    assert sync_feed.page(account.db, account.ws, 0) == ([], 0, False, False)


def test_retention_reset_and_paging(account):
    a = account
    with a.db._lock, a.db._tx() as conn:
        for _ in range(1005):
            sync_feed.record(conn, a.ws, "sync.request", {})
    assert sync_feed.page(a.db, a.ws, 4) == ([], 1005, False, True)
    assert sync_feed.page(a.db, a.ws, 1006) == ([], 1005, False, True)
    after = 5
    seen = []
    while after < 1005:
        batch, after, more, reset = sync_feed.page(a.db, a.ws, after)
        assert len(batch) == 200
        assert reset is False
        assert more is (after < 1005)
        seen.extend(e["seq"] for e in batch)
    assert seen == list(range(6, 1006))
    assert sync_feed.page(a.db, a.ws, 1005) == ([], 1005, False, False)
    assert sync_feed.page(a.db, "ws_empty", 1) == ([], 0, False, True)


@pytest.mark.parametrize(
    "suffix,error",
    [
        ("&after=-1", "Invalid event cursor"),
        ("&after=9223372036854775808", "Invalid event cursor"),
        ("&after=garbage", "Invalid event cursor"),
        ("&after=", "Invalid event cursor"),
        ("&wait=-1", "Invalid wait"),
        ("&wait=26", "Invalid wait"),
        ("&wait=1.5", "Invalid wait"),
        ("&wait=", "Invalid wait"),
    ],
)
def test_invalid_request(account, suffix, error):
    h = request(account, "/api/events?feed=sync" + suffix)
    assert h.status == 400
    assert h.json == {"error": error}


def test_authorization_precedes_parameter_parsing(account):
    h = request(
        account,
        "/api/events?feed=sync&after=bad",
        principal=replace(account.principal, client="other"),
    )
    assert h.status == 403
    assert h.json == {"error": "Sync feed requires a capture or import connection"}


@pytest.mark.parametrize(
    "interval,next_at", [(0, None), (900, NOW), (900, NOW + timedelta(seconds=900))]
)
def test_state_delegates_to_cadence(account, monkeypatch, interval, next_at):
    a = account
    monkeypatch.setattr(ingest_cadence, "account_interval", lambda db, p: interval)
    a.next_window.return_value = next_at
    expected = {
        "capture_enabled": True,
        "interval_seconds": interval,
        "next_window_at": next_at.isoformat().replace("+00:00", "Z") if next_at else None,
    }
    assert sync_feed.state(a.db, a.principal, NOW) == expected
    a.next_window.assert_called_once_with(a.db, a.user, interval, NOW)
    a.db._conn.execute("DELETE FROM capture_settings")
    assert sync_feed.state(a.db, a.principal, NOW)["capture_enabled"] is False


@pytest.mark.parametrize("personal", [True, False])
def test_free_and_unknown_never_wait(account, monkeypatch, personal):
    a = account
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    p = a.principal
    if not personal:
        p = replace(p, workspace_id=a.db.create_workspace("shared", a.user))
    a.next_window.return_value = NOW
    monkeypatch.setattr(sync_feed, "_wait", Mock(side_effect=AssertionError("Free waited")))
    h = request(a, "/api/events?feed=sync&wait=5", principal=p)
    assert h.status == 200
    assert set(h.json) == SYNC_ENVELOPE_KEYS
    assert h.json["waited"] is False
    assert h.json["state"]["interval_seconds"] == 900
    assert h.json["state"]["next_window_at"] == "2026-01-02T12:30:45.123456Z"
    assert h.response_headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("paid", [False, True])
def test_live_wait_and_concurrent_settings_post(account, monkeypatch, paid):
    a = account
    if paid:
        monkeypatch.setenv("COMMONPLACE_PLANS", "on")
        plans.set_plan(a.db, a.user, "paid", actor=a.user)
    ready = watch_wait(monkeypatch)
    routes = {}
    gateway = SimpleNamespace(
        db=a.db,
        principal_for=lambda h: replace(a.principal, person=True, policy=person_policy("owner")),
        add_route=lambda method, path, handler: routes.update({(method, path): handler}),
    )
    capture.register(SimpleNamespace(control_db=a.db, gateway=gateway))
    with ThreadPoolExecutor(max_workers=2) as pool:
        job = pool.submit(request, a, "/api/events?feed=sync&wait=5")
        try:
            assert ready.wait(2)
            assert sync_feed.waiter_count() == 1
            h = Handler("/api/settings/capture", body={"enabled": False})
            post = pool.submit(routes["POST", "/api/settings/capture"], h)
            post.result(timeout=2)
            assert h.status == 200
            # The feed writer belongs to another unit; publish its frozen
            # post-commit notification explicitly in this isolated test.
            emit(a, "sync.capture", {"enabled": False})
        finally:
            emit(a)
        answer = job.result(timeout=2)
    assert answer.status == 200
    assert answer.json["waited"] is True
    assert answer.json["state"]["capture_enabled"] is False
    assert answer.json["events"][0]["data"] == {"enabled": False}


def test_second_database_instance_wakes_without_notification(account, monkeypatch):
    a = account
    ready = watch_wait(monkeypatch)
    other = ControlDB(a.db.path)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(request, a, "/api/events?feed=sync&wait=5")
            assert ready.wait(2)
            with other._lock, other._tx() as conn:
                sync_feed.record(conn, a.ws, "sync.request", {})
            answer = job.result(timeout=3)
        assert answer.status == 200
        assert answer.json["waited"] is True
        assert [e["seq"] for e in answer.json["events"]] == [1]
    finally:
        other.close()


def test_waiter_never_observes_rolled_back_record(account, monkeypatch):
    a = account
    ready = watch_wait(monkeypatch)
    written = threading.Event()
    rollback = threading.Event()

    def writer():
        with a.db._lock:
            try:
                with a.db._tx() as conn:
                    sync_feed.record(conn, a.ws, "sync.capture", {"enabled": False})
                    written.set()
                    assert rollback.wait(2)
                    raise ValueError("synthetic rollback")
            except ValueError:
                pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        job = pool.submit(request, a, "/api/events?feed=sync&wait=5")
        assert ready.wait(2)
        write = pool.submit(writer)
        try:
            assert written.wait(2)
            sync_feed.notify(a.ws)
        finally:
            rollback.set()
        write.result(timeout=2)
        emit(a)
        answer = job.result(timeout=2)
    assert answer.status == 200
    assert answer.json["waited"] is True
    assert answer.json["events"] == [
        {"seq": 1, "at": "2026-01-02T12:30:45Z", "kind": "sync.request", "data": {}}
    ]


@pytest.mark.parametrize("scope", ["global", "connection", "session"])
def test_waiter_caps_return_immediately(account, monkeypatch, scope):
    a = account
    entered = threading.Event()
    original = sync_feed._condition.wait

    def wait(timeout=None):
        entered.set()
        return original(timeout)

    monkeypatch.setattr(sync_feed._condition, "wait", wait)
    if scope == "global":
        monkeypatch.setattr(sync_feed, "_MAX_WAITERS", 1)
    else:
        monkeypatch.setattr(sync_feed, "_MAX_PER_CALLER", 1)
    p = replace(a.principal, person=scope == "session")
    cookie = "cmmn_session=synthetic-session" if p.person else None
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request, a, "/api/events?feed=sync&wait=5", principal=p, cookie=cookie)
        try:
            assert entered.wait(2)
            second_p = replace(p, connection_id="conn_other") if scope == "global" else p
            second = pool.submit(
                request, a, "/api/events?feed=sync&wait=5", principal=second_p, cookie=cookie
            ).result(timeout=2)
            assert second.status == 200
            assert second.json["waited"] is False
            assert sync_feed.waiter_count() == 1
        finally:
            emit(a)
        assert first.result(timeout=2).json["waited"] is True


def test_waiter_slots_cleaned_on_error(account, monkeypatch):
    monkeypatch.setattr(sync_feed.time, "monotonic", Mock(side_effect=RuntimeError("synthetic")))
    with pytest.raises(RuntimeError, match="synthetic"):
        request(account, "/api/events?feed=sync&wait=5")
    assert sync_feed.waiter_count() == 0


def test_wait_timeout_is_bounded_and_releases_slot(account, monkeypatch):
    monkeypatch.setattr(sync_feed.time, "monotonic", Mock(side_effect=[0, 0, 1, 2]))
    wait = Mock()
    monkeypatch.setattr(sync_feed._condition, "wait", wait)
    h = request(account, "/api/events?feed=sync&wait=2")
    assert h.status == 200
    assert h.json["waited"] is True
    assert h.json["events"] == []
    assert [call.args for call in wait.call_args_list] == [(1,), (1,)]
    assert sync_feed.waiter_count() == 0


@pytest.mark.parametrize("after,wait", [(0, 0), (0, 5), (50, 5)])
def test_pending_events_and_reset_never_wait(account, monkeypatch, after, wait):
    emit(account)
    monkeypatch.setattr(sync_feed, "_wait", Mock(side_effect=AssertionError("Unexpected wait")))
    h = request(account, f"/api/events?feed=sync&after={after}&wait={wait}")
    assert h.status == 200
    assert h.json["waited"] is False
    assert h.json["reset"] is (after == 50)


def test_ordinary_feed_matches_frozen_wire_snapshot(account, monkeypatch):
    a = account
    routes = {}
    principal = replace(a.principal, person=True, policy=person_policy("owner"))
    gateway = SimpleNamespace(
        db=a.db,
        principal_for=lambda h: principal,
        add_route=lambda method, path, handler: routes.update({(method, path): handler}),
        add_prefix_route=lambda *args: None,
    )
    web.install(gateway)
    monkeypatch.setattr(events, "_now", lambda: "2026-01-02T12:30:45Z")
    events.emit(
        a.db, a.ws, "card.created", subject="1", actor="user:synthetic", data={"title": "synthetic"}
    )
    emit(a, "sync.capture", {"enabled": True})
    h = Handler("/api/events?after=0")
    routes["GET", "/api/events"](h)
    expected = (
        '{"events": [{"seq": 1, "at": "2026-01-02T12:30:45Z", "workspace_id": "'
        + a.ws
        + '", "kind": "card.created", "subject": "1", "actor": "user:synthetic", '
        '"data": {"title": "synthetic"}}], "seq": 1}'
    ).encode()
    assert h.status == 200
    assert h.wfile.getvalue() == expected
    principal = a.principal
    denied = Handler("/api/events")
    routes["GET", "/api/events"](denied)
    assert denied.status == 403
    assert denied.wfile.getvalue() == b'{"error": "Workspace event access required"}'
    synced = Handler("/api/events?feed=sync")
    routes["GET", "/api/events"](synced)
    assert synced.status == 200
    assert synced.json["feed"] == "sync"
