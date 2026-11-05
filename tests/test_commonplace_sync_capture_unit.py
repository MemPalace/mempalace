"""Pure capture/cadence units; continuity and feed use the frozen call seams."""

import hashlib
import io
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

import commonplace
from commonplace import capture, continuity_schema, import_grant, ingest_cadence, plans, sessions
from commonplace.control_db import ControlDB
from commonplace.handoff.models import ThreadMessage
from commonplace.handoff.thread_store import ThreadStore
from commonplace.policy import Policy

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def native(text=" café\r\nExact words.\t", sid=None):
    record = {"type": "user", "message": {"role": "user", "content": text}}
    if sid:
        record["sessionId"] = sid
    return json.dumps(record, ensure_ascii=False) + "\r\n"


def body(text=None, tid="session", **extra):
    return {
        "source": "claude-code",
        "thread_id": tid,
        "transcript": native() if text is None else text,
        "offset": 0,
        "prefix_sha256": capture.EMPTY_SHA256,
        **extra,
    }


@pytest.fixture
def unit(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    monkeypatch.setattr(plans, "_now", lambda: NOW)
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    user = db.create_user("owner@example.test")
    ws = db.create_workspace("personal", user, personal=True)
    store = ThreadStore(str(tmp_path / "threads.sqlite3"))
    routes = {}
    gateway = SimpleNamespace(
        principal=sessions.person_principal(db, user, ws),
        storage_meter=SimpleNamespace(audit_limit=lambda *args: None),
    )
    gateway.principal_for = lambda handler: gateway.principal
    gateway.add_route = lambda method, path, route: routes.__setitem__((method, path), route)
    ctx = SimpleNamespace(
        control_db=db,
        gateway=gateway,
        data_dir=tmp_path,
        has_threads=lambda workspace: bool(store.list_source_range(workspace, "capture:", limit=1)),
        store_for=lambda workspace: store,
    )
    calls = []
    backfills = []
    notices = []

    def stream_push(conn, target, **kw):
        # Exercise the ungrouped base writer with B1's real gate. Grouping is
        # the continuity unit's responsibility, not reimplemented by this fake.
        calls.append(kw)
        assert conn.in_transaction
        return capture.append_capture(
            target,
            kw["ws"],
            kw["stream_id"],
            kw["source"],
            kw["data"].decode(),
            kw["offset"],
            kw["prefix"],
            actor=kw["actor"],
            maximum=kw["maximum"],
            interval=ingest_cadence.account_interval(db, gateway.principal),
            gate=kw["gate"],
        )

    def backfill(target_db, target_store, workspace, filer):
        assert not target_db._conn.in_transaction
        assert not target_db._lock._is_owned()
        backfills.append((workspace, filer))

    core = SimpleNamespace(stream_push=stream_push, backfill=backfill)

    def record(conn, workspace, kind, data):
        assert conn.in_transaction
        conn.execute(
            "INSERT INTO sync_events VALUES (?,(SELECT coalesce(max(seq),0)+1 FROM sync_events "
            "WHERE workspace_id=?),?,?,?)",
            (workspace, workspace, plans._now().isoformat(), kind, json.dumps(data)),
        )

    def notify(workspace):
        assert not db._conn.in_transaction
        notices.append(workspace)

    monkeypatch.setattr(commonplace, "continuity", core, raising=False)
    monkeypatch.setattr(
        commonplace, "sync_feed", SimpleNamespace(record=record, notify=notify), raising=False
    )
    capture.register(ctx)
    with db._lock:
        db._conn.execute("INSERT INTO capture_settings VALUES (?,1)", (ws,))

    def request(data=None, *, path="/api/capture", method="POST", principal=None):
        gateway.principal = principal or sessions.person_principal(db, user, ws)
        raw = json.dumps(data).encode()
        headers = Message()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(raw))
        handler = SimpleNamespace(
            path=path + ("?" + urlencode(data) if method == "GET" and data else ""),
            command=method,
            headers=headers,
            rfile=io.BytesIO(raw),
        )
        replies = []
        handler._send_json = lambda status, result, headers: replies.append((status, result))
        routes[method, path](handler)
        return replies[0]

    try:
        yield SimpleNamespace(
            db=db,
            user=user,
            ws=ws,
            store=store,
            request=request,
            core=core,
            calls=calls,
            backfills=backfills,
            notices=notices,
            ctx=ctx,
        )
    finally:
        store.close()
        db.close()


