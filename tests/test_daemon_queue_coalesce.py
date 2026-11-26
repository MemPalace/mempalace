"""Queue coalescing, debounce, and schema migration (#3058).

The queue is the hand-off between hooks and the single-writer daemon, so the
behaviour that matters is observable through ``QueueStore``'s own API:
what ``enqueue`` returns, what ``claim_next`` hands out and when.
"""

from __future__ import annotations

import sqlite3

import pytest

from mempalace import daemon


class _Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def store(tmp_path):
    clock = _Clock()
    s = daemon.QueueStore(tmp_path / "queue.sqlite3", clock=clock)
    s.test_clock = clock
    return s


def test_enqueue_while_running_queues_one_rerun_with_the_latest_payload(store):
    first = store.enqueue("mine", {"rev": 1}, dedupe_key="src")
    running = store.claim_next()
    assert running.id == first.id

    rerun = store.enqueue("mine", {"rev": 2}, dedupe_key="src")
    again = store.enqueue("mine", {"rev": 3}, dedupe_key="src")

    assert rerun.id != first.id, "an event during a run must not vanish into the running job"
    assert again.id == rerun.id, "further events coalesce into the one pending rerun"
    assert store.get(rerun.id).payload == {"rev": 3}
    assert store.counts() == {"running": 1, "queued": 1}


def test_pending_rerun_waits_for_the_running_job_of_the_same_source(store):
    store.enqueue("mine", {"rev": 1}, dedupe_key="src")
    running = store.claim_next()
    store.enqueue("mine", {"rev": 2}, dedupe_key="src")
    other = store.enqueue("mine", {"rev": 1}, dedupe_key="other")

    # Same source is never processed twice at once; unrelated work is not blocked.
    assert store.claim_next().id == other.id
    assert store.claim_next() is None

    store.finish(running.id, state="succeeded", result={"success": True})
    rerun = store.claim_next()
    assert rerun.payload == {"rev": 2}


def test_settle_window_debounces_a_burst_into_one_run(store):
    clock = store.test_clock
    t0 = clock.t
    job = store.enqueue("mine", {"n": 1}, dedupe_key="s", settle_seconds=120, max_wait_seconds=900)
    assert job.run_after == pytest.approx(t0 + 120)

    clock.t = t0 + 60
    assert store.claim_next() is None, "still inside the settle window"
    store.enqueue("mine", {"n": 2}, dedupe_key="s", settle_seconds=120, max_wait_seconds=900)
    clock.t = t0 + 179
    assert store.claim_next() is None, "the second event pushed run_after out"
    clock.t = t0 + 180
    claimed = store.claim_next()
    assert claimed.id == job.id and claimed.payload == {"n": 2}


def test_steady_event_stream_still_runs_within_max_wait(store):
    clock = store.test_clock
    t0 = clock.t
    for step in range(0, 1000, 100):
        clock.t = t0 + step
        job = store.enqueue(
            "mine", {"step": step}, dedupe_key="s", settle_seconds=120, max_wait_seconds=900
        )
    # Capped at the first enqueue plus max_wait, however many events follow.
    assert job.run_after == pytest.approx(t0 + 900)
    clock.t = t0 + 900
    assert store.claim_next().payload == {"step": 900}


def test_immediate_trigger_is_not_delayed_by_a_later_debounced_one(store):
    clock = store.test_clock
    t0 = clock.t
    store.enqueue("mine", {"from": "precompact"}, dedupe_key="s", settle_seconds=0)
    clock.t = t0 + 1
    job = store.enqueue(
        "mine", {"from": "stop"}, dedupe_key="s", settle_seconds=120, max_wait_seconds=900
    )
    assert job.run_after == pytest.approx(t0)
    assert store.claim_next().payload == {"from": "stop"}


def test_immediate_trigger_cuts_a_pending_debounce_short(store):
    clock = store.test_clock
    t0 = clock.t
    store.enqueue(
        "mine", {"from": "stop"}, dedupe_key="s", settle_seconds=120, max_wait_seconds=900
    )
    clock.t = t0 + 5
    store.enqueue("mine", {"from": "session_end"}, dedupe_key="s", settle_seconds=0)
    assert store.claim_next().payload == {"from": "session_end"}


def test_recover_running_supersedes_a_crashed_run_that_already_has_a_rerun(store):
    store.enqueue("mine", {"rev": 1}, dedupe_key="src")
    crashed = store.claim_next()
    rerun = store.enqueue("mine", {"rev": 2}, dedupe_key="src")

    # Daemon died mid-run: restart must not trip the unique index.
    assert store.recover_running() == 0
    closed = store.get(crashed.id)
    assert closed.state == "cancelled"
    assert closed.error["error_class"] == daemon.SUPERSEDED_ERROR_CLASS
    assert store.claim_next().id == rerun.id


