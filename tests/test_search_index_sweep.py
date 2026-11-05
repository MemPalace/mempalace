"""Synthetic bounded search-index sweep and service scheduling checks."""

import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from commonplace import continuity_schema, plans, sessions
from commonplace.control_db import ControlDB
from commonplace.handoff import search_index
from commonplace.handoff.models import ThreadMessage
from commonplace.handoff.service import HandoffService
from commonplace.handoff.thread_store import ThreadStore, ThreadStoreV2


class Worker:
    def __init__(self):
        self.entries = {}
        self.batches = []
        self.fail_at = None
        self.writes = []

    def __call__(self, principal, name, args):
        if name == "mempalace_list_drawers":
            return {"drawers": [], "total": 0}
        assert name == "write_mirror_units"
        if len(self.batches) == self.fail_at:
            raise RuntimeError("synthetic interruption")
        self.batches.append(len(args["units"]))
        ids = []
        for unit in args["units"]:
            key = hashlib.sha256(
                json.dumps([args["thread_id"], unit], sort_keys=True).encode()
            ).hexdigest()
            self.entries[key] = unit
            ids.append(key)
        self.writes.append(ids)
        return {"drawer_ids": ids}


@pytest.fixture(params=[ThreadStore, ThreadStoreV2], ids=["v1", "v2"])
def world(tmp_path, monkeypatch, request):
    db = ControlDB(str(tmp_path / "control.db"))
    continuity_schema.ensure_schema(db)
    user = db.create_user("owner@example.test")
    ws = db.create_workspace("synthetic", user)
    store = request.param(str(tmp_path / "threads.v2.sqlite3"))
    worker = Worker()
    service = HandoffService(
        control_db=db,
        handoff_db=SimpleNamespace(set_document_store=lambda _: None),
        settings=None,
        store_for=lambda _: store,
        events=None,
        call_tool=worker,
    )
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    monkeypatch.delenv("COMMONPLACE_SEARCH_INDEX", raising=False)
    monkeypatch.setattr(search_index, "PAUSE_SECONDS", 0)
    yield SimpleNamespace(db=db, ws=ws, user=user, store=store, worker=worker, service=service)
    service.close()
    store.close()
    db.close()


def put(world, source="import:synthetic", count=1, text="synthetic words"):
    return world.store.store(
        world.ws,
        source,
        [ThreadMessage(i, "user", "message", f"{text}{i}", "{}") for i in range(count)],
        actor="synthetic",
    )


def test_u3_a_coalescing_and_enrolment(world, monkeypatch):
    stored = put(world, "capture:active")
    entered, release = threading.Event(), threading.Event()
    original = search_index.sweep

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(search_index, "sweep", blocked)
    world.service.index_later(world.ws, [stored.id])
    assert entered.wait(5)
    for _ in range(5):
        world.service.index_later(world.ws, [stored.id])
    assert len(world.service._mirror_jobs) == 1
    assert world.store.mirror_progress(stored.id) == (-1, -1)
    release.set()
    world.service.wait_for_mirrors(10)
    assert len(world.worker.entries) == 1
    assert not world.service._index_active


def test_u3_b_job_bounds_and_draining(world, monkeypatch):
    for i in range(23):
        put(world, f"import:{i}")
    jobs = []
    original = search_index.sweep

    def record(*args):
        before = len(world.worker.entries)
        result = original(*args)
        jobs.append(len(world.worker.entries) - before)
        return result

    monkeypatch.setattr(search_index, "sweep", record)
    world.service.index_later(world.ws)
    world.service.wait_for_mirrors(10)
    assert jobs == [20, 3]
    assert world.service.index_status(world.ws)["threads_pending"] == 0
    # One large thread also spans jobs, with no job exceeding the unit bound.
    put(world, "import:large", text="x" * 4000, count=1100)
    jobs.clear()
    world.service.index_later(world.ws)
    world.service.wait_for_mirrors(20)
    assert len(jobs) >= 2
    assert max(jobs) <= 2000
    assert world.service.index_status(world.ws)["threads_pending"] == 0


