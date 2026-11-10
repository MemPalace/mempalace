"""Restricted webhook identities carry no sequence or membership assumptions."""

# ruff: noqa: F811
import hashlib
import hmac
import json

import pytest

from commonplace import events, webhooks
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_person_private_scope import PRIVATE, world  # noqa: F401
from tests.test_person_private_scope_r1 import _set_role
from tests.test_person_private_scope_r2 import emit, poll, service


@pytest.fixture
def capture(world, monkeypatch):
    webhooks.stop(world.hub.gateway)
    sent = []
    monkeypatch.setattr(webhooks, "validate_url", lambda url: None)
    monkeypatch.setattr(
        webhooks, "_post", lambda url, body, *a, **kw: sent.append(json.loads(body)) or 200
    )
    return sent


def hook(w, headers, suffix="hook"):
    status, body = w.http(
        headers,
        "/api/webhooks",
        {"url": "https://example.test/" + suffix, "events": ["card.updated"]},
    )
    assert status == 201
    with w.hub.db._lock:
        return w.hub.db._conn.execute("SELECT * FROM webhooks WHERE id=?", (body["id"],)).fetchone()


def pending(w, dispatcher, hook_id):
    with w.hub.db._lock:
        rows = w.hub.db._conn.execute(
            "SELECT * FROM webhook_deliveries WHERE webhook_id=? AND status IN ('pending','retry') ORDER BY rowid",
            (hook_id,),
        ).fetchall()
    for row in rows:
        dispatcher._deliver(row)


def opaque_id(w, row, seq):
    with w.hub.db._lock:
        columns = {r[1] for r in w.hub.db._conn.execute("PRAGMA table_info(webhooks)")}
        assert "event_id_key" in columns, "server-only event identity key is missing"
        key = w.hub.db._conn.execute(
            "SELECT event_id_key FROM webhooks WHERE id=?", (row["id"],)
        ).fetchone()[0]
    return (
        "evt_" + hmac.new(key.encode(), str(seq).encode("ascii"), hashlib.sha256).hexdigest()[:32]
    )


@pytest.mark.parametrize("kind", ["person", "owner_shared", "member_token"])
def test_restricted_webhook_http_exposes_no_sequence(world, capture, kind):
    w = world
    service(w).board_add(slug="visible", wing="shared")
    service(w).board_add(slug="hidden", wing="private_notes")
    headers = w.member
    if kind != "person":
        token = w.hub.token(
            kind,
            user=w.hub.admin if kind == "owner_shared" else w.member_id,
            profile="full",
            scopes=["memory:read"],
            read_wings=["shared"],
            trust="trusted",
        )
        principal = w.hub.db.authenticate_token(token)
        assert principal.role == ("owner" if kind == "owner_shared" else "member")
        assert not principal.policy.sees_all_wings
        headers = {"Authorization": f"Bearer {token}"}
    restricted, owner = hook(w, headers), hook(w, w.owner, "owner")
    first = emit(w, "visible", "first")
    hidden = emit(w, "hidden", PRIVATE)
    last = emit(w, "visible", "last")
    dispatcher = webhooks.Dispatcher(w.hub.db, gateway=w.hub.gateway)
    dispatcher.reconcile()
    status, listing = w.http(headers, f"/api/webhooks/{restricted['id']}/deliveries")
    assert status == 200 and len(listing["deliveries"]) == 2
    assert all("event_seq" not in item for item in listing["deliveries"]), (
        "restricted subscription exposes event numbers"
    )
    # A workspace owner viewing the same registration cannot change its authority.
    assert all(
        "event_seq" not in item
        for item in w.http(w.owner, f"/api/webhooks/{restricted['id']}/deliveries")[1]["deliveries"]
    )
    with w.hub.db._lock:
        stored = w.hub.db._conn.execute(
            "SELECT payload FROM webhook_deliveries WHERE webhook_id=? ORDER BY rowid",
            (restricted["id"],),
        ).fetchall()
    assert all("seq" not in json.loads(bytes(item["payload"])) for item in stored)
    pending(w, dispatcher, restricted["id"])
    assert [p["id"] for p in capture] == [
        opaque_id(w, restricted, event["seq"]) for event in (first, last)
    ]
    assert all("seq" not in payload for payload in capture)
    assert PRIVATE not in json.dumps(capture)
    capture.clear()
    pending(w, dispatcher, owner["id"])
    assert [p["seq"] for p in capture] == [first["seq"], hidden["seq"], last["seq"]]
    assert all(p["id"] == f"evt_{p['seq']}" for p in capture)
    assert PRIVATE in json.dumps(capture)