def test_defer_supersedes_a_refused_run_that_already_has_a_rerun(store):
    store.enqueue("mine", {"rev": 1}, dedupe_key="src")
    refused = store.claim_next()
    rerun = store.enqueue("mine", {"rev": 2}, dedupe_key="src")

    out = store.defer(refused.id, claimed_started_at=refused.started_at)

    assert out.state == "cancelled"
    assert store.get(rerun.id).state == "queued"


def _old_schema_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
            state TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, dedupe_key TEXT,
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            result_json TEXT, error_json TEXT, attempts INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_jobs_state ON jobs(state, priority);
        CREATE INDEX idx_jobs_dedupe ON jobs(dedupe_key, state);
        CREATE UNIQUE INDEX idx_jobs_dedupe_active
            ON jobs(dedupe_key) WHERE state IN ('queued', 'running');
        INSERT INTO jobs VALUES ('q1','mine','{"a":1}','queued',0,'A',
            '2026-01-01T00:00:00',NULL,NULL,NULL,NULL,0);
        INSERT INTO jobs VALUES ('r1','mine','{"b":1}','running',0,'B',
            '2026-01-01T00:00:01','2026-01-01T00:00:02',NULL,NULL,NULL,1);
        INSERT INTO jobs VALUES ('d1','mine','{"c":1}','succeeded',0,'C',
            '2026-01-01T00:00:00','2026-01-01T00:00:00','2026-01-01T00:00:03','{}',NULL,1);
        """
    )
    conn.commit()
    conn.close()


def test_existing_queue_db_migrates_in_place_without_losing_jobs(tmp_path):
    path = tmp_path / "queue.sqlite3"
    _old_schema_db(path)

    store = daemon.QueueStore(path)

    assert {j.id: j.state for j in store.list(10)} == {
        "q1": "queued",
        "r1": "running",
        "d1": "succeeded",
    }
    conn = sqlite3.connect(str(path))
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert "idx_jobs_dedupe_active" not in indexes
    assert "idx_jobs_dedupe_queued" in indexes
    assert version == daemon.QUEUE_SCHEMA_VERSION

    # The legacy queued row (no run_after) is claimable at once.
    assert store.claim_next().id == "q1"
    # The legacy running row now gets a pending rerun instead of swallowing the event.
    rerun = store.enqueue("mine", {"b": 2}, dedupe_key="B")
    assert rerun.id != "r1"
    # Re-opening a migrated DB is a no-op.
    daemon.QueueStore(path)
    assert store.get(rerun.id).state == "queued"


def test_debounce_defaults_follow_the_ticket(tmp_path, monkeypatch):
    from mempalace.config import MempalaceConfig

    monkeypatch.delenv("MEMPALACE_DAEMON_STOP_SETTLE_SECONDS", raising=False)
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.daemon_debounce("stop") == (120.0, 900.0)
    assert cfg.daemon_debounce("precompact") == (0.0, 0.0)
    assert cfg.daemon_debounce("session_end") == (0.0, 0.0)
    assert cfg.daemon_debounce("unknown") == (0.0, 0.0)


def test_debounce_is_configurable_by_file_and_env(tmp_path, monkeypatch):
    import json

    from mempalace.config import MempalaceConfig

    (tmp_path / "config.json").write_text(
        json.dumps(
            {"daemon": {"debounce": {"stop": {"settle_seconds": 30, "max_wait_seconds": "bad"}}}}
        )
    )
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.daemon_debounce("stop") == (30.0, 900.0)
    monkeypatch.setenv("MEMPALACE_DAEMON_STOP_SETTLE_SECONDS", "5")
    assert cfg.daemon_debounce("stop") == (5.0, 900.0)


def test_stop_hook_mine_is_queued_with_the_stop_debounce(tmp_path, monkeypatch):
    from mempalace import daemon_queue, hooks_cli

    palace = tmp_path / "palace"
    palace.mkdir()
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace))
    monkeypatch.setenv(daemon_queue.STATE_ROOT_ENV, str(tmp_path / "state"))
    monkeypatch.delenv("MEMPALACE_DAEMON_STOP_SETTLE_SECONDS", raising=False)
    monkeypatch.setattr(hooks_cli, "_ensure_daemon_started", lambda _p: None)

    with hooks_cli._hook_trigger("stop"):
        stop = hooks_cli._submit_daemon_job("mine", {"source": "/x"}, dedupe_key="stop-k")
        diary = hooks_cli._submit_daemon_job("diary_write", {"entry": "e"})
    with hooks_cli._hook_trigger("precompact"):
        pre = hooks_cli._submit_daemon_job("mine", {"source": "/y"}, dedupe_key="pre-k")

    store = daemon_queue.QueueStore(daemon_queue.queue_path(str(palace)))
    stop_job, diary_job, pre_job = (store.get(j["id"]) for j in (stop, diary, pre))
    created = stop_job.deadline - 900
    assert stop_job.run_after == pytest.approx(created + 120)
    assert pre_job.run_after == pytest.approx(pre_job.deadline)
    assert diary_job.run_after == pytest.approx(diary_job.deadline), "diary is never debounced"
    # The daemon, never the client, decides which palace a job writes to.
    assert stop_job.payload["palace_path"] == daemon_queue.canonical_palace_path(str(palace))


def test_claim_rechecks_debounce_after_select(store, monkeypatch):
    import contextlib

    hook = daemon.QueueStore(store.path, clock=store.clock)
    job = hook.enqueue("mine", {"rev": 1}, dedupe_key="k", settle_seconds=5, max_wait_seconds=60)
    store.test_clock.t = job.run_after
    connect = store._connect
    updates = []

    @contextlib.contextmanager
    def interleave():
        with connect() as conn:

            def before_update(sql):
                if "SET state = 'running'" in sql:
                    updates.append(
                        hook.enqueue(
                            "mine",
                            {"rev": 2},
                            dedupe_key="k",
                            settle_seconds=5,
                            max_wait_seconds=60,
                        )
                    )

            conn.set_trace_callback(before_update)
            yield conn

    with monkeypatch.context() as m:
        m.setattr(store, "_connect", interleave)
        claimed = store.claim_next()
    assert len(updates) == 1 and updates[0].run_after > store.clock()
    assert claimed is None
    assert store.get(job.id).attempts == 0
    store.test_clock.t = updates[0].run_after
    assert store.claim_next().payload == {"rev": 2}


def test_failed_migration_rolls_back_schema(tmp_path, monkeypatch):
    import contextlib

    path = tmp_path / "queue.sqlite3"
    _old_schema_db(path)
    connect = daemon.QueueStore._connect

    @contextlib.contextmanager
    def refuse_drop(store):
        with connect(store) as conn:
            conn.set_authorizer(
                lambda action, *_: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_DROP_INDEX
                    else sqlite3.SQLITE_OK
                )
            )
            yield conn

    with monkeypatch.context() as m:
        m.setattr(daemon.QueueStore, "_connect", refuse_drop)
        with pytest.raises(sqlite3.DatabaseError):
            daemon.QueueStore(path)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert "run_after" not in [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
    assert len(daemon.QueueStore(path).list()) == 3


def test_debounced_hook_preserves_legacy_rows_immediate_schedule(tmp_path):
    path = tmp_path / "queue.sqlite3"
    _old_schema_db(path)
    store = daemon.QueueStore(path, clock=_Clock())
    store.enqueue("mine", {"rev": 2}, dedupe_key="A", settle_seconds=120)
    assert store.claim_next().payload == {"rev": 2}


def test_concurrent_processes_migrate_and_enqueue_a_single_rerun(tmp_path):
    import subprocess
    import sys
    import textwrap

    path = tmp_path / "queue.sqlite3"
    _old_schema_db(path)
    script = textwrap.dedent("""
        import sys
        from pathlib import Path
        from mempalace.daemon_queue import QueueStore
        print("ready", flush=True)
        input()
        store = QueueStore(Path(sys.argv[1]))
        job = store.enqueue("mine", {"rev": int(sys.argv[2])}, dedupe_key="B")
        print(job.id, flush=True)
    """)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path), str(i)],
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        for i in range(4)
    ]
    try:
        for proc in procs:
            assert proc.stdout.readline().strip() == "ready"
        for proc in procs:
            proc.stdin.write("\n")
            proc.stdin.flush()
        ids = {proc.communicate(timeout=10)[0].strip() for proc in procs}
        assert all(proc.returncode == 0 for proc in procs)
        assert len(ids) == 1
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    store = daemon.QueueStore(path)
    assert store.get("r1").state == "running"
    last = store.enqueue("mine", {"rev": 99}, dedupe_key="B")
    assert last.id in ids
    store.finish("r1", state="succeeded")
    assert store.claim_next().id == "q1"
    assert store.claim_next().payload == {"rev": 99}
