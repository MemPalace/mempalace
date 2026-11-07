"""Epoch reset regressions; identical source is exercised on the pinned base."""

import json
from contextlib import contextmanager
from threading import Event, Thread, get_ident

import pytest

from commonplace.runner import jobs, registry, tools
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._phase_b_helpers import World, seed
from tests.runner_harness import http


@pytest.fixture
def world(hub):
    return World(hub)


def epoch(w):
    with w.db._lock:
        columns = [r[1] for r in w.db._conn.execute("PRAGMA table_info(runner_machines)")]
    assert "binding_epoch" in columns
    return w.machine()["binding_epoch"]


def confirm(w, provider="worker"):
    m = w.machine()
    return registry.attest(
        w.db,
        w.owner,
        w.machine_id,
        {
            "topics": json.loads(m["topics"]),
            "provider_instance_id": provider,
            "worker_connection_id": m["worker_connection_id"],
        },
    )


def reset(w):
    topics = json.loads(w.machine()["topics"])
    registry.patch(
        w.db, w.owner, w.machine_id, {"topics": ["other"] if topics == ["worker"] else ["worker"]}
    )


def create(w, machine=None):
    task = w.card(board="plain")
    return jobs.create(
        w.js,
        task_number=task,
        board_slug="plain",
        machine=dict(machine or w.machine()),
        project="demo",
        topic="worker",
        requested_by="user:" + w.owner.user_id,
        person=True,
    )[1]


def stored(w, row):
    return w.rows("SELECT * FROM runner_jobs WHERE id=?", (row["id"],))[0]


def start(w, leased):
    return http(
        w.hub.port,
        "POST",
        "/api/runner/starting",
        token=w.machine_token,
        body={"lease_token": leased["lease_token"]},
    )


def test_topic_reset(world):
    w = world
    before = epoch(w)
    row = create(w)
    reset(w)
    assert epoch(w) == before + 1
    assert w.machine()["binding_state"] == "unbound"
    confirm(w)
    jobs.sweep(w.js, w.db, w.ws)
    assert stored(w, row)["outcome_code"] == "binding_epoch_changed"


def test_wing_reset(world):
    w = world
    before = epoch(w)
    registry.patch(w.db, w.owner, w.machine_id, {"wings": ["shared", "shared/extra"]})
    assert epoch(w) == before + 1
    assert w.machine()["binding_state"] == "unbound"
    worker = w.db.connection(w.machine()["worker_connection_id"], w.ws)
    assert "shared_extra" in json.loads(worker["policy"])["read_wings"]


def test_worker_rotation(world, monkeypatch):
    w = world
    before = epoch(w)
    row = create(w)
    leased = jobs.lease(w.js, w.machine())
    assert leased is not None
    registry.rotate(w.db, w.owner, w.machine_id)
    assert epoch(w) == before + 1
    monkeypatch.setattr(jobs, "_sweep", lambda store: None)
    assert start(w, leased)[0] == 409
    assert stored(w, row)["state"] == "cancelled"
    assert stored(w, row)["starting_at"] is None


def test_revoke(world):
    w = world
    before = epoch(w)
    row = create(w)
    registry.revoke(w.db, w.owner, w.machine_id)
    assert epoch(w) == before + 1
    jobs.sweep(w.js, w.db, w.ws)
    assert stored(w, row)["outcome_code"] == "machine_revoked"
    assert stored(w, row)["starting_at"] is None


def test_projects_only_and_confirm_preserve_epoch(world):
    w = world
    before = epoch(w)
    row = create(w)
    registry.patch(w.db, w.owner, w.machine_id, {"projects": ["demo", "extra"]})
    confirm(w)
    assert epoch(w) == before
    assert stored(w, row)["binding_epoch"] == before
    assert "binding_epoch" not in jobs.CONFIG_COLUMNS


