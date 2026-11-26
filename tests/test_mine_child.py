"""The daemon's mine child: compute at idle priority, every write in the daemon.

These run real child processes. The child gets a deterministic embedder (the
same shape as ``conftest``'s) through a small launcher, so no ONNX model is
loaded and the run stays fast.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest
from conftest import _TEST_EMBED_DIM

from mempalace import daemon
from mempalace.mine_supervisor import run_mine_in_child

_EMBEDDER = f"""
import hashlib, math, re
_DIM = {_TEST_EMBED_DIM}
_TOK = re.compile(r"[a-z0-9_]+")

def _vec(text):
    vec = [0.0] * _DIM
    for token in _TOK.findall((text or "").lower()) or [""]:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        vec[int.from_bytes(digest[:4], "little") % _DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]

import mempalace.backends.embedding_wrapper as _ew
_ew._embed_texts = lambda texts, **_: [_vec(str(t)) for t in texts]
_ew._resolve_document_embedder = lambda: None
"""

# The real child, with the test embedder in place of the configured one.
REAL_CHILD = (
    _EMBEDDER + "\nimport sys\nfrom mempalace import mine_child\nsys.exit(mine_child.main())\n"
)

# A child that speaks the protocol, opens the drawers collection, then stalls
# until RELEASE exists -- a mine stuck in its compute.
STALLING_CHILD = textwrap.dedent(
    """
    import os, sys, time
    from mempalace.mine_child import _read_msg, _write_msg
    rx, tx = sys.stdin.buffer, sys.stdout.buffer
    job = _read_msg(rx)
    _write_msg(tx, {"op": "started", "pid": os.getpid(), "priority": "test"})
    _write_msg(tx, {"op": "open", "kwargs": {"collection_name": None, "create": True}})
    opened = _read_msg(rx)
    assert opened["ok"], opened
    release = os.environ["TEST_RELEASE"]
    while not os.path.exists(release):
        time.sleep(0.05)
    _write_msg(tx, {"op": "call", "handle": opened["value"]["handle"], "method": "count",
                    "args": (), "kwargs": {}})
    counted = _read_msg(rx)
    _write_msg(tx, {"op": "done", "result": {"success": True, "count": counted["value"]}})
    """
)


def _child_argv(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def _project(tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "notes.md").write_text(
        "# Field notes\n\n"
        + "The heron waited by the reed bed until the tide turned silver.\n" * 20,
        encoding="utf-8",
    )
    return src


def _counting_daemon_embedder(monkeypatch):
    """Record every text the daemon process itself embeds."""
    from conftest import _StableTestEmbeddingFunction

    import mempalace.backends.chroma as chroma_mod

    seen: list[str] = []
    base = _StableTestEmbeddingFunction()

    class Counting(_StableTestEmbeddingFunction):
        def __call__(self, input):
            seen.extend(str(t) for t in list(input or []))
            return base(input=input)

    ef = Counting()
    monkeypatch.setattr(
        chroma_mod.ChromaBackend, "_resolve_embedding_function", staticmethod(lambda **_: ef)
    )
    return seen


def test_mine_runs_in_child_and_daemon_stores_its_vectors(tmp_path, monkeypatch):
    """A whole project mine through the child: the drawers land verbatim, and
    the daemon only writes -- it embeds none of the mined text."""
    palace = tmp_path / "palace"
    src = _project(tmp_path)
    seen = _counting_daemon_embedder(monkeypatch)

    result = run_mine_in_child(
        {"palace_path": str(palace), "source": str(src), "mode": "projects", "wing": "proj"},
        palace_path=str(palace),
        write_lock=threading.RLock(),
        argv=_child_argv(REAL_CHILD),
    )

    assert result["success"] is True, result
    assert result["child"]["calls"] > 0
    assert result["child"]["pid"] != os.getpid()
    assert not any("heron" in text for text in seen), "the daemon re-embedded mined text"

    from mempalace.palace import get_collection

    stored = get_collection(str(palace)).get(where={"wing": "proj"}, include=["documents"])
    joined = "\n".join(stored.documents)
    assert "The heron waited by the reed bed until the tide turned silver." in joined


@pytest.mark.skipif(not hasattr(os, "SCHED_IDLE"), reason="SCHED_IDLE is Linux-only")
def test_child_drops_to_sched_idle_before_heavy_imports():
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys\n"
            "from mempalace.mine_child import lower_priority\n"
            "applied = lower_priority()\n"
            "heavy = [m for m in ('onnxruntime', 'chromadb', 'numpy') if m in sys.modules]\n"
            "print(applied, os.sched_getscheduler(0) == os.SCHED_IDLE, heavy)\n",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert out.stdout.split()[:2] == ["sched-idle", "True"]
    assert out.stdout.strip().endswith("[]")


def test_backend_open_inside_child_is_refused():
    """Every storage path in the child goes to the daemon; a direct backend open
    fails loudly instead of quietly becoming a second Chroma writer."""
    script = textwrap.dedent(
        """
        from mempalace import mine_child, miner, convo_miner, palace
        mine_child._install_remote_storage()
        assert miner.get_collection.__module__ == "mempalace.mine_child"
        assert convo_miner.get_collection.__module__ == "mempalace.mine_child"
        assert palace.get_collection.__module__ == "mempalace.mine_child"
        from mempalace.backends.chroma import ChromaBackend
        try:
            ChromaBackend().get_collection("/nonexistent", collection_name="x", create=True)
        except mine_child.ChildWriterViolation:
            print("refused")
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "refused"


