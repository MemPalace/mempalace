"""Head-only correctness tests; NOT paired old-red proof.

Direct synthetic ControlDB setup and operator-shaped principals are deliberate
unit exceptions here. The parent's artifact PASS still precedes any execution.
"""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import import_grant, imports, ingest_cadence, plans, sessions
from commonplace.control_db import ControlDB, Principal
from commonplace.handoff import recall
from commonplace.handoff.thread_store import ThreadStore, canonical_sha256
from commonplace.import_parts import append_part
from commonplace.oauth import store as oauth_store
from commonplace.policy import Policy
from commonplace.sessions import person_policy
from tests.test_commonplace_email_auth import fresh as fresh
from tests.test_commonplace_import import multipart_items
from tests.test_commonplace_import_grant import body, journey as journey, native

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    user = db.create_user("synthetic-owner@example.test")
    workspace = db.create_workspace("synthetic personal", user, personal=True)
    ingest_cadence.ensure_schema(db)
    try:
        yield SimpleNamespace(db=db, user=user, workspace=workspace, path=db.path)
    finally:
        db.close()


def agent(account, **changes):
    principal = Principal(
        user_id=account.user,
        user_email="synthetic-owner@example.test",
        workspace_id=account.workspace,
        connection_id="conn_synthetic",
        connection="synthetic",
        client="test",
        auth="pat",
        role="owner",
        person=False,
        policy=Policy.build(
            scopes=["memory:read", "memory:write", "memory:import"],
            read_wings=["*"],
            write_wing="inbox",
            trust="untrusted",
        ),
    )
    return replace(principal, **changes)


def admit(account, *, connection="conn_synthetic", digest="a" * 64, now=NOW, db=None):
    return ingest_cadence.admit(
        db or account.db,
        account_id=account.user,
        connection_id=connection,
        request_sha256=digest,
        interval=900,
        now=now,
    )


def test_eligible_requires_personal_owner_even_with_members(account, monkeypatch):
    db = account.db
    shared = db.create_workspace("synthetic shared", account.user)
    member = db.create_user("synthetic-member@example.test")
    db.add_member(account.workspace, member, "member", actor=account.user)
    assert import_grant.eligible(db, account.user, shared) is False
    assert import_grant.eligible(db, member, account.workspace) is False
    assert import_grant.eligible(db, account.user, account.workspace) is True
    assert import_grant.eligible(db, account.user, "ws_missing") is False
    assert import_grant.eligible(db, account.user, "synthetic personal") is False

    # Name collisions cannot alter the authenticated ID, including inside the
    # existing OAuth transaction. An eligibility read must not start a tx or
    # use the ID-or-name resolver.
    db.create_workspace(account.workspace, account.user)

    def forbidden(*args, **kwargs):
        raise AssertionError("eligibility used a resolver or nested transaction")

    with db._lock, db._tx():
        with monkeypatch.context() as patch:
            patch.setattr(db, "workspace", forbidden)
            patch.setattr(db, "_tx", forbidden)
            assert import_grant.eligible(db, account.user, account.workspace) is True


def test_disabled_user_is_refused_by_authentication(account):
    connection, token = account.db.create_token(
        workspace_ref=account.workspace,
        user_ref=account.user,
        name="synthetic-disabled",
        client="test",
        actor=account.user,
        policy=agent(account).policy,
    )
    assert account.db.authenticate_token(token) is not None
    with account.db._lock:
        account.db._conn.execute(
            "UPDATE users SET disabled_at=? WHERE id=?", (NOW.isoformat(), account.user)
        )
    assert account.db.authenticate_token(token) is None
    assert account.db.authenticate_connection(connection) is None