def test_reset_between_validation_and_insert(world):
    w = world
    before = epoch(w)
    snapshot = dict(w.machine())
    reset(w)
    confirm(w)
    row = create(w, snapshot)
    assert row["binding_epoch"] == before
    assert epoch(w) == before + 1
    jobs.sweep(w.js, w.db, w.ws)
    assert stored(w, row)["state"] == "cancelled"
    assert stored(w, row)["outcome_code"] == "binding_epoch_changed"
    assert jobs.lease(w.js, w.machine()) is None


def test_cancelled_job_never_revives(world):
    w = world
    epoch(w)
    row = create(w)
    for _ in range(2):
        reset(w)
        confirm(w)
        jobs.sweep(w.js, w.db, w.ws)
        assert jobs.lease(w.js, w.machine()) is None
        assert stored(w, row)["state"] == "cancelled"
    assert json.loads(w.machine()["topics"]) == ["worker"]


def test_card_d1_stale_starting_retry(world, monkeypatch):
    w = world
    epoch(w)
    row = create(w)
    leased = jobs.lease(w.js, w.machine())
    assert start(w, leased)[0] == 200
    monkeypatch.setattr(jobs, "_sweep", lambda store: None)
    reset(w)
    confirm(w)
    status, body = start(w, leased)
    assert status == 409
    assert body["error"] == "binding_epoch_changed"
    assert stored(w, row)["state"] == "starting"
    jobs.sweep(w.js, w.db, w.ws)
    assert stored(w, row)["state"] == "uncertain"
    assert jobs.lease(w.js, w.machine()) is None


class TrackingLock:
    def __init__(self, lock):
        self.lock = lock
        self.owners = {}
        self.attempt = None

    def owned(self):
        return self.owners.get(get_ident(), 0) > 0

    def acquire(self, *args, **kwargs):
        if self.attempt:
            self.attempt(self)
        acquired = self.lock.acquire(*args, **kwargs)
        if acquired:
            ident = get_ident()
            self.owners[ident] = self.owners.get(ident, 0) + 1
        return acquired

    def release(self):
        ident = get_ident()
        self.owners[ident] -= 1
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


@pytest.mark.parametrize("switch", ["on", "on_after_write"])
def test_card_d1_switch_race(world, monkeypatch, switch):
    w = world
    seed(w, switch != "off")
    create(w)
    leased = jobs.lease(w.js, w.machine())
    assert leased is not None
    monkeypatch.setattr(jobs, "_sweep", lambda store: None)
    if switch == "on_after_write":
        assert start(w, leased)[0] == 200
        reset(w)
        assert stored(w, leased)["state"] == "starting"
        jobs.sweep(w.js, w.db, w.ws)
        assert stored(w, leased)["state"] == "uncertain"
        return
    # BASE drops control after validate. A serialized candidate holds it until
    # commit. Never wait for reset completion in that protected candidate leg.
    tracker = TrackingLock(w.db._lock)
    monkeypatch.setattr(w.db, "_lock", tracker)
    original = jobs.starting
    reset_attempted, reset_done = Event(), Event()
    errors, reset_threads = [], []
    protected = False

    def resetting():
        try:
            reset_attempted.set()
            reset(w)
            reset_done.set()
        except BaseException as exc:
            errors.append(exc)

    def starting(*args, validate, **kwargs):
        def checked(row):
            nonlocal protected
            value = validate(row)
            protected = tracker.owned()
            thread = Thread(target=resetting)
            reset_threads.append(thread)
            thread.start()
            assert reset_attempted.wait(5)
            if not tracker.owned():
                assert reset_done.wait(5), "unserialized reset did not commit"
            return value

        return original(*args, validate=checked, **kwargs)

    monkeypatch.setattr(jobs, "starting", starting)
    status, _ = start(w, leased)
    for thread in reset_threads:
        thread.join(7)
        assert not thread.is_alive()
    assert not errors
    assert reset_done.is_set()
    if status == 200:
        # A 200 is legal only when serialization made reset commit AFTER write.
        assert stored(w, leased)["state"] == "starting"
        assert tracker.owners  # tracking was exercised
        # BASE's unprotected validate must never reach this success path.
        assert protected, "reset committed before guarded write but start succeeded"
        if switch != "off":
            jobs.sweep(w.js, w.db, w.ws)
            assert stored(w, leased)["state"] == "uncertain"
            assert jobs.lease(w.js, w.machine()) is None
    else:
        assert status == 409
        assert stored(w, leased)["state"] == "cancelled"
        assert stored(w, leased)["starting_at"] is None


