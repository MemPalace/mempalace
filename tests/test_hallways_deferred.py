"""Deferred hallway recompute: the durable dirty mark and who clears it.

A mine that defers its hallways marks the wing dirty in the hallway store.
The mark is cleared only by a recompute that read the wing's drawers after
the mark was made, so a mine that lands during a recompute keeps the wing
dirty instead of being lost.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mempalace import hallways as hallways_mod


@pytest.fixture
def store(monkeypatch, tmp_path):
    hallway_file = tmp_path / "hallways.json"
    monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **kw: str(hallway_file))
    monkeypatch.setattr(
        hallways_mod, "_legacy_hallway_file", lambda: str(tmp_path / "legacy-hallways.json")
    )
    return hallway_file


def _collection(drawers, on_get=None):
    """A paged collection over ``drawers``; ``on_get`` runs once per page read."""
    col = MagicMock(spec=["get", "count"])

    def _get(limit=None, offset=0, include=None, where=None, **kwargs):
        if on_get is not None:
            on_get()
        wing = (where or {}).get("wing")
        metas = [d for d in drawers if wing is None or d.get("wing") == wing]
        page = metas[offset : offset + limit] if limit is not None else metas
        return {"ids": [str(i) for i in range(len(page))], "metadatas": page}

    col.get.side_effect = _get
    return col


def _drawers(wing="w", n=2):
    return [{"wing": wing, "room": "r", "entities": "Aya;Lumi"} for _ in range(n)]


def _dirty(wing):
    return [d for d in hallways_mod.dirty_wings() if d["wing"] == wing]


def test_a_marked_wing_is_listed_dirty_until_a_recompute_clears_it(store):
    hallways_mod.mark_wing_dirty("w")
    assert [d["wing"] for d in hallways_mod.dirty_wings()] == ["w"]

    created = hallways_mod.compute_hallways_for_wing("w", col=_collection(_drawers()))

    assert [h["entity_a"] for h in created] == ["Aya"]
    assert hallways_mod.dirty_wings() == []


def test_repeat_marks_keep_the_first_mark_time_and_or_the_tunnel_flag(store):
    hallways_mod.mark_wing_dirty("w", now=100.0)
    hallways_mod.mark_wing_dirty("w", entity_tunnels=True, now=200.0)
    hallways_mod.mark_wing_dirty("w", now=300.0)

    (mark,) = _dirty("w")
    assert mark["first_marked_at"] == 100.0
    assert mark["last_marked_at"] == 300.0
    assert mark["entity_tunnels"] is True


def test_a_mark_made_while_the_recompute_reads_drawers_survives_it(store):
    """A mine that files drawers after the recompute's read must not be lost."""
    hallways_mod.mark_wing_dirty("w")
    marked_again = []

    def mine_lands_mid_read():
        if not marked_again:
            marked_again.append(hallways_mod.mark_wing_dirty("w"))

    hallways_mod.compute_hallways_for_wing("w", col=_collection(_drawers(), mine_lands_mid_read))

    assert len(_dirty("w")) == 1


def test_a_recompute_of_another_wing_leaves_the_mark(store):
    hallways_mod.mark_wing_dirty("w")
    hallways_mod.compute_hallways_for_wing("other", col=_collection(_drawers("other")))
    assert len(_dirty("w")) == 1


def test_an_empty_wing_clears_its_mark(store):
    """A wing with no drawers left has nothing to recompute; its mark must not
    re-enqueue forever."""
    hallways_mod.mark_wing_dirty("gone")
    assert hallways_mod.compute_hallways_for_wing("gone", col=_collection([])) == []
    assert hallways_mod.dirty_wings() == []


def test_a_failed_drawer_read_keeps_the_mark(store):
    hallways_mod.mark_wing_dirty("w")
    col = MagicMock(spec=["get", "count"])
    col.get.side_effect = RuntimeError("disk on fire")

    assert hallways_mod.compute_hallways_for_wing("w", col=col) == []
    assert len(_dirty("w")) == 1


