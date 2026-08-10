import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from _chroma_palace_helper import make_minimal_chroma_sqlite

from mempalace import daemon
from mempalace import service

_LOCK_CONTENDER = """
from mempalace.palace import MineAlreadyRunning, mine_palace_lock
import sys
try:
    with mine_palace_lock(sys.argv[1]):
        raise SystemExit(0)
except MineAlreadyRunning:
    raise SystemExit(23)
"""

# POSIX file-mode bits (0600/0700) are not representable on Windows: os.chmod
# can only toggle the read-only attribute, so a "private" file still reports
# 0o666. The daemon relies on the user-profile directory ACLs for privacy
# there, so the owner-only assertions only make sense on POSIX.
_posix_only_perms = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX 0600/0700 file-mode bits are not representable on Windows (ACL-based privacy)",
)

# Env keys run_server mutates from its background thread, plus umask. If a
# lifecycle test times out before the server comes up, run_server's finally
# never runs and those mutations leak into the rest of the suite — every later
# test that reads MempalaceConfig().palace_path sees a stale deleted tmp path and
# fails (the 60+ test cascade seen on slow CI runners). The fixtures below force a
# clean baseline around every daemon test so a leaked thread can't poison the
# process for tests/test_mcp_server.py and friends (which have no such guard).
_LEAK_ENV_KEYS = ("MEMPALACE_PALACE_PATH", "MEMPALACE_BACKEND", "MEMPALACE_BACKEND_EXPLICIT")


@pytest.fixture(scope="module")
def _clean_env_snapshot():
    """Capture the true pre-suite values once, before any daemon test runs."""
    return {key: os.environ.get(key) for key in _LEAK_ENV_KEYS}


@pytest.fixture(autouse=True)
def _isolate_process_global_state(_clean_env_snapshot):
    """Restore the process-global env + umask to the pre-suite baseline after every
    daemon test, even if a leaked run_server thread is still holding them mutated.
    """
    prev_umask = os.umask(0o022)
    os.umask(prev_umask)  # read current umask without changing it
    yield
    for key, value in _clean_env_snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    os.umask(prev_umask)


def _raise_not_ready(*a, **kw):
    """Stand-in for DaemonClient when the spawned daemon must never come up."""
    raise daemon.DaemonError("not ready")