def test_card_d1_switch_race_off_stale_outcome_after_write(world, monkeypatch):
    w = world
    seed(w, False)
    create(w)
    leased = jobs.lease(w.js, w.machine())
    monkeypatch.setattr(jobs, "_sweep", lambda store: None)
    assert start(w, leased)[0] == 200
    reset(w)
    assert stored(w, leased)["state"] == "starting"
    jobs.sweep(w.js, w.db, w.ws)
    assert stored(w, leased)["state"] == "uncertain"


def test_card_d1_switch_race_off_lock_order(world, monkeypatch):
    w = world
    seed(w, False)
    create(w)
    leased = jobs.lease(w.js, w.machine())
    tracker = TrackingLock(w.db._lock)
    monkeypatch.setattr(w.db, "_lock", tracker)
    flags = []
    original = jobs.JobStore.transaction

    @contextmanager
    def transaction(store):
        flags.append(tracker.owned())
        with original(store) as conn:
            yield conn

    monkeypatch.setattr(jobs.JobStore, "transaction", transaction)
    assert start(w, leased)[0] == 200
    assert flags
    assert not any(flags), flags


@pytest.mark.parametrize("flip", [False, True])
def test_card_d1_switch_race_off_stale_outcome(world, monkeypatch, flip):
    from commonplace.projects import orchestration as orch

    original = orch.enabled
    # Set up first, and flip only on the route's first switch read.
    w = world
    if flip:
        original_start = start

        def flipped_start(w, leased):
            calls = []

            def enabled(db, ws):
                value = original(db, ws)
                calls.append(value)
                if len(calls) == 1:
                    seed(w, False)
                return value

            monkeypatch.setattr(orch, "enabled", enabled)
            return original_start(w, leased)

        monkeypatch.setitem(globals(), "start", flipped_start)
    test_card_d1_switch_race(w, monkeypatch, "flip" if flip else "off")


def test_provider_change_requires_reset(world):
    w = world
    before = dict(w.machine())
    with w.db._lock:
        audit = list(w.db._conn.execute("SELECT * FROM audit_log"))
    status, error = 200, None
    try:
        confirm(w, "different-provider")
    except registry.Conflict as exc:
        status, error = 409, str(exc)
    assert (status, error) == (409, "provider_change_requires_reset")
    with w.db._lock:
        assert list(w.db._conn.execute("SELECT * FROM audit_log")) == audit
    assert dict(w.machine()) == before
    reset(w)
    assert epoch(w) == before["binding_epoch"] + 1
    confirm(w, "different-provider")
    assert epoch(w) == before["binding_epoch"] + 1
    assert w.machine()["binding_provider_instance_id"] == "different-provider"