def admit(unit, *, seconds=0, digest="first", connection="conn_unit", **kwargs):
    return ingest_cadence.admit(
        unit.db,
        account_id=unit.user,
        connection_id=connection,
        request_sha256=digest,
        interval=900,
        now=NOW + timedelta(seconds=seconds),
        **kwargs,
    )


def ledger(unit):
    return tuple(
        unit.db._conn.execute("SELECT last_admitted_at,window_until FROM ingest_cadence").fetchone()
    )


def test_default_import_cadence_and_schema_upgrade_are_unchanged(unit):
    # Recreate the old two-column ledger, then upgrade twice without losing it.
    unit.db._conn.execute("DROP TABLE ingest_cadence")
    unit.db._conn.execute(
        "CREATE TABLE ingest_cadence(account_id TEXT PRIMARY KEY,last_admitted_at TEXT NOT NULL)"
    )
    unit.db._conn.execute("INSERT INTO ingest_cadence VALUES (?,?)", (unit.user, NOW.isoformat()))
    ingest_cadence.ensure_schema(unit.db)
    ingest_cadence.ensure_schema(unit.db)
    assert ledger(unit) == (NOW.isoformat(), None)
    assert admit(unit, seconds=1) == 900
    assert admit(unit, seconds=900) == 0
    assert ledger(unit) == ((NOW + timedelta(seconds=900)).isoformat(), None)
    assert admit(unit, seconds=901) == 0  # exact own retry
    with unit.db._lock, unit.db._tx():
        with pytest.raises(RuntimeError, match="inside a control transaction"):
            admit(unit)


def test_capture_window_rides_and_backwards_clock(unit):
    assert admit(unit, window=60) == 0
    original = ledger(unit)
    assert admit(unit, seconds=30, digest="capture", window=60) == 0
    assert admit(unit, seconds=30, digest="import", ride=True) == 0
    assert admit(unit, seconds=30, digest="ordinary") == 871
    assert ledger(unit) == original
    assert admit(unit, seconds=-1, digest="backwards", ride=True) == 900
    assert admit(unit, seconds=60, digest="closed", window=60) == 841
    assert admit(unit, seconds=900, digest="next", window=60) == 0
    assert ledger(unit)[1] == (NOW + timedelta(seconds=960)).isoformat()


def test_import_slot_never_opens_window_and_prunes_exact_retries(unit):
    assert admit(unit) == 0
    assert ledger(unit)[1] is None
    assert admit(unit, seconds=30, digest="capture", window=60) == 871
    later = ingest_cadence.RETRY_WINDOW_SECONDS + 1
    assert admit(unit, seconds=later, digest="new") == 0
    assert admit(unit, seconds=later) == 900
    assert [
        row[0] for row in unit.db._conn.execute("SELECT request_sha256 FROM ingest_requests")
    ] == ["new"]


@pytest.mark.parametrize(
    "seconds,expected",
    [(-10, 900), (0, 0), (59, 59), (60, 900), (899, 900), (900, 900), (901, 901)],
)
def test_next_window_truth_table_and_no_writes(unit, seconds, expected):
    assert ingest_cadence.next_window(unit.db, unit.user, 0, NOW) is None
    assert ingest_cadence.next_window(unit.db, unit.user, 900, NOW) == NOW
    admit(unit, window=60)
    before = unit.db._conn.total_changes
    assert ingest_cadence.next_window(
        unit.db, unit.user, 900, NOW + timedelta(seconds=seconds)
    ) == NOW + timedelta(seconds=expected)
    assert unit.db._conn.total_changes == before


def test_caller_transaction_rolls_back_and_paid_needs_no_transaction(unit, monkeypatch):
    with pytest.raises(ValueError), unit.db._lock, unit.db._tx() as conn:
        assert admit(unit, window=60, conn=conn) == 0
        raise ValueError("rollback")
    assert unit.db._conn.execute("SELECT count(*) FROM ingest_cadence").fetchone()[0] == 0
    plans.set_plan(unit.db, unit.user, "paid", actor=unit.user)
    principal = sessions.person_principal(unit.db, unit.user, unit.ws)
    assert ingest_cadence.account_interval(unit.db, principal) == 0
    monkeypatch.setattr(unit.db, "_tx", lambda: pytest.fail("paid admission opened a transaction"))
    assert (
        ingest_cadence.admit(
            unit.db,
            account_id=unit.user,
            connection_id="conn",
            request_sha256="x",
            interval=0,
            now=NOW,
        )
        == 0
    )