def test_prune_terminal_drops_old_terminal_jobs_keeps_active(tmp_path, monkeypatch):
    """Terminal jobs older than the retention window are pruned; queued/running
    and fresh terminal jobs are untouched. Bounded queue growth for the DB that
    holds verbatim payloads."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    store = daemon.QueueStore(daemon.queue_path(str(palace)))

    old_term = store.enqueue("mine", {"source": "old"})
    store.finish(old_term.id, state="succeeded", result={"success": True})
    fresh_term = store.enqueue("mine", {"source": "fresh"})
    store.finish(fresh_term.id, state="succeeded", result={"success": True})
    queued = store.enqueue("mine", {"source": "queued"})

    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with store._lock, store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET finished_at = ? WHERE id = ?",
            (cutoff, old_term.id),
        )

    pruned = store.prune_terminal(older_than_days=7)
    assert pruned == 1
    # The old terminal job is gone; the fresh terminal and queued jobs survive.
    with pytest.raises(daemon.DaemonError):
        store.get(old_term.id)
    assert store.get(fresh_term.id).state == "succeeded"
    assert store.get(queued.id).state == "queued"


def test_queue_dedupes_and_recovers_running_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()

    store = daemon.QueueStore(daemon.queue_path(str(palace)))
    first = store.enqueue("mine", {"source": "a"}, dedupe_key="same")
    second = store.enqueue("mine", {"source": "a"}, dedupe_key="same")

    assert second.id == first.id

    claimed = store.claim_next()
    assert claimed.id == first.id
    assert claimed.state == "running"

    recovered = store.recover_running()
    assert recovered == 1
    assert store.get(first.id).state == "queued"


def test_daemon_http_lifecycle_executes_job(tmp_path, monkeypatch):
    calls = []

    def fake_execute(kind, payload):
        calls.append((kind, payload))
        return {"success": True, "exit_code": 0, "stdout": "done\n"}

    client, thread, palace, holders = _start_server(tmp_path, monkeypatch, fake_execute)

    health = client.health()
    assert health["ok"] is True
    assert health["palace_path"] == daemon.canonical_palace_path(str(palace))

    job = client.submit("mine", {"source": "src"}, dedupe_key="job")
    finished = client.wait(job["id"], timeout=5)

    assert finished["state"] == "succeeded"
    assert finished["result"]["stdout"] == "done\n"
    assert calls == [("mine", {"source": "src", "palace_path": str(palace.resolve())})]

    _stop_server(client, thread, holders)


def test_daemon_holds_local_backend_writer_lease_for_lifetime(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    client, thread, palace, holders = _start_server(
        tmp_path, monkeypatch, lambda kind, payload: {"success": True, "exit_code": 0}
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", _LOCK_CONTENDER, str(palace)],
            check=False,
            env=os.environ.copy(),
            timeout=10,
        )
        assert result.returncode == 23
    finally:
        _stop_server(client, thread, holders)

    released = subprocess.run(
        [sys.executable, "-c", _LOCK_CONTENDER, str(palace)],
        check=False,
        env=os.environ.copy(),
        timeout=10,
    )
    assert released.returncode == 0


def test_submit_job_uses_client_and_waits(monkeypatch, tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()

    class DummyClient:
        def __init__(self):
            self.submitted = None

        def submit(self, kind, payload, dedupe_key=None, priority=0):
            self.submitted = (kind, payload, dedupe_key, priority)
            return {"id": "job-1", "state": "queued"}

        def wait(self, job_id, timeout=daemon.DEFAULT_WAIT_TIMEOUT, stop_on_lock_deferral=False):
            assert job_id == "job-1"
            return {
                "id": "job-1",
                "state": "succeeded",
                "result": {"success": True, "exit_code": 0},
            }

    dummy = DummyClient()
    monkeypatch.setattr(daemon, "ensure_client", lambda *a, **kw: dummy)

    job = daemon.submit_job(
        "mine",
        {"source": "src"},
        palace_path=str(palace),
        dedupe_key="dedupe",
        wait=True,
    )

    assert job["state"] == "succeeded"
    assert dummy.submitted[0] == "mine"
    # palace_path is overridden (not trusted from the payload), never appended.
    assert dummy.submitted[1]["palace_path"] == daemon.canonical_palace_path(str(palace))
    assert dummy.submitted[2] == "dedupe"


def test_service_tool_classification():
    assert service.classify_tool("mempalace_search") == "read"
    assert service.classify_tool("mempalace_add_drawer") == "write"
    assert service.classify_tool("mempalace_checkpoint") == "write"
    assert service.classify_tool("mempalace_delete_by_source") == "write"
    assert service.classify_tool("mempalace_mine") == "maintenance"
    assert service.classify_tool("unknown") == "unknown"


# --- helpers for HTTP-lifecycle tests ---


def _capture_httpd(monkeypatch):
    """Capture the httpd instance run_server creates.

    run_server defines a local ``class _Server(ThreadingHTTPServer)``; by
    monkeypatching ``daemon.ThreadingHTTPServer`` before run_server runs, that
    subclass inherits from a capturing base that records each instance. The
    httpd can then be force-stopped from the test thread (see _stop_server) so a
    slow/failed ``client.shutdown()`` POST can never leave the server thread
    alive — which on Windows hangs the interpreter at process exit on an open
    listening socket.
    """
    holders: list = []
    base = daemon.ThreadingHTTPServer

    class _CapturingServer(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            holders.append(self)

    monkeypatch.setattr(daemon, "ThreadingHTTPServer", _CapturingServer)
    return holders


def _stop_server(client, thread, holders, *, join_timeout=5.0):
    """Shut the daemon down deterministically and assert the thread died.

    First try the normal path (POST /shutdown). If the server thread is still
    alive afterwards — the POST was slow, lost, or the drain overran the join —
    call httpd.shutdown() directly from this thread (stdlib-safe: it is a
    different thread than serve_forever) to force serve_forever to return, then
    re-join. The assert turns a leak into a visible failure instead of a silent
    interpreter-exit hang.
    """
    try:
        client.shutdown()
    except Exception:  # noqa: BLE001 - best-effort; we force-shutdown below
        pass
    thread.join(timeout=join_timeout)
    if thread.is_alive() and holders:
        httpd = holders[-1]
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        thread.join(timeout=join_timeout)
    assert not thread.is_alive(), "daemon server thread did not shut down"


def _start_server(tmp_path, monkeypatch, execute_fn):
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    monkeypatch.setattr(service, "execute_job", execute_fn)
    holders = _capture_httpd(monkeypatch)

    # Capture any exception run_server raises in its thread. Without this a
    # startup crash is invisible: the poll below would just spin for 30s and
    # fail with a bare ``assert client is not None`` giving no cause.
    server_error: list = []

    def _serve():
        try:
            daemon.run_server(palace_path=str(palace), port=0)
        except BaseException as exc:  # noqa: BLE001 - re-surfaced to the test thread
            server_error.append(exc)

    thread = threading.Thread(target=_serve, name="test-daemon-server", daemon=True)
    thread.start()
    client = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if server_error:
            raise AssertionError(f"run_server crashed during startup: {server_error[0]!r}")
        client = daemon.get_client_if_running(str(palace))
        if client is not None:
            break
        time.sleep(0.05)
    if client is None:
        raise AssertionError(
            "daemon did not become ready within 30s "
            f"(thread_alive={thread.is_alive()}, httpd_bound={bool(holders)}, "
            f"endpoint_exists={daemon.endpoint_path(str(palace)).exists()})"
        )
    return client, thread, palace, holders


# --- ship-blocker regressions ---


def test_systemexit_in_job_does_not_kill_worker(tmp_path, monkeypatch):
    """A SystemExit (BaseException, not Exception) must be caught, the job
    marked failed, and the worker kept alive for the next job. Regression for
    the critical worker-death bug."""
    state = {"first": True}

    def fake_execute(kind, payload):
        if state["first"]:
            state["first"] = False
            raise SystemExit("boom")
        return {"success": True, "exit_code": 0}

    client, thread, palace, holders = _start_server(tmp_path, monkeypatch, fake_execute)
    try:
        first = client.submit("mine", {"source": "src"})
        finished_first = client.wait(first["id"], timeout=5)
        assert finished_first["state"] == "failed"
        assert finished_first["error"]["error_class"] == "SystemExit"

        # Worker must still be alive — health reports it and a second job runs.
        assert client.health()["worker_alive"] is True
        second = client.submit("mine", {"source": "src2"})
        finished_second = client.wait(second["id"], timeout=5)
        assert finished_second["state"] == "succeeded"
    finally:
        _stop_server(client, thread, holders)


def test_shutdown_cancels_active_job(tmp_path, monkeypatch):
    """POST /shutdown must not leave an in-flight job 'running' for blind
    re-queue on next start. The worker is drained (bounded), then the active
    job is marked 'cancelled' so recover_running won't re-run it.

    In production the serve process exits immediately after run_server returns,
    killing the daemon worker thread before it can overwrite the cancelled
    state. The test mirrors that by asserting the cancelled state *before*
    releasing the blocked worker.
    """
    block = threading.Event()

    def fake_execute(kind, payload):
        # Simulate a long-running job that never finishes on its own.
        block.wait(30)
        return {"success": True, "exit_code": 0}

    monkeypatch.setattr(daemon, "SHUTDOWN_DRAIN_SECONDS", 0.2)
    client, thread, palace, holders = _start_server(tmp_path, monkeypatch, fake_execute)
    job = client.submit("mine", {"source": "src"}, dedupe_key="x")
    # Wait until the worker has claimed it (state flips to running).
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get_job(job["id"])["state"] == "running":
            break
        time.sleep(0.02)
    assert client.get_job(job["id"])["state"] == "running"

    _stop_server(client, thread, holders)

    # The interrupted job must be cancelled (terminal), not left running.
    store = daemon.QueueStore(daemon.queue_path(str(palace)))
    final = store.get(job["id"])
    assert final.state == "cancelled"
    # And recover_running must not re-queue a cancelled job.
    assert store.recover_running() == 0

    # The server thread is gone, but its timed-out worker is still executing.
    # Writer ownership must stay with that worker until it truly exits.
    contender = subprocess.run(
        [sys.executable, "-c", _LOCK_CONTENDER, str(palace)],
        check=False,
        env=os.environ.copy(),
        timeout=10,
    )
    assert contender.returncode == 23

    # Release the blocked worker so it (and the daemon thread) can exit.
    block.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        released = subprocess.run(
            [sys.executable, "-c", _LOCK_CONTENDER, str(palace)],
            check=False,
            env=os.environ.copy(),
            timeout=10,
        )
        if released.returncode == 0:
            break
        time.sleep(0.02)
    assert released.returncode == 0


def test_recover_running_dead_letters_exhausted_jobs(tmp_path, monkeypatch):
    """A job that has crashed MAX_ATTEMPTS times must be dead-lettered to
    'failed', not re-queued — non-idempotent kinds (diary_write) would
    otherwise duplicate verbatim content on every restart."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    store = daemon.QueueStore(daemon.queue_path(str(palace)))
    job = store.enqueue("diary_write", {"entry": "x"})
    # Simulate MAX_ATTEMPTS claims that each crashed (running, attempts=MAX).
    with store._lock, store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET state='running', attempts=? WHERE id=?",
            (daemon.MAX_ATTEMPTS, job.id),
        )

    recovered = store.recover_running()
    assert recovered == 0  # not re-queued
    final = store.get(job.id)
    assert final.state == "failed"
    assert final.attempts == daemon.MAX_ATTEMPTS


# --- #2014: a lease refusal is a deferral, not a failure ---