LEGACY_MACHINE_DDL = "\n            CREATE TABLE IF NOT EXISTS runner_machines (\n                id TEXT PRIMARY KEY,\n                workspace_id TEXT NOT NULL,\n                label TEXT NOT NULL,\n                machine_connection_id TEXT NOT NULL UNIQUE,\n                worker_connection_id TEXT NOT NULL UNIQUE,\n                projects TEXT NOT NULL, topics TEXT NOT NULL, wings TEXT NOT NULL,\n                max_runtime_mode TEXT NOT NULL DEFAULT 'approval-required'\n                    CHECK(max_runtime_mode IN ('approval-required','auto-accept-edits','auto','full-access')),\n                requesters TEXT NOT NULL DEFAULT '[]',\n                max_in_flight INTEGER NOT NULL DEFAULT 1 CHECK(max_in_flight BETWEEN 1 AND 4),\n                max_starts_per_hour INTEGER NOT NULL DEFAULT 6 CHECK(max_starts_per_hour BETWEEN 1 AND 30),\n                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),\n                grant_version INTEGER NOT NULL DEFAULT 1,\n                binding_state TEXT NOT NULL DEFAULT 'unbound'\n                    CHECK(binding_state IN ('unbound','owner_attested')),\n                binding_topics TEXT, binding_provider_instance_id TEXT,\n                binding_worker_connection_id TEXT, binding_attested_by TEXT,\n                binding_attested_at TEXT, binding_grant_version INTEGER,\n                enrolled_by TEXT NOT NULL, updated_by TEXT NOT NULL,\n                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revoked_at TEXT\n            );\n            CREATE UNIQUE INDEX IF NOT EXISTS runner_machines_label\n                ON runner_machines(workspace_id, label) WHERE revoked_at IS NULL;\n        "
LEGACY_JOB_DDL = "\nCREATE TABLE IF NOT EXISTS runner_jobs (\n id TEXT PRIMARY KEY,\n task_number INTEGER NOT NULL, board_slug TEXT NOT NULL,\n machine_id TEXT NOT NULL, project TEXT NOT NULL, topic TEXT NOT NULL,\n grant_version INTEGER NOT NULL, runtime_mode TEXT NOT NULL,\n requested_by TEXT NOT NULL, request_key TEXT, request_hash TEXT,\n state TEXT NOT NULL CHECK (state IN\n ('queued','leased','starting','started','failed','uncertain','cancelled')),\n attempt INTEGER NOT NULL DEFAULT 0,\n lease_token TEXT, lease_until TEXT, starting_at TEXT, started_at TEXT,\n thread_id TEXT, outcome_code TEXT,\n cancel_requested_at TEXT, cancelled_by TEXT, resolved_by TEXT, resolved_at TEXT,\n created_at TEXT NOT NULL, updated_at TEXT NOT NULL\n);\nCREATE UNIQUE INDEX IF NOT EXISTS runner_jobs_one_active ON runner_jobs(task_number)\n WHERE state IN ('queued','leased','starting','uncertain');\nCREATE INDEX IF NOT EXISTS runner_jobs_board_state ON runner_jobs(board_slug,state);\nCREATE UNIQUE INDEX IF NOT EXISTS runner_jobs_request\n ON runner_jobs(task_number,requested_by,request_key) WHERE request_key IS NOT NULL;\n"


def test_pre_p0_upgrade(world):
    w = world
    # Recreate the table using frozen pre-epoch DDL, preserving the bound row.
    original = dict(w.machine())
    legacy = {k: v for k, v in original.items() if k != "binding_epoch"}
    with w.db._lock:
        w.db._conn.execute("DROP TABLE runner_machines")
        w.db._conn.executescript(LEGACY_MACHINE_DDL)
        w.db._conn.execute(
            "INSERT INTO runner_machines ("
            + ",".join(legacy)
            + ") VALUES ("
            + ",".join("?" for _ in legacy)
            + ")",
            tuple(legacy.values()),
        )
    with w.js.connection() as conn:
        conn.execute("DROP TABLE runner_jobs")
        conn.executescript(LEGACY_JOB_DDL)
        conn.execute(
            "INSERT INTO runner_jobs(id,task_number,board_slug,machine_id,project,topic,grant_version,runtime_mode,requested_by,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy",
                999,
                "plain",
                w.machine_id,
                "demo",
                "worker",
                1,
                "approval-required",
                "user:" + w.owner.user_id,
                "queued",
                jobs._now(),
                jobs._now(),
            ),
        )
    for _ in range(2):
        registry.ensure(w.db)
        w.js = jobs.open_store(w.gw, w.ws)
        assert epoch(w) == 1
        assert w.machine()["binding_state"] == "owner_attested"
        assert {k: v for k, v in dict(w.machine()).items() if k != "binding_epoch"} == legacy
        with w.db._lock:
            column = next(
                r
                for r in w.db._conn.execute("PRAGMA table_info(runner_machines)")
                if r[1] == "binding_epoch"
            )
        assert column[3] == 1 and str(column[4]) == "1"
        with w.js.connection() as conn:
            columns = {r[1]: r for r in conn.execute("PRAGMA table_info(runner_jobs)")}
            assert "binding_epoch" in columns
            assert columns["binding_epoch"][3] == 0
    jobs.sweep(w.js, w.db, w.ws)
    legacy_row = w.rows("SELECT * FROM runner_jobs WHERE id='legacy'")[0]
    assert legacy_row["binding_epoch"] is None
    assert legacy_row["state"] == "cancelled"
    assert legacy_row["outcome_code"] == "binding_epoch_changed"
    row = create(w)
    assert row["binding_epoch"] == 1
    leased = jobs.lease(w.js, w.machine())
    assert leased["id"] == row["id"]
    assert start(w, leased)[0] == 200