def test_readers_keep_the_last_records_while_a_wing_is_dirty(store):
    hallways_mod.compute_hallways_for_wing("w", col=_collection(_drawers()))
    before = hallways_mod.list_hallways("w")

    hallways_mod.mark_wing_dirty("w")

    assert hallways_mod.list_hallways("w") == before
    assert len(before) == 1


def test_no_store_means_no_dirty_wings_and_reading_creates_nothing(store, tmp_path):
    assert hallways_mod.dirty_wings() == []
    assert not (tmp_path / "hallways.sqlite3").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Mines: inline by default, deferred on request, never dropped
# ─────────────────────────────────────────────────────────────────────────────

_EXCHANGE = (
    "> How does the ChromaBackend talk to the MemoryStack today?\n"
    "The ChromaBackend hands every batch to the MemoryStack, which keeps it in order "
    "and flushes it once the batch is complete.\n\n"
)


@pytest.fixture
def convo_palace(tmp_path, monkeypatch):
    """A transcript dir, a palace beside it, and a private daemon state root."""
    from mempalace import daemon_queue

    monkeypatch.setenv(daemon_queue.STATE_ROOT_ENV, str(tmp_path / "state"))
    for key in ("MEMPALACE_HALLWAYS_DEFER", "MEMPALACE_HALLWAYS_SETTLE_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    convos = tmp_path / "convos"
    convos.mkdir()
    (convos / "chat.txt").write_text(_EXCHANGE * 4, encoding="utf-8")
    return convos, tmp_path / "palace"


@pytest.fixture
def daemon_serving(monkeypatch):
    """A daemon serves the palace, so a deferring mine may leave it the work."""
    from mempalace import hallway_refresh

    monkeypatch.setattr(hallway_refresh, "_daemon_serves", lambda *a, **k: True)


def _palace_config(palace):
    from mempalace.config import MempalaceConfig

    return MempalaceConfig(palace_path=str(palace))


def _pairs(records):
    return sorted(
        (h["wing"], h["entity_a"], h["entity_b"], h["co_occurrence_count"], tuple(h["rooms"]))
        for h in records
    )


def _queued_hallway_jobs(palace):
    from mempalace import daemon_queue

    store = daemon_queue.QueueStore(daemon_queue.queue_path(str(palace)))
    return [j for j in store.list(limit=100) if j.kind == "hallways"]


def test_a_mine_recomputes_hallways_inline_by_default(convo_palace):
    from mempalace.convo_miner import mine_convos

    convos, palace = convo_palace
    mine_convos(str(convos), str(palace), wing="chat")

    config = _palace_config(palace)
    assert _pairs(hallways_mod.list_hallways("chat", config=config)), "expected hallways"
    assert hallways_mod.dirty_wings(config) == []
    assert _queued_hallway_jobs(palace) == []


def test_a_deferred_mine_marks_the_wing_and_queues_one_debounced_recompute(
    convo_palace, daemon_serving
):
    import time

    from mempalace.convo_miner import mine_convos

    convos, palace = convo_palace
    before = time.time()
    mine_convos(str(convos), str(palace), wing="chat", hallways="defer")

    config = _palace_config(palace)
    assert hallways_mod.list_hallways("chat", config=config) == []
    assert [d["wing"] for d in hallways_mod.dirty_wings(config)] == ["chat"]
    (job,) = _queued_hallway_jobs(palace)
    assert job.payload["wing"] == "chat"
    assert job.dedupe_key == "hallways:chat"
    assert before + 600 - 5 <= job.run_after <= time.time() + 600


def test_config_always_defers_a_mine_that_did_not_choose(convo_palace, monkeypatch, daemon_serving):
    from mempalace.convo_miner import mine_convos

    convos, palace = convo_palace
    monkeypatch.setenv("MEMPALACE_HALLWAYS_DEFER", "always")
    mine_convos(str(convos), str(palace), wing="chat")

    assert [d["wing"] for d in hallways_mod.dirty_wings(_palace_config(palace))] == ["chat"]


def test_the_deferred_recompute_equals_the_inline_one(convo_palace, tmp_path, daemon_serving):
    from mempalace.convo_miner import mine_convos
    from mempalace.hallway_refresh import run_refresh

    convos, deferred_palace = convo_palace
    inline_palace = tmp_path / "inline" / "palace"
    mine_convos(str(convos), str(inline_palace), wing="chat")
    mine_convos(str(convos), str(deferred_palace), wing="chat", hallways="defer")

    result = run_refresh({"palace_path": str(deferred_palace), "wing": "chat"})

    assert result["success"] is True, result
    inline = _pairs(hallways_mod.list_hallways("chat", config=_palace_config(inline_palace)))
    deferred = _pairs(hallways_mod.list_hallways("chat", config=_palace_config(deferred_palace)))
    assert deferred == inline and inline
    assert hallways_mod.dirty_wings(_palace_config(deferred_palace)) == []


def test_a_refresh_of_a_clean_wing_does_nothing(convo_palace):
    from mempalace.hallway_refresh import run_refresh

    _convos, palace = convo_palace
    result = run_refresh({"palace_path": str(palace), "wing": "chat"})
    assert result["success"] is True
    assert result.get("skipped") == "clean"


def test_a_deferred_project_mine_also_defers_its_entity_tunnels(
    tmp_path, monkeypatch, daemon_serving
):
    from mempalace import daemon_queue, miner as miner_mod

    monkeypatch.setenv(daemon_queue.STATE_ROOT_ENV, str(tmp_path / "state"))
    tunnel_calls = []
    monkeypatch.setattr(
        miner_mod, "_compute_entity_tunnels_for_wing", lambda *a, **k: tunnel_calls.append(a) or 0
    )
    project = tmp_path / "proj"
    project.mkdir()
    (project / "notes.md").write_text(_EXCHANGE * 4, encoding="utf-8")
    palace = tmp_path / "palace"

    miner_mod.mine(str(project), str(palace), wing_override="proj", hallways="defer")

    assert tunnel_calls == [], "entity tunnels read hallways; they wait for the recompute"
    (mark,) = hallways_mod.dirty_wings(_palace_config(palace))
    assert mark["wing"] == "proj" and mark["entity_tunnels"] is True

    from mempalace.hallway_refresh import run_refresh

    assert run_refresh({"palace_path": str(palace), "wing": "proj"})["success"] is True
    assert len(tunnel_calls) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Scheduling: a burst of mines is one recompute, bounded by max wait
# ─────────────────────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def queue(tmp_path):
    from mempalace.daemon_queue import QueueStore

    return QueueStore(tmp_path / "queue.sqlite3", clock=_Clock())


def _mine_marks(store, queue, wing, at):
    """What one deferring mine does to the store and the queue, at time ``at``."""
    from mempalace.hallway_refresh import schedule

    queue.clock.t = at
    hallways_mod.mark_wing_dirty(wing, now=at)
    return schedule(wing, "/palace", store=queue)


def test_a_burst_of_mines_is_one_recompute_after_the_settle(store, queue):
    t0 = queue.clock.t
    jobs = {_mine_marks(store, queue, "w", t0 + 30 * i).id for i in range(10)}
    last = t0 + 30 * 9

    assert len(jobs) == 1, "every mine of the burst coalesces into one job"
    queue.clock.t = last + 599
    assert queue.claim_next() is None, "still settling after the last mine"
    queue.clock.t = last + 600
    claimed = queue.claim_next()
    assert claimed.kind == "hallways" and claimed.payload == {"wing": "w"}
    assert queue.claim_next() is None


def test_continuous_mines_still_recompute_within_max_wait(store, queue):
    t0 = queue.clock.t
    t = t0
    while t < t0 + 3600:
        _mine_marks(store, queue, "w", t)
        assert queue.claim_next() is None or t >= t0 + 3600
        t += 300  # a mine every 5 minutes never lets a 10-minute settle pass

    queue.clock.t = t0 + 3600
    claimed = queue.claim_next()
    assert claimed is not None and claimed.kind == "hallways"


def test_two_wings_get_two_recomputes(store, queue):
    t0 = queue.clock.t
    _mine_marks(store, queue, "a", t0)
    _mine_marks(store, queue, "b", t0)
    queue.clock.t = t0 + 600
    claimed = {queue.claim_next().payload["wing"], queue.claim_next().payload["wing"]}
    assert claimed == {"a", "b"}


def test_a_mark_queued_late_keeps_its_first_mark_deadline(store, queue):
    """A mark that waited (no daemon, a crash) runs within max wait of the mark."""
    from mempalace.hallway_refresh import enqueue_dirty_wings

    t0 = queue.clock.t
    hallways_mod.mark_wing_dirty("w", now=t0 - 3000)  # made 50 minutes ago
    queue.clock.t = t0
    assert enqueue_dirty_wings("/palace", store=queue) == 1
    job = _queued(queue)[0]
    assert job.run_after == pytest.approx(t0 + 600)

    hallways_mod.mark_wing_dirty("x", now=t0 - 7200)  # older than max wait
    enqueue_dirty_wings("/palace", store=queue)
    assert queue.claim_next().payload == {"wing": "x"}, "overdue marks run at once"


def _queued(queue):
    return [j for j in queue.list(limit=100) if j.state == "queued"]


# ─────────────────────────────────────────────────────────────────────────────
# The daemon: runs the job, survives crashes, re-queues what is left
# ─────────────────────────────────────────────────────────────────────────────


def _wait_for(predicate, timeout=60.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def _run_daemon_until(palace, predicate):
    from mempalace import daemon

    runtime = daemon.DaemonRuntime(str(palace))
    runtime.start_worker()
    try:
        return _wait_for(lambda: predicate(runtime))
    finally:
        runtime.shutdown_event.set()
        runtime.worker_thread.join(timeout=30)


def _hallway_jobs_done(runtime):
    jobs = [j for j in runtime.store.list(limit=100) if j.kind == "hallways"]
    if jobs and all(j.state in ("succeeded", "failed", "cancelled") for j in jobs):
        return jobs
    return None


@pytest.fixture
def fast_refresh(monkeypatch):
    monkeypatch.setenv("MEMPALACE_HALLWAYS_SETTLE_SECONDS", "0")


def test_daemon_mines_defer_and_a_burst_recomputes_once(convo_palace, fast_refresh, monkeypatch):
    """Hook mines through the daemon: no inline recompute, one deferred one."""
    from mempalace import daemon_queue

    convos, palace = convo_palace
    calls = []
    real = hallways_mod.compute_hallways_for_wing

    def counting(wing, *a, **kw):
        calls.append(wing)
        return real(wing, *a, **kw)

    monkeypatch.setattr(hallways_mod, "compute_hallways_for_wing", counting)
    monkeypatch.setenv("MEMPALACE_HALLWAYS_SETTLE_SECONDS", "2")
    store = daemon_queue.QueueStore(daemon_queue.queue_path(str(palace)))
    for i in range(3):
        (convos / f"chat{i}.txt").write_text(_EXCHANGE * 4 + f"> n{i}?\nok {i}\n", "utf-8")
        store.enqueue("mine", {"source": str(convos), "mode": "convos", "wing": "chat"})

    jobs = _run_daemon_until(palace, _hallway_jobs_done)

    assert [j.state for j in jobs] == ["succeeded"], [j.error for j in jobs]
    assert calls == ["chat"], "three mines, exactly one recompute"
    assert hallways_mod.dirty_wings(_palace_config(palace)) == []
    assert _pairs(hallways_mod.list_hallways("chat", config=_palace_config(palace)))


def test_an_interactive_mine_through_the_daemon_stays_inline(convo_palace, fast_refresh):
    from mempalace import daemon_queue

    convos, palace = convo_palace
    store = daemon_queue.QueueStore(daemon_queue.queue_path(str(palace)))
    job = store.enqueue(
        "mine", {"source": str(convos), "mode": "convos", "wing": "chat", "hallways": "inline"}
    )

    _run_daemon_until(palace, lambda rt: rt.store.get(job.id).state == "succeeded")

    config = _palace_config(palace)
    assert _pairs(hallways_mod.list_hallways("chat", config=config))
    assert hallways_mod.dirty_wings(config) == []
    assert _queued_hallway_jobs(palace) == []


def test_a_mark_left_by_a_crash_is_processed_when_the_daemon_restarts(
    convo_palace, fast_refresh, daemon_serving
):
    """The mine committed and marked the wing, then the process died before
    any job was queued: the next daemon finds the mark and recomputes."""
    from mempalace.convo_miner import mine_convos

    convos, palace = convo_palace
    mine_convos(str(convos), str(palace), wing="chat", hallways="defer")
    from mempalace import daemon_queue

    store = daemon_queue.QueueStore(daemon_queue.queue_path(str(palace)))
    for job in store.list(limit=100):  # the crash lost the queue row
        store.finish(job.id, state="cancelled")

    jobs = _run_daemon_until(palace, _hallway_jobs_done)

    assert any(j.state == "succeeded" for j in jobs)
    assert hallways_mod.dirty_wings(_palace_config(palace)) == []
    assert _pairs(hallways_mod.list_hallways("chat", config=_palace_config(palace)))


def test_a_recompute_killed_mid_run_reruns_after_restart(
    convo_palace, fast_refresh, daemon_serving
):
    from mempalace import daemon_queue
    from mempalace.convo_miner import mine_convos

    convos, palace = convo_palace
    mine_convos(str(convos), str(palace), wing="chat", hallways="defer")
    store = daemon_queue.QueueStore(daemon_queue.queue_path(str(palace)))
    store.clock = lambda: 10**12  # past every debounce window
    running = store.claim_next()  # the daemon that claimed it died
    assert running.kind == "hallways"

    jobs = _run_daemon_until(palace, _hallway_jobs_done)

    assert [j.state for j in jobs] == ["succeeded"]
    assert hallways_mod.dirty_wings(_palace_config(palace)) == []


def test_a_mark_that_outlives_its_job_is_queued_again(
    convo_palace, fast_refresh, monkeypatch, daemon_serving
):
    """A recompute that failed leaves the mark; the daemon queues it again."""
    from mempalace import hallway_refresh
    from mempalace.convo_miner import mine_convos

    convos, palace = convo_palace
    mine_convos(str(convos), str(palace), wing="chat", hallways="defer")
    monkeypatch.setenv("MEMPALACE_HALLWAYS_MAX_WAIT_SECONDS", "0")  # runs the queued job now
    real = hallway_refresh.run_refresh
    attempts = []

    def flaky(payload):
        attempts.append(payload["wing"])
        if len(attempts) == 1:
            return {"success": False, "error": "drawers unreadable"}
        return real(payload)

    monkeypatch.setattr(hallway_refresh, "run_refresh", flaky)
    monkeypatch.setattr(hallway_refresh, "RETRY_FLOOR_SECONDS", 0.5)

    _run_daemon_until(palace, lambda rt: len(attempts) >= 2 and _hallway_jobs_done(rt))

    assert hallways_mod.dirty_wings(_palace_config(palace)) == []


# ─────────────────────────────────────────────────────────────────────────────
# CLI: interactive mines stay inline; deferral is a flag or config
# ─────────────────────────────────────────────────────────────────────────────


def _cli(monkeypatch, *argv):
    import sys

    from mempalace import cli

    monkeypatch.setattr(sys, "argv", ["mempalace", *argv])
    cli.main()


def test_cli_mine_recomputes_hallways_inline(convo_palace, monkeypatch):
    convos, palace = convo_palace
    _cli(
        monkeypatch,
        "--palace",
        str(palace),
        "mine",
        str(convos),
        "--mode",
        "convos",
        "--wing",
        "chat",
    )

    config = _palace_config(palace)
    assert _pairs(hallways_mod.list_hallways("chat", config=config))
    assert hallways_mod.dirty_wings(config) == []


def test_cli_mine_defer_flag_then_dirty_rebuild(convo_palace, monkeypatch, capsys, daemon_serving):
    convos, palace = convo_palace
    _cli(
        monkeypatch,
        "--palace",
        str(palace),
        "mine",
        str(convos),
        "--mode",
        "convos",
        "--wing",
        "chat",
        "--defer-hallways",
    )
    config = _palace_config(palace)
    assert hallways_mod.list_hallways("chat", config=config) == []

    capsys.readouterr()
    _cli(monkeypatch, "--palace", str(palace), "hallways", "--dirty")
    assert "chat" in capsys.readouterr().out
    assert [d["wing"] for d in hallways_mod.dirty_wings(config)] == ["chat"]

    _cli(monkeypatch, "--palace", str(palace), "hallways", "--rebuild", "--dirty")
    assert hallways_mod.dirty_wings(config) == []
    assert _pairs(hallways_mod.list_hallways("chat", config=config))


def test_cli_inline_flag_beats_config(convo_palace, monkeypatch):
    convos, palace = convo_palace
    monkeypatch.setenv("MEMPALACE_HALLWAYS_DEFER", "always")
    _cli(
        monkeypatch,
        "--palace",
        str(palace),
        "mine",
        str(convos),
        "--mode",
        "convos",
        "--wing",
        "chat",
        "--inline-hallways",
    )
    assert hallways_mod.dirty_wings(_palace_config(palace)) == []


def test_an_explicit_hallway_flag_keeps_the_mine_off_the_hub():
    from argparse import Namespace

    from mempalace.cli import _mine_args_forwardable

    base = dict(no_gitignore=False, mode="convos", max_chunks_per_file=None, backend=None)
    assert _mine_args_forwardable(Namespace(**base, hallways=None), [])
    assert not _mine_args_forwardable(Namespace(**base, hallways="defer"), [])


@pytest.mark.parametrize(
    ("setting", "explicit", "background", "expected"),
    [
        (None, None, False, "inline"),
        (None, None, True, "defer"),
        ("never", None, True, "inline"),
        ("always", None, False, "defer"),
        ("always", "inline", True, "inline"),
        ("bogus", None, False, "inline"),
        ("false", None, True, "inline"),
    ],
)
def test_hallway_mode_resolution(monkeypatch, setting, explicit, background, expected):
    from mempalace.config import MempalaceConfig

    if setting is None:
        monkeypatch.delenv("MEMPALACE_HALLWAYS_DEFER", raising=False)
    else:
        monkeypatch.setenv("MEMPALACE_HALLWAYS_DEFER", setting)
    assert MempalaceConfig().hallways_mode(explicit, background=background) == expected


# ─────────────────────────────────────────────────────────────────────────────
# Nothing stays stale: no daemon, failed jobs, busy wings
# ─────────────────────────────────────────────────────────────────────────────


def test_a_mark_that_survives_a_recompute_restarts_its_wait_at_the_read(store):
    """Drawers filed during a recompute are newer than its read, not than the
    first mark: their wait starts there, so a busy wing past max wait is not
    recomputed back to back."""
    import time

    hallways_mod.mark_wing_dirty("w", now=1.0)  # long overdue
    marked_again = []

    def mine_lands_mid_read():
        if not marked_again:
            marked_again.append(hallways_mod.mark_wing_dirty("w"))

    before = time.time()
    hallways_mod.compute_hallways_for_wing("w", col=_collection(_drawers(), mine_lands_mid_read))

    (mark,) = _dirty("w")
    assert mark["first_marked_at"] >= before - 1


def test_a_deferred_cli_mine_with_no_daemon_recomputes_inline(convo_palace, monkeypatch, capsys):
    """Daemons are opt-in: with none serving the palace a deferral would sit in a
    queue nobody reads, so the mine recomputes inline instead."""
    convos, palace = convo_palace
    monkeypatch.setenv("MEMPALACE_HALLWAYS_DEFER", "always")
    _cli(
        monkeypatch,
        "--palace",
        str(palace),
        "mine",
        str(convos),
        "--mode",
        "convos",
        "--wing",
        "chat",
    )

    config = _palace_config(palace)
    assert _pairs(hallways_mod.list_hallways("chat", config=config))
    assert hallways_mod.dirty_wings(config) == []
    assert _queued_hallway_jobs(palace) == []


def test_a_deferred_mine_defers_while_a_daemon_serves_the_palace(convo_palace, monkeypatch):
    from mempalace import daemon_queue
    from mempalace.convo_miner import mine_convos

    convos, palace = convo_palace
    monkeypatch.setattr(daemon_queue, "probe_daemon_health", lambda *a, **k: True)
    mine_convos(str(convos), str(palace), wing="chat", hallways="defer")

    assert [d["wing"] for d in hallways_mod.dirty_wings(_palace_config(palace))] == ["chat"]
    assert len(_queued_hallway_jobs(palace)) == 1


def test_with_no_daemon_a_mine_runs_the_marks_a_daemon_left(convo_palace, monkeypatch):
    """A daemon deferred a wing and was then turned off: the next direct mine,
    of any wing, runs that recompute once it is overdue."""
    convos, palace = convo_palace
    config = _palace_config(palace)
    from mempalace.convo_miner import mine_convos

    monkeypatch.setenv("MEMPALACE_HALLWAYS_DEFER", "never")
    mine_convos(str(convos), str(palace), wing="left")
    expected = _pairs(hallways_mod.list_hallways("left", config=config))
    with hallways_mod._writing(config) as conn:  # the daemon's deferred mine
        conn.execute("DELETE FROM hallways WHERE wing = 'left'")
    hallways_mod.mark_wing_dirty("left", config, now=1.0)
    monkeypatch.delenv("MEMPALACE_HALLWAYS_DEFER")

    other = convos.parent / "other"
    other.mkdir()
    (other / "x.txt").write_text(_EXCHANGE * 2, encoding="utf-8")
    _cli(monkeypatch, "--palace", str(palace), "mine", str(other), "--mode", "convos")

    assert hallways_mod.dirty_wings(config) == []
    assert _pairs(hallways_mod.list_hallways("left", config=config)) == expected and expected


def test_a_failed_recompute_is_retried_after_a_pause_not_at_once(
    convo_palace, fast_refresh, monkeypatch
):
    """An overdue mark whose recompute keeps failing must not spin the daemon."""
    import time

    from mempalace import hallway_refresh
    from mempalace.convo_miner import mine_convos

    convos, palace = convo_palace
    monkeypatch.setattr(hallway_refresh, "_daemon_serves", lambda *a, **k: True)
    mine_convos(str(convos), str(palace), wing="chat", hallways="defer")
    monkeypatch.setenv("MEMPALACE_HALLWAYS_MAX_WAIT_SECONDS", "0")
    attempts = []
    monkeypatch.setattr(
        hallway_refresh,
        "run_refresh",
        lambda payload: attempts.append(1) or {"success": False, "error": "unreadable"},
    )

    _run_daemon_until(palace, lambda rt: attempts)
    time.sleep(1.0)

    assert len(attempts) == 1
    (retry,) = [j for j in _queued_hallway_jobs(palace) if j.state == "queued"]
    assert retry.run_after >= time.time() + 30