def test_lease_refusal_defers_job_and_runs_it_on_the_next_claim(tmp_path, monkeypatch):
    """A LockHeldByOtherProcess refusal means the palace write never landed, so
    re-running it cannot duplicate content. It must be deferred and picked up
    again, not dead-lettered. Regression for #2014, where a lock conflict ended
    terminal 'failed' and only ever ran again if some hook happened to re-submit
    equivalent work."""
    calls = {"n": 0}

    def fake_execute(kind, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "success": False,
                "error": "palace /p is held by PID 999 (mempalace-mcp)",
                "error_class": daemon.LOCK_REFUSAL_ERROR_CLASS,
                "exit_code": 1,
            }
        return {"success": True, "exit_code": 0}

    monkeypatch.setattr(daemon, "LOCK_DEFER_BACKOFF_SECONDS", 0.05)
    client, thread, palace, holders = _start_server(tmp_path, monkeypatch, fake_execute)
    try:
        job = client.submit("diary_write", {"entry": "verbatim"})
        finished = client.wait(job["id"], timeout=10)
        assert finished["state"] == "succeeded"
        assert calls["n"] == 2  # refused once, then executed for real
        # The refusal spent no attempt: only the successful claim counted.
        assert finished["attempts"] == 1
    finally:
        _stop_server(client, thread, holders)


def test_repeated_lease_refusal_never_exhausts_attempts(tmp_path, monkeypatch):
    """#2014's actual promise: a palace locked across many claims must never
    dead-letter the work. One refusal cannot show that -- MAX_ATTEMPTS is 3, so
    the job has to survive more refusals than that and still run."""
    calls = {"n": 0}
    refusals = daemon.MAX_ATTEMPTS + 2

    def fake_execute(kind, payload):
        calls["n"] += 1
        if calls["n"] <= refusals:
            return {
                "success": False,
                "error": "palace /p is held by PID 999",
                "error_class": daemon.LOCK_REFUSAL_ERROR_CLASS,
                "exit_code": 1,
            }
        return {"success": True, "exit_code": 0}

    monkeypatch.setattr(daemon, "LOCK_DEFER_BACKOFF_SECONDS", 0.02)
    client, thread, palace, holders = _start_server(tmp_path, monkeypatch, fake_execute)
    try:
        job = client.submit("mine", {"source": "src"})
        finished = client.wait(job["id"], timeout=20)
        assert finished["state"] == "succeeded"
        assert calls["n"] == refusals + 1
        assert finished["attempts"] == 1  # every refusal refunded its claim
    finally:
        _stop_server(client, thread, holders)


def test_wait_returns_a_lock_deferred_job_instead_of_waiting_out_the_holder(tmp_path, monkeypatch):
    """An interactive caller must not block until a deferred job goes terminal --
    it never will while the holder lives, so the default hour-long wait would
    strand the terminal behind a peer session. Background waiters keep waiting."""

    def fake_execute(kind, payload):
        return {
            "success": False,
            "error": "palace /p is held by PID 999 (mempalace-mcp)",
            "error_class": daemon.LOCK_REFUSAL_ERROR_CLASS,
            "exit_code": 1,
        }

    monkeypatch.setattr(daemon, "LOCK_DEFER_BACKOFF_SECONDS", 5.0)
    client, thread, palace, holders = _start_server(tmp_path, monkeypatch, fake_execute)
    try:
        job = client.submit("mine", {"source": "src"})
        parked = client.wait(job["id"], timeout=10, stop_on_lock_deferral=True)
        assert parked["state"] == "queued"  # returned early, not terminal
        assert daemon.job_deferred_by_lock(parked)
        assert "PID 999" in parked["error"]["message"]

        # Without the flag the same job is not terminal, so the wait times out
        # rather than returning -- the hang a naive deferral would introduce.
        with pytest.raises(daemon.DaemonError):
            client.wait(job["id"], timeout=0.5)
    finally:
        _stop_server(client, thread, holders)


def test_job_deferred_by_lock_ignores_ordinary_queued_and_failed_jobs(tmp_path, monkeypatch):
    """The predicate must not mistake a job merely awaiting its turn -- or a
    genuinely failed one -- for a lock deferral, or the CLI would bail out of a
    healthy queue."""
    assert not daemon.job_deferred_by_lock({"state": "queued", "error": None})
    assert not daemon.job_deferred_by_lock({"state": "queued", "error": {}})
    assert not daemon.job_deferred_by_lock(
        {"state": "failed", "error": {"error_class": daemon.LOCK_REFUSAL_ERROR_CLASS}}
    )
    assert not daemon.job_deferred_by_lock(
        {"state": "queued", "error": {"error_class": "ValueError"}}
    )
    assert daemon.job_deferred_by_lock(
        {"state": "queued", "error": {"error_class": daemon.LOCK_REFUSAL_ERROR_CLASS}}
    )


def test_claim_clears_a_stale_deferral_reason(tmp_path, monkeypatch):
    """The reason defer parks on a queued job describes the claim that ended.
    Leaving it would outlive its cause: the predicate would report a lock
    refusal for a job that later crashed, and recover_running's dead-letter
    (which COALESCEs error_json) would blame the lock instead of
    MaxAttemptsExceeded."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    store = daemon.QueueStore(daemon.queue_path(str(palace)))
    job = store.enqueue("diary_write", {"entry": "x"})

    store.claim_next()
    store.defer(job.id, error={"error_class": daemon.LOCK_REFUSAL_ERROR_CLASS, "message": "held"})
    assert daemon.job_deferred_by_lock(daemon.job_to_dict(store.get(job.id)))

    claimed = store.claim_next()
    assert claimed.error is None  # the refusal belonged to the previous claim

    # Now crash out at MAX_ATTEMPTS: the dead-letter must name the real cause.
    with store._lock, store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET state='running', attempts=? WHERE id=?",
            (daemon.MAX_ATTEMPTS, job.id),
        )
    store.recover_running()
    final = store.get(job.id)
    assert final.state == "failed"
    assert final.error["error_class"] == "MaxAttemptsExceeded"


def test_defer_records_why_the_job_went_back_to_the_queue(tmp_path, monkeypatch):
    """A job parked behind the palace lock must be distinguishable from one
    merely awaiting its turn.

    The reason is what job_deferred_by_lock keys on, and what the foreground CLI
    and the hook log quote back at the operator; without it a parked job and a
    job simply waiting its turn are the same row. (`daemon jobs` prints only
    id/state/kind/created_at, so it does not show this -- surfacing it there is
    a separate change.)"""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    store = daemon.QueueStore(daemon.queue_path(str(palace)))
    job = store.enqueue("mine", {"source": "src"})
    store.claim_next()

    deferred = store.defer(
        job.id,
        error={"error_class": daemon.LOCK_REFUSAL_ERROR_CLASS, "message": "held by PID 999"},
    )
    assert deferred.state == "queued"
    assert deferred.error["error_class"] == daemon.LOCK_REFUSAL_ERROR_CLASS
    assert "PID 999" in deferred.error["message"]


def test_invalid_lock_backoff_env_falls_back_to_the_default(monkeypatch):
    """Anything that is not a positive, finite number yields the default.

    A typo must not crash the daemon at import; 0 or a negative value would make
    the cooldown expire instantly and spin the worker against a lock it cannot
    take; and inf/nan are the sharp edges of float() -- inf parses fine and is
    > 0, so a bare positivity check would park the job until the daemon
    restarts, which is exactly the dropped work #2014 is about."""
    for bad in ("not-a-float", "-5", "0", "", "inf", "-inf", "nan"):
        monkeypatch.setenv("MEMPALACE_DAEMON_LOCK_BACKOFF_SECONDS", bad)
        assert daemon._lock_defer_backoff_seconds() == daemon._DEFAULT_LOCK_BACKOFF_SECONDS, bad

    monkeypatch.setenv("MEMPALACE_DAEMON_LOCK_BACKOFF_SECONDS", "0.5")
    assert daemon._lock_defer_backoff_seconds() == 0.5

    monkeypatch.delenv("MEMPALACE_DAEMON_LOCK_BACKOFF_SECONDS")
    assert daemon._lock_defer_backoff_seconds() == daemon._DEFAULT_LOCK_BACKOFF_SECONDS