def test_two_openers_migrate_once(tmp_path):
    from threading import Barrier, RLock
    from types import SimpleNamespace

    from mempalace.sqlite_crypto import connect

    machine_path = str(tmp_path / "control.sqlite3")
    runner_path = str(tmp_path / "runner.sqlite3")
    for path, ddl in ((machine_path, LEGACY_MACHINE_DDL), (runner_path, LEGACY_JOB_DDL)):
        conn = connect(path, isolation_level=None)
        conn.executescript(ddl)
        conn.close()
    barrier = Barrier(2)
    errors = []

    def opener():
        conn = connect(machine_path, timeout=5, isolation_level=None)
        try:
            barrier.wait(5)
            registry.ensure(SimpleNamespace(_lock=RLock(), _conn=conn))
            gw = SimpleNamespace(workspace_dir=lambda ws: str(tmp_path))
            jobs.JobStore(gw, "ws")
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [Thread(target=opener) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert not errors, errors
    for path, table in ((machine_path, "runner_machines"), (runner_path, "runner_jobs")):
        conn = connect(path, isolation_level=None)
        try:
            names = [r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")]
            assert names.count("binding_epoch") == 1
        finally:
            conn.close()


@pytest.mark.parametrize("switch", [False, True, "flip"])
@pytest.mark.parametrize("reattest", [False, True])
def test_reset_first_direct_fences(world, monkeypatch, switch, reattest):
    w = world
    epoch(w)
    seed(w, switch is not False)
    row = create(w)
    leased = jobs.lease(w.js, w.machine())
    assert leased is not None
    monkeypatch.setattr(jobs, "_sweep", lambda store: None)
    original = jobs.starting

    def starting(*args, **kwargs):
        reset(w)
        if reattest:
            confirm(w)
        if switch == "flip":
            seed(w, False)
        return original(*args, **kwargs)

    monkeypatch.setattr(jobs, "starting", starting)
    status, body = start(w, leased)
    assert status == 409
    assert body["error"] == ("binding_epoch_changed" if reattest else "binding_not_attested")
    assert stored(w, row)["state"] == "cancelled"
    assert stored(w, row)["starting_at"] is None


@pytest.mark.parametrize("case", ["unbound", "null", "mismatch", "matching", "disabled"])
def test_retry_and_starting_sweep_vectors(world, monkeypatch, case):
    w = world
    epoch(w)
    row = create(w)
    leased = jobs.lease(w.js, w.machine())
    assert start(w, leased)[0] == 200
    monkeypatch.setattr(jobs, "_sweep", lambda store: None)
    if case in ("unbound", "mismatch"):
        reset(w)
        if case == "mismatch":
            confirm(w)
    elif case == "null":
        with w.js.transaction() as conn:
            conn.execute("UPDATE runner_jobs SET binding_epoch=NULL WHERE id=?", (row["id"],))
    elif case == "disabled":
        original_starting = jobs.starting

        def disable_before_write(*args, **kwargs):
            registry.patch(w.db, w.owner, w.machine_id, {"enabled": False})
            return original_starting(*args, **kwargs)

        monkeypatch.setattr(jobs, "starting", disable_before_write)
    status, body = start(w, leased)
    assert status == (200 if case == "matching" else 409)
    if case != "matching":
        expected = {"disabled": "machine_disabled", "unbound": "binding_not_attested"}.get(
            case, "binding_epoch_changed"
        )
        assert body["error"] == expected
    assert stored(w, row)["state"] == "starting"
    jobs.sweep(w.js, w.db, w.ws)
    assert stored(w, row)["state"] == ("starting" if case == "matching" else "uncertain")
    assert jobs.lease(w.js, w.machine()) is None
    if case == "matching":
        with w.js.transaction() as conn:
            conn.execute(
                "UPDATE runner_jobs SET starting_at='2000-01-01T00:00:00Z' WHERE id=?", (row["id"],)
            )
        jobs.sweep(w.js, w.db, w.ws)
        assert stored(w, row)["state"] == "uncertain"


@pytest.mark.parametrize("case", ["unbound", "null", "mismatch", "disabled"])
def test_lease_direct_fence_without_cleanup(world, monkeypatch, case):
    w = world
    epoch(w)
    row = create(w)
    snapshot = dict(w.machine())
    monkeypatch.setattr(jobs, "_sweep", lambda store: None)
    if case in ("unbound", "mismatch"):
        reset(w)
        if case == "mismatch":
            confirm(w)
    elif case == "null":
        with w.js.transaction() as conn:
            conn.execute("UPDATE runner_jobs SET binding_epoch=NULL WHERE id=?", (row["id"],))
    else:
        registry.patch(w.db, w.owner, w.machine_id, {"enabled": False})
    assert jobs.lease(w.js, snapshot) is None
    assert stored(w, row)["state"] == "cancelled"
    assert stored(w, row)["outcome_code"] == {
        "disabled": "machine_revoked",
        "unbound": "binding_not_attested",
    }.get(case, "binding_epoch_changed")


@pytest.mark.parametrize("switch", [False, True, "flip"])
@pytest.mark.parametrize("action", ["starting", "lease", "resolve"])
def test_all_runner_and_board_entries_release_control(world, monkeypatch, switch, action):
    from commonplace.boards.store import BoardStore
    from commonplace.projects import orchestration as orch

    w = world
    seed(w, switch is not False)
    row = create(w)
    leased = jobs.lease(w.js, w.machine())
    if action == "resolve":
        with w.js.transaction() as conn:
            conn.execute("UPDATE runner_jobs SET state='uncertain' WHERE id=?", (row["id"],))
    tracker = TrackingLock(w.db._lock)
    monkeypatch.setattr(w.db, "_lock", tracker)
    flags, board_flags = [], []
    original, board_original, enabled_original = (
        jobs.JobStore.transaction,
        BoardStore.connection,
        orch.enabled,
    )

    @contextmanager
    def transaction(store):
        flags.append(tracker.owned())
        with original(store) as conn:
            yield conn

    @contextmanager
    def board_connection(store):
        board_flags.append(tracker.owned())
        with board_original(store) as conn:
            yield conn

    calls = []

    def enabled(db, ws):
        value = enabled_original(db, ws)
        calls.append(value)
        if switch == "flip" and len(calls) == 1:
            seed(w, False)
        return value

    monkeypatch.setattr(jobs.JobStore, "transaction", transaction)
    monkeypatch.setattr(BoardStore, "connection", board_connection)
    monkeypatch.setattr(orch, "enabled", enabled)
    if action == "resolve":
        assert tools.card_start_resolve(w.ctx(w.owner), row["id"], "failed")["state"] == "failed"
    elif action == "lease":
        assert w.machine_call("lease", {})[0] == 204
    else:
        assert start(w, leased)[0] == 200
    assert flags and not any(flags), flags
    assert not any(board_flags), board_flags


def test_card_d2_same_provider_legacy_no_bump(world):
    w = world
    before = dict(w.machine())
    confirm(w)
    assert w.machine()["binding_provider_instance_id"] == before["binding_provider_instance_id"]
    if "binding_epoch" in before:
        assert epoch(w) == before["binding_epoch"]
