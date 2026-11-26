"""The daemon as the palace's single writer, under the failures real use brings.

Each test drives one hazard deterministically -- an injected clock, a
monkeypatched seam, or a child crash placed at an exact storage call -- rather
than sleeping and hoping a race lines up:

* a queue written by an older daemon, migrated with a crashed run still in it;
* hooks in separate processes enqueuing at the same moment;
* a mine child killed at every storage call, then re-mined;
* every write a mine makes reaching the daemon, in order, errors included;
* a killed mine coming back on its own.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from test_mine_child import _EMBEDDER, REAL_CHILD, _child_argv

from mempalace import daemon, daemon_queue
from mempalace._proc import pid_alive
from mempalace.mine_supervisor import run_mine_in_child


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


# --- Child <-> daemon storage proxy -------------------------------------------


class _RecordingCollection:
    """Daemon-side collection that logs every call and fails on request."""

    def __init__(self, log):
        self.log = log

    def _record(self, name, *args, **kwargs):
        self.log.append((name, kwargs.get("ids") or (args[0] if args else None)))

    def add(self, *a, **kw):
        self._record("add", *a, **kw)

    def upsert(self, *a, **kw):
        self._record("upsert", *a, **kw)

    def update(self, *a, **kw):
        self._record("update", *a, **kw)

    def delete(self, *a, **kw):
        self._record("delete", *a, **kw)
        if kw.get("ids") == ["boom"]:
            raise ValueError("delete refused: boom")
        if kw.get("ids") == ["unpicklable"]:
            exc = RuntimeError("cannot cross")
            exc.payload = lambda: None  # not picklable
            raise exc

    def count(self):
        self._record("count")
        return len(self.log)


_PROXY_CHILD = textwrap.dedent(
    """
    import json, os, sys
    from mempalace import mine_child as m
    rx, tx = sys.stdin.buffer, sys.stdout.buffer
    m._CHANNEL = channel = m._Channel(rx, tx)
    m._read_msg(rx)
    channel.notify({"op": "started", "pid": os.getpid(), "priority": "t"})
    opened = channel.request({"op": "open", "kwargs": {"collection_name": None, "create": True}})
    remote = m._remote_collection_class()[0](opened["handle"], opened["caps"])
    seen = []
    for i in range(20):
        remote.upsert(ids=[f"d{i}"], documents=["x"], metadatas=[{}], embeddings=[[0.0]])
        if i % 5 == 4:
            remote.update(ids=[f"d{i}"], metadatas=[{"chunk_total": 1}])
    for ids in (["boom"], ["unpicklable"]):
        try:
            remote.delete(ids=ids)
        except Exception as exc:
            seen.append([type(exc).__name__, str(exc)])
    remote.add(ids=["after"], documents=["y"], metadatas=[{}], embeddings=[[0.0]])
    channel.notify({"op": "done", "result": {"success": True, "seen": seen,
                                             "count": remote.count()}})
    """
)


def test_proxy_runs_every_write_in_the_daemon_in_order_and_returns_errors(tmp_path, monkeypatch):
    from mempalace import mine_supervisor, palace

    log: list = []
    monkeypatch.setattr(palace, "get_collection", lambda *a, **kw: _RecordingCollection(log))
    monkeypatch.setattr(mine_supervisor, "_caps", lambda *a, **kw: {"methods": []})
    writes_seen_under_lock = []

    class _Lock:
        def __enter__(self):
            writes_seen_under_lock.append(len(log))

        def __exit__(self, *exc):
            return False

    result = run_mine_in_child(
        {}, palace_path=str(tmp_path), write_lock=_Lock(), argv=_child_argv(_PROXY_CHILD)
    )
    assert result["success"], result
    expected = []
    for i in range(20):
        expected.append(("upsert", [f"d{i}"]))
        if i % 5 == 4:
            expected.append(("update", [f"d{i}"]))
    expected += [
        ("delete", ["boom"]),
        ("delete", ["unpicklable"]),
        ("add", ["after"]),
        ("count", None),
    ]
    assert log == expected, "writes reached the daemon out of order or not at all"
    # The daemon's error is raised in the child as itself; one that cannot be
    # pickled arrives as a RuntimeError naming it. The mine went on after both.
    assert result["seen"] == [
        ["ValueError", "delete refused: boom"],
        ["RuntimeError", "RuntimeError: cannot cross"],
    ]
    # Each call ran under the daemon's write lock (one entry per call + open).
    assert len(writes_seen_under_lock) == len(expected) + 1


_SIDECAR_CHILD = textwrap.dedent(
    """
    import os, sys
    from mempalace import mine_child as m
    rx, tx = sys.stdin.buffer, sys.stdout.buffer
    m._CHANNEL = channel = m._Channel(rx, tx)
    m._read_msg(rx)
    channel.notify({"op": "started", "pid": os.getpid(), "priority": "t"})
    tunnels = channel.request({"op": "topic_tunnels", "wing": "w"})
    opened = channel.request({"op": "open", "kwargs": {"collection_name": None, "create": True}})
    remote = m._remote_collection_class()[0](opened["handle"], opened["caps"])
    channel.notify({"op": "done", "result": {"success": True, "tunnels": tunnels,
                                             "count": remote.count()}})
    """
)


def test_sidecar_writes_do_not_hold_the_collection_write_lock(tmp_path, monkeypatch):
    """Tunnels, hallways, revisions and origin each have a file lock of their
    own. Holding the collection lock for them would stall the diary lane for a
    whole tunnel or pass-zero computation."""
    from mempalace import mine_supervisor, miner, palace

    monkeypatch.setattr(palace, "get_collection", lambda *a, **kw: _RecordingCollection([]))
    monkeypatch.setattr(mine_supervisor, "_caps", lambda *a, **kw: {"methods": []})
    depth = []

    class _Lock:
        held = 0

        def __enter__(self):
            _Lock.held += 1

        def __exit__(self, *exc):
            _Lock.held -= 1
            return False

    def tunnels(wing, config=None):
        depth.append(_Lock.held)
        return 3

    monkeypatch.setattr(miner, "_compute_topic_tunnels_for_wing", tunnels)
    result = run_mine_in_child(
        {}, palace_path=str(tmp_path), write_lock=_Lock(), argv=_child_argv(_SIDECAR_CHILD)
    )
    assert result["success"] and result["tunnels"] == 3, result
    assert depth == [0], "the tunnel recompute ran under the collection write lock"


# An audit hook installed in the child before the mine: any write-mode file
# open, sqlite connection or rename/remove inside the palace or the daemon
# state directory is a second writer, and aborts the child.
_AUDITED_CHILD = _EMBEDDER + textwrap.dedent(
    """
    import os, sys
    _ROOTS = tuple(os.path.realpath(p) + os.sep for p in os.environ["AUDIT_ROOTS"].split(os.pathsep))
    _ALLOWED = (".lock",)

    def _inside(path):
        try:
            path = os.path.realpath(os.fsdecode(path))
        except (TypeError, ValueError):
            return False
        return path.startswith(_ROOTS) and not path.endswith(_ALLOWED)

    def _audit(event, args):
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            mode, flags = args[1] or "r", args[2] or 0
            writing = any(c in str(mode) for c in "wax+") or flags & (os.O_WRONLY | os.O_RDWR)
            if writing and _inside(args[0]):
                os.write(2, f"SECOND WRITER: open {args[0]!r} {mode!r}\\n".encode())
                os._exit(41)
        elif event == "sqlite3.connect" and args and _inside(args[0]):
            os.write(2, f"SECOND WRITER: sqlite3.connect {args[0]!r}\\n".encode())
            os._exit(42)
        elif event in ("os.rename", "os.replace", "os.remove", "os.unlink") and args:
            if _inside(args[0]):
                os.write(2, f"SECOND WRITER: {event} {args[0]!r}\\n".encode())
                os._exit(43)

    sys.addaudithook(_audit)
    from mempalace import mine_child
    sys.exit(mine_child.main())
    """
)


def _convo_source(tmp_path) -> Path:
    src = tmp_path / "convos"
    src.mkdir()
    lines = []
    for i in range(40):
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": f"Question {i}: where did the heron nest this spring?",
                    },
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": f"Answer {i}: by the reed bed, tide {i}."}
                        ],
                    },
                }
            )
        )
    (src / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return src


def _project_source(tmp_path, files: int = 3) -> Path:
    src = tmp_path / "proj"
    src.mkdir()
    for n in range(files):
        (src / f"notes_{n}.md").write_text(
            f"# Notes {n}\n\n"
            + "".join(
                f"Paragraph {n}.{i}: the heron waited by the reed bed until tide {i}.\n\n"
                for i in range(30)
            ),
            encoding="utf-8",
        )
    return src


@pytest.mark.parametrize("mode", ["projects", "convos"])
def test_mine_child_writes_nothing_itself(tmp_path, monkeypatch, mode):
    """The real mine, in the child, with an audit hook that kills it on any
    write to the palace or daemon state: every write has to go to the daemon."""
    palace = tmp_path / "palace"
    state_root = tmp_path / "state"
    monkeypatch.setenv(daemon_queue.STATE_ROOT_ENV, str(state_root))
    monkeypatch.setenv("AUDIT_ROOTS", os.pathsep.join([str(palace), str(state_root)]))
    src = _project_source(tmp_path) if mode == "projects" else _convo_source(tmp_path)
    payload = {"palace_path": str(palace), "source": str(src), "mode": mode, "wing": "w"}

    result = run_mine_in_child(
        payload,
        palace_path=str(palace),
        write_lock=threading.RLock(),
        argv=_child_argv(_AUDITED_CHILD),
    )
    assert result["success"] is True, result.get("stdout", result)
    from mempalace.palace import get_collection

    docs = get_collection(str(palace)).get(where={"wing": "w"}, include=["documents"]).documents
    assert any("heron" in d for d in docs)


# --- Kill the child at every stage -------------------------------------------

# REAL_CHILD, killed with os._exit right before (or right after) its Nth
# storage request reaches the daemon.
_KILL_AT = textwrap.dedent(
    """
    import os
    from mempalace import mine_child as _mc
    _N = int(os.environ["KILL_AT"])
    _AFTER = os.environ.get("KILL_AFTER") == "1"
    _orig_request = _mc._Channel.request
    _count = [0]
    def _request(self, msg):
        _count[0] += 1
        if _count[0] == _N and not _AFTER:
            os._exit(9)
        if _count[0] == _N and _AFTER:
            self._send(msg)  # the daemon runs it; we die before the reply
            os._exit(9)
        return _orig_request(self, msg)
    _mc._Channel.request = _request
    """
)


def _snapshot(palace: str) -> dict:
    from mempalace.palace import get_closets_collection, get_collection

    drawers = get_collection(palace).get(include=["documents", "metadatas"])
    rows = {
        i: (doc, meta.get("chunk_total"), meta.get("chunk_index"))
        for i, doc, meta in zip(drawers.ids, drawers.documents, drawers.metadatas)
    }
    closets = get_closets_collection(palace).get(include=["documents"])
    return {"drawers": rows, "closets": sorted(closets.documents)}


def _mine(payload, palace, extra=""):
    return run_mine_in_child(
        payload,
        palace_path=palace,
        write_lock=threading.RLock(),
        argv=_child_argv(extra + REAL_CHILD),
    )


def _kill_and_remine(tmp_path, monkeypatch, mode, choose_stages, files=3):
    """Kill a fresh mine at each chosen stage, re-mine, compare with a clean mine.

    ``choose_stages(ops)`` gets the clean mine's storage calls in order, as
    ``(op, method)``, and returns ``(call_number, after_send)`` pairs.
    """
    from mempalace import mine_supervisor

    src = _project_source(tmp_path, files) if mode == "projects" else _convo_source(tmp_path)

    def payload(palace):
        return {"palace_path": palace, "source": str(src), "mode": mode, "wing": "w"}

    ops: list = []
    serve = mine_supervisor._serve

    def recording_serve(op, msg, *args):
        ops.append((op, msg.get("method")))
        return serve(op, msg, *args)

    clean = str(tmp_path / "clean")
    with monkeypatch.context() as m:
        m.setattr(mine_supervisor, "_serve", recording_serve)
        baseline_run = _mine(payload(clean), clean)
    assert baseline_run["success"], baseline_run
    baseline = _snapshot(clean)
    assert baseline["drawers"]
    assert len(ops) == baseline_run["child"]["calls"] > 3

    failures = []
    for n, after in choose_stages(ops):
        palace = str(tmp_path / f"kill_{n}_{int(after)}")
        monkeypatch.setenv("KILL_AT", str(n))
        monkeypatch.setenv("KILL_AFTER", "1" if after else "0")
        killed = _mine(payload(palace), palace, _KILL_AT)
        assert killed["success"] is False and killed["error_class"] == "MineChildDied", killed
        _snapshot(palace)  # readable after the kill
        monkeypatch.delenv("KILL_AT")
        recovered = _mine(payload(palace), palace)
        assert recovered["success"], recovered
        if _snapshot(palace) != baseline:
            failures.append((n, after, ops[n - 1]))
    assert not failures, f"a re-mine after a kill at these stages differs: {failures}"


_WRITES = {"add", "upsert", "update", "delete"}


@pytest.mark.parametrize("mode", ["projects", "convos"])
def test_killing_the_child_around_each_write_of_a_file_loses_nothing(tmp_path, monkeypatch, mode):
    """Kill the child just after each write call for the first file reaches the
    daemon (drawers in, stale closets purged, closets in, the completion mark),
    and just before the completion mark. The palace stays readable, and a plain re-mine ends
    with exactly the drawers and closets an uninterrupted mine makes."""

    def stages(ops):
        writes = [i + 1 for i, (op, method) in enumerate(ops) if method in _WRITES]
        first_complete = next(
            n for n in writes if ops[n - 1][1] == "update"
        )  # the first file's completion mark
        # After each write lands, and before the completion mark is sent.
        return [(n, True) for n in writes if n <= first_complete] + [(first_complete, False)]

    _kill_and_remine(tmp_path, monkeypatch, mode, stages, files=1)


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["projects", "convos"])
def test_killing_the_child_at_every_storage_call_loses_nothing(tmp_path, monkeypatch, mode):
    """The exhaustive sweep: every storage call of the mine, before and after."""
    _kill_and_remine(
        tmp_path,
        monkeypatch,
        mode,
        lambda ops: [(n, after) for n in range(1, len(ops) + 1) for after in (False, True)],
    )


# --- Idle exit against a hook enqueuing --------------------------------------


def _serve_in_thread(state, **kwargs):
    errors: list = []

    def _serve():
        try:
            daemon.run_server(state, port=0, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            errors.append(exc)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return thread, errors


# --- A mine cut short comes back -----------------------------------------------


def test_killed_mine_is_retried_with_backoff_then_dead_lettered(state, monkeypatch):
    """A mine whose child dies is re-queued (mines are file-idempotent) with
    a growing delay, and fails for good after MAX_ATTEMPTS claims."""
    monkeypatch.setenv("MEMPALACE_DAEMON_MINE_CHILD", "1")
    runtime = daemon.DaemonRuntime(state)
    runtime.mine_child_argv = _child_argv("import sys; sys.stdin.buffer.read(4); sys.exit(3)")
    clock = _Clock(time.time())
    runtime.store.clock = clock
    job = runtime.store.enqueue("mine", {"source": "s"}, dedupe_key="k")
    runtime.start_worker()
    try:
        for attempt in range(1, daemon.MAX_ATTEMPTS):

            def requeued_for(n=attempt):
                j = runtime.store.get(job.id)
                return j if j.state == "queued" and j.attempts == n else None

            requeued = _wait_for(requeued_for)
            assert requeued.error["error_class"] == "MineChildDied"
            assert requeued.run_after == pytest.approx(
                clock.t + daemon.MINE_RETRY_BACKOFF_SECONDS * attempt
            )
            clock.t = requeued.run_after
            runtime.worker_wake.set()
        failed = _wait_for(
            lambda: (lambda j: j if j.state == "failed" else None)(runtime.store.get(job.id))
        )
        assert failed.attempts == daemon.MAX_ATTEMPTS
        assert failed.error["error_class"] == "MineChildDied"
    finally:
        runtime.shutdown_event.set()
        for thread in runtime.worker_threads.values():
            thread.join(timeout=30)


_WAITING_CHILD = textwrap.dedent(
    """
    import os, sys, time
    from mempalace.mine_child import _read_msg, _write_msg
    _read_msg(sys.stdin.buffer)
    _write_msg(sys.stdout.buffer, {"op": "started", "pid": os.getpid(), "priority": "t"})
    _write_msg(sys.stdout.buffer, {"op": "progress"})
    time.sleep(600)
    """
)


@pytest.mark.parametrize("prior_attempts", [0, daemon_queue.MAX_ATTEMPTS - 1])
def test_shutdown_requeues_the_running_mine_until_its_attempts_run_out(
    state, monkeypatch, prior_attempts
):
    """A stop re-queues the mine (it is file-idempotent), but the cut-short run
    counts: a mine that every ``daemon stop`` interrupts is not restarted by
    every later daemon forever."""
    monkeypatch.setenv("MEMPALACE_DAEMON_MINE_CHILD", "1")
    runtime = daemon.DaemonRuntime(state)
    runtime.mine_child_argv = _child_argv(_WAITING_CHILD)
    job = runtime.store.enqueue("mine", {"source": "s"}, dedupe_key="k")
    with runtime.store._connect() as conn:
        conn.execute("UPDATE jobs SET attempts = ? WHERE id = ?", (prior_attempts, job.id))
    runtime.start_worker()
    _wait_for(lambda: runtime.active_child is not None)
    child = runtime.active_child
    try:
        daemon._drain_and_cleanup(runtime, state, {})
        after = runtime.store.get(job.id)
        if prior_attempts + 1 < daemon_queue.MAX_ATTEMPTS:
            assert (after.state, after.attempts) == ("queued", prior_attempts + 1)
        else:
            # Dead-lettered: the worker fails the killed run, or the drain cancels it.
            assert after.state in {"failed", "cancelled"}, after.state
        # Not os.kill(pid, 0): on Windows that terminates the child, so the
        # test would do the product's job.
        _wait_for(lambda: not pid_alive(child.pid), timeout=5)
    finally:
        child.proc.kill()
        child.proc.wait(timeout=30)