def test_defer_returns_job_to_queued_without_spending_an_attempt(tmp_path, monkeypatch):
    """defer must undo claim_next's increment, so a palace held across many
    claims cannot walk a job to MAX_ATTEMPTS on work that never landed."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    store = daemon.QueueStore(daemon.queue_path(str(palace)))
    job = store.enqueue("mine", {"source": "src"})

    claimed = store.claim_next()
    assert claimed.attempts == 1
    assert claimed.state == "running"

    deferred = store.defer(job.id)
    assert deferred.state == "queued"
    assert deferred.attempts == 0
    assert deferred.started_at is None
    # Still claimable -- the work is held, not lost.
    assert store.claim_next().id == job.id


def test_defer_scoped_to_a_stale_claim_is_a_no_op(tmp_path, monkeypatch):
    """defer is scoped to the claim that was refused. If the row was meanwhile
    re-queued and re-claimed (recover_running on another daemon start), a late
    defer keyed to the old claim must not flip the newer claim back to 'queued'
    -- that would refund its attempt and re-run work whose outcome the newer
    claimant already owns."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    store = daemon.QueueStore(daemon.queue_path(str(palace)))
    job = store.enqueue("mine", {"source": "s"})

    # started_at is the claim token this test compares, and adjacent claims can
    # land inside one clock tick on hosts with a coarse wall clock (Windows
    # datetime.now() ticks at ~15.6ms before 3.13). Make every stamp unique so
    # the assertions exercise the guard, not the clock.
    real_now = daemon._now
    stamps = iter(range(10_000))
    monkeypatch.setattr(daemon, "_now", lambda: f"{real_now()}#{next(stamps):04d}")

    stale = store.claim_next()  # the claim that got the refusal
    deferred = store.defer(job.id, claimed_started_at=stale.started_at)
    assert deferred.state == "queued"  # scoping to one's own claim still defers

    fresh = store.claim_next()  # the row is claimed again behind the first claimant
    assert fresh.state == "running"

    late = store.defer(job.id, claimed_started_at=stale.started_at)
    assert late.state == "running", "a defer scoped to a stale claim must not fire"
    assert late.attempts == fresh.attempts


def test_defer_preserves_attempts_spent_on_earlier_real_executions(tmp_path, monkeypatch):
    """The refund undoes one claim, not the job's whole history. An execution
    that crashed left an unknown outcome and must keep costing its attempt --
    otherwise an alternating crash/refusal pattern would defeat MAX_ATTEMPTS and
    re-file verbatim content forever."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    store = daemon.QueueStore(daemon.queue_path(str(palace)))
    job = store.enqueue("diary_write", {"entry": "x"})

    store.claim_next()  # attempt 1: crashes mid-execution (outcome unknown)
    assert store.recover_running() == 1  # re-queued, attempt NOT refunded
    assert store.get(job.id).attempts == 1

    store.claim_next()  # attempt 2: refused the lock this time
    deferred = store.defer(job.id)
    assert deferred.attempts == 1  # only the refused claim was refunded, not the crash


def test_shutdown_while_a_job_is_cooling_leaves_it_queued(tmp_path, monkeypatch):
    """A job already deferred must survive shutdown as 'queued', not be swept
    into a terminal state and dropped.

    The worker does not hold a deferred job: it records the cooldown and moves
    on, so `finally` clears active_job_id and the drain (which joins the worker
    before reading it) finds nothing in flight to cancel. Pin that, because a
    shutdown that cancelled a deferred job would be #2014 again."""
    monkeypatch.setattr(daemon, "LOCK_DEFER_BACKOFF_SECONDS", 5.0)

    def fake_execute(kind, payload):
        return {
            "success": False,
            "error": "palace /p is held by PID 999",
            "error_class": daemon.LOCK_REFUSAL_ERROR_CLASS,
            "exit_code": 1,
        }

    client, thread, palace, holders = _start_server(tmp_path, monkeypatch, fake_execute)
    try:
        job = client.submit("diary_write", {"entry": "verbatim"})
        # Wait for the REFUSAL, not merely for 'queued': a job is queued the moment
        # it is submitted, so waiting on the state alone would pass this test before
        # any deferral had happened. The marker only appears once defer ran.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if daemon.job_deferred_by_lock(client.get_job(job["id"])):
                break
            time.sleep(0.02)
        assert daemon.job_deferred_by_lock(client.get_job(job["id"])), "never reached a deferral"
    finally:
        _stop_server(client, thread, holders)

    store = daemon.QueueStore(daemon.queue_path(str(palace)))
    final = store.get(job["id"])
    assert final.state == "queued", "a deferred job must not be cancelled by shutdown"
    assert store.claim_next().id == job["id"]  # still runnable on the next start


def test_defer_does_not_resurrect_a_cancelled_job(tmp_path, monkeypatch):
    """defer mirrors finish(only_if_running=True): once shutdown cancelled an
    in-flight job, a late deferral must not queue it for re-execution."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    store = daemon.QueueStore(daemon.queue_path(str(palace)))
    job = store.enqueue("diary_write", {"entry": "x"})
    store.claim_next()
    store.finish(job.id, state="cancelled", result={})

    after = store.defer(job.id)
    assert after.state == "cancelled"
    assert store.claim_next() is None


def test_claim_next_skips_excluded_jobs_without_binding_a_parameter_each(tmp_path, monkeypatch):
    """The cooling set must not be spliced into the SQL as one host parameter per
    job: SQLITE_MAX_VARIABLE_NUMBER defaults to 999 below SQLite 3.32 and this
    project still supports Python 3.9. Over the cap the query raises into the
    worker's catch-all, which retries against a set that only ages out on the
    cooldown, so the queue stalls in cooldown-long fits. It also has to keep
    picking the right row: the oldest queued job that is not excluded."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    store = daemon.QueueStore(daemon.queue_path(str(palace)))

    first = store.enqueue("mine", {"source": "first"})
    second = store.enqueue("mine", {"source": "second"})

    # Excluding the oldest hands back the next one, not None.
    claimed = store.claim_next(exclude={first.id})
    assert claimed is not None
    assert claimed.id == second.id

    # Excluding every queued job yields nothing rather than the wrong job.
    store.defer(second.id)
    assert store.claim_next(exclude={first.id, second.id}) is None

    if not hasattr(sqlite3.Connection, "setlimit"):  # setlimit is 3.11+
        pytest.skip("sqlite3.Connection.setlimit needed to pin the parameter cap")

    # Force the pre-3.32 cap of 999 rather than trusting whatever SQLite this
    # interpreter bundles: on a build with a large cap the old
    # `id NOT IN (?, ?, ...)` shape passes and this test would guard nothing.
    real_connect = sqlite3.connect

    def capped_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        return conn

    monkeypatch.setattr(sqlite3, "connect", capped_connect)

    huge = {f"cooling-{i:07d}" for i in range(5_000)}  # 5x the cap
    claimed = store.claim_next(exclude=huge)
    assert claimed is not None
    assert claimed.id == first.id  # priority DESC, created_at ASC still honoured

    # And priority still wins over age, with an exclusion in play.
    store.defer(first.id)
    urgent = store.enqueue("diary_write", {"entry": "x"}, priority=10)
    assert store.claim_next(exclude=huge).id == urgent.id


