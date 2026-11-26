"""The daemon as the palace's single writer, under the failures real use brings.

Each test drives one hazard deterministically -- an injected clock, a
monkeypatched seam, or a child crash placed at an exact storage call -- rather
than sleeping and hoping a race lines up:

* a queue written by an older daemon, migrated with a crashed run still in it;
* hooks in separate processes enqueuing at the same moment;
* a run cut short while newer events for its source arrive.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from mempalace import daemon, daemon_queue


class _Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv(daemon_queue.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    return str(palace)


def _wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


# --- Queue migration ---------------------------------------------------------

# The queue schema exactly as develop's daemon creates it (WAL, the
# ``idx_jobs_dedupe_active`` index over queued *and* running rows).
_DEVELOP_SCHEMA = """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        dedupe_key TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        result_json TEXT,
        error_json TEXT,
        attempts INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, priority);
    CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key, state);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe_active
        ON jobs(dedupe_key) WHERE state IN ('queued', 'running');
"""


def _develop_queue(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DEVELOP_SCHEMA)
    conn.executemany(
        "INSERT INTO jobs (id, kind, payload_json, state, priority, dedupe_key, created_at,"
        " started_at, attempts) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_develop_queue_with_a_crashed_run_migrates_and_runs_the_latest_payload_once(tmp_path):
    """A develop daemon died mid-mine of K, leaving K ``running``. After the
    upgrade a hook's event for K must queue beside it (develop's index forbade
    that), the stale run must give way to it, and K's newest payload must run
    exactly once -- with every other row kept."""
    path = tmp_path / "queue.sqlite3"
    _develop_queue(
        path,
        [
            (
                "crashed",
                "mine",
                '{"rev": 1}',
                "running",
                "K",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:01",
                1,
            ),
            ("waiting", "mine", '{"other": 1}', "queued", "O", "2026-01-01T00:00:02", None, 0),
            (
                "plain",
                "diary_write",
                '{"entry": "x"}',
                "queued",
                None,
                "2026-01-01T00:00:03",
                None,
                0,
            ),
        ],
    )
    clock = _Clock()
    hook = daemon.QueueStore(path, clock=clock, timeout=0.3)  # migrates
    assert {j.id: j.state for j in hook.list(10)} == {
        "crashed": "running",
        "waiting": "queued",
        "plain": "queued",
    }
    first = hook.enqueue("mine", {"rev": 2}, dedupe_key="K", settle_seconds=5, max_wait_seconds=60)
    latest = hook.enqueue("mine", {"rev": 3}, dedupe_key="K", settle_seconds=5, max_wait_seconds=60)
    assert latest.id == first.id != "crashed"

    daemon_store = daemon.QueueStore(path, clock=clock)
    # While the crashed row still counts as running, the rerun of K waits.
    clock.t += 10
    claimed = [daemon_store.claim_next().id, daemon_store.claim_next().id]
    assert set(claimed) == {"waiting", "plain"}
    assert daemon_store.claim_next() is None
    for job_id in claimed:
        daemon_store.finish(job_id, state="succeeded", result={"success": True})

    # A daemon start recovers the crashed run: the queued rerun supersedes it.
    assert daemon_store.recover_running() == 0
    crashed = daemon_store.get("crashed")
    assert crashed.state == "cancelled"
    assert crashed.error["error_class"] == daemon.SUPERSEDED_ERROR_CLASS

    rerun = daemon_store.claim_next()
    assert (rerun.id, rerun.payload) == (latest.id, {"rev": 3})
    daemon_store.finish(rerun.id, state="succeeded", result={"success": True})
    assert daemon_store.claim_next() is None
    assert len(daemon_store.list(10)) == 4, "no row was dropped"


def test_develop_queue_crashed_run_alone_is_requeued_not_lost(tmp_path):
    path = tmp_path / "queue.sqlite3"
    _develop_queue(
        path,
        [
            (
                "crashed",
                "mine",
                '{"rev": 1}',
                "running",
                "K",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:01",
                1,
            )
        ],
    )
    store = daemon.QueueStore(path)
    assert store.recover_running() == 1
    job = store.claim_next()
    assert (job.id, job.payload, job.attempts) == ("crashed", {"rev": 1}, 2)


# --- Cross-process enqueue races ----------------------------------------------

_HOOK_PROCESS = textwrap.dedent(
    """
    import os, sys, types
    from mempalace import daemon_queue, hooks_cli
    # A real daemon would start here; record the spawn instead.
    def fake_popen(cmd, **kwargs):
        with open(os.environ["SPAWN_LOG"], "a") as fh:
            fh.write(f"{os.getpid()}\\n")
        # The pid of a process that stays up, as a starting daemon would.
        return types.SimpleNamespace(pid=int(os.environ["DAEMON_STANDIN_PID"]))
    daemon_queue.subprocess.Popen = fake_popen
    print("ready", flush=True)
    sys.stdin.readline()
    with hooks_cli._hook_trigger("stop"), hooks_cli._hook_write_routing_context():
        job = hooks_cli._submit_daemon_job(
            "mine", {"source": "/t", "mode": "convos", "rev": int(sys.argv[1])}, dedupe_key="K"
        )
    print(job["id"], flush=True)
    """
)


@pytest.mark.parametrize("schema", ["fresh", "develop"])
def test_hooks_in_separate_processes_enqueue_one_job_and_spawn_one_daemon(tmp_path, state, schema):
    """Eight Stop hooks released at once, against a queue that does not exist
    yet or one develop wrote: every hook succeeds inside its busy timeout,
    they share one queued job holding the last payload written, and none
    starts a daemon."""
    qpath = daemon_queue.queue_path(state)
    qpath.parent.mkdir(parents=True, exist_ok=True)
    if schema == "develop":
        _develop_queue(qpath, [])
    spawn_log = tmp_path / "spawns.log"
    standin = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    env = {
        **os.environ,
        "MEMPALACE_PALACE_PATH": state,
        "MEMPALACE_HOOK_WRITE_ROUTING": "require",
        "SPAWN_LOG": str(spawn_log),
        "DAEMON_STANDIN_PID": str(standin.pid),
    }
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _HOOK_PROCESS, str(i)],
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(8)
    ]
    try:
        for proc in procs:
            assert proc.stdout.readline().strip() == "ready", proc.stderr.read()
        for proc in procs:
            proc.stdin.write("go\n")
            proc.stdin.flush()
        outs = [proc.communicate(timeout=60) for proc in procs]
    finally:
        for proc in [*procs, standin]:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert all(proc.returncode == 0 for proc in procs), [err for _, err in outs]
    ids = {out.strip() for out, _ in outs}
    assert len(ids) == 1, ids
    store = daemon_queue.QueueStore(qpath)
    queued = store.list(10)
    assert [j.state for j in queued] == ["queued"]
    assert queued[0].payload["rev"] in range(8)
    assert not spawn_log.exists(), "a hook started a daemon"


# --- Running job plus one pending rerun ----------------------------------------


def test_rerun_keeps_the_latest_payload_through_retry_and_supersede(tmp_path):
    """A source mined while new events arrive: the run in flight and its one
    pending rerun never lose the newest payload, whether the run succeeds,
    is cut short and retried, or is interrupted by a shutdown."""
    clock = _Clock()
    store = daemon.QueueStore(tmp_path / "q.sqlite3", clock=clock)
    store.enqueue("mine", {"rev": 1}, dedupe_key="K")
    running = store.claim_next()

    # A cut-short run with no rerun waiting is retried later, payload intact.
    assert store.retry(running.id, claimed_started_at=running.started_at, delay_seconds=30)
    assert store.claim_next() is None, "held back by the retry delay"
    clock.t += 30
    running = store.claim_next()
    assert (running.payload, running.attempts) == ({"rev": 1}, 2)

    # Events during the run coalesce into one rerun with the newest payload.
    for rev in (2, 3, 4):
        store.enqueue("mine", {"rev": rev}, dedupe_key="K")
    assert store.claim_next() is None, "the rerun waits for its running sibling"

    # The run is cut short: the queued rerun supersedes it instead of a retry.
    assert not store.retry(running.id, claimed_started_at=running.started_at, delay_seconds=30)
    assert store.get(running.id).error["error_class"] == daemon.SUPERSEDED_ERROR_CLASS
    rerun = store.claim_next()
    assert rerun.payload == {"rev": 4}

    # A late retry from an older claim cannot touch the newer claim.
    assert not store.retry(rerun.id, claimed_started_at="1999-01-01", delay_seconds=0)
    assert store.get(rerun.id).state == "running"
    store.finish(rerun.id, state="succeeded", result={"success": True})
    assert store.counts() == {"cancelled": 1, "succeeded": 1}