_CHILD_WRITERS = {
    "mempalace.palace:_validate_palace_fts5_after_mine": ("/nonexistent",),
    "mempalace.cli:_run_pass_zero": ("/nonexistent", "/nonexistent-palace", None),
    "mempalace.revisions:append_drawer_revisions": ("/nonexistent", []),
    "mempalace.miner:_compute_topic_tunnels_for_wing": ("w",),
    "mempalace.miner:_compute_entity_tunnels_for_wing": ("w",),
    "mempalace.hallways:_store_wing_hallways": ("w", [], {}, {}, 1),
}


@pytest.mark.parametrize("target", sorted(_CHILD_WRITERS))
def test_original_writer_held_elsewhere_is_refused_inside_child(target):
    """The identity sweep rebinds module globals only. A reference it cannot
    reach (kept in a closure, a default argument, a callback table) must not
    write the palace from the child."""
    module, name = target.split(":")
    kwargs = {"config": None} if name == "_store_wing_hallways" else {}
    script = textwrap.dedent(
        f"""
        import importlib, sys
        held = {{"fn": getattr(importlib.import_module({module!r}), {name!r})}}
        from mempalace import mine_child
        mine_child._install_remote_storage()
        try:
            held["fn"](*{_CHILD_WRITERS[target]!r}, **{kwargs!r})
        except mine_child.ChildWriterViolation as exc:
            print("refused", {name!r} in str(exc), file=sys.__stdout__)
        except Exception as exc:
            print("ran", type(exc).__name__, file=sys.__stdout__)
        else:
            print("ran", file=sys.__stdout__)
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "refused True"


def _wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_diary_write_lands_while_a_mine_runs(tmp_path, monkeypatch):
    """A Stop-hook checkpoint is not held behind a long mine: the diary lane
    files it while the mine child is still working."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    monkeypatch.setenv("MEMPALACE_DAEMON_MINE_CHILD", "1")
    release = tmp_path / "release"
    monkeypatch.setenv("TEST_RELEASE", str(release))
    palace = tmp_path / "palace"
    palace.mkdir()

    runtime = daemon.DaemonRuntime(str(palace))
    runtime.mine_child_argv = _child_argv(STALLING_CHILD)
    runtime.start_worker()
    try:
        mine = runtime.store.enqueue("mine", {"source": str(tmp_path)}, dedupe_key="m")
        runtime.worker_wake.set()
        _wait_for(lambda: runtime.active_child is not None and runtime.active_child.calls >= 1)

        diary = runtime.store.enqueue(
            "diary_write",
            {"agent_name": "tester", "entry": "checkpoint filed mid-mine", "topic": "t"},
            priority=10,
        )
        runtime.worker_wake.set()
        finished = _wait_for(
            lambda: (
                runtime.store.get(diary.id)
                if runtime.store.get(diary.id).state in daemon.TERMINAL_STATES
                else None
            )
        )
        assert finished.state == "succeeded", finished.error
        # The mine is still running: the checkpoint did not wait for it.
        assert runtime.store.get(mine.id).state == "running"
        assert runtime.progress()["pid"] == runtime.active_child.pid

        release.write_text("go")
        done = _wait_for(
            lambda: (
                runtime.store.get(mine.id)
                if runtime.store.get(mine.id).state in daemon.TERMINAL_STATES
                else None
            )
        )
        assert done.state == "succeeded", done.error
        assert done.result["count"] >= 1  # the diary drawer, seen through the daemon
    finally:
        release.write_text("go")
        runtime.shutdown_event.set()
        for thread in runtime.worker_threads.values():
            thread.join(timeout=30)


def test_child_that_dies_fails_the_job_and_keeps_the_daemon(tmp_path):
    palace = tmp_path / "palace"
    result = run_mine_in_child(
        {"palace_path": str(palace)},
        palace_path=str(palace),
        write_lock=threading.RLock(),
        argv=_child_argv("import sys; sys.stdin.buffer.read(4); sys.exit(3)"),
    )
    assert result["success"] is False
    assert result["error_class"] == "MineChildDied"
    assert "code 3" in result["error"]


def test_mine_child_env_caps_threads_and_turns_off_spinning(monkeypatch):
    from mempalace.embedding import _intra_op_session_options
    from mempalace.mine_supervisor import _child_env

    monkeypatch.delenv("MEMPALACE_EMBEDDING_THREADS", raising=False)
    monkeypatch.delenv("MEMPALACE_ORT_ALLOW_SPINNING", raising=False)
    env = _child_env("/palace", 2)
    assert env["MEMPALACE_EMBEDDING_THREADS"] == "2"
    assert env["MEMPALACE_ORT_ALLOW_SPINNING"] == "0"

    # A user's own thread setting wins over the daemon default.
    monkeypatch.setenv("MEMPALACE_EMBEDDING_THREADS", "6")
    assert _child_env("/palace", 2)["MEMPALACE_EMBEDDING_THREADS"] == "6"

    monkeypatch.setenv("MEMPALACE_ORT_ALLOW_SPINNING", "0")
    so = _intra_op_session_options(0)
    assert so is not None
    assert so.get_session_config_entry("session.intra_op.allow_spinning") == "0"
    assert _intra_op_session_options(2).intra_op_num_threads == 2


def test_reaper_kills_a_wedged_child(tmp_path):
    """A child that stops talking (no calls, no heartbeat) is killed and the job
    fails; the supervisor returns instead of waiting on it forever (#2917)."""
    palace = tmp_path / "palace"
    wedged = textwrap.dedent(
        """
        import os, sys, time
        from mempalace.mine_child import _read_msg, _write_msg
        _read_msg(sys.stdin.buffer)
        _write_msg(sys.stdout.buffer, {"op": "started", "pid": os.getpid(), "priority": "t"})
        time.sleep(600)
        """
    )
    started = time.monotonic()
    result = run_mine_in_child(
        {"palace_path": str(palace)},
        palace_path=str(palace),
        write_lock=threading.RLock(),
        argv=_child_argv(wedged),
        stall_seconds=1.0,
    )
    assert time.monotonic() - started < 30
    assert result["success"] is False
    assert result["error_class"] == "MineChildKilled"
    assert "sent nothing" in result["error"]


def test_heartbeat_keeps_a_slow_child_alive(tmp_path, monkeypatch):
    """A long silent compute is not a stall while the child's heartbeat runs."""
    palace = tmp_path / "palace"
    slow = textwrap.dedent(
        """
        import os, sys, time
        from mempalace import mine_child
        rx, tx = sys.stdin.buffer, sys.stdout.buffer
        mine_child._CHANNEL = mine_child._Channel(rx, tx)
        mine_child._read_msg(rx)
        mine_child._CHANNEL.notify({"op": "started", "pid": os.getpid(), "priority": "t"})
        stop = mine_child._CHANNEL.start_heartbeat(interval=0.2)
        time.sleep(3)
        stop.set()
        mine_child._CHANNEL.notify({"op": "done", "result": {"success": True}})
        """
    )
    result = run_mine_in_child(
        {"palace_path": str(palace)},
        palace_path=str(palace),
        write_lock=threading.RLock(),
        argv=_child_argv(slow),
        stall_seconds=1.0,
    )
    assert result["success"] is True, result


def test_child_that_reports_done_but_never_exits_is_reaped(tmp_path):
    palace = tmp_path / "palace"
    lingering = textwrap.dedent(
        """
        import os, sys, time
        from mempalace.mine_child import _read_msg, _write_msg
        _read_msg(sys.stdin.buffer)
        _write_msg(sys.stdout.buffer, {"op": "done", "result": {"success": True, "files": 1}})
        time.sleep(600)
        """
    )
    started = time.monotonic()
    result = run_mine_in_child(
        {"palace_path": str(palace)},
        palace_path=str(palace),
        write_lock=threading.RLock(),
        argv=_child_argv(lingering),
        exit_grace_seconds=1.0,
    )
    assert time.monotonic() - started < 30
    # The mine's own result stands -- its writes are in -- and the reap is noted.
    assert result["success"] is True
    assert result["files"] == 1
    assert "did not exit" in result["reaped"]


def test_unpicklable_reply_becomes_the_childs_error(tmp_path):
    """A storage result that cannot cross the pipe fails that one call in the
    child, which can handle it, instead of crashing the supervisor."""
    from mempalace import mine_supervisor

    class Unpicklable:
        def __reduce__(self):
            raise TypeError("no pickling")

    import io
    import pickle

    out = io.BytesIO()
    mine_supervisor._send_reply(out, {"ok": True, "value": Unpicklable()})
    reply = pickle.loads(out.getvalue()[4:])
    assert reply["ok"] is False
    assert "could not send" in str(reply["exc"])


def test_child_takes_the_lock_itself_when_the_daemon_does_not_hold_it(tmp_path):
    """On a backend whose daemon holds no palace lock, the child must not
    pretend it does, or a daemon mine would stop excluding other miners."""
    script = textwrap.dedent(
        """
        import os, sys
        from mempalace import mine_child, palace, paths
        calls = []
        mine_child._adopt_palace_lock = lambda p: calls.append(p)
        mine_child._install_remote_storage = lambda: None
        import mempalace.service as service
        service.run_mine = lambda payload: {"success": True, "adopted": bool(calls)}
        sys.exit(mine_child.main())
        """
    )
    for held in (True, False):
        palace_dir = tmp_path / f"palace_{held}"
        result = run_mine_in_child(
            {"palace_path": str(palace_dir)},
            palace_path=str(palace_dir),
            write_lock=threading.RLock(),
            argv=_child_argv(script),
            lock_held=held,
        )
        assert result["success"] is True, result
        assert result["adopted"] is held


def test_proxy_respects_server_embedder(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mempalace import mine_supervisor, palace

    monkeypatch.setattr(
        palace,
        "get_backend_for_palace",
        lambda *_a, **_kw: SimpleNamespace(capabilities={"server_embedder"}),
    )
    assert mine_supervisor._caps(object(), str(tmp_path), None)["server_embedder"]


# A child that heartbeats every 50 ms while it burns CPU, marking progress
# every PROGRESS_EVERY CPU-seconds (0 = never), then reports done.
_BURNING_CHILD = textwrap.dedent(
    """
    import os, sys, time
    from mempalace import mine_child as m
    m._CHANNEL = channel = m._Channel(sys.stdin.buffer, sys.stdout.buffer)
    m._read_msg(sys.stdin.buffer)
    channel.notify({"op": "started", "pid": os.getpid(), "priority": "normal"})
    channel.start_heartbeat(interval=0.05)
    every = float(os.environ["PROGRESS_EVERY"])
    start = mark = time.process_time()
    while time.process_time() - start < 2.0:
        if every and time.process_time() - mark > every:
            channel.mark_progress()
            mark = time.process_time()
    channel.notify({"op": "done", "result": {"success": True}})
    """
)


def test_cpu_bound_child_heartbeating_without_progress_is_reaped(tmp_path, monkeypatch):
    """A pure-Python loop that never returns still heartbeats; its CPU gives it away."""
    monkeypatch.setenv("PROGRESS_EVERY", "0")
    result = run_mine_in_child(
        {},
        palace_path=str(tmp_path),
        write_lock=threading.RLock(),
        argv=_child_argv(_BURNING_CHILD),
        stall_seconds=5.0,
        cpu_stall_seconds=0.5,
    )
    assert result.get("error_class") == "MineChildKilled", result
    assert "CPU-s without progress" in result["error"]


def test_long_compute_that_reports_progress_is_not_reaped(tmp_path, monkeypatch):
    """Embedding one 1000-drawer upsert takes minutes of CPU before its single
    storage call. Embedded slices count as progress, so that mine is not taken
    for a spinning one (which it was when only storage calls counted)."""
    monkeypatch.setenv("PROGRESS_EVERY", "0.2")
    result = run_mine_in_child(
        {},
        palace_path=str(tmp_path),
        write_lock=threading.RLock(),
        argv=_child_argv(_BURNING_CHILD),
        stall_seconds=5.0,
        cpu_stall_seconds=0.5,
    )
    assert result["success"] is True, result


def test_default_cpu_budget_is_a_multiple_of_the_stall_limit(tmp_path, monkeypatch):
    """With only ``stall_seconds`` set, a heartbeating compute may run several
    stall windows of CPU before it is judged stuck."""
    from mempalace import mine_supervisor

    monkeypatch.setenv("PROGRESS_EVERY", "0")
    assert mine_supervisor.CPU_STALL_FACTOR >= 5
    result = run_mine_in_child(
        {},
        palace_path=str(tmp_path),
        write_lock=threading.RLock(),
        argv=_child_argv(_BURNING_CHILD),
        stall_seconds=0.5,  # 2 CPU-s of work is under 5 x 0.5
    )
    assert result["success"] is True, result


def test_sliced_embedder_keeps_order_and_marks_progress(monkeypatch):
    from mempalace import mine_child

    calls = []

    def embed(texts, *, query=False, metadatas=None, embedder=None):
        calls.append((len(texts), None if metadatas is None else [m["i"] for m in metadatas]))
        return [[float(t)] for t in texts]

    marks = []
    monkeypatch.setattr(
        mine_child, "_CHANNEL", type("C", (), {"mark_progress": lambda self: marks.append(1)})()
    )
    texts = [str(i) for i in range(600)]
    metas = [{"i": i} for i in range(600)]
    out = mine_child._sliced_embedder(embed)(texts, metadatas=metas)
    assert out == [[float(i)] for i in range(600)]
    assert [n for n, _ in calls] == [256, 256, 88]
    assert all(n % 32 == 0 for n, _ in calls[:-1]), "slices must keep 32-doc sub-batches"
    assert calls[1][1] == list(range(256, 512)), "metadatas must stay aligned with texts"
    assert len(marks) == 3
    assert mine_child._sliced_embedder(embed)([]) == []


# A child that reports SCHED_IDLE, then goes silent: no heartbeat, no call.
_SILENT_IDLE_CHILD = textwrap.dedent(
    """
    import os, sys, time
    from mempalace import mine_child as m
    m._read_msg(sys.stdin.buffer)
    m._write_msg(sys.stdout.buffer, {"op": "started", "pid": os.getpid(),
                                     "priority": os.environ["CHILD_PRIORITY"]})
    time.sleep(float(os.environ["SILENT_FOR"]))
    m._write_msg(sys.stdout.buffer, {"op": "done", "result": {"success": True}})
    """
)


def _silent_idle(tmp_path, monkeypatch, sampler, silent_for, priority="sched-idle", stall=0.4):
    monkeypatch.setenv("SILENT_FOR", str(silent_for))
    monkeypatch.setenv("CHILD_PRIORITY", priority)
    return run_mine_in_child(
        {},
        palace_path=str(tmp_path),
        write_lock=threading.RLock(),
        argv=_child_argv(_SILENT_IDLE_CHILD),
        stall_seconds=stall,
        proc_sampler=sampler,
    )


_NO_SIGNAL = [
    ("nice-19", lambda pid: (100, True)),
    ("taskpolicy-background", lambda pid: None),
    ("windows-background", lambda pid: None),
    ("windows-idle", lambda pid: None),
    # SCHED_IDLE where /proc cannot be read.
    ("sched-idle", lambda pid: None),
]


@pytest.mark.parametrize("priority,sampler", _NO_SIGNAL, ids=[p for p, _ in _NO_SIGNAL])
def test_background_child_without_a_starvation_signal_gets_the_long_stall_limit(
    tmp_path, monkeypatch, priority, sampler
):
    """No way to tell starved from wedged: silence past the plain stall limit
    is not enough to kill a background-priority child."""
    result = _silent_idle(tmp_path, monkeypatch, sampler, 1.5, priority=priority, stall=0.2)
    assert result["success"], result


def test_background_child_without_a_signal_is_still_reaped_past_the_long_limit(
    tmp_path, monkeypatch
):
    from mempalace import mine_supervisor

    monkeypatch.setattr(mine_supervisor, "UNOBSERVED_STALL_FACTOR", 3.0)
    result = _silent_idle(tmp_path, monkeypatch, None, 60, priority="nice-19", stall=0.2)
    assert result["error_class"] == "MineChildKilled", result
    assert "stall limit 0.6 s" in result["error"]


def test_default_unobserved_stall_limit_is_well_past_a_minute():
    from mempalace import mine_supervisor

    limit = mine_supervisor.UNOBSERVED_STALL_FACTOR * mine_supervisor.DEFAULT_STALL_SECONDS
    assert limit >= 600


def test_normal_priority_child_keeps_the_plain_stall_limit(tmp_path, monkeypatch):
    """Not deprioritised, so not starved: the plain limit applies."""
    result = _silent_idle(tmp_path, monkeypatch, None, 60, priority="normal", stall=0.2)
    assert result["error_class"] == "MineChildKilled", result


def test_starved_idle_child_survives_heartbeat_silence(tmp_path, monkeypatch):
    """SCHED_IDLE on a saturated machine: runnable, CPU flat. Not a stall."""
    result = _silent_idle(tmp_path, monkeypatch, lambda pid: (100, True), silent_for=1.5)
    assert result["success"], result


def test_blocked_idle_child_is_reaped(tmp_path, monkeypatch):
    """CPU flat and no thread runnable: blocked for good (a native hang), not starved."""
    result = _silent_idle(tmp_path, monkeypatch, lambda pid: (100, False), silent_for=60)
    assert result["error_class"] == "MineChildKilled", result


def test_spinning_idle_child_is_reaped(tmp_path, monkeypatch):
    """CPU advancing while silent: the child runs but cannot talk. Killed."""
    ticks = iter(range(10**6))
    result = _silent_idle(tmp_path, monkeypatch, lambda pid: (next(ticks), True), silent_for=60)
    assert result["error_class"] == "MineChildKilled", result


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc sampling is Linux-only")
def test_real_sampler_reaps_a_sleeping_idle_child(tmp_path, monkeypatch):
    """Through /proc: a sleeping child has no runnable thread, so it is reaped."""
    from mempalace.mine_supervisor import _sample_proc

    assert _sample_proc(os.getpid())[1] is True  # this thread is running
    result = _silent_idle(tmp_path, monkeypatch, _sample_proc, silent_for=60)
    assert result["error_class"] == "MineChildKilled", result


def test_killed_mine_rebuilds_closet_for_committed_drawers(tmp_path, monkeypatch):
    from mempalace.palace import get_closets_collection, get_collection

    palace = str(tmp_path / "palace")
    source = _project(tmp_path)
    payload = {"palace_path": palace, "source": str(source), "mode": "projects", "wing": "proj"}
    skip_model = "import mempalace.embedding as emb\nemb.probe_dimension = lambda: _DIM\n"
    crash = "import os, mempalace.miner as miner\nminer.purge_file_closets = lambda *args: os._exit(7)\n"

    def run(extra):
        return run_mine_in_child(
            payload,
            palace_path=palace,
            write_lock=threading.RLock(),
            argv=_child_argv(skip_model + extra + REAL_CHILD),
        )

    assert run(crash)["error_class"] == "MineChildDied"
    original = get_collection(palace).get(include=["documents"])
    assert original.ids and "heron" in original.documents[0]
    assert run("")["success"]
    after = get_collection(palace).get(include=["documents"])
    assert (after.ids, after.documents) == (original.ids, original.documents)
    assert get_closets_collection(palace).count() > 0


def test_mine_sidecar_writes_are_served_by_the_daemon(tmp_path, monkeypatch):
    import contextlib

    from mempalace import hallways, miner, revisions

    writes = []

    @contextlib.contextmanager
    def track_hallways(config):
        writes.append("hallways")
        with writing(config) as conn:
            yield conn

    writing = hallways._writing
    monkeypatch.setattr(hallways, "_writing", track_hallways)
    monkeypatch.setattr(
        miner, "_compute_topic_tunnels_for_wing", lambda *a, **kw: writes.append("topics") or 0
    )
    monkeypatch.setattr(
        miner, "_compute_entity_tunnels_for_wing", lambda *a, **kw: writes.append("entities") or 0
    )
    monkeypatch.setattr(
        revisions, "append_drawer_revisions", lambda *a: writes.append("revisions") or ["rev"]
    )
    script = textwrap.dedent("""
        import sys
        from mempalace import mine_child, service
        def run(payload):
            from mempalace import hallways, miner, revisions
            from mempalace.config import MempalaceConfig
            cfg = MempalaceConfig(palace_path=payload["palace_path"])
            class Rows:
                def get(self, **kwargs):
                    rows = [{"entities": "First,Second", "room": "r", "wing": "w"}] if kwargs["offset"] == 0 else []
                    return {"ids": ["d"] if rows else [], "metadatas": rows}
            hallways.compute_hallways_for_wing("w", col=Rows(), config=cfg)
            miner._compute_topic_tunnels_for_wing("w", config=cfg)
            miner._compute_entity_tunnels_for_wing("w", config=cfg)
            revisions.append_drawer_revisions(cfg.palace_path, [("d","prior text",{})])
            return {"success":True}
        service.run_mine = run
        sys.exit(mine_child.main())
    """)
    result = run_mine_in_child(
        {"palace_path": str(tmp_path)},
        palace_path=str(tmp_path),
        write_lock=threading.RLock(),
        argv=_child_argv(script),
    )
    assert result["success"], result
    assert writes == ["hallways", "topics", "entities", "revisions"]


def test_adapter_with_direct_graph_access_runs_under_daemon_write_lock(tmp_path, monkeypatch):
    from mempalace import service

    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "state"))
    monkeypatch.setenv("MEMPALACE_DAEMON_MINE_CHILD", "1")
    runtime = daemon.DaemonRuntime(str(tmp_path))

    def execute(kind, payload):
        return {"success": True, "writer": os.getpid()}

    monkeypatch.setattr(service, "execute_job", execute)
    job = runtime.store.enqueue("mine", {"source_adapter": "fixture"})
    assert runtime._run_job(job, job.payload).get("writer") == os.getpid()


def test_mine_child_is_off_by_default(tmp_path, monkeypatch):
    """Starvation detection only works for SCHED_IDLE on Linux, so the child
    is opt-in for now."""
    from mempalace.config import MempalaceConfig

    monkeypatch.delenv("MEMPALACE_DAEMON_MINE_CHILD", raising=False)
    assert MempalaceConfig(config_dir=str(tmp_path)).daemon_mine_child is False
    (tmp_path / "config.json").write_text('{"daemon": {"mine_child": true}}')
    assert MempalaceConfig(config_dir=str(tmp_path)).daemon_mine_child is True