def test_submit_job_forwards_stop_on_lock_deferral_to_wait(monkeypatch):
    """submit_job is the only door the CLI and the hooks use, so if it drops the
    flag on the floor every caller silently goes back to waiting out a holder
    that may never let go. Nothing else in the suite covers the forwarding."""
    seen = {}

    class _Client:
        def submit(self, kind, payload, dedupe_key=None, priority=0):
            return {"id": "job-1", "state": "queued"}

        def wait(self, job_id, *, timeout=None, stop_on_lock_deferral=False):
            seen["stop_on_lock_deferral"] = stop_on_lock_deferral
            return {"id": job_id, "state": "queued"}

    monkeypatch.setattr(daemon, "ensure_client", lambda *a, **kw: _Client())

    daemon.submit_job("mine", {"source": "s"}, palace_path="/p", stop_on_lock_deferral=True)
    assert seen["stop_on_lock_deferral"] is True

    daemon.submit_job("mine", {"source": "s"}, palace_path="/p")
    assert seen["stop_on_lock_deferral"] is False  # default stays the old waiting behaviour


def test_a_deferred_job_does_not_stall_unrelated_queued_work(tmp_path, monkeypatch):
    """The worker is the only one, and claim_next hands back the oldest queued
    job -- which, after a refusal, is the deferred job itself. If the worker
    waited out the cooldown in line, that one job would hold the queue hostage
    for as long as the holder lived (the flock is held while the write runs, and
    a wedged write holds it indefinitely, #2024), and nothing else would run.
    The cooldown must skip the job, not block the loop."""
    monkeypatch.setattr(daemon, "LOCK_DEFER_BACKOFF_SECONDS", 30.0)
    ran = []

    def fake_execute(kind, payload):
        ran.append(kind)
        if kind == "mine":  # older, and refused for the whole test
            return {
                "success": False,
                "error": "palace /p is held by PID 999",
                "error_class": daemon.LOCK_REFUSAL_ERROR_CLASS,
                "exit_code": 1,
            }
        return {"success": True, "exit_code": 0}

    client, thread, palace, holders = _start_server(tmp_path, monkeypatch, fake_execute)
    try:
        blocked = client.submit("mine", {"source": "src"})
        unrelated = client.submit("diary_write", {"entry": "verbatim"})

        # The unrelated job must run despite a 30s cooldown on the older one.
        done = client.wait(unrelated["id"], timeout=10)
        assert done["state"] == "succeeded"
        assert "diary_write" in ran

        # And the blocked job is still held, not lost.
        parked = client.get_job(blocked["id"])
        assert parked["state"] == "queued"
        assert daemon.job_deferred_by_lock(parked)
    finally:
        _stop_server(client, thread, holders)


def test_a_job_queued_behind_a_deferred_one_gets_its_own_refusal_marker(tmp_path, monkeypatch):
    """A caller waiting on a job that is merely queued behind a deferred job has
    nothing to key on: the job was never claimed, so it carries no marker, and
    stop_on_lock_deferral cannot fire. It would wait out its full timeout and
    then report a failure that never happened -- #2014 through the back door.
    Because the cooldown skips the deferred job instead of blocking the worker,
    the second job is claimed, refused, and marked on its own."""
    monkeypatch.setattr(daemon, "LOCK_DEFER_BACKOFF_SECONDS", 30.0)

    def fake_execute(kind, payload):
        return {
            "success": False,
            "error": "palace /p is held by PID 999 (mempalace-mcp)",
            "error_class": daemon.LOCK_REFUSAL_ERROR_CLASS,
            "exit_code": 1,
        }

    client, thread, palace, holders = _start_server(tmp_path, monkeypatch, fake_execute)
    try:
        first = client.submit("mine", {"source": "a"})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if daemon.job_deferred_by_lock(client.get_job(first["id"])):
                break
            time.sleep(0.02)
        assert daemon.job_deferred_by_lock(client.get_job(first["id"]))

        # Submitted while the first job is cooling: it must still be claimed.
        second = client.submit("mine", {"source": "b"})
        parked = client.wait(second["id"], timeout=10, stop_on_lock_deferral=True)
        assert parked["state"] == "queued"
        assert daemon.job_deferred_by_lock(parked), "the CLI/hook short-circuit cannot fire"
        assert "PID 999" in parked["error"]["message"]
    finally:
        _stop_server(client, thread, holders)


def test_failure_that_is_not_a_lease_refusal_stays_terminal(tmp_path, monkeypatch):
    """Only a lease refusal defers. A genuine failure has an unknown outcome --
    the daemon may have written before dying -- which is exactly what MAX_ATTEMPTS
    guards, so it must stay terminal and never be blindly re-run."""

    def fake_execute(kind, payload):
        return {"success": False, "error": "boom", "error_class": "ValueError", "exit_code": 1}

    client, thread, palace, holders = _start_server(tmp_path, monkeypatch, fake_execute)
    try:
        job = client.submit("diary_write", {"entry": "x"})
        finished = client.wait(job["id"], timeout=10)
        assert finished["state"] == "failed"
        assert finished["error"]["message"] == "boom"
    finally:
        _stop_server(client, thread, holders)