def test_person_scopes_and_oauth_defaults_are_exact():
    expected = Policy.build(
        profile="full",
        scopes=[
            "memory:read",
            "memory:write",
            "memory:delete",
            "skills:read",
            "kg:read",
            "kg:write",
            "events:read",
            "events:write",
            "private:read",
            "private:write",
            "threads:read",
            "handoff:write",
        ],
        read_wings=["*"],
        write_wings=["*"],
        trust="trusted",
    )
    assert person_policy().to_json() == expected.to_json()
    assert oauth_store.OAUTH_DEFAULT_SCOPES == ("memory:read", "memory:write")
    assert oauth_store.scope(None) == "memory:read memory:write"
    assert oauth_store.SCOPES == ("memory:read", "memory:write", "memory:import")
    assert recall._GRANT_PREFIX == import_grant.GRANT_PREFIX + ":"


def test_grant_path_rechecks_eligibility_and_companion_scopes(account):
    principal = agent(account)
    assert import_grant.grant_path(account.db, principal) is True
    assert import_grant.grant_path(account.db, replace(principal, person=True)) is False
    for omitted in ("memory:import", "memory:read", "memory:write"):
        policy = Policy.build(
            scopes=[scope for scope in principal.policy.scopes if scope != omitted],
            read_wings=["*"],
            write_wing="inbox",
            trust="untrusted",
        )
        assert import_grant.grant_path(account.db, replace(principal, policy=policy)) is False
    with account.db._lock:
        account.db._conn.execute(
            "UPDATE memberships SET role='member' WHERE workspace_id=? AND user_id=?",
            (account.workspace, account.user),
        )
    assert import_grant.grant_path(account.db, principal) is False


def test_grant_id_and_request_digest_pin_exact_bytes(account):
    assert import_grant.grant_id(account.user, "claude-code", "synthetic") == (
        f"import-private:{account.user}:claude-code:synthetic"
    )
    data = {"threads": [{"text": " café\r\n"}], "source": "synthetic"}
    serialized = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(b"http:/api/import\n" + serialized.encode()).hexdigest()
    assert import_grant.request_digest("http:/api/import", data) == expected
    assert (
        import_grant.request_digest("http:/api/import", dict(reversed(list(data.items()))))
        == expected
    )
    assert import_grant.request_digest("mcp:facts_import", data) != expected
    changed = {"threads": [{"text": " café\n"}], "source": "synthetic"}
    assert import_grant.request_digest("http:/api/import", changed) != expected


def test_cadence_unknown_paid_and_disabled_plans(account, monkeypatch):
    assert ingest_cadence.interval_for(account.db, account.user) == 900
    assert ingest_cadence.interval_for(account.db, "usr_unknown") == 900
    assert ingest_cadence.interval_for(account.db, None) == 900
    plans.set_plan(account.db, account.user, "paid", actor=account.user)
    assert ingest_cadence.interval_for(account.db, account.user) == 0
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    assert ingest_cadence.interval_for(account.db, account.user) == 0
    assert ingest_cadence.interval_for(account.db, "usr_unknown") == 0


def test_cadence_durable_across_control_db_reopen(account):
    assert admit(account) == 0
    # Close the original connection and reopen the same file: no in-memory
    # token/account cache is available to reset the slot.
    account.db.close()
    reopened = ControlDB(account.path)
    try:
        ingest_cadence.ensure_schema(reopened)
        wait = admit(account, db=reopened, connection="conn_other", digest="b" * 64)
        assert wait == 900
        assert admit(account, db=reopened) == 0
        assert admit(account, db=reopened, digest="c" * 64, now=NOW + timedelta(seconds=901)) == 0
    finally:
        reopened.close()


def test_cadence_expired_own_retry_is_pruned_and_no_longer_exempt(account):
    assert ingest_cadence.RETRY_WINDOW_SECONDS == 24 * 3600
    assert admit(account) == 0
    later = NOW + timedelta(seconds=ingest_cadence.RETRY_WINDOW_SECONDS + 1)
    assert admit(account, connection="conn_other", digest="b" * 64, now=later) == 0
    assert admit(account, now=later) == 900
    with account.db._lock:
        rows = account.db._conn.execute(
            "SELECT connection_id,request_sha256 FROM ingest_requests"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("conn_other", "b" * 64)]