def test_account_identity_for_grants_shared_and_unknown_accounts(unit):
    owner = sessions.person_principal(unit.db, unit.user, unit.ws)
    grant = replace(
        owner,
        person=False,
        connection_id="conn_grant",
        policy=Policy.build(
            scopes=["memory:read", "memory:write", import_grant.IMPORT_SCOPE], write_wing="inbox"
        ),
    )
    assert import_grant.grant_path(unit.db, grant)
    assert ingest_cadence.account_key(unit.db, grant) == grant.user_id
    shared = unit.db.create_workspace("shared", unit.user)
    principal = replace(owner, workspace_id=shared)
    plans.set_plan(unit.db, unit.user, "paid", actor=unit.user)
    assert ingest_cadence.account_key(unit.db, principal) == unit.user
    assert ingest_cadence.account_interval(unit.db, principal) == 900
    plans.set_plan(unit.db, unit.user, "none", actor=unit.user)
    assert ingest_cadence.account_interval(unit.db, owner) == 900


def test_card223_account_gate_distinguishes_capture_cadence(unit, monkeypatch):
    status, first = unit.request(body())
    assert status == 200
    assert unit.backfills == []  # no native identity: no adoption
    monkeypatch.setattr(plans, "_now", lambda: NOW + timedelta(seconds=30))
    status, refused = unit.request(
        body(native("suffix"), offset=first["offset"], prefix_sha256=first["prefix_sha256"])
    )
    assert status == 429
    assert refused["error"].startswith("Capture not due; retry in ")
    assert unit.request(body(tid="second"))[0] == 200
    monkeypatch.setattr(plans, "_now", lambda: NOW + timedelta(seconds=61))
    status, refused = unit.request(body(tid="third"))
    assert status == 429
    assert refused["error"] == "Account capture not due; retry in 840 seconds"  # RED
    rows = unit.db._conn.execute("SELECT connection_id FROM ingest_requests").fetchall()
    assert [row[0] for row in rows] == [unit.user]
    assert (
        capture.cursor(unit.store, unit.store.get(first["stored_thread_id"]))["last_sync"]
        == NOW.isoformat()
    )


def test_unknown_free_quota_refusal_at_next_window_rolls_back_slot(unit, monkeypatch):
    assert unit.request(body())[0] == 200
    before = ledger(unit)
    monkeypatch.setattr(plans, "_now", lambda: NOW + timedelta(seconds=901))
    monkeypatch.setattr(plans, "FREE", replace(plans.FREE, storage_bytes=1))
    status, refused = unit.request(body(tid="quota"))
    assert status == 507
    assert refused["code"] == "storage_limit"
    assert ledger(unit) == before
    assert unit.db._conn.execute("SELECT count(*) FROM ingest_requests").fetchone()[0] == 1
    assert unit.store.by_source(unit.ws, f"capture:{unit.user}:claude-code:quota") is None


def test_op_replay_conflict_expiry_and_capture_off(unit, monkeypatch):
    data = body(client_op_id="op_12345678901234")
    status, first = unit.request(data)
    assert status == 200
    calls = len(unit.calls)
    monkeypatch.setattr(plans, "_now", lambda: NOW + timedelta(seconds=5))
    assert unit.request(data) == (200, first)
    assert len(unit.calls) == calls
    status, conflict = unit.request({**data, "transcript": native("changed")})
    assert status == 409
    assert conflict == {
        "error": "client_op_id was used for a different request",
        "code": "op_conflict",
    }
    assert len(unit.calls) == calls
    assert unit.request(body())[1]["status"] == "duplicate"
    monkeypatch.setattr(plans, "_now", lambda: NOW + timedelta(days=1, seconds=1))
    assert unit.request(body(tid="new"))[0] == 200
    assert unit.db._conn.execute("SELECT count(*) FROM capture_ops").fetchone()[0] == 0
    assert unit.request({"enabled": False}, path="/api/settings/capture")[0] == 200
    assert unit.request(data)[0] == 409