def test_claim_next_does_not_reclaim_running_job(tmp_path, monkeypatch):
    """The conditional UPDATE (WHERE state='queued') means a job already
    flipped to 'running' cannot be claimed again — the cross-process
    double-execution guard."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    store = daemon.QueueStore(daemon.queue_path(str(palace)))
    job = store.enqueue("mine", {"source": "src"})
    first = store.claim_next()
    assert first.id == job.id
    # Manually re-mark it queued but leave a second claim attempt: claim_next
    # should still only ever return one running job per claim. After finishing
    # the first, the next claim returns None (queue empty).
    store.finish(first.id, state="succeeded", result={"success": True})
    assert store.claim_next() is None


@_posix_only_perms
def test_queue_db_file_is_owner_only(tmp_path, monkeypatch):
    """The queue DB holds verbatim payloads — it must be 0600, not the sqlite
    default 0644. Regression for the privacy-principle violation."""
    import os as _os

    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    store = daemon.QueueStore(daemon.queue_path(str(palace)))
    store.enqueue("diary_write", {"entry": "secret verbatim content"})
    mode = _os.stat(str(store.path)).st_mode & 0o777
    assert mode == 0o600, f"queue.sqlite3 is {oct(mode)}, expected 0600"


@_posix_only_perms
def test_token_file_is_owner_only(tmp_path, monkeypatch):
    import os as _os

    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    daemon.ensure_token(str(palace))
    token_path = daemon.state_dir(str(palace)) / "token"
    assert (_os.stat(str(token_path)).st_mode & 0o777) == 0o600


def test_health_rejects_missing_and_wrong_token(tmp_path, monkeypatch):
    from urllib import error as urlerror
    from urllib import request as urlrequest

    client, thread, palace, holders = _start_server(
        tmp_path, monkeypatch, lambda k, p: {"success": True}
    )
    try:
        base = f"http://127.0.0.1:{client.port}"
        # No Authorization header → 401.
        with pytest.raises(urlerror.HTTPError):
            urlrequest.urlopen(urlrequest.Request(base + "/health"), timeout=3)
        # Wrong token → 401.
        with pytest.raises(urlerror.HTTPError):
            urlrequest.urlopen(
                urlrequest.Request(base + "/health", headers={"Authorization": "Bearer wrong"}),
                timeout=3,
            )
    finally:
        _stop_server(client, thread, holders)


def test_worker_overrides_client_palace_path(tmp_path, monkeypatch):
    """An authenticated client must not be able to retarget the daemon at a
    different palace by stuffing palace_path into the payload."""
    seen = {}

    def fake_execute(kind, payload):
        seen["palace_path"] = payload.get("palace_path")
        return {"success": True, "exit_code": 0}

    client, thread, palace, holders = _start_server(tmp_path, monkeypatch, fake_execute)
    try:
        job = client.submit(
            "mine", {"source": "src", "palace_path": "/tmp/other-palace"}, dedupe_key="p"
        )
        client.wait(job["id"], timeout=5)
    finally:
        _stop_server(client, thread, holders)
    assert seen["palace_path"] == daemon.canonical_palace_path(str(palace))
    assert seen["palace_path"] != "/tmp/other-palace"


def test_mcp_tool_allowlist_rejects_non_write_tools(tmp_path, monkeypatch):
    """The daemon queue is a durable write surface; read/maintenance/unknown
    tools must be rejected so verbatim content can't be exfiltrated into the
    queue or retried destructively."""
    # read tool → rejected
    out = service.run_mcp_tool({"name": "mempalace_search", "arguments": {}})
    assert out["success"] is False
    assert "only accepts write tools" in out["error"]
    # maintenance tool → rejected (has its own kinds: mine/sync)
    out = service.run_mcp_tool({"name": "mempalace_mine", "arguments": {}})
    assert out["success"] is False
    # unknown tool → rejected
    out = service.run_mcp_tool({"name": "mempalace_bogus", "arguments": {}})
    assert out["success"] is False
    # write tool → passes the allowlist (handler not called here since TOOLS
    # won't have it under the test name; but classification must let it through)
    assert service.classify_tool("mempalace_add_drawer") == "write"


def test_execute_job_isolates_env_per_job(monkeypatch):
    """A job that mutates MEMPALACE_BACKEND must not leak into the next job's
    env. Regression for the per-job isolation bug (_apply_backend poisoning)."""
    import os as _os

    monkeypatch.delenv("MEMPALACE_BACKEND", raising=False)
    monkeypatch.delenv("MEMPALACE_PALACE_PATH", raising=False)

    def fake_mine(payload):
        _os.environ["MEMPALACE_BACKEND"] = "leaked-backend"
        return {"success": True, "exit_code": 0}

    monkeypatch.setattr(service, "run_mine", fake_mine)
    service.execute_job("mine", {"palace_path": "/tmp/p", "source": "s"})
    assert _os.environ.get("MEMPALACE_BACKEND") is None


def test_daemon_client_raises_on_endpoint_missing_port(tmp_path, monkeypatch):
    """A malformed endpoint.json must raise DaemonError, not a bare KeyError."""
    import json as _json

    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    daemon.ensure_token(str(palace))
    # endpoint with no port
    daemon.state_dir(str(palace)).mkdir(parents=True, exist_ok=True)
    (daemon.state_dir(str(palace)) / "endpoint.json").write_text(
        _json.dumps({"host": "127.0.0.1", "pid": 1}) + "\n", encoding="utf-8"
    )

    with pytest.raises(daemon.DaemonError):
        daemon.DaemonClient(str(palace))


def test_pid_alive_probe_is_signal_free_and_correct():
    """``_pid_alive`` must be a pure liveness probe.

    On Windows ``os.kill(pid, 0)`` is NOT harmless — signal 0 is
    ``CTRL_C_EVENT``, so it emits a console Ctrl-C to the target's process
    group. The daemon client polls a same-process endpoint, so that Ctrl-C was
    delivered back to the interpreter and surfaced as a spurious
    ``KeyboardInterrupt`` that hung the whole test session on CI runners (which,
    unlike a detached dev shell, have an attached console). Assert the probe is
    both correct and emits no SIGINT even when hammered like the poll loop.
    """
    import signal

    assert daemon._pid_alive(os.getpid()) is True
    assert daemon._pid_alive(0) is False
    assert daemon._pid_alive(-1) is False
    # A pid that is almost certainly not running.
    assert daemon._pid_alive(2_000_000_000) is False

    # pytest runs tests on the main thread, so installing a SIGINT handler is
    # allowed. If the probe regresses to os.kill(pid, 0) on Windows, the repeated
    # calls below deliver CTRL_C_EVENT and this handler fires.
    fired = []
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: fired.append(1))
    try:
        for _ in range(25):
            daemon._pid_alive(os.getpid())
        time.sleep(0.25)
    finally:
        signal.signal(signal.SIGINT, previous)
    assert fired == [], "_pid_alive delivered a console control event (CTRL_C_EVENT)"


def test_start_daemon_kills_orphan_on_readiness_timeout(tmp_path, monkeypatch):
    """If the spawned daemon never becomes ready, start_daemon must kill and
    reap the orphaned subprocess rather than leaking it with the port/token."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    daemon.ensure_token(str(palace))

    monkeypatch.setattr(daemon, "get_client_if_running", lambda *a, **kw: None)

    class FakeProc:
        def __init__(self):
            self.killed = False
            self.returncode = None

        def poll(self):
            return self.returncode  # None == still alive

        def kill(self):
            self.killed = True

        def wait(self):
            self.returncode = -9
            return self.returncode

    fake = FakeProc()

    def fake_popen(*a, **kw):
        return fake

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon, "DaemonClient", _raise_not_ready)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a, **kw: None)

    with pytest.raises(daemon.DaemonError):
        daemon.start_daemon(str(palace), timeout=0.05)
    assert fake.killed is True