@pytest.mark.parametrize("failure", ["missing", "exception"])
def test_unresolved_subscription_numbering_fails_closed(world, capture, monkeypatch, failure):
    w = world
    service(w).board_add(slug="visible", wing="shared")
    token = w.hub.owner_token()
    row = hook(w, {"Authorization": f"Bearer {token}"})
    event = emit(w, "visible", "shared")
    original = w.hub.db.authenticate_connection

    def unresolved(connection_id):
        if connection_id != row["connection_id"]:
            return original(connection_id)
        if failure == "exception":
            raise LookupError("unresolved subscription")
        return None

    monkeypatch.setattr(w.hub.db, "authenticate_connection", unresolved)
    # Direct enqueue/send exercise the fail-closed serializer even though normal
    # delivery admission would cancel an unresolvable registration altogether.
    try:
        delivery_id = webhooks._enqueue(w.hub.db, row, event)
    except LookupError:
        raise AssertionError("subscription resolution error did not fail closed") from None
    with w.hub.db._lock:
        delivery = w.hub.db._conn.execute(
            "SELECT d.*,w.workspace_id,w.connection_id,w.owner_actor,w.secret,w.url,w.auth_header "
            "FROM webhook_deliveries d JOIN webhooks w ON w.id=d.webhook_id WHERE d.id=?",
            (delivery_id,),
        ).fetchone()
    assert "seq" not in json.loads(bytes(delivery["payload"])), (
        "unresolved principal exposes sequence"
    )
    listing = w.http(w.owner, f"/api/webhooks/{row['id']}/deliveries")[1]
    assert all("event_seq" not in item for item in listing["deliveries"])
    webhooks.Dispatcher(w.hub.db, gateway=w.hub.gateway)._send(delivery)
    assert capture == [{**json.loads(bytes(delivery["payload"]))}]
    assert capture[0]["id"] == opaque_id(w, row, event["seq"])


def test_demotion_never_reuses_event_ids_or_exposes_numbers(world, capture):
    w = world
    service(w).board_add(slug="visible", wing="shared")
    service(w).board_add(slug="hidden", wing="private_notes")
    row = hook(w, w.owner)
    dispatcher = webhooks.Dispatcher(w.hub.db, gateway=w.hub.gateway)
    for index in range(3):
        emit(w, "visible", f"owner {index}")
        emit(w, "hidden", PRIVATE)
    dispatcher.reconcile()
    pending(w, dispatcher, row["id"])
    owner_payloads = list(capture)
    assert PRIVATE in json.dumps(owner_payloads)
    assert all(p["id"] == f"evt_{p['seq']}" for p in owner_payloads)
    queued = emit(w, "visible", "queued as owner")
    dispatcher.reconcile()
    _set_role(w, w.hub.admin, "member")
    member_events = [emit(w, "visible", f"member {index}") for index in range(20)]
    emit(w, "hidden", "hidden after demotion")
    dispatcher.reconcile()
    pending(w, dispatcher, row["id"])
    assert len(capture) == len(owner_payloads) + 21
    ids = [payload["id"] for payload in capture]
    assert len(ids) == len(set(ids)), "demotion reuses an id already sent for a different event"
    after = capture[len(owner_payloads) :]
    assert all("seq" not in payload for payload in after)
    assert [p["id"] for p in after] == [
        opaque_id(w, row, e["seq"]) for e in [queued, *member_events]
    ]
    listing = w.http(w.owner, f"/api/webhooks/{row['id']}/deliveries")[1]
    assert all("event_seq" not in item for item in listing["deliveries"])


def test_restricted_ids_are_stable_on_retry_and_subscription_specific(world, capture, monkeypatch):
    w = world
    service(w).board_add(slug="visible", wing="shared")
    one, two = hook(w, w.member, "one"), hook(w, w.member, "two")
    event = emit(w, "visible", "retry shared")
    dispatcher = webhooks.Dispatcher(w.hub.db, gateway=w.hub.gateway)
    attempts = []

    def post(url, body, *args, **kwargs):
        attempts.append(json.loads(body))
        return 500 if len(attempts) == 1 else 200

    monkeypatch.setattr(webhooks, "_post", post)
    dispatcher.reconcile()
    duplicate = webhooks._enqueue(w.hub.db, one, event)
    assert duplicate == webhooks._enqueue(w.hub.db, one, event)
    pending(w, dispatcher, one["id"])
    pending(w, dispatcher, one["id"])
    pending(w, dispatcher, two["id"])
    assert len(attempts) == 3
    expected = [
        opaque_id(w, one, event["seq"]),
        opaque_id(w, one, event["seq"]),
        opaque_id(w, two, event["seq"]),
    ]
    assert [p["id"] for p in attempts] == expected, (
        "event identities are not subscription-keyed HMACs"
    )
    assert expected[0] != expected[2] and all("seq" not in p for p in attempts)