@pytest.mark.parametrize("op", [None, "short", 123, "x" * 65, "x" * 15 + "!"])
def test_invalid_op_id_refused(unit, op):
    assert unit.request(body(client_op_id=op))[0] == 400
    assert unit.calls == []


def test_person_op_ids_are_scoped_to_actor(unit, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    member = unit.db.create_user("member@example.test")
    unit.db.add_member(unit.ws, member, "member", actor=unit.user)
    person = sessions.person_principal(unit.db, member, unit.ws)
    data = body(client_op_id="op_12345678901234")
    owner_status, owner = unit.request(data)
    member_status, saved = unit.request(data, principal=person)
    assert (owner_status, member_status) == (200, 200)
    assert saved["stored_thread_id"] != owner["stored_thread_id"]
    assert unit.store.get(saved["stored_thread_id"]).source_thread_id.startswith(
        f"capture:{member}:"
    )
    assert set(row[0] for row in unit.db._conn.execute("SELECT actor_id FROM capture_ops")) == {
        unit.user,
        member,
    }
    # The owner's op id is still unused by this member.
    assert unit.request(body(client_op_id="op_owner_only_123", tid="owner"))[0] == 200
    assert (
        unit.request(
            body(native("different"), client_op_id="op_owner_only_123", tid="member"),
            principal=person,
        )[0]
        == 200
    )


def test_person_admission_cannot_borrow_owners_exact_retry(unit, monkeypatch):
    # Personal workspace is essential: both persons share the owner's account.
    member = unit.db.create_user("member@example.test")
    unit.db.add_member(unit.ws, member, "member", actor=unit.user)
    person = sessions.person_principal(unit.db, member, unit.ws)
    assert person.connection_id == ""
    assert unit.request(body())[0] == 200
    monkeypatch.setattr(plans, "_now", lambda: NOW + timedelta(seconds=61))
    assert unit.request(body(), principal=person) == (
        429,
        {"error": "Account capture not due; retry in 840 seconds"},
    )
    assert [
        row[0] for row in unit.db._conn.execute("SELECT connection_id FROM ingest_requests")
    ] == [unit.user]


def test_settings_events_are_atomic_and_notify_after_commit(unit):
    status, result = unit.request({"sync_now": True}, path="/api/settings/capture")
    assert status == 200
    assert result == {"enabled": True, "interval_seconds": 900, "private": True}
    assert (
        unit.request({"enabled": False, "sync_now": True}, path="/api/settings/capture")[0] == 200
    )
    assert [
        row[0] for row in unit.db._conn.execute("SELECT kind FROM sync_events ORDER BY seq")
    ] == ["sync.request", "sync.capture", "sync.request"]
    assert unit.notices == [unit.ws, unit.ws]
    for invalid in (
        {},
        {"sync_now": False},
        {"sync_now": 1},
        {"enabled": 1},
        {"enabled": True, "sync_now": False},
    ):
        assert unit.request(invalid, path="/api/settings/capture")[0] == 400
    assert len(unit.notices) == 2
    assert unit.request(method="GET", path="/api/settings/capture")[1]["enabled"] is False
    person = sessions.person_principal(unit.db, unit.user, unit.ws)
    assert (
        unit.request(
            {"sync_now": True},
            path="/api/settings/capture",
            principal=replace(person, person=False),
        )[0]
        == 403
    )
    assert unit.db._conn.execute("SELECT count(*) FROM ingest_cadence").fetchone()[0] == 0


def seed_stream(unit, identity, stored, raw, *, legacy=0):
    unit.db._conn.execute(
        "INSERT INTO sync_streams(workspace_id,stream_id,user_id,connection_id,kind,group_id,"
        "own_stored_id,offset,last_sync,legacy) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            unit.ws,
            identity,
            unit.user,
            "",
            "capture",
            "group",
            stored.id,
            len(raw),
            NOW.isoformat(),
            legacy,
        ),
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_grouped_get_uses_own_cursor_without_backfill_or_writes(unit, legacy):
    raw = native(sid="grouped").encode()
    identity = f"capture:{unit.user}:claude-code:session"
    if legacy:
        stored = unit.store.store(
            unit.ws,
            identity,
            [ThreadMessage(0, "system", "other", "", raw.decode(), name=capture.RAW_NAME)],
            actor="synthetic",
            wing=capture.PRIVATE_WING,
        )
        raw += native("continued").encode()
    else:
        stored = unit.store.store(
            unit.ws,
            identity,
            [ThreadMessage(0, "system", "other", "", '{"v":1}', name=continuity_schema.MARKER)],
            actor="synthetic",
            wing=capture.PRIVATE_WING,
        )
    seed_stream(unit, identity, stored, raw, legacy=legacy)
    expected = {
        "offset": len(raw),
        "prefix_sha256": hashlib.sha256(raw).hexdigest(),
        "stored_thread_id": stored.id,
        "last_sync": NOW.isoformat(),
    }

    def stream_cursor(conn, store, **kw):
        assert kw == {"ws": unit.ws, "stream_id": identity, "own_identity": identity}
        assert store is unit.store
        return expected

    unit.core.stream_cursor = stream_cursor
    before = unit.db._conn.total_changes
    count = unit.store.get(stored.id).message_count
    status, result = unit.request({"source": "claude-code", "thread_id": "session"}, method="GET")
    assert status == 200
    assert result == dict(expected, interval_seconds=900)
    assert unit.db._conn.total_changes == before
    assert unit.store.get(stored.id).message_count == count
    assert unit.backfills == []


def test_ungrouped_get_equals_base_cursor_and_unknown_settings_fail_closed(unit):
    status, saved = unit.request(body())
    assert status == 200
    stored = unit.store.get(saved["stored_thread_id"])
    before = unit.db._conn.total_changes
    assert unit.request({"source": "claude-code", "thread_id": "session"}, method="GET") == (
        200,
        dict(capture.cursor(unit.store, stored), interval_seconds=900),
    )
    assert unit.db._conn.total_changes == before
    assert unit.request(method="GET", path="/api/settings/capture")[1]["interval_seconds"] == 900


def test_crashed_own_marker_dispatches_group_step_to_rebuild_row(unit):
    text = native(sid="session")
    identity = f"capture:{unit.user}:claude-code:session"
    holder = unit.store.store(
        unit.ws,
        "capture:conn_holder:claude-code:session",
        [ThreadMessage(0, "system", "other", "", text, name=capture.RAW_NAME)],
        actor="synthetic",
        wing=capture.PRIVATE_WING,
    )
    marker = unit.store.store(
        unit.ws,
        identity,
        [ThreadMessage(0, "system", "other", "", '{"v":1}', name=continuity_schema.MARKER)],
        actor="synthetic",
        wing=capture.PRIVATE_WING,
        parent_stored_id=holder.id,
    )

    def group_step(conn, store, **kw):
        assert kw["stream_id"] == identity
        assert (
            conn.execute("SELECT 1 FROM sync_streams WHERE stream_id=?", (identity,)).fetchone()
            is None
        )
        assert store.messages(marker.id)[0].name == continuity_schema.MARKER
        kw["gate"]()
        seed_stream(unit, identity, marker, text.encode())
        return {
            "status": "appended",
            "offset": len(text.encode()),
            "prefix_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "stored_thread_id": marker.id,
            "last_sync": plans._now().isoformat(),
            "added_messages": 1,
        }

    # Only the group seam may heal this crash; base append would archive a copy.
    unit.core.stream_push = group_step
    status, saved = unit.request(body(text))
    assert status == 200
    assert saved["stored_thread_id"] == marker.id
    assert unit.store.get(marker.id).message_count == 1
    assert unit.db._conn.execute(
        "SELECT offset FROM sync_streams WHERE stream_id=?", (identity,)
    ).fetchone()[0] == len(text.encode())
    assert unit.backfills == [(unit.ws, unit.user)]


def test_backfill_precheck_preserves_disabled_error_order(unit):
    assert unit.request(body())[0] == 200
    assert unit.request(body(native(sid="new"), tid="new"))[0] == 200
    assert unit.backfills == [(unit.ws, unit.user)]
    assert unit.request({"enabled": False}, path="/api/settings/capture")[0] == 200
    unit.backfills.clear()
    assert unit.request({"source": "unsupported"}) == (409, {"error": "Workspace capture is off"})
    assert unit.backfills == []