def test_u3_c_crash_retry_idempotence(world):
    stored = put(world, count=40, text="x" * 8000)
    world.worker.fail_at = 1
    world.service.index_later(world.ws, [stored.id])
    world.service.wait_for_mirrors(10)
    first = set(world.worker.entries)
    assert len(first) == 64
    assert world.store.mirror_progress(stored.id)[0] == -1
    world.worker.fail_at = None
    # No range finished: retry must replay this pinned range, including lost units.
    world.worker.entries.clear()
    world.service.index_later(world.ws)
    world.service.wait_for_mirrors(10)
    clean = Worker()
    from commonplace.handoff import palace_mirror

    palace_mirror.mirror(
        clean, None, stored, palace_mirror.class_units(world.store.messages(stored.id))
    )
    assert set(world.worker.entries) == set(clean.entries)
    assert first <= set(world.worker.entries)
    before = len(world.worker.batches)
    units = world.service.index_status(world.ws)["units_written"]
    world.service.index_later(world.ws)
    world.service.wait_for_mirrors(10)
    assert len(world.worker.batches) == before
    assert world.service.index_status(world.ws)["units_written"] == units


def test_u3_d_quota_defers_between_batches(world, monkeypatch):
    stored = put(world, count=40, text="x" * 8000)
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    monkeypatch.setattr(plans, "workspace_account", lambda *_: world.user)
    monkeypatch.setattr(plans, "get_plan", lambda *_: plans.FREE)
    monkeypatch.setattr(
        plans.StorageMeter,
        "_walk",
        lambda _: (
            plans.FREE.storage_bytes - search_index.STORAGE_MARGIN if world.worker.batches else 0
        ),
    )
    world.service.index_later(world.ws, [stored.id])
    world.service.wait_for_mirrors(10)
    assert world.worker.batches == [64]
    assert world.service.index_status(world.ws)["state"] == "deferred_storage"
    assert world.service.index_status(world.ws)["units_written"] == 64
    assert world.store.mirror_progress(stored.id)[0] == -1
    monkeypatch.setattr(plans.StorageMeter, "_walk", lambda _: 0)
    world.service.index_later(world.ws)
    world.service.wait_for_mirrors(10)
    assert world.service.index_status(world.ws)["threads_pending"] == 0


def test_u3_e_kill_switch_and_never_raises(world, monkeypatch):
    stored = put(world, "capture:active")
    monkeypatch.setenv("COMMONPLACE_SEARCH_INDEX", "off")
    world.service.index_later(world.ws, [stored.id])
    assert world.store.mirror_progress(stored.id) is None
    assert world.service.index_status(world.ws)["state"] == "off"
    monkeypatch.setenv("COMMONPLACE_SEARCH_INDEX", "on")
    monkeypatch.setattr(world.service, "store_for", lambda _: 1 / 0)
    world.service.index_later(world.ws, [stored.id])


def test_u3_f_principals_and_import_groups(world, monkeypatch):
    filer = world.db.create_user("member@example.test")
    world.db.add_member(world.ws, filer, "member", actor=world.user)
    stored = put(world, f"import-private:{filer}:snapshot")
    assert search_index.principal_for(world.db, stored).user_id == filer
    original = sessions.person_principal
    monkeypatch.setattr(
        sessions,
        "person_principal",
        lambda db, uid, ws: None if uid == filer else original(db, uid, ws),
    )
    assert search_index.principal_for(world.db, stored).user_id == world.user
    monkeypatch.setattr(sessions, "person_principal", lambda *_: None)
    assert search_index.principal_for(world.db, stored) is None
    monkeypatch.setattr(sessions, "person_principal", original)
    holder = put(world, f"capture:{filer}:holder")
    fork = put(world, f"capture-fork:{filer}:fork")
    untouched = put(world, f"capture:{filer}:untouched")
    with world.db._lock, world.db._tx() as conn:
        conn.execute(
            "INSERT INTO sync_groups VALUES (?,?,?,?,?,?)",
            ("group", world.ws, filer, "synthetic", "native", holder.id),
        )
        conn.execute(
            "INSERT INTO sync_streams (workspace_id,stream_id,user_id,connection_id,kind,"
            "group_id,own_stored_id,fork_stored_id) VALUES (?,?,?,?,?,?,?,?)",
            (world.ws, "stream", filer, "", "import", "group", stored.id, fork.id),
        )
    assert search_index.import_groups(world.db, world.ws) == {holder.id, fork.id}
    world.service.index_later(world.ws)
    world.service.wait_for_mirrors(10)
    assert world.store.mirror_progress(holder.id)[0] == 0
    assert world.store.mirror_progress(fork.id)[0] == 0
    assert world.store.mirror_progress(untouched.id) is None


