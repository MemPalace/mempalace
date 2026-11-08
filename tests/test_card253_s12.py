"""Authenticated progress uses current read authority, including private captures."""

import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from commonplace import plans, sessions
from commonplace.control_db import ControlDB
from commonplace.gateway import Gateway
from commonplace.handoff import search_index
from commonplace.handoff.models import ThreadMessage
from commonplace.handoff.thread_store import ThreadStore, ThreadStoreV2
from commonplace.workers import WorkerSupervisor
from tests.test_commonplace_email_auth import Client


@pytest.fixture(params=[ThreadStore, ThreadStoreV2], ids=["v1", "v2"])
def worlds(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    monkeypatch.delenv("COMMONPLACE_SEARCH_INDEX", raising=False)
    monkeypatch.setattr(search_index, "PAUSE_SECONDS", 0)
    opened = []

    def create(label):
        root = tmp_path / label
        root.mkdir()
        db = ControlDB(str(root / "control.sqlite3"))
        identities = iter(("usr_0000000000000001", "usr_0000000000000002", "ws_0000000000000001"))
        with patch("commonplace.control_db._new_id", side_effect=lambda _: next(identities)):
            owner = db.create_user("owner@example.test")
            member = db.create_user("member@example.test")
            ws = db.create_workspace("synthetic", owner)
        db.add_member(ws, member, "member", actor=owner)
        supervisor = WorkerSupervisor(str(root))
        gateway = Gateway(db, supervisor)
        ctx = gateway.handoff
        store = request.param(str(root / "threads.v2.sqlite3"))
        ctx._stores[ws] = store
        server = gateway.build_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        client = Client(gateway, server.server_address[1])
        cookie = sessions.COOKIE + "=" + sessions.start_session(db, owner, ws)
        principal = sessions.person_principal(db, owner, ws)
        assert principal.person and principal.role == "owner"
        assert principal.policy.trust == "trusted"
        assert principal.policy.has_scope("threads:read")
        w = SimpleNamespace(
            db=db,
            owner=owner,
            member=member,
            ws=ws,
            gateway=gateway,
            service=ctx.service,
            store=store,
            client=client,
            cookie=cookie,
            principal=principal,
            writes=[],
            release=threading.Event(),
        )

        def worker(principal, name, args):
            if name == "mempalace_list_drawers":
                return {"drawers": [], "total": 0}
            assert name == "write_mirror_units"
            w.writes.append((args["thread_id"], len(args["units"])))
            return {"drawer_ids": [f"unit-{len(w.writes)}-{i}" for i in range(len(args["units"]))]}

        w.worker = worker
        w.service.call_tool = worker
        opened.append((w, server, thread, supervisor))
        return w

    yield create
    for w, server, thread, supervisor in opened:
        w.release.set()
        w.service.wait_for_mirrors(10)
        server.shutdown()
        thread.join(3)
        server.server_close()
        w.gateway.webhooks.stop()
        w.gateway.handoff.close()
        supervisor.stop_all()
        w.db.close()


def put(w, label, *, hidden=False, count=1, indexed=False, identity=None):
    actor = w.member if hidden else w.owner
    source = f"capture:{actor}:{label}"
    identity = identity or hashlib.sha256(source.encode()).hexdigest()[:16]
    with patch("commonplace.handoff.thread_store.secrets.token_hex", return_value=identity):
        stored = w.store.store(
            w.ws,
            source,
            [
                ThreadMessage(i, "user", "message", "x" * 8000 if count > 1 else label, "{}")
                for i in range(count)
            ],
            actor=actor,
            wing="private_chats",
        )
    with w.store._lock, w.store._conn:
        w.store._conn.execute(
            "UPDATE threads SET created_at=? WHERE id=?", ("2026-11-01T00:00:00+00:00", stored.id)
        )
    stored = w.store.get(stored.id)
    w.store.begin_mirror(stored.id, -1)
    if indexed:
        through = stored.message_count - 1
        w.store.begin_mirror(stored.id, through)
        w.store.finish_mirror(stored.id, through)
    assert w.gateway.handoff.recall.can_read(w.principal, stored.id) is (not hidden)
    return stored


def poll(w):
    status, headers, raw = w.client.request("/api/search-index", cookie=w.cookie)
    assert status == 200, raw
    assert headers["Cache-Control"] == "no-store"
    result = json.loads(raw)
    assert set(result) == {"state", "threads_indexed", "threads_pending", "units_written"}
    return result


@pytest.mark.parametrize("kind", ["indexed", "pending", "units", "running", "deferred"])
def test_card253_s12_progress(worlds, monkeypatch, kind):
    control, hidden = worlds("control"), worlds("hidden")
    for w in (control, hidden):
        visible = put(w, "visible", indexed=kind not in ("units",))
        if kind == "units":
            w.service.index_later(w.ws)
            w.service.wait_for_mirrors(10)
            assert w.writes == [(visible.id, 1)]
    assert control.store.get(visible.id) == hidden.store.get(visible.id)
    h = put(
        hidden,
        "hidden",
        hidden=True,
        indexed=kind == "indexed",
        count=40 if kind in ("running", "deferred") else 1,
    )
    if kind in ("units", "deferred"):
        if kind == "deferred":
            monkeypatch.setenv("COMMONPLACE_PLANS", "on")
            monkeypatch.setattr(plans, "workspace_account", lambda *_: hidden.owner)
            monkeypatch.setattr(plans, "get_plan", lambda *_: plans.FREE)
            monkeypatch.setattr(
                plans.StorageMeter,
                "_walk",
                lambda _: plans.FREE.storage_bytes if hidden.writes else 0,
            )
        hidden.service.index_later(hidden.ws)
        hidden.service.wait_for_mirrors(10)
        assert hidden.writes[-1:] == [(h.id, 64 if kind == "deferred" else 1)]
        if kind == "deferred":
            assert hidden.store.mirror_progress(h.id)[0] == -1
    if kind == "running":
        entered = threading.Event()

        def held(principal, name, args):
            if name == "write_mirror_units":
                entered.set()
                assert hidden.release.wait(10)
            return hidden.worker(principal, name, args)

        hidden.service.call_tool = held
        hidden.service.index_later(hidden.ws)
        assert entered.wait(5), "scheduler must reach real hidden write"
    try:
        actual, expected = poll(hidden), poll(control)
        if kind in ("running", "deferred"):
            assert actual["state"] == expected["state"], "s12 hidden work changed owner state"
        elif kind == "units":
            assert actual["units_written"] == expected["units_written"], (
                "s12 owner progress exposed H units"
            )
        assert actual == expected, "s12 owner progress counted H"
    finally:
        hidden.release.set()


def test_card253_s12_all_readable_and_reauthorize_units(worlds):
    w = worlds("readable")
    own = put(w, "own")
    other = w.store.store(
        w.ws, "native:member", [ThreadMessage(0, "user", "message", "shared", "{}")], actor=w.member
    )
    w.store.begin_mirror(other.id, -1)
    assert w.gateway.handoff.recall.can_read(w.principal, other.id)
    w.service.index_later(w.ws)
    w.service.wait_for_mirrors(10)
    assert poll(w) == dict(state="idle", threads_indexed=2, threads_pending=0, units_written=2)
    with w.store._lock, w.store._conn:
        w.store._conn.execute(
            "UPDATE threads SET source_thread_id=? WHERE id=?",
            (f"capture:{w.member}:revoked", own.id),
        )
    assert not w.gateway.handoff.recall.can_read(w.principal, own.id)
    assert poll(w) == dict(state="idle", threads_indexed=1, threads_pending=0, units_written=1)
    w.service._index_states[w.ws] = search_index.State()
    assert poll(w)["units_written"] == 0
    # Unattributed historical totals are never recovered into an authenticated poll.
    w.service._index_states[w.ws].units_written = 999
    assert poll(w)["units_written"] == 0


def test_card253_s12_readable_running_and_precedence(worlds, monkeypatch):
    w = worlds("running")
    stored = put(w, "active")
    entered = threading.Event()

    def held(principal, name, args):
        if name == "write_mirror_units":
            entered.set()
            assert w.release.wait(10)
        return w.worker(principal, name, args)

    w.service.call_tool = held
    w.service.index_later(w.ws)
    assert entered.wait(5)
    try:
        assert poll(w)["state"] == "running"
        # A prior real quota refusal outranks another readable active thread.
        blocked = put(w, "blocked")
        with w.service._mirror_lock:
            w.service._index_states[w.ws].blocked_threads.add(blocked.id)
        assert poll(w)["state"] == "deferred_storage"
        monkeypatch.setenv("COMMONPLACE_SEARCH_INDEX", "off")
        assert poll(w)["state"] == "off"
        # Completed work cannot retain a public quota-blocked label.
        with w.service._mirror_lock:
            w.service._index_states[w.ws].blocked_threads.discard(blocked.id)
    finally:
        w.release.set()
    w.service.wait_for_mirrors(10)
    assert stored.id not in w.service._index_states[w.ws].active_threads


def test_card253_s12_global_quota_causality(worlds, monkeypatch):
    control, hidden = worlds("quota-control"), worlds("quota-hidden")
    h = put(hidden, "hidden-storage", hidden=True, identity="0000000000000001")
    readable = put(hidden, "readable-pending", identity="ffffffffffffffff")
    put(control, "readable-pending", identity="ffffffffffffffff")
    attempts = []
    original_mirror = search_index.palace_mirror.mirror_pending

    def mirror(call, principal, stored, *args, **kwargs):
        attempts.append((principal.workspace_id, stored.id))
        return original_mirror(call, principal, stored, *args, **kwargs)

    monkeypatch.setattr(search_index.palace_mirror, "mirror_pending", mirror)
    assert h.id < readable.id
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    monkeypatch.setattr(plans, "workspace_account", lambda *_: hidden.owner)
    monkeypatch.setattr(plans, "get_plan", lambda *_: plans.FREE)
    monkeypatch.setattr(
        plans.StorageMeter, "_walk", lambda _: plans.FREE.storage_bytes if hidden.writes else 0
    )
    control.service.index_later(control.ws)
    control.service.wait_for_mirrors(10)
    hidden.service.index_later(hidden.ws)
    hidden.service.wait_for_mirrors(10)
    assert hidden.writes == [(h.id, 1)]
    assert hidden.store.mirror_progress(h.id) == (0, 0)
    assert attempts[-2:] == [(hidden.ws, h.id), (hidden.ws, readable.id)]
    assert readable.id in hidden.service._index_states[hidden.ws].blocked_threads
    assert poll(control)["state"] == "idle"
    assert poll(hidden) == dict(
        state="deferred_storage", threads_indexed=0, threads_pending=1, units_written=0
    )
    monkeypatch.setattr(plans.StorageMeter, "_walk", lambda _: 0)
    hidden.service.index_later(hidden.ws)
    hidden.service.wait_for_mirrors(10)
    assert poll(hidden) == dict(state="idle", threads_indexed=1, threads_pending=0, units_written=1)


def test_card253_s12_completed_block_no_longer_deferred(worlds):
    w = worlds("completed")
    stored = put(w, "done", indexed=True)
    state = search_index.State()
    state.blocked_threads.add(stored.id)
    w.service._index_states[w.ws] = state
    assert poll(w)["state"] == "idle"


@pytest.mark.parametrize(
    "fault", ["page", "final-page", "reader", "deadline", "missing-reader", "wrong-workspace"]
)
def test_card253_s12_incomplete_fixed_unavailable(worlds, monkeypatch, fault):
    replies = []
    for label in ("absent", "present"):
        w = worlds(label)
        put(w, "visible", indexed=True)
        if label == "present":
            put(w, "hidden", hidden=True)
        monkeypatch.setattr(w.store, "index_counts", lambda *_: pytest.fail("no global fallback"))
        if fault in ("page", "final-page"):
            original = w.store.index_descriptors
            calls = []

            def broken(*args, _original=original, _calls=calls, **kwargs):
                _calls.append(1)
                if len(_calls) == (2 if fault == "page" else 4):
                    raise RuntimeError("private failure details")
                return _original(*args, **kwargs)

            monkeypatch.setattr(w.store, "index_descriptors", broken)
        elif fault == "reader":
            monkeypatch.setattr(w.service, "progress_can_read", lambda *_: 1 / 0)
        elif fault == "missing-reader":
            monkeypatch.setattr(w.service, "progress_can_read", None)
        elif fault == "wrong-workspace":
            original = w.service.index_status
            monkeypatch.setattr(
                w.service,
                "index_status",
                lambda ws, *, principal, _original=original: _original(
                    "other", principal=principal
                ),
            )
        else:
            monkeypatch.setattr(search_index, "PROGRESS_TIMEOUT", 0)
        status, headers, raw = w.client.request("/api/search-index", cookie=w.cookie)
        replies.append((status, json.loads(raw)))
        assert headers["Cache-Control"] == "no-store"
    assert replies == [(503, {"error": "Progress unavailable"})] * 2


@pytest.mark.parametrize("fault", ["store", "private-reader"])
def test_card253_s12_authority_fault_fixed_unavailable(worlds, monkeypatch, fault):
    replies = []
    for label in ("absent", "present"):
        w = worlds(label)
        put(w, "healthy", indexed=True)
        failing = put(w, "faulted", indexed=True)
        if label == "present":
            put(w, "hidden", hidden=True)
        assert poll(w)["threads_indexed"] == 2
        recall = w.gateway.handoff.recall
        target = w.store if fault == "store" else recall._private
        method = "get" if fault == "store" else "readable"
        original = getattr(target, method)
        calls = []

        def broken(*args, _original=original, _id=failing.id, _calls=calls, **kwargs):
            stored_id = args[0] if fault == "store" else args[1].id
            if stored_id == _id:
                _calls.append(stored_id)
                raise sqlite3.OperationalError("private authority failure details")
            return _original(*args, **kwargs)

        monkeypatch.setattr(target, method, broken)
        # Ordinary consumers still get the established deny-on-fault behavior.
        assert recall.can_read(w.principal, failing.id) is False
        calls.clear()
        monkeypatch.setattr(w.store, "index_counts", lambda *_: pytest.fail("no global fallback"))
        status, headers, raw = w.client.request("/api/search-index", cookie=w.cookie)
        assert calls == [failing.id], "owner poll must reach the real authority fault"
        assert headers["Cache-Control"] == "no-store"
        replies.append((status, json.loads(raw)))
    assert replies == [(503, {"error": "Progress unavailable"})] * 2


def test_card253_s12_final_snapshot_excludes_changed_descriptor(worlds, monkeypatch):
    w = worlds("changed")
    stored = put(w, "visible", indexed=True)
    state = search_index.State()
    state.units_by_thread[stored.id] = 5
    w.service._index_states[w.ws] = state
    original = w.service.progress_can_read

    def change(principal, stored_id):
        allowed = original(principal, stored_id)
        assert allowed
        w.store._conn.execute(
            "UPDATE threads SET source_thread_id=? WHERE id=?",
            (f"capture:{w.member}:now-hidden", stored_id),
        )
        return allowed

    monkeypatch.setattr(w.service, "progress_can_read", change)
    assert poll(w) == dict(state="idle", threads_indexed=0, threads_pending=0, units_written=0)


def test_card253_s12_store_lock_deadline(worlds, monkeypatch):
    w = worlds("locked")
    put(w, "visible")
    monkeypatch.setattr(search_index, "PROGRESS_TIMEOUT", 0.03)
    entered = threading.Event()

    def hold():
        with w.store._lock:
            entered.set()
            assert w.release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(3)
    try:
        status, _, raw = w.client.request("/api/search-index", cookie=w.cookie)
        assert (status, json.loads(raw)) == (503, {"error": "Progress unavailable"})
    finally:
        w.release.set()
        holder.join(3)
    assert not holder.is_alive()


def test_card253_s12_http_auth(worlds):
    w = worlds("auth")
    assert w.client.request("/api/search-index")[0] == 401
    cookie = sessions.COOKIE + "=" + sessions.start_session(w.db, w.member, w.ws)
    assert w.client.request("/api/search-index", cookie=cookie)[0] == 403


def test_card253_s12_failed_writes_do_not_accrue(worlds):
    w = worlds("failed")
    put(w, "write")
    w.service.call_tool = lambda *_: {"error": "refused", "drawer_ids": ["invalid"]}
    w.service.index_later(w.ws)
    w.service.wait_for_mirrors(10)
    assert poll(w) == dict(state="idle", threads_indexed=0, threads_pending=1, units_written=0)


def test_card253_s12_bounded_metadata_polling(worlds, monkeypatch, record_property):
    import statistics
    from time import perf_counter

    w = worlds("polling")
    for i in range(501):
        put(w, f"pending-{i}")
    calls, pages = [], []
    original_reader = w.service.can_read
    original_page = w.store.index_descriptors

    def reader(*args):
        calls.append(args[1])
        return original_reader(*args)

    def page(*args, **kwargs):
        assert 0 < kwargs["limit"] <= 500
        result = original_page(*args, **kwargs)
        pages.append(len(result))
        return result

    def no_words(*args, **kwargs):
        pytest.fail("progress must never load message or chunk content")

    monkeypatch.setattr(w.service, "progress_can_read", reader)
    monkeypatch.setattr(w.store, "index_descriptors", page)
    monkeypatch.setattr(w.store, "messages", no_words)
    monkeypatch.setattr(w.store, "chunks", no_words)
    timings = {}
    for mode in ("cold-sqlite-cache", "warm"):
        durations = []
        for _ in range(20):
            if mode == "cold-sqlite-cache":
                w.store._conn.execute("PRAGMA shrink_memory")
                w.db._conn.execute("PRAGMA shrink_memory")
                w.gateway.handoff.recall._private._cache.clear()
            calls.clear()
            pages.clear()
            start = perf_counter()
            assert poll(w)["threads_pending"] == 501
            durations.append((perf_counter() - start) * 1000)
            assert len(calls) == len(set(calls)) == 501
            assert pages == [500, 1, 0, 500, 1, 0]
        timings[mode] = dict(p50_ms=statistics.median(durations), p95_ms=sorted(durations)[18])
    record_property(
        "polling",
        json.dumps(
            dict(
                threads=501,
                timings=timings,
                calls_per_poll=501,
                pages_per_poll=6,
                deadline_s=search_index.PROGRESS_TIMEOUT,
            )
        ),
    )