def test_cadence_backwards_clock_refuses_with_bounded_wait(account):
    assert admit(account) == 0
    wait = admit(account, digest="b" * 64, now=NOW - timedelta(days=1))
    assert 1 <= wait <= 900
    assert admit(account) == 0  # An exact own retry does not move the clock.


def test_cadence_refuses_nested_control_transaction(account):
    with account.db._lock, account.db._tx():
        with pytest.raises(RuntimeError, match="inside a control transaction"):
            admit(account)
    assert admit(account) == 0


def test_cadence_zero_interval_does_not_open_transaction(account, monkeypatch):
    def forbidden():
        raise AssertionError("zero interval opened a transaction")

    monkeypatch.setattr(account.db, "_tx", forbidden)
    assert (
        ingest_cadence.admit(
            account.db,
            account_id=account.user,
            connection_id="conn_synthetic",
            request_sha256="a" * 64,
            interval=0,
            now=NOW,
        )
        == 0
    )


def test_append_part_default_acl_snapshot(tmp_path):
    """Freeze the existing ACL response, ID and wing, including repeat status."""
    store = ThreadStore(str(tmp_path / "threads.sqlite3"))
    raw = native("synthetic multipart café")
    item = multipart_items([raw], identity="synthetic-default")[0]
    try:
        result = append_part(
            store,
            "ws_synthetic",
            "claude-code",
            item["thread_id"],
            item["format"],
            item["transcript"],
            item["part"],
            actor="conn:synthetic",
            maximum=None,
        )
        stored = store.by_source("ws_synthetic", "import:claude-code:synthetic-default")
        assert stored is not None and stored.wing == "threads"
        expected = {
            "status": "imported",
            "unit": "part",
            "stored_thread_id": stored.id,
            "format": "claude-code",
            "part": item["part"],
            "source_bytes": len(raw.encode()),
            "messages": 1,
            "offset": len(raw.encode()),
            "prefix_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "next_index": 1,
            "session_state": "complete",
            "prefix_work": {"previous_rows": 0, "previous_source_bytes": 0, "readback_rows": 3},
        }
        assert result == expected
        duplicate = append_part(
            store,
            "ws_synthetic",
            "claude-code",
            item["thread_id"],
            item["format"],
            item["transcript"],
            item["part"],
            actor="conn:synthetic",
            maximum=0,
        )
        assert duplicate == {
            **expected,
            "status": "duplicate",
            "prefix_work": {
                "previous_rows": 3,
                "previous_source_bytes": len(raw.encode()),
                "readback_rows": 0,
            },
        }
    finally:
        store.close()


def test_append_part_private_placement_and_duplicate_at_quota(tmp_path):
    store = ThreadStore(str(tmp_path / "threads.sqlite3"))
    parts = multipart_items([native("synthetic first"), native("synthetic next")])
    source_id = "import-private:usr_synthetic:claude-code:multipart"
    try:
        first, second = parts
        options = {"actor": "conn:synthetic", "source_id": source_id, "wing": "private_chats"}
        result = append_part(
            store,
            "ws_synthetic",
            "claude-code",
            first["thread_id"],
            first["format"],
            first["transcript"],
            first["part"],
            maximum=None,
            **options,
        )
        stored = store.by_source("ws_synthetic", source_id)
        assert stored.wing == "private_chats" and result["status"] == "imported"
        assert store.by_source("ws_synthetic", "import:claude-code:multipart") is None
        before = store.messages(stored.id)
        duplicate = append_part(
            store,
            "ws_synthetic",
            "claude-code",
            first["thread_id"],
            first["format"],
            first["transcript"],
            first["part"],
            maximum=0,
            **options,
        )
        assert duplicate["status"] == "duplicate"
        from commonplace.handoff.thread_store import ThreadStorageLimit

        with pytest.raises(ThreadStorageLimit):
            append_part(
                store,
                "ws_synthetic",
                "claude-code",
                second["thread_id"],
                second["format"],
                second["transcript"],
                second["part"],
                maximum=0,
                **options,
            )
        assert store.messages(stored.id) == before
    finally:
        store.close()


