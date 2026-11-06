"""Admission gate (creators vs. the backup freeze) and the successor registry."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import admission
from commonplace.admission import (
    GATE,
    AdmissionError,
    AdmissionGate,
    FreezeTimeout,
    GateBusy,
    RegistryError,
    SuccessorRegistry,
)

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")

BLOCK_S = 0.3  # how long a frozen creator must stay blocked
JOIN_S = 10.0


@pytest.fixture(autouse=True)
def open_gate():
    GATE.thaw()
    yield
    GATE.thaw()


@pytest.fixture
def admin(tmp_path, monkeypatch):
    directory = tmp_path / "admin"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("COMMONPLACE_ADMIN_SOCKET", str(directory / "gateway.sock"))
    return directory


@pytest.fixture
def sessions():
    """Session ids to kill at teardown."""
    started = []
    yield started
    for sid in started:
        try:
            os.killpg(sid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _in_thread(target):
    box = SimpleNamespace(done=threading.Event(), error=None, value=None)

    def run():
        try:
            box.value = target()
        except BaseException as error:  # noqa: BLE001 - reported to the test
            box.error = error
        finally:
            box.done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    box.thread = thread
    return box


def _wait_for(predicate, timeout=JOIN_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _alive(pid):
    return admission.process_info(pid) is not None


# ── gate ──


def test_creating_waits_while_frozen_and_runs_after_thaw():
    gate = AdmissionGate()
    gate.freeze(timeout=1, lease_s=30)
    box = _in_thread(lambda: gate.creating().__enter__())
    assert not box.done.wait(BLOCK_S)
    assert gate.thaw() is True
    assert box.done.wait(JOIN_S) and box.error is None


def test_freeze_drains_running_creators():
    gate = AdmissionGate()
    entered, release = threading.Event(), threading.Event()

    def creator():
        with gate.creating():
            entered.set()
            release.wait(JOIN_S)

    worker = _in_thread(creator)
    assert entered.wait(JOIN_S)
    freezer = _in_thread(lambda: gate.freeze(timeout=JOIN_S, lease_s=30))
    assert not freezer.done.wait(BLOCK_S)  # draining
    late = _in_thread(lambda: gate.creating().__enter__())
    assert not late.done.wait(BLOCK_S)  # new creators wait behind a pending freeze
    release.set()
    assert freezer.done.wait(JOIN_S) and freezer.error is None
    assert worker.done.wait(JOIN_S)
    assert gate.frozen and not late.done.is_set()
    gate.thaw()
    assert late.done.wait(JOIN_S)


def test_freeze_times_out_and_reopens_the_gate():
    gate = AdmissionGate()
    entered, release = threading.Event(), threading.Event()

    def creator():
        with gate.creating():
            entered.set()
            release.wait(JOIN_S)

    _in_thread(creator)
    assert entered.wait(JOIN_S)
    with pytest.raises(FreezeTimeout):
        gate.freeze(timeout=0.2, lease_s=30)
    assert not gate.frozen
    other = _in_thread(lambda: gate.creating().__enter__())
    assert other.done.wait(JOIN_S)  # no longer blocked
    release.set()


def test_creating_is_reentrant_while_a_freeze_drains():
    gate = AdmissionGate()
    outer, nested_done, release = threading.Event(), threading.Event(), threading.Event()

    def creator():
        with gate.creating():
            outer.set()
            release.wait(JOIN_S)  # a freeze is now pending
            with gate.creating():  # must not wait for the freeze it blocks
                with gate.creating():
                    nested_done.set()

    worker = _in_thread(creator)
    assert outer.wait(JOIN_S)
    freezer = _in_thread(lambda: gate.freeze(timeout=JOIN_S, lease_s=30))
    assert _wait_for(lambda: gate._pending)
    release.set()
    assert nested_done.wait(JOIN_S), "nested creating() stalled behind the freeze"
    assert worker.done.wait(JOIN_S) and freezer.done.wait(JOIN_S)
    assert freezer.error is None and gate.frozen and gate.active == 0
    gate.thaw()


def test_freeze_lease_lapse_thaws_and_reports():
    gate = AdmissionGate()
    lapsed = threading.Event()
    gate.freeze(timeout=1, lease_s=0.2, on_lapse=lapsed.set)
    box = _in_thread(lambda: gate.creating().__enter__())
    assert lapsed.wait(JOIN_S)
    assert box.done.wait(JOIN_S)
    assert gate.lapsed and not gate.frozen
    assert gate.thaw() is False  # the backup learns the lease lapsed


def test_freeze_lease_from_environment(monkeypatch):
    monkeypatch.setenv("COMMONPLACE_BACKUP_FREEZE_LEASE_S", "0.2")
    gate = AdmissionGate()
    gate.freeze(timeout=1)
    assert _wait_for(lambda: not gate.frozen, 5)
    assert gate.lapsed
    monkeypatch.setenv("COMMONPLACE_BACKUP_FREEZE_LEASE_S", "nonsense")
    assert admission.freeze_lease_s() == 10.0


def test_freeze_is_exclusive_and_not_from_a_creator():
    gate = AdmissionGate()
    gate.freeze(timeout=1, lease_s=30)
    with pytest.raises(AdmissionError):
        gate.freeze(timeout=1, lease_s=30)
    gate.thaw()
    with gate.creating():
        with pytest.raises(AdmissionError):
            gate.freeze(timeout=1, lease_s=30)


def test_frozen_open_of_existing_paths_does_not_wait(tmp_path):
    gate = AdmissionGate()
    existing = tmp_path / "boards.sqlite3"
    existing.write_bytes(b"")
    gate.freeze(timeout=1, lease_s=30)
    fast = _in_thread(lambda: gate.creating(existing).__enter__())
    assert fast.done.wait(JOIN_S)
    slow = _in_thread(lambda: gate.creating(existing, tmp_path / "new.sqlite3").__enter__())
    assert not slow.done.wait(BLOCK_S)
    gate.thaw()
    assert slow.done.wait(JOIN_S)


def test_existing_path_waiter_passes_once_the_drain_completes(tmp_path):
    gate = AdmissionGate()
    existing = tmp_path / "rules.sqlite3"
    existing.write_bytes(b"")
    entered, release = threading.Event(), threading.Event()

    def creator():
        with gate.creating():
            entered.set()
            release.wait(JOIN_S)

    _in_thread(creator)
    assert entered.wait(JOIN_S)
    freezer = _in_thread(lambda: gate.freeze(timeout=JOIN_S, lease_s=30))
    assert _wait_for(lambda: gate._pending)
    opener = _in_thread(lambda: gate.creating(existing).__enter__())
    assert not opener.done.wait(BLOCK_S)  # still draining: wait
    release.set()
    assert freezer.done.wait(JOIN_S) and gate.frozen
    assert opener.done.wait(JOIN_S), "existing-path open stayed blocked while frozen"
    assert gate.frozen
    gate.thaw()


def test_creating_timeout_raises_gate_busy():
    gate = AdmissionGate()
    gate.freeze(timeout=1, lease_s=30)
    with pytest.raises(GateBusy):
        with gate.creating(timeout=0.05):
            pass
    assert gate.busy == 1
    gate.thaw()
    with gate.creating(timeout=0.05):
        pass


# ── wrapped sites (frozen → blocked; thawed → done) ──


class _Supervisor:
    def __init__(self, root):
        self.root = Path(root)

    def workspace_dir(self, workspace_id):
        return self.root / workspace_id


def _site_cases(tmp_path):
    from commonplace.boards.store import BoardStore
    from commonplace.chat.store import ReplyStore
    from commonplace.control_db import ControlDB
    from commonplace.handoff import launchers, manual
    from commonplace.handoff.wiring import HandoffContext
    from commonplace.rules.store import RuleStore
    from commonplace.runner.jobs import JobStore
    from commonplace.skills.store import SkillStore
    from commonplace.workspace_store import open_threads

    control = ControlDB(str(tmp_path / "control.sqlite3"))
    owner = control.create_user("owner@example.test")
    supervisor = _Supervisor(tmp_path / "sup")
    gateway = SimpleNamespace(
        supervisor=supervisor, workspace_dir=lambda ws: str(supervisor.workspace_dir(ws))
    )
    skills_dir = tmp_path / "skills-ws"
    threads_dir = _mk(tmp_path / "threads-ws")
    SkillStore(skills_dir)  # exists before the freeze; append creates files
    context = HandoffContext(gateway=None, control_db=None, data_dir=tmp_path / "data")

    def append_skill():
        SkillStore(skills_dir).append(
            [{"name": "s", "markdown": "# s\n", "source": {"kind": "test"}}]
        )
        return skills_dir

    def manual_lock():
        with manual._start_lock(tmp_path / "manual", "ws_a", "thread"):
            pass
        return tmp_path / "manual"

    return {
        "control_db.create_workspace": lambda: control.create_workspace("W", owner),
        "workspace_store.open_threads": lambda: open_threads(threads_dir),
        "wiring.store_for": lambda: context.store_for("ws_abc123"),
        "chat.store": lambda: ReplyStore(gateway, "ws_chat")._connection(create=True),
        "boards.store": lambda: BoardStore(tmp_path / "boards-ws"),
        "rules.store": lambda: RuleStore(tmp_path / "rules-ws"),
        "runner.jobs": lambda: JobStore(gateway, "ws_jobs"),
        "skills.store.init": lambda: SkillStore(tmp_path / "skills-new"),
        "skills.store.append": append_skill,
        "launchers.write_prompt": lambda: launchers._write_prompt(tmp_path / "p", "hof_x", "hi"),
        "manual.start_lock": manual_lock,
    }


def _mk(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


SITES = [
    "control_db.create_workspace",
    "workspace_store.open_threads",
    "wiring.store_for",
    "chat.store",
    "boards.store",
    "rules.store",
    "runner.jobs",
    "skills.store.init",
    "skills.store.append",
    "launchers.write_prompt",
    "manual.start_lock",
]


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


# Sites gated for consistency that create no path of their own (a control row).
PATHLESS = {"control_db.create_workspace"}


@pytest.mark.parametrize("site", SITES)
def test_frozen_site_blocks_until_thaw(site, tmp_path, admin):
    call = _site_cases(tmp_path)[site]
    before = _tree(tmp_path)
    GATE.freeze(timeout=1, lease_s=30)
    try:
        box = _in_thread(call)
        # Every site: the call itself is held while frozen.
        assert not box.done.wait(BLOCK_S), f"{site} ran while the gate was frozen"
        if site not in PATHLESS:
            assert _tree(tmp_path) == before, f"{site} changed the tree while frozen"
    finally:
        GATE.thaw()
    assert box.done.wait(JOIN_S) and box.error is None, box.error
    if site in PATHLESS:
        assert box.value, f"{site} returned nothing after the thaw"
    else:
        assert _tree(tmp_path) != before, f"{site} created nothing after the thaw"


def test_frozen_console_append_drops_instead_of_waiting(tmp_path):
    from commonplace.console.store import ActivityStore

    store = ActivityStore(SimpleNamespace(supervisor=_Supervisor(tmp_path)), "ws_console")
    busy = GATE.busy
    GATE.freeze(timeout=1, lease_s=30)
    try:
        started = time.monotonic()
        assert store.append("conn", "memory_search", "ok", 3) is None
        assert time.monotonic() - started < 2
        assert store.dropped == 1 and GATE.busy == busy + 1
        assert not (tmp_path / "ws_console" / "console.sqlite3").exists()
    finally:
        GATE.thaw()
    assert store.append("conn", "memory_search", "ok", 3) is not None


# ── registry ──


def _registry(admin):
    return SuccessorRegistry(admin / "successors.json")


def _spawn(argv, sessions, env=None):
    process = subprocess.Popen(
        argv, start_new_session=True, stdin=subprocess.DEVNULL, env={**os.environ, **(env or {})}
    )
    sessions.append(process.pid)
    return process


def _pid_from(path):
    assert _wait_for(lambda: path.exists() and path.read_text().strip())
    return int(path.read_text().strip())


def test_after_gateway_restart_descendant_keeps_refusing(admin, sessions, tmp_path):
    """(a) leader exited before the restart; a non-Python descendant is alive."""
    registry = _registry(admin)
    pidfile = tmp_path / "child.pid"
    token = registry.begin_launch("hof_a")
    leader = _spawn(["sh", "-c", f'sleep 60 & echo $! > "{pidfile}"; exit 0'], sessions)
    registry.launched(token, leader.pid)
    child = _pid_from(pidfile)
    assert leader.wait(JOIN_S) == 0 and not _alive(leader.pid)

    restarted = _registry(admin)  # a new gateway reads the persisted registry
    live = restarted.rehydrate()
    assert [e["sid"] for e in live] == [leader.pid]
    assert live[0]["live_pids"] == [child]
    fields = restarted.hello_fields()
    assert fields["successor_sids"] == [leader.pid] and fields["successor_pids"] == [child]


def test_grandchild_forked_after_preflight_keeps_refusing(admin, sessions, tmp_path):
    """(b) preflight sees the leader; the leader forks and exits; next preflight refuses."""
    registry = _registry(admin)
    gate, pidfile = tmp_path / "go", tmp_path / "grandchild.pid"
    with registry.launching("hof_b") as started:
        leader = _spawn(
            [
                "sh",
                "-c",
                f'while [ ! -e "{gate}" ]; do sleep 0.05; done; '
                f'(sleep 60 & echo $! > "{pidfile}"); exit 0',
            ],
            sessions,
        )
        started(leader.pid)
    first = registry.active()
    assert first and leader.pid in first[0]["live_pids"]
    gate.write_text("")
    grandchild = _pid_from(pidfile)
    assert leader.wait(JOIN_S) == 0
    second = registry.active()
    assert second and second[0]["live_pids"] == [grandchild]


def _fake_proc(root, processes):
    """processes: {pid: (state, sid, starttime)}"""
    import shutil

    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    for pid, (state, sid, starttime) in processes.items():
        (root / str(pid)).mkdir()
        fields = [state, "1", str(sid), str(sid)] + ["0"] * 15 + [str(starttime)] + ["0"] * 10
        (root / str(pid) / "stat").write_text(f"{pid} (claude) {' '.join(fields)}\n")
    return str(root)


def _seed(path, entries, counter=0):
    path.write_text(json.dumps({"version": 1, "counter": counter, "entries": entries}))
    path.chmod(0o600)


def test_emptied_session_is_pruned_then_reused_sid_passes(admin, tmp_path):
    """(c) prune-on-empty, then a new leader with pid == stored sid does not refuse."""
    path = admin / "successors.json"
    entry = {"state": "running", "token": "t", "handoff_id": "hof_c", "pid": 100}
    entry.update(starttime=5, sid=100, sid_leader_starttime=5)
    _seed(path, [entry])
    proc = tmp_path / "proc"
    registry = SuccessorRegistry(path, proc=_fake_proc(proc, {1: ("S", 1, 1)}))
    assert registry.active() == []
    assert registry.entries() == []  # pruned from the file, not just hidden
    _fake_proc(proc, {1: ("S", 1, 1), 100: ("S", 100, 999)})
    assert registry.hello_fields()["successor_sids"] == []


def test_reused_leader_pid_with_new_starttime_is_another_session(admin, tmp_path):
    """B5.m1: a live pid == sid with a different starttime leads a different session."""
    path = admin / "successors.json"
    entry = {"state": "running", "token": "t", "handoff_id": "hof_r", "pid": 100}
    entry.update(starttime=5, sid=100, sid_leader_starttime=5)
    _seed(path, [entry])
    proc = _fake_proc(tmp_path / "proc", {100: ("S", 100, 999), 101: ("S", 100, 1000)})
    assert SuccessorRegistry(path, proc=proc).active() == []
    _fake_proc(tmp_path / "proc", {100: ("S", 100, 5), 101: ("S", 100, 6)})
    _seed(path, [entry])
    live = SuccessorRegistry(path, proc=proc).active()
    assert live and live[0]["live_pids"] == [100, 101]


def test_leader_gone_any_session_member_keeps_entry(admin, tmp_path):
    path = admin / "successors.json"
    entry = {"state": "running", "token": "t", "handoff_id": "hof_m", "pid": 100}
    entry.update(starttime=5, sid=100, sid_leader_starttime=5)
    _seed(path, [entry])
    proc = _fake_proc(tmp_path / "proc", {300: ("R", 100, 50)})
    live = SuccessorRegistry(path, proc=proc).active()
    assert live and live[0]["live_pids"] == [300]


def test_zombie_only_session_is_pruned(admin, sessions):
    """(d) the only remaining member is an unreaped zombie → pruned, nothing refuses."""
    registry = _registry(admin)
    with registry.launching("hof_d") as started:
        zombie = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
        sessions.append(zombie.pid)
        started(zombie.pid)
    assert _wait_for(lambda: Path(f"/proc/{zombie.pid}/stat").exists() and not _alive(zombie.pid))
    assert Path(f"/proc/{zombie.pid}").exists()  # still unreaped
    assert registry.active() == [] and registry.entries() == []
    zombie.wait(JOIN_S)


def test_starting_entry_refuses_and_is_never_pruned(admin, tmp_path):
    registry = SuccessorRegistry(
        admin / "successors.json", proc=_fake_proc(tmp_path / "proc", {1: ("S", 1, 1)})
    )
    registry.begin_launch("hof_s")
    for _ in range(2):
        fields = registry.hello_fields()
        assert fields["successors_starting"] == ["hof_s"]
        assert fields["successor_counter"] == 1
    assert [e["state"] for e in registry.entries()] == ["starting"]


def test_spawn_failure_removes_entry_and_keeps_bump(admin):
    registry = _registry(admin)
    before = registry.counter()
    with pytest.raises(OSError):
        with registry.launching("hof_f") as started:
            started(subprocess.Popen(["/nonexistent/successor-binary"]).pid)
    assert registry.entries() == []
    assert registry.counter() == before + 1  # a run spanning this attempt ends INVALID


def test_restart_adopts_starting_entry_with_live_child(admin, sessions):
    registry = _registry(admin)
    registry.begin_launch("hof_adopt")
    registry.begin_launch("hof_gone")
    child = _spawn(["sleep", "60"], sessions, env={"COMMONPLACE_HANDOFF_ID": "hof_adopt"})
    assert _wait_for(lambda: _alive(child.pid))
    time.sleep(0.05)  # exec done: environ is the child's
    live = _registry(admin).rehydrate()
    assert [(e["state"], e["handoff_id"], e["pid"]) for e in live] == [
        ("running", "hof_adopt", child.pid)
    ]
    assert live[0]["sid"] == child.pid
    assert [e["handoff_id"] for e in _registry(admin).entries()] == ["hof_adopt"]


def test_registry_file_is_private_and_corruption_fails_closed(admin):
    registry = admission.REGISTRY  # resolves from COMMONPLACE_ADMIN_SOCKET
    assert registry.path == admin / "successors.json"
    registry.begin_launch("hof_p")
    assert (registry.path.stat().st_mode & 0o777) == 0o600
    registry.path.write_text("{not json")
    with pytest.raises(RegistryError):
        registry.hello_fields()
    with pytest.raises(RegistryError):
        registry.begin_launch("hof_q")


def test_insecure_admin_dir_is_refused(tmp_path, monkeypatch):
    directory = tmp_path / "open"
    directory.mkdir()
    directory.chmod(0o755)
    monkeypatch.setenv("COMMONPLACE_ADMIN_SOCKET", str(directory / "gateway.sock"))
    with pytest.raises(RegistryError):
        admission.REGISTRY.counter()


# ── launcher path ──


def _handoff():
    from commonplace.handoff.models import Handoff

    return Handoff(
        id="hof_launch-gate",
        workspace_id="ws_test",
        idempotency_key="thread:source",
        source_thread_id="source",
        state="stored",
        stored_thread_id="thr_stored",
    )


@pytest.fixture
def command_launchers(monkeypatch):
    monkeypatch.setenv("COMMONPLACE_HANDOFF_LAUNCHERS", "command")


def test_command_launch_registers_before_spawn(
    admin, tmp_path, sessions, command_launchers, monkeypatch
):
    from commonplace.handoff import launchers
    from commonplace.handoff.models import LauncherConfig

    registry = admission.REGISTRY
    seen = []
    original = subprocess.Popen

    def popen(*args, **kwargs):
        seen.append([e["state"] for e in registry.entries()])
        process = original(*args, **kwargs)
        sessions.append(process.pid)
        return process

    monkeypatch.setattr(launchers.subprocess, "Popen", popen)
    config = LauncherConfig(kind="command", argv=("sh", "-c", "sleep 60 & exit 0"))
    ref = launchers.launch(config, _handoff(), "prompt", prompt_dir=str(tmp_path / "p"))
    pid = int(ref.agent_ref.split(":")[1])
    assert seen == [["starting"]]  # intent recorded before the spawn
    assert registry.counter() == 1
    fields = registry.hello_fields()
    assert fields["successor_sids"] == [pid] and fields["successor_pids"]


def test_command_launch_retries_a_failed_registration_once(
    admin, tmp_path, sessions, command_launchers, monkeypatch
):
    from commonplace.handoff import launchers
    from commonplace.handoff.models import LauncherConfig

    registry = admission.REGISTRY
    real = registry.launched
    calls = []

    def flaky(token, pid):
        calls.append(pid)
        if len(calls) == 1:
            sessions.append(pid)
            raise admission.RegistryError("transient")
        return real(token, pid)

    monkeypatch.setattr(registry, "launched", flaky)
    config = LauncherConfig(kind="command", argv=("sh", "-c", "sleep 60 & exit 0"))
    launchers.launch(config, _handoff(), "prompt", prompt_dir=str(tmp_path / "p"))
    assert len(calls) == 2
    assert [e["state"] for e in registry.entries()] == ["running"], "a sticky starting entry"


def test_command_spawn_failure_leaves_no_entry_and_bumps(admin, tmp_path, command_launchers):
    from commonplace.handoff import launchers
    from commonplace.handoff.models import LauncherConfig

    config = LauncherConfig(kind="command", argv=("/nonexistent/claude",))
    with pytest.raises(launchers.LaunchError):
        launchers.launch(config, _handoff(), "prompt", prompt_dir=str(tmp_path / "p"))
    assert admission.REGISTRY.entries() == []
    assert admission.REGISTRY.counter() == 1


def test_command_launch_refused_when_registry_unavailable(tmp_path, monkeypatch, command_launchers):
    from commonplace.handoff import launchers
    from commonplace.handoff.models import LauncherConfig

    directory = tmp_path / "open"
    directory.mkdir()
    directory.chmod(0o755)
    monkeypatch.setenv("COMMONPLACE_ADMIN_SOCKET", str(directory / "gateway.sock"))
    spawned = []
    monkeypatch.setattr(launchers.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    config = LauncherConfig(kind="command", argv=("sh", "-c", "exit 0"))
    with pytest.raises(launchers.LaunchError, match="record"):
        launchers.launch(config, _handoff(), "prompt", prompt_dir=str(tmp_path / "p"))
    assert spawned == []


def test_frozen_command_launch_waits_and_is_counted_after_thaw(
    admin, tmp_path, sessions, command_launchers
):
    from commonplace.handoff import launchers
    from commonplace.handoff.models import LauncherConfig

    config = LauncherConfig(kind="command", argv=("sh", "-c", "exit 0"))
    GATE.freeze(timeout=1, lease_s=30)
    try:
        box = _in_thread(
            lambda: launchers.launch(config, _handoff(), "prompt", prompt_dir=str(tmp_path / "p"))
        )
        assert not box.done.wait(BLOCK_S)
        assert admission.REGISTRY.counter() == 0
    finally:
        GATE.thaw()
    assert box.done.wait(JOIN_S) and box.error is None, box.error
    assert admission.REGISTRY.counter() == 1