def test_webhook_schema_has_no_counter_or_backfill(world):
    db = world.hub.db
    webhooks.ensure_schema(db)
    with db._lock:
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(webhook_deliveries)")}
    assert "ordinal" not in columns, "counter schema/backfill is still installed"


def test_promotion_resets_member_epoch_before_owner_raw_feed(world):
    w = world
    service(w).board_add(slug="visible", wing="shared")
    service(w).board_add(slug="hidden", wing="private_notes")
    emit(w, "visible", "already seen")
    emit(w, "hidden", PRIVATE)
    first = poll(w)
    seen = poll(w, 0, first["epoch"])
    assert any(e["data"].get("title") == "already seen" for e in seen["events"])
    _set_role(w, w.member_id, "owner")
    status, page = w.http(w.member, f"/api/events?after={seen['seq']}&epoch={first['epoch']}")
    latest = events.latest_seq(w.hub.db, w.hub.workspace)
    assert (status, page) == (200, {"events": [], "seq": latest, "reset": True}), (
        "owner path replays a member-era cursor"
    )
    assert w.http(w.member, f"/api/events?after={latest}") == (200, {"events": [], "seq": latest})
    private = emit(w, "hidden", "owner private control")
    owner_page = w.http(w.member, f"/api/events?after={latest}")[1]
    assert owner_page == {"events": [private], "seq": private["seq"]}


def test_subscriber_signing_secret_cannot_recover_event_sequence(world, capture, caplog):
    w = world
    service(w).board_add(slug="visible", wing="shared")
    status, created = w.http(
        w.member,
        "/api/webhooks",
        {"url": "https://example.test/subscriber", "events": ["card.updated"]},
    )
    assert status == 201
    event = emit(w, "visible", "public shared event")
    dispatcher = webhooks.Dispatcher(w.hub.db, gateway=w.hub.gateway)
    dispatcher.reconcile()
    pending(w, dispatcher, created["id"])
    assert len(capture) == 1 and "seq" not in capture[0], "subscriber receives an event sequence"
    signing_key = created["secret"].encode()
    matches = [
        candidate
        for candidate in range(1, 1025)
        if (
            "evt_"
            + hmac.new(signing_key, str(candidate).encode("ascii"), hashlib.sha256).hexdigest()[:32]
        )
        == capture[0]["id"]
    ]
    assert not matches, "subscriber recovers the raw sequence using its disclosed signing secret"
    assert event["seq"] < 1025  # The enumeration actually covered the emitted sequence.
    with w.hub.db._lock:
        private_key = w.hub.db._conn.execute(
            "SELECT event_id_key FROM webhooks WHERE id=?", (created["id"],)
        ).fetchone()[0]
    public = [
        created,
        capture,
        w.http(w.member, "/api/webhooks")[1],
        w.http(w.member, f"/api/webhooks/{created['id']}/deliveries")[1],
    ]
    exposed = private_key in json.dumps(public) or private_key in caplog.text
    assert not exposed, "server-only event identity key escaped through an API or log"


def test_restricted_event_identity_survives_database_reopen(world, capture, monkeypatch):
    from commonplace.control_db import ControlDB

    w = world
    service(w).board_add(slug="visible", wing="shared")
    row = hook(w, w.member)
    event = emit(w, "visible", "restart shared control")
    attempts = []

    def post(url, body, *args, **kwargs):
        attempts.append(json.loads(body))
        return 503 if len(attempts) == 1 else 200

    monkeypatch.setattr(webhooks, "_post", post)
    dispatcher = webhooks.Dispatcher(w.hub.db, gateway=w.hub.gateway)
    dispatcher.reconcile()
    pending(w, dispatcher, row["id"])
    assert "seq" not in attempts[0], "restricted retry includes a sequence"
    reopened = ControlDB(w.hub.db.path)
    try:
        restarted = webhooks.Dispatcher(reopened, gateway=w.hub.gateway)
        with reopened._lock:
            work = reopened._conn.execute(
                "SELECT * FROM webhook_deliveries WHERE webhook_id=? AND status='retry'",
                (row["id"],),
            ).fetchone()
        assert work is not None
        restarted._deliver(work)
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert attempts[1]["id"] == opaque_id(w, row, event["seq"])
    finally:
        reopened.close()
