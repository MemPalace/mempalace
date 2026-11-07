"""Barrier-controlled cross-path admissions against independent SQLite connections."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from commonplace.projects import limits
from commonplace.runner import jobs, tools
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._phase_b_helpers import World, call, cap, claim, seed


@pytest.fixture
def world(hub, monkeypatch):
    from commonplace.boards import service

    w = World(hub)
    # Each operation still opens an independent connection. Avoid schema migration
    # write locks hiding a missing admission lock in a contender's constructor.
    monkeypatch.setattr(service, "open_store", lambda *args, **kwargs: w.boards.store)
    return w


def race(world, monkeypatch, module, name, first, second, *, outside=False):
    """Pause after the first path's in-lock operation, start its contender, resume."""
    paused, entered, finished = Event(), Event(), Event()
    original = getattr(module, name)
    once = False
    lock_held = False

    def wrap(*args, **kwargs):
        nonlocal once, lock_held
        result = original(*args, **kwargs)
        if not once:
            once = True
            from mempalace.sqlite_crypto import connect

            probe = connect(world.js.path, timeout=0, isolation_level=None)
            try:
                try:
                    probe.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc):
                        raise
                    lock_held = True
            finally:
                probe.rollback()
                probe.close()
            paused.set()
            if not (finished if outside else entered).wait(10):
                raise RuntimeError("contender did not enter")
        return result

    monkeypatch.setattr(module, name, wrap)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first)
        if not paused.wait(10):
            raise RuntimeError("first path did not enter")

        def contend():
            entered.set()
            try:
                return second()
            finally:
                finished.set()

        b = pool.submit(contend)
        results = a.result(timeout=15), b.result(timeout=15)
    if not outside:
        assert lock_held, "admission lock must be held until the operation commits"
    return results


def assert_one(results):
    assert sum(status in (200, 201) for status, _ in results) == 1 and "project_limit" in str(
        results
    )  # RED


def test_r22_claim_then_start(world, monkeypatch):
    w = world
    seed(w)
    cap(w, 1)
    a, b = w.card(), w.card()
    assert_one(
        race(w, monkeypatch, limits, "check", lambda: claim(w, a), lambda: call(w.person_start, b))
    )


def test_r23_start_then_claim(world, monkeypatch):
    w = world
    seed(w)
    cap(w, 1)
    a, b = w.card(), w.card()
    assert_one(
        race(
            w, monkeypatch, jobs, "create_in", lambda: call(w.person_start, a), lambda: claim(w, b)
        )
    )


def test_r24_lease_then_claim(world, monkeypatch):
    w = world
    seed(w)
    a, b = w.card(), w.card()
    w.person_start(a)
    cap(w, 1)
    results = race(
        w,
        monkeypatch,
        jobs,
        "_update",
        lambda: call(jobs.lease, w.js, w.machine()),
        lambda: claim(w, b),
    )
    assert_one(results)
    assert results[0][0] == 200 and results[0][1]["state"] == "leased"
    assert w.all_jobs()[0]["state"] == "leased"


def test_r25_configured_start_then_claim(world, monkeypatch):
    from commonplace.orch import runner_bridge

    w = world
    seed(w)
    cap(w, 1)
    a, b = w.card(), w.card()
    w.configure(max_agents=1)
    assert_one(
        race(
            w,
            monkeypatch,
            runner_bridge,
            "_fenced",
            lambda: call(w.person_start, a),
            lambda: claim(w, b),
        )
    )


def test_r26_two_stores_start(world, monkeypatch):
    w = world
    seed(w)
    cap(w, 1)
    a, b = w.card(), w.card()
    other_store = jobs.open_store(w.gw, w.ws)
    original = jobs.open_store
    monkeypatch.setattr(
        jobs,
        "open_store",
        lambda gateway, ws: other_store if gateway is w.gw else original(gateway, ws),
    )
    assert_one(
        race(
            w,
            monkeypatch,
            jobs,
            "create_in",
            lambda: call(w.person_start, a),
            lambda: call(w.person_start, b),
        )
    )


def test_r26b_claim_before_enrollment(world, monkeypatch):
    w = world
    seed(w)
    cap(w, 1)
    # Remove the enrolled machine rows. A claim must still take the runner lock.
    with w.db._lock:
        w.db._conn.execute("DELETE FROM runner_machines")
    a, b = w.card(), w.card()

    def enroll_start():
        from tests._phase_b_helpers import machine_two

        m = machine_two(w)
        return call(tools.card_start, w.ctx(w.owner), b, m["id"], "demo", "worker")

    assert_one(race(w, monkeypatch, limits, "check", lambda: claim(w, a), enroll_start))


def test_r8c_lease_claim_race(world, monkeypatch):
    w = world
    seed(w)
    a, b = w.card(), w.card()
    w.person_start(a)
    cap(w, 1)
    results = race(
        w,
        monkeypatch,
        jobs,
        "_update",
        lambda: call(jobs.lease, w.js, w.machine()),
        lambda: claim(w, b),
    )
    assert_one(results)
    assert results[0][0] == 200 and results[0][1]["state"] == "leased"
    assert w.all_jobs()[0]["state"] == "leased"