# --- service.run_* happy-path coverage ---
# These close the draft PR's follow-up ("Add focused happy-path tests for
# service.run_mine / run_diary_write / run_mcp_tool") and, now that the daemon
# tests complete reliably, keep service.py's coverage above the CI gate. The
# capsys-using tests come first; the two that import mempalace.mcp_server (which
# rebinds sys.stdout) come last and do not use capsys, so the rebind can't break
# capture in this file or later files (capsys activates after the rebind).


def test_print_job_result_replays_stdout_stderr_and_returns_exit_code(capsys):
    from mempalace import service

    code = service.print_job_result(
        {"success": False, "error": "boom", "stdout": "out\n", "stderr": "err\n", "exit_code": 3}
    )
    assert code == 3
    captured = capsys.readouterr()
    assert "out" in captured.out
    assert "err" in captured.err


def test_print_job_result_prints_error_to_stderr_when_no_stderr(capsys):
    from mempalace import service

    code = service.print_job_result({"success": False, "error": "boom", "exit_code": 1})
    assert code == 1
    captured = capsys.readouterr()
    assert "mempalace: boom" in captured.err


def test_run_sync_returns_success_when_palace_dir_missing(tmp_path):
    from mempalace import service

    result = service.run_sync({"palace_path": str(tmp_path / "nope"), "dry_run": True})
    assert result["success"] is True
    assert result["exit_code"] == 0


def test_run_sync_returns_success_when_palace_has_no_backend_artifact(tmp_path):
    from mempalace import service

    palace = tmp_path / "palace"
    palace.mkdir()
    result = service.run_sync({"palace_path": str(palace), "dry_run": True})
    assert result["success"] is True
    assert result["exit_code"] == 0


def test_run_mine_invalid_mode_returns_structured_error(tmp_path):
    from mempalace import service

    palace = tmp_path / "palace"
    palace.mkdir()
    out = service.run_mine({"palace_path": str(palace), "mode": "bogus"})
    assert out["success"] is False
    assert "invalid mine mode" in out["error"]
    assert out["exit_code"] == 2


def test_run_mcp_tool_rejects_non_dict_arguments():
    from mempalace import service

    out = service.run_mcp_tool({"name": "mempalace_add_drawer", "arguments": "nope"})
    assert out["success"] is False
    assert "must be an object" in out["error"]
    assert out["exit_code"] == 2


def test_run_mcp_tool_dispatches_write_tool(monkeypatch):
    import mempalace.mcp_server as mcp
    from mempalace import service

    captured = {}

    def fake_handler(**arguments):
        captured["arguments"] = arguments
        return {"success": True, "written": True}

    monkeypatch.setattr(mcp, "TOOLS", {"mempalace_add_drawer": {"handler": fake_handler}})
    out = service.run_mcp_tool({"name": "mempalace_add_drawer", "arguments": {"x": 1}})
    assert out["success"] is True
    assert out["written"] is True
    assert out["exit_code"] == 0
    assert captured["arguments"] == {"x": 1}


def test_run_diary_write_forwards_args_and_sets_exit_code(monkeypatch):
    import mempalace.mcp_server as mcp
    from mempalace import service

    captured = {}

    def fake_diary(agent_name, entry, topic, wing):
        captured.update(agent_name=agent_name, entry=entry, topic=topic, wing=wing)
        return {"success": True}

    monkeypatch.setattr(mcp, "tool_diary_write", fake_diary)
    out = service.run_diary_write(
        {"agent_name": "alice", "entry": "hello", "topic": "t", "wing": "w"}
    )
    assert out["success"] is True
    assert out["exit_code"] == 0
    assert captured == {"agent_name": "alice", "entry": "hello", "topic": "t", "wing": "w"}


def test_run_mine_applies_backend_before_mode_validation(tmp_path):
    """Covers _apply_backend (env set + get_backend_class validation) on the daemon
    path; the invalid mode short-circuits before any mining runs."""
    from mempalace import service

    palace = tmp_path / "palace"
    palace.mkdir()
    out = service.run_mine({"palace_path": str(palace), "mode": "bogus", "backend": "chroma"})
    assert out["success"] is False
    assert out["exit_code"] == 2


def test_execute_job_dispatches_diary_write_mcp_tool_and_unknown(monkeypatch):
    """Covers execute_job's kind dispatch for diary_write, mcp_tool, and the
    unknown-kind fallback."""
    import mempalace.mcp_server as mcp
    from mempalace import service

    monkeypatch.setattr(mcp, "tool_diary_write", lambda **kw: {"success": True})
    monkeypatch.setattr(
        mcp, "TOOLS", {"mempalace_add_drawer": {"handler": lambda **kw: {"success": True}}}
    )
    assert service.execute_job("diary_write", {"entry": "x"})["success"] is True
    assert (
        service.execute_job("mcp_tool", {"name": "mempalace_add_drawer", "arguments": {}})[
            "success"
        ]
        is True
    )
    unknown = service.execute_job("bogus_kind", {})
    assert unknown["success"] is False
    assert unknown["exit_code"] == 2


def test_run_sync_structured_errors_on_sync_failures(tmp_path, monkeypatch):
    """Covers run_sync's three exception handlers (MineAlreadyRunning, ValueError,
    generic Exception) so a failing sync_palace returns a structured error instead
    of propagating."""
    import mempalace.sync as sync_module
    from mempalace import service
    from mempalace.palace import MineAlreadyRunning

    palace = tmp_path / "palace"
    palace.mkdir()
    make_minimal_chroma_sqlite(palace)

    def _raise(exc):
        def fn(**kw):
            raise exc

        return fn

    monkeypatch.setattr(sync_module, "sync_palace", _raise(MineAlreadyRunning("locked")))
    r = service.run_sync({"palace_path": str(palace), "dry_run": True})
    assert r["success"] is False
    assert r["error_class"] == "LockHeldByOtherProcess"

    monkeypatch.setattr(sync_module, "sync_palace", _raise(ValueError("bad scope")))
    r = service.run_sync({"palace_path": str(palace), "dry_run": True})
    assert r["success"] is False
    assert r["exit_code"] == 2

    monkeypatch.setattr(sync_module, "sync_palace", _raise(RuntimeError("boom")))
    r = service.run_sync({"palace_path": str(palace), "dry_run": True})
    assert r["success"] is False
    assert "sync failed" in r["error"]


def test_run_mine_lease_refusal_returns_the_lock_error_class(tmp_path, monkeypatch):
    """run_mine is the sole source of the refusal marker for kind 'mine' -- the
    primary #2014 vector, submitted by the hooks and the CLI. The worker's
    defer-vs-dead-letter gate keys on this error_class, so if a refactor drops
    it (say, a broad handler reordered above MineAlreadyRunning), the daemon
    silently goes back to dead-lettering refused mines while every worker test
    stays green: they all inject the marker by hand."""
    import mempalace.miner as miner_module
    from mempalace import service
    from mempalace.palace import MineAlreadyRunning

    palace = tmp_path / "palace"
    palace.mkdir()

    def _locked(**kw):
        raise MineAlreadyRunning("palace is held by PID 999")

    monkeypatch.setattr(miner_module, "mine", _locked)
    r = service.run_mine(
        {"palace_path": str(palace), "source": str(tmp_path / "src"), "mode": "projects"}
    )
    assert r["success"] is False
    assert r["error_class"] == daemon.LOCK_REFUSAL_ERROR_CLASS
    assert r["exit_code"] == 1