def test_import_groups_enrolled_once_per_state(world, monkeypatch):
    groups = []
    monkeypatch.setattr(search_index, "import_groups", lambda *_: groups.append(world.ws) or set())
    state = search_index.State()
    assert search_index.sweep(world.service, world.ws, state) is False
    assert search_index.sweep(world.service, world.ws, state) is False
    assert groups == [world.ws]


def test_bounded_ranges_advance_durably_without_cache(world, monkeypatch):
    monkeypatch.setattr(search_index, "MAX_UNITS", 64)
    stored = put(world, "import:bounded", count=200, text="p" * 4000)
    state = search_index.State()
    completed = []
    for _ in range(10):
        more = search_index.sweep(world.service, world.ws, state)
        progress = world.store.mirror_progress(stored.id)
        completed.append(progress[0])
        assert progress[0] == progress[1]
        if not more:
            break
    else:
        pytest.fail("bounded sweep did not finish")
    assert completed == [31, 63, 95, 127, 159, 191, 199]
    assert len(world.worker.entries) == 400
    assert world.worker.batches == [64] * 6 + [16]
    assert not hasattr(state, "batches")


def test_finished_range_loss_is_trusted_not_repaired(world, monkeypatch):
    monkeypatch.setattr(search_index, "MAX_UNITS", 64)
    stored = put(world, "import:silent-loss", count=200, text="p" * 4000)
    state = search_index.State()
    assert search_index.sweep(world.service, world.ws, state) is True
    assert world.store.mirror_progress(stored.id) == (31, 31)
    first_ids = set(world.worker.entries)
    world.worker.entries.clear()
    for _ in range(10):
        if not search_index.sweep(world.service, world.ws, state):
            break
    else:
        pytest.fail("bounded sweep did not finish")
    assert len(world.worker.entries) == 336
    assert first_ids.isdisjoint(world.worker.entries)
    assert world.store.mirror_progress(stored.id) == (199, 199)
    assert world.store.index_backlog(world.ws, after_id="", limit=10) == []


def test_crash_mid_range_replays_only_unfinished_range(world, monkeypatch):
    monkeypatch.setattr(search_index, "MAX_UNITS", 128)
    stored = put(world, "import:range-retry", count=100, text="p" * 8000)
    state = search_index.State()
    assert search_index.sweep(world.service, world.ws, state) is True
    assert world.store.mirror_progress(stored.id)[0] == 31
    finished_ids = set(world.worker.entries)
    world.worker.fail_at = len(world.worker.batches) + 1
    search_index.sweep(world.service, world.ws, state)
    assert world.store.mirror_progress(stored.id) == (31, 63)
    before_retry = len(world.worker.writes)
    state = search_index.State()  # Process-local state can be lost; the range is durable.
    world.worker.fail_at = None
    for _ in range(10):
        if not search_index.sweep(world.service, world.ws, state):
            break
    else:
        pytest.fail("bounded sweep did not finish")
    replayed_ids = {key for batch in world.worker.writes[before_retry:] for key in batch}
    assert finished_ids.isdisjoint(replayed_ids)
    assert len(world.worker.entries) == 300
    assert world.store.mirror_progress(stored.id) == (99, 99)


def test_one_oversized_message_finishes_without_cache(world, monkeypatch):
    monkeypatch.setattr(search_index, "MAX_UNITS", 2)
    stored = put(world, "import:oversized", count=1, text="".join(f"{i:04d}" for i in range(5000)))
    search_index.sweep(world.service, world.ws, search_index.State())
    assert len(world.worker.entries) == 6
    assert world.store.mirror_progress(stored.id) == (0, 0)


def test_preexisting_whole_thread_pin_can_finish_without_cache(world, monkeypatch):
    monkeypatch.setattr(search_index, "MAX_UNITS", 64)
    stored = put(world, "import:legacy-pin", count=200, text="p" * 4000)
    world.store.begin_mirror(stored.id, 199)
    search_index.sweep(world.service, world.ws, search_index.State())
    assert len(world.worker.entries) == 400
    assert world.store.mirror_progress(stored.id) == (199, 199)