@pytest.mark.parametrize("change", ["demote", "off", "revoke"])
def test_starting_rechecks_authority_in_transaction(world, monkeypatch, change):
    from commonplace.projects import connect, orchestration
    from commonplace.runner import registry

    w = world
    seed(w)
    _, connected = connect.connect(w.gw, w.owner, "acme", allow_shared=True)
    seat = w.gw.authenticate("Bearer " + connected["token"])
    connect.trust(w.gw, w.owner, "acme", {"confirm": True, "machines": [w.machine_id]})
    card = w.card()
    tools.card_start(w.ctx(seat), card, w.machine_id, "demo", "worker")
    payload = w.lease()
    with w.boards.store.connection() as conn:
        before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (card,)).fetchone())

    def change_authority():
        if change == "demote":
            connect.trust(w.gw, w.owner, "acme", {"trusted": False})
        elif change == "off":
            orchestration.set_enabled(w.db, w.owner, False)
        else:
            registry.revoke(w.db, w.owner, w.machine_id)

    result, _ = race(
        w,
        monkeypatch,
        jobs,
        "_sweep",
        lambda: w.machine_call("starting", {"lease_token": payload["lease_token"]}),
        change_authority,
        outside=True,
    )
    expected = "machine_disabled" if change == "revoke" else "requester_invalid"
    assert result == (409, {"error": expected})
    job = w.all_jobs()[0]
    assert job["state"] == "cancelled" and job["outcome_code"] == expected
    assert job["starting_at"] is None and job["thread_id"] is None
    with w.boards.store.connection() as conn:
        assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (card,)).fetchone()) == before
    assert w.db.authenticate_connection(seat.connection_id).policy.trust == (
        "trusted" if change == "revoke" else "standard"
    )
    assert bool(w.machine()["revoked_at"]) == (change == "revoke")


@pytest.mark.parametrize("constructor", ["service", "store"])
def test_starting_never_constructs_boards_in_runner_transaction(hub, monkeypatch, constructor):
    from contextlib import contextmanager
    from threading import local

    from commonplace.boards.service import BoardService
    from commonplace.boards.store import BoardStore

    w = World(hub)
    seed(w)
    card = w.card()
    w.person_start(card)
    payload = w.lease()
    state = local()
    violations = []
    transaction = jobs.JobStore.transaction
    target = BoardService if constructor == "service" else BoardStore
    initialize = target.__init__

    @contextmanager
    def tracked(store):
        with transaction(store) as conn:
            state.in_runner_transaction = True
            try:
                yield conn
            finally:
                state.in_runner_transaction = False

    def guarded(self, *args, **kwargs):
        if getattr(state, "in_runner_transaction", False):
            violations.append(constructor)
            raise AssertionError("board construction inside runner transaction")
        initialize(self, *args, **kwargs)

    monkeypatch.setattr(jobs.JobStore, "transaction", tracked)
    monkeypatch.setattr(target, "__init__", guarded)
    result = w.machine_call("starting", {"lease_token": payload["lease_token"]})
    assert result[0] == 200 and violations == [], (result, violations)
    assert w.all_jobs()[0]["state"] == "starting"


def test_claim_during_start_authority_check_is_fast(hub, monkeypatch):
    from time import monotonic

    from commonplace.projects import connect

    # Use real constructors: the shared world fixture's open_store patch would
    # hide BoardStore's schema write lock and the reverse lock order.
    w = World(hub)
    seed(w)
    _, connected = connect.connect(w.gw, w.owner, "acme", allow_shared=True)
    seat = w.gw.authenticate("Bearer " + connected["token"])
    connect.trust(w.gw, w.owner, "acme", {"confirm": True, "machines": [w.machine_id]})
    started = w.card()
    tools.card_start(w.ctx(seat), started, w.machine_id, "demo", "worker")
    payload = w.lease()
    claimed = w.card()
    paused, in_claim, release = Event(), Event(), Event()
    fenced, check = jobs._fenced, limits.check

    def pause(conn, *args, **kwargs):
        row = fenced(conn, *args, **kwargs)
        if conn.in_transaction and not paused.is_set():
            paused.set()
            if not release.wait(10):
                raise RuntimeError("claim did not reach its admission check")
        return row

    def entering(*args, **kwargs):
        if kwargs.get("card_id") == claimed:
            in_claim.set()
        return check(*args, **kwargs)

    def claiming():
        began = monotonic()
        result = claim(w, claimed)
        return result, monotonic() - began

    monkeypatch.setattr(jobs, "_fenced", pause)
    monkeypatch.setattr(limits, "check", entering)
    with ThreadPoolExecutor(max_workers=2) as pool:
        start = pool.submit(w.machine_call, "starting", {"lease_token": payload["lease_token"]})
        try:
            assert paused.wait(10), "start did not reach its transaction"
            contender = pool.submit(claiming)
            assert in_claim.wait(10), "claim did not reach admission"
        finally:
            release.set()
        result, elapsed = contender.result(timeout=15)
        start_result = start.result(timeout=15)
    assert result[0] == 200 and elapsed < 2, (result, elapsed)
    assert start_result[0] == 200, start_result
    assert w.jobs_for(started)[0]["state"] == "starting"
    with w.boards.store.connection() as conn:
        row = conn.execute("SELECT status,assignee FROM tasks WHERE id=?", (claimed,)).fetchone()
        assert tuple(row) == ("doing", w.a.actor)