def test_grant_batch_serializes_http_and_context_calls(journey, monkeypatch):
    """Observe batch execution, not an invented exposed lock attribute."""
    token = journey.pat()["token"]
    principal = journey.client.gateway.authenticate("Bearer " + token)
    ctx = journey.client.gateway.handoff
    entered, release, direct_attempted, overlap = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    guard = threading.Lock()
    active, maximum = 0, 0
    original = imports.run_batch

    def observed(*args, **kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
            if active > 1:
                overlap.set()
        try:
            entered.set()
            assert release.wait(3), "synthetic batch was not released"
            return original(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    def direct():
        direct_attempted.set()
        return ctx.import_batch(principal, "claude-code", body("direct")["threads"], path="grant")

    monkeypatch.setattr(imports, "run_batch", observed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        http = pool.submit(journey.upload, body("http"), token=token)
        pending = None
        try:
            assert entered.wait(3)
            pending = pool.submit(direct)
            assert direct_attempted.wait(3)
            assert not overlap.wait(0.1), "HTTP and context batches overlapped"
        finally:
            release.set()
        http_report = http.result(timeout=5)
        assert pending is not None
        direct_report = pending.result(timeout=5)
    assert maximum == 1 and active == 0
    assert http_report["accepted"] == direct_report["accepted"] == 1
    assert journey.stored("http") is not None and journey.stored("direct") is not None


def test_grant_admission_finishes_control_transaction_before_store(journey, monkeypatch):
    token = journey.pat()["token"]
    ctx = journey.client.gateway.handoff
    original = ctx.store_for
    visits = []

    def inspected(workspace_id):
        assert not journey.client.db._conn.in_transaction
        visits.append(workspace_id)
        return original(workspace_id)

    monkeypatch.setattr(ctx, "store_for", inspected)
    report = journey.upload(body(), token=token)
    assert report["accepted"] == 1
    assert visits and set(visits) == {journey.owner.workspace}


def test_grant_conflict_forks_with_canonical_sha256(journey):
    token = journey.pat()["token"]
    assert journey.upload(body("fork", "synthetic original"), token=token)["accepted"] == 1
    original = journey.stored("fork")
    before = journey.store().messages(original.id)
    journey.advance()
    assert journey.upload(body("fork", "synthetic changed"), token=token)["accepted"] == 1
    parsed = imports.parse_import("claude-code", native("synthetic changed"), "auto")
    digest = canonical_sha256(parsed)
    fork = journey.store().by_source(
        journey.owner.workspace,
        f"import-private:{journey.owner.user}:claude-code:fork#sha256:{digest}",
    )
    assert fork is not None and fork.id != original.id and fork.wing == "private_chats"
    assert journey.store().messages(original.id) == before
    assert journey.store().messages(fork.id)[0].raw == native("synthetic changed")


def test_operator_shaped_grant_uses_private_path_and_ineligible_refuses(journey):
    """Head-only operator setup: broad ACL scopes must never select ACL first."""
    db, owner = journey.client.db, journey.owner
    policy = Policy.build(
        profile="full",
        scopes=["memory:read", "memory:write", "memory:import", "threads:read"],
        read_wings=["*"],
        write_wings=["*"],
        trust="standard",
    )
    _, token = db.create_token(
        workspace_ref=owner.workspace,
        user_ref=owner.user,
        name="synthetic-operator",
        client="test",
        actor=owner.user,
        policy=policy,
    )
    report = journey.upload(body("operator"), token=token)
    assert report["accepted"] == 1 and "imported" not in report
    assert journey.stored("operator").wing == "private_chats"
    assert journey.store().by_source(owner.workspace, "import:claude-code:operator") is None
    with db._lock:
        db._conn.execute("UPDATE workspaces SET personal_of=NULL WHERE id=?", (owner.workspace,))
    status, _, raw = journey.request("/api/import", "POST", token=token, data=body("refused"))
    assert status == 403
    assert json.loads(raw)["error"] == import_grant.INELIGIBLE_TEXT
    assert journey.store().by_source(owner.workspace, "import:claude-code:refused") is None
    assert journey.stored("refused") is None


def test_operator_import_without_companion_scope_never_falls_back_to_acl(journey):
    db, owner = journey.client.db, journey.owner
    for index, missing in enumerate(("memory:read", "memory:write")):
        policy = Policy.build(
            profile="full",
            scopes=[
                scope
                for scope in ("memory:read", "memory:write", "memory:import", "threads:read")
                if scope != missing
            ],
            read_wings=["*"],
            write_wings=["*"],
            trust="standard",
        )
        _, token = db.create_token(
            workspace_ref=owner.workspace,
            user_ref=owner.user,
            name=f"synthetic-missing-{index}",
            client="test",
            actor=owner.user,
            policy=policy,
        )
        status, _, raw = journey.request("/api/import", "POST", token=token, data=body())
        assert status == 403
        assert json.loads(raw)["error"] == import_grant.INELIGIBLE_TEXT


def test_no_plan_grant_at_real_quota_returns_item_error(journey, monkeypatch):
    token = journey.pat()["token"]
    owner, db = journey.owner, journey.client.db
    plans.set_plan(db, owner.user, "none", actor=owner.user)
    assert plans.get_plan(db, owner.user) is None
    small = replace(plans.FREE, storage_bytes=1024 * 1024)
    monkeypatch.setattr(plans, "FREE", small)
    monkeypatch.setitem(plans.PLANS, "free", small)
    # A real regular synthetic file is counted by the account meter; no
    # always-refuse budget stub and no fake storage exception.
    directory = journey.client.gateway.workspace_dir(owner.workspace)
    (Path(directory) / "synthetic-quota-padding.bin").write_bytes(b"x" * small.storage_bytes)
    report = journey.upload(body("quota"), token=token)
    assert report["errors"] == 1 and report["accepted"] == 0
    row = report["results"][0]
    assert row["code"] == "storage_limit"
    assert row["error"] == plans.storage_message(small)


def test_cadence_waits_for_other_thread_control_transaction(account):
    held, release, started = threading.Event(), threading.Event(), threading.Event()
    results, errors = [], []

    def hold():
        with account.db._lock, account.db._tx():
            held.set()
            assert release.wait(5)

    def request():
        started.set()
        try:
            results.append(admit(account))
        except Exception as error:
            errors.append(error)

    holder = threading.Thread(target=hold)
    requester = threading.Thread(target=request)
    holder.start()
    try:
        assert held.wait(2)
        requester.start()
        assert started.wait(2)
        requester.join(1)
        assert requester.is_alive(), f"admission finished while locked: {errors}"
    finally:
        release.set()
        holder.join(2)
        if requester.ident is not None:
            requester.join(2)
    assert not holder.is_alive() and not requester.is_alive()
    assert errors == []
    assert results == [0]


def test_ineligible_owner_cannot_create_import_connection(journey):
    # Direct session setup is intentional: exercise a shared-workspace owner
    # through the real HTTP handler without changing the personal journey.
    db = journey.client.db
    workspace = db.create_workspace("synthetic shared", journey.owner.user)
    cookie = sessions.start_session(db, journey.owner.user, workspace)
    owner = SimpleNamespace(cookie=f"{sessions.COOKIE}={cookie}")
    before = db.connections(workspace)
    status, _, raw = journey.request(
        "/api/connections",
        "POST",
        owner=owner,
        data={"name": "synthetic denied", "import": True},
    )
    assert status == 403
    assert json.loads(raw) == {"error": import_grant.OWNER_ONLY_TEXT}
    assert db.connections(workspace) == before