def test_safe_defer_swallows_store_errors_and_leaves_the_job_running(tmp_path, monkeypatch):
    """_safe_defer mirrors _safe_finish: a queue-DB hiccup during defer must not
    kill the worker. The job stays 'running' for recover_running to re-queue on
    the next start. Dropping the swallow would let the error reach the worker's
    outer except, which dead-letters the lock-refused job -- the #2014 outcome
    this branch exists to remove."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()
    runtime = daemon.DaemonRuntime(str(palace))
    job = runtime.store.enqueue("mine", {"source": "s"})
    runtime.store.claim_next()

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime.store, "defer", _boom)
    runtime._safe_defer(job.id, error={"error_class": daemon.LOCK_REFUSAL_ERROR_CLASS})
    assert runtime.store.get(job.id).state == "running"


# --- post-merge review follow-ups (Copilot review on #1826) ---


@_posix_only_perms
def test_run_server_tightens_umask_before_building_queue(tmp_path, monkeypatch):
    """The owner-only umask must be active BEFORE the queue DB is built.

    SQLite's WAL/SHM sidecars hold un-checkpointed verbatim payloads and are
    created with the process umask, so a loose umask at DaemonRuntime/QueueStore
    construction time would leave them world-readable. Capture the umask at the
    moment DaemonRuntime is constructed and assert it is already 0o077.
    """
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    palace = tmp_path / "palace"
    palace.mkdir()

    captured = {}

    def _spy_runtime(*args, **kwargs):
        current = os.umask(0o022)
        os.umask(current)  # restore without changing
        captured["umask"] = current
        raise RuntimeError("stop before binding a real socket")

    monkeypatch.setattr(daemon, "DaemonRuntime", _spy_runtime)
    with pytest.raises(RuntimeError):
        daemon.run_server(str(palace), port=0)
    assert captured["umask"] == 0o077


def test_negative_content_length_is_rejected_without_blocking(tmp_path, monkeypatch):
    """A POST with Content-Length: -1 must get a prompt 400, not hang the worker.

    rfile.read(-1) would read until the client closes the socket (an auth-gated
    DoS) and bypass the MAX_BODY_BYTES cap. The recv timeout below turns a
    regression into a failure instead of a hang.
    """
    import socket

    client, thread, palace, holders = _start_server(
        tmp_path, monkeypatch, lambda k, p: {"success": True}
    )
    try:
        sock = socket.create_connection((client.host, client.port), timeout=5)
        sock.settimeout(5)
        request = (
            "POST /jobs HTTP/1.1\r\n"
            "Host: daemon\r\n"
            f"Authorization: Bearer {client.token}\r\n"
            "Content-Length: -1\r\n"
            "Connection: close\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        status_line = sock.recv(4096).decode("latin-1").split("\r\n", 1)[0]
        sock.close()
        assert "400" in status_line, f"expected 400, got {status_line!r}"
    finally:
        _stop_server(client, thread, holders)


def test_run_mcp_tool_marks_bare_error_dict_as_failure(monkeypatch):
    """A write tool that returns {"error": ...} with no success flag must be
    recorded as a failed job, not succeeded (Copilot review)."""
    import mempalace.mcp_server as mcp
    from mempalace import service

    monkeypatch.setattr(
        mcp,
        "TOOLS",
        {"mempalace_create_tunnel": {"handler": lambda **kw: {"error": "bad endpoint"}}},
    )
    out = service.run_mcp_tool({"name": "mempalace_create_tunnel", "arguments": {}})
    assert out["success"] is False
    assert out["exit_code"] == 1
    assert out["error"] == "bad endpoint"

    # A result with neither an explicit success flag nor an error is a success.
    monkeypatch.setattr(
        mcp, "TOOLS", {"mempalace_create_tunnel": {"handler": lambda **kw: {"tunnel_id": "t1"}}}
    )
    out = service.run_mcp_tool({"name": "mempalace_create_tunnel", "arguments": {}})
    assert out["success"] is True
    assert out["exit_code"] == 0


def test_get_client_if_running_uses_short_probe_timeout(monkeypatch):
    """The hook liveness precheck must pass a short health timeout so a wedged
    daemon can't stall the hook past its budget (Copilot review)."""
    captured = {}

    class _FakeClient:
        def __init__(self, palace_path):
            pass

        def health(self, *, timeout):
            captured["timeout"] = timeout
            return {"ok": True}

    monkeypatch.setattr(daemon, "DaemonClient", _FakeClient)

    assert daemon.HOOK_PROBE_TIMEOUT <= 0.5
    client = daemon.get_client_if_running("/p", health_timeout=daemon.HOOK_PROBE_TIMEOUT)
    assert client is not None
    assert captured["timeout"] == daemon.HOOK_PROBE_TIMEOUT


# --- _detached_kwargs ---


def test_detached_kwargs_posix(tmp_path, monkeypatch):
    monkeypatch.setattr("mempalace.daemon.os.name", "posix")
    kwargs = daemon._detached_kwargs(tmp_path / "daemon.log")
    fh = kwargs["stdout"]
    try:
        assert kwargs.get("start_new_session") is True
        assert kwargs.get("stdin") is subprocess.DEVNULL
        assert kwargs.get("close_fds") is True
        assert "creationflags" not in kwargs
    finally:
        fh.close()


def test_detached_kwargs_windows(tmp_path, monkeypatch):
    monkeypatch.setattr("mempalace.daemon.os.name", "nt")
    monkeypatch.setattr("mempalace.daemon.subprocess.CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr("mempalace.daemon.subprocess.DETACHED_PROCESS", 0x00000008, raising=False)
    monkeypatch.setattr(
        "mempalace.daemon.subprocess.CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False
    )
    monkeypatch.setattr(
        "mempalace.daemon.subprocess.CREATE_BREAKAWAY_FROM_JOB", 0x01000000, raising=False
    )
    kwargs = daemon._detached_kwargs(tmp_path / "daemon.log")
    fh = kwargs["stdout"]
    try:
        flags = kwargs.get("creationflags", 0)
        assert flags & 0x08000000, "CREATE_NO_WINDOW must be set"
        assert not (flags & 0x00000008), (
            "DETACHED_PROCESS must NOT be set (it suppresses CREATE_NO_WINDOW)"
        )
        assert flags & 0x00000200, "CREATE_NEW_PROCESS_GROUP preserved (Ctrl-Break group isolation)"
        assert flags & 0x01000000, (
            "CREATE_BREAKAWAY_FROM_JOB preserved (survive parent Job-Object close)"
        )
        assert kwargs.get("stdin") is subprocess.DEVNULL
        assert kwargs.get("close_fds") is True
        # creationflags + start_new_session are mutually exclusive on Windows
        # (Popen raises ValueError); the nt branch must not set the latter.
        assert "start_new_session" not in kwargs
    finally:
        fh.close()
