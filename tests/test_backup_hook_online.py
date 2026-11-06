"""The backup hook keeps the serve container running and leaves no one-off container.

These tests drive ``backup-hook.sh`` against the fake Docker host in
``tests/backup_fakes.py``: the serve container is a real ``commonplace serve`` process
on a synthetic data directory, and every docker/compose call is logged. Every
assertion that guards the behaviour carries the ``RED238:`` prefix; harness problems
fail with ``SETUP:`` instead, so the two cannot be confused.

Only entry points that exist before the online backup do are imported here (the hook,
the public CLI, ``commonplace.backup``), so this module always collects.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from commonplace.backup import restore, verify_backup
from commonplace.control_db import ControlDB
from tests.backup_fakes import DISPLACING_VERBS, ROOT, FakeEngine, pid_alive

HOOK = ROOT / "backup-hook.sh"
VERBATIM = b"Exact words, kept verbatim.\n\x00\xff"
# A tiny reserve and margin so the disk preflight never depends on the test host's free space.
BASE_KNOBS = {
    "COMMONPLACE_BACKUP_RESERVE_BYTES": str(1 << 20),
    "COMMONPLACE_BACKUP_MARGIN_BYTES": str(1 << 20),
}
HOOK_TIMEOUT = 120.0
# Kill tests: a stale heartbeat, a short lease and a near deadline end the run quickly.
KILL_KNOBS = {
    "COMMONPLACE_BACKUP_HEARTBEAT_STALE_S": "3",
    "COMMONPLACE_BACKUP_LEASE_S": "2",
    "COMMONPLACE_BACKUP_DEADLINE": "20",
}
KILL_WINDOW = int(KILL_KNOBS["COMMONPLACE_BACKUP_HEARTBEAT_STALE_S"]) + 25
HUNG_DEADLINE = 10
# Deadline, then TERM, 10 s, KILL, 10 s, plus start-up slack.
HUNG_BOUND = HUNG_DEADLINE + 35
HANG_SOURCE = "workspaces/hang-source.bin"
SERVE_GONE = "RED238: serve displaced"
ONEOFF = "RED238: one-off left or serve displaced"


@dataclass
class Stack:
    tmp: Path
    data: Path
    backups: Path
    engine: FakeEngine
    workspace: str
    knobs: dict = field(default_factory=dict)
    runs: list = field(default_factory=list)

    @property
    def data_staging(self) -> Path:
        return self.data / ".backup-staging"

    @property
    def backup_staging(self) -> Path:
        return self.backups / ".commonplace-staging"


def make_data(root: Path, *, encrypted: bool = False) -> str:
    root.mkdir(mode=0o700)
    with closing(ControlDB(str(root / "control.sqlite3"))) as control:
        user = control.create_user("owner@example.test")
        workspace = control.create_workspace("hook-test", user, personal=True)
        sealed = control.create_workspace("sealed", user) if encrypted else None
    directory = root / "workspaces" / workspace
    directory.mkdir(parents=True)
    (directory / "verbatim.txt").write_bytes(VERBATIM)
    with closing(sqlite3.connect(directory / "notes.sqlite3", isolation_level=None)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE note (text TEXT)")
        conn.execute("INSERT INTO note VALUES ('kept as written')")
    (root / HANG_SOURCE).write_bytes(b"source bytes\n" * 64)
    if sealed:
        # Opt-in encryption metadata plus a ciphertext-like database file (synthetic).
        directory = root / "workspaces" / sealed
        directory.mkdir(parents=True)
        info = {
            "version": 1,
            "cipher": "sqlcipher",
            "workspace_id": sealed,
            "key_id": "a" * 32,
            "database_paths": [],
        }
        (directory / "workspace.json").write_text(json.dumps({"encryption": info}))
        (directory / "threads.v2.sqlite3").write_bytes(bytes(range(256)) * 16)
    return workspace


@pytest.fixture
def make_stack(tmp_path):
    engines = []
    stacks = []

    def build(*, encrypted=False, knobs=None, hang=False) -> Stack:
        data = tmp_path / "data"
        workspace = make_data(data, encrypted=encrypted)
        backups = tmp_path / "backups"
        backups.mkdir(mode=0o700)
        env = {**BASE_KNOBS, **(knobs or {})}
        engine = FakeEngine(
            tmp_path / "host", data, env=env, hang=hang, hang_source=HANG_SOURCE if hang else None
        )
        engines.append(engine)
        try:
            engine.up()
        except RuntimeError as exc:
            pytest.fail(f"SETUP: {exc}")
        stack = Stack(tmp_path, data.resolve(), backups.resolve(), engine, workspace, env)
        stacks.append(stack)
        return stack

    yield build
    for stack in stacks:
        for run in stack.runs:
            # A failed assertion can leave a hook running; never leak it or its logs.
            if run.proc.poll() is None:
                run.kill_tree()
            run.text()
    for engine in engines:
        engine.close()


# --------------------------------------------------------------------------- hook runs


@dataclass
class HookRun:
    stack: Stack
    proc: subprocess.Popen
    out: Path
    err: Path
    started: float
    logs: set = field(default_factory=set)

    def capture_logs(self) -> None:
        """Remember the hook's /tmp diagnostic logs from its open fds (before a kill)."""
        sid = self.proc.pid
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if int((entry / "stat").read_text().rsplit(")", 1)[1].split()[3]) != sid:
                    continue
                fds = list((entry / "fd").iterdir())
            except (OSError, ValueError, IndexError):
                continue
            for fd in fds:
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith("/tmp/commonplace-") and target.endswith(".log"):
                    self.logs.add(target)

    def text(self) -> str:
        """stdout, stderr and every diagnostic log the hook pointed at (then removed)."""
        parts = [self.out.read_text(errors="replace"), self.err.read_text(errors="replace")]
        for log in sorted({*re.findall(r"details: ([^)\s]+)\)", parts[1]), *self.logs}):
            path = Path(log)
            if path.is_file():
                parts.append(path.read_text(errors="replace"))
                if path.parent == Path("/tmp") and path.name.startswith("commonplace-"):
                    path.unlink()
        return "\n".join(parts)

    def kill_tree(self) -> None:
        self.capture_logs()
        for sig in (signal.SIGCONT, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def start_hook(stack: Stack, env=None) -> HookRun:
    out = stack.tmp / f"hook-{time.time_ns()}.out"
    err = out.with_suffix(".err")
    full_env = {**stack.engine.hook_env(), **stack.knobs, **(env or {})}
    with open(out, "wb") as stdout, open(err, "wb") as stderr:
        proc = subprocess.Popen(
            ["bash", str(HOOK), str(stack.backups)],
            env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    run = HookRun(stack, proc, out, err, time.monotonic())
    stack.runs.append(run)
    return run


def wait_hook(run: HookRun, timeout: float) -> bool:
    try:
        run.proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def run_hook_to_end(stack: Stack, env=None) -> tuple:
    run = start_hook(stack, env)
    finished = wait_hook(run, HOOK_TIMEOUT)
    if not finished:
        run.kill_tree()
    text = run.text()
    assert finished, f"RED238: hook did not finish within {HOOK_TIMEOUT:.0f} s:\n{text}"
    return run.proc.returncode, text


def wait_for_snapshot_call(run: HookRun, timeout: float = 60.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        calls = run.stack.engine.snapshot_calls()
        if calls:
            return calls[0]
        if run.proc.poll() is not None:
            break
        time.sleep(0.05)
    run.kill_tree()
    pytest.fail(
        "SETUP: the hook never made the snapshot call; "
        f"rc={run.proc.returncode}\n{run.text()}\ncalls={run.stack.engine.docker_calls()}"
    )


class HealthSampler(threading.Thread):
    def __init__(self, engine: FakeEngine, interval: float = 1.0):
        super().__init__(daemon=True)
        self.engine = engine
        self.interval = interval
        self.samples = []
        self.done = threading.Event()

    def run(self):
        while True:
            self.samples.append((time.time(), self.engine.healthz()))
            if self.done.wait(self.interval):
                break

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.done.set()
        self.join(10)


# --------------------------------------------------------------------------- observations


def serve_problems(stack: Stack, before: tuple, samples=()) -> list:
    problems = []
    after = stack.engine.identity()
    if after != before:
        problems.append(f"container (Id, StartedAt, pid, running) {before} -> {after}")
    if not stack.engine.serve_alive():
        problems.append("serve is not running")
    failed = [sample for sample in samples if sample[1] != 200]
    if failed:
        problems.append(f"{len(failed)}/{len(samples)} healthz samples failed: {failed[:3]}")
    displacing = [verb_line(call) for call in stack.engine.displacing_calls()]
    if displacing:
        problems.append(f"displacing docker calls: {displacing}")
    return problems


def verb_line(call: dict) -> str:
    """A short form of a logged docker call: the verb and what follows it."""
    argv = call["argv"]
    start = argv.index(call["verb"]) if call["verb"] in argv else 0
    return " ".join(argv[start:])


def published(stack: Stack) -> list:
    return sorted(stack.backups.glob("commonplace-*"))


def staging_entries(stack: Stack, prefix: str = "") -> list:
    out = []
    for directory in (stack.data_staging, stack.backup_staging):
        if directory.is_dir():
            out.extend(sorted(p for p in directory.iterdir() if p.name.startswith(prefix)))
    return out


def held_locks(stack: Stack) -> list:
    held = []
    candidates = [*stack.backups.glob("*.lock"), *stack.data.glob("*.lock")]
    for directory in (stack.data_staging, stack.backup_staging):
        if directory.is_dir():
            candidates.extend(directory.glob("*.lock"))
    for path in candidates:
        # Serve holds the data lock shared for its lifetime; nobody else may hold it.
        mode = fcntl.LOCK_SH if path.name == ".commonplace.lock" else fcntl.LOCK_EX
        try:
            with open(path, "rb") as handle:
                fcntl.flock(handle, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            held.append(str(path))
        except OSError:
            pass
    return held


def leftovers(stack: Stack) -> dict:
    return {
        "oneoffs": [record["Name"] for record in stack.engine.oneoffs()],
        "container processes": stack.engine.container_processes(),
    }


def wait_until(predicate, timeout: float, step: float = 0.2):
    end = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= end:
            return value
        time.sleep(step)


def no_leftovers(stack: Stack) -> bool:
    found = leftovers(stack)
    return not found["oneoffs"] and not found["container processes"]


def fd_holders(path: Path) -> list:
    """Live processes with an open descriptor on ``path``."""
    target = str(path)
    holders = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            fds = list((entry / "fd").iterdir())
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(fd) == target:
                    holders.append({"pid": int(entry.name), "argv": [a.decode() for a in argv[:6]]})
                    break
            except OSError:
                continue
    return holders


def lock_free(path: Path) -> bool:
    with open(path, "rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def session_sleeps(sid: int) -> list:
    """Live ``sleep`` processes still in the (exited) hook's session."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except (OSError, IndexError):
            continue
        if fields[0] == "Z" or int(fields[3]) != sid:
            continue
        if os.path.basename(argv[0].decode(errors="replace")) == "sleep":
            found.append({"pid": int(entry.name), "argv": [a.decode() for a in argv if a]})
    return found


def kill_session(sid: int) -> None:
    """SIGKILL every process left in a killed hook's session (timeout runs its own group)."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if int((entry / "stat").read_text().rsplit(")", 1)[1].split()[3]) == sid:
                os.kill(int(entry.name), signal.SIGKILL)
        except (OSError, ValueError, IndexError):
            continue


# --------------------------------------------------------------------------- H1


def test_hook_keeps_serve_running_and_publishes(make_stack):
    stack = make_stack()
    before = stack.engine.identity()
    with HealthSampler(stack.engine, interval=1.0) as health:
        rc, text = run_hook_to_end(stack)
    if not stack.engine.snapshot_calls():
        pytest.fail(f"SETUP: the hook never made the snapshot call; rc={rc}\n{text}")

    problems = serve_problems(stack, before, health.samples)
    assert not problems, f"{SERVE_GONE}: {problems}"
    assert rc == 0, f"RED238: hook failed with exit {rc}:\n{text}"
    snapshots = published(stack)
    assert len(snapshots) == 1, f"RED238: expected one published snapshot, found {snapshots}"
    snapshot = snapshots[0]
    stamp = snapshot.name.removeprefix("commonplace-")
    assert (snapshot / "manifest.json").is_file(), "RED238: published snapshot has no manifest"
    assert not staging_entries(stack, stamp), (
        f"RED238: staging still holds {staging_entries(stack, stamp)}"
    )
    try:
        verify_backup(snapshot)
        restored = stack.tmp / "restored"
        restore(snapshot, restored)
    except Exception as exc:  # the published tree must restore with the unchanged code
        raise AssertionError(f"RED238: published snapshot does not verify/restore: {exc}") from exc
    assert (restored / "workspaces" / stack.workspace / "verbatim.txt").read_bytes() == VERBATIM, (
        "RED238: restored snapshot lost verbatim bytes"
    )
    with closing(
        sqlite3.connect(restored / "workspaces" / stack.workspace / "notes.sqlite3")
    ) as conn:
        assert conn.execute("SELECT text FROM note").fetchall() == [("kept as written",)], (
            "RED238: restored snapshot lost a committed row"
        )
    assert wait_until(lambda: no_leftovers(stack), 10), f"RED238: leftovers {leftovers(stack)}"
    assert not held_locks(stack), f"RED238: locks still held {held_locks(stack)}"


# --------------------------------------------------------------------------- H2/H3


@pytest.mark.parametrize("signame", ["TERM", "KILL"])
def test_hook_kill_leaves_serve_and_no_oneoff(make_stack, signame):
    stack = make_stack(knobs=KILL_KNOBS, hang=True)
    before = stack.engine.identity()
    run = start_hook(stack)
    wait_for_snapshot_call(run)
    # Prefer a kill while the snapshot is inside its copy; not every path reaches it.
    wait_until(stack.engine.hang_engaged, 10)
    run.capture_logs()
    os.kill(run.proc.pid, getattr(signal, f"SIG{signame}"))

    problems = serve_problems(stack, before)
    assert not problems, f"{ONEOFF}: {problems}"

    settled = wait_until(lambda: run.proc.poll() is not None and no_leftovers(stack), KILL_WINDOW)
    hook_alive = run.proc.poll() is None
    if hook_alive:
        run.kill_tree()
    text = run.text()
    assert settled, (
        f"{ONEOFF}: {KILL_WINDOW} s after SIG{signame}: hook alive={hook_alive}, "
        f"{leftovers(stack)}\n{text}"
    )
    problems = serve_problems(stack, before)
    assert not problems, f"{ONEOFF}: {problems}"
    assert not published(stack), f"RED238: a killed run published {published(stack)}"
    assert wait_until(lambda: not held_locks(stack), 5), (
        f"RED238: locks still held after the kill: {held_locks(stack)}"
    )
    assert stack.engine.healthz() == 200, "RED238: serve stopped answering /healthz"


@pytest.mark.parametrize("stage", ["check-online", "snapshot", "cleanup"])
def test_hook_kill_frees_run_lock_at_once(make_stack, stage):
    """A KILLed hook's exec clients hold no descriptor on the per-run lock.

    The hook is killed while one exec client hangs: the CLI preflight
    (``--check-online``), the snapshot, or the cleanup after a failed snapshot
    (``--online-cleanup``). The heartbeat stale value is the production default
    (60 s), so a hung client outlives the hook for a minute or more; the next run
    must not be locked out for that window by an inherited lock descriptor.
    """
    stack = make_stack(knobs={"COMMONPLACE_BACKUP_DEADLINE": "300"}, hang=stage == "snapshot")
    if stage == "check-online":
        stack.engine.hang_exec("--check-online")
    elif stage == "cleanup":
        stack.engine.set_snapshot_exit(9)
        stack.engine.hang_exec("--online-cleanup")
    engaged = stack.engine.hang_engaged if stage == "snapshot" else stack.engine.exec_hang_engaged
    lock = stack.backups / ".commonplace-backup.lock"
    run = start_hook(stack)
    try:
        if not wait_until(engaged, 60):
            run.kill_tree()
            pytest.fail(
                f"SETUP: the {stage} hang never engaged; rc={run.proc.returncode}\n"
                f"{run.text()}\ncalls={stack.engine.docker_calls()}"
            )
        run.capture_logs()
        os.kill(run.proc.pid, signal.SIGKILL)
        run.proc.wait(timeout=10)
        wait_until(lambda: not fd_holders(lock) and lock_free(lock), 3)
        holders = fd_holders(lock)
        assert not holders, f"RED238: processes still hold the run lock fd: {holders}"
        assert lock_free(lock), "RED238: the run lock is still held after the hook was killed"
    finally:
        kill_session(run.proc.pid)


def test_hook_heartbeat_sleep_ends_with_the_hook(make_stack, tmp_path):
    """The heartbeat's running ``sleep`` does not outlive a finished hook.

    A ``sleep`` shim on PATH stretches the heartbeat interval to 30 s, so an
    orphaned sleep would still be visible well after the hook exits instead of
    expiring by chance inside the check window.
    """
    real_sleep = shutil.which("sleep", path=os.defpath)
    assert real_sleep, "SETUP: no sleep binary on the default PATH"
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sleep").write_text(f"#!/bin/sh\nexec {real_sleep} 30\n")
    (shim / "sleep").chmod(0o755)
    stack = make_stack()
    path = f"{shim}{os.pathsep}{stack.engine.hook_env()['PATH']}"
    run = start_hook(stack, env={"PATH": path})
    try:
        finished = wait_hook(run, HOOK_TIMEOUT)
        if not finished:
            run.kill_tree()
        text = run.text()
        assert finished and run.proc.returncode == 0, f"SETUP: hook failed:\n{text}"
        wait_until(lambda: not session_sleeps(run.proc.pid), 2)
        left = session_sleeps(run.proc.pid)
        assert not left, f"RED238: the heartbeat sleep outlived the hook: {left}"
    finally:
        kill_session(run.proc.pid)


# --------------------------------------------------------------------------- H4


def test_hook_bounds_hung_snapshot(make_stack):
    knobs = {"COMMONPLACE_BACKUP_DEADLINE": str(HUNG_DEADLINE)}
    stack = make_stack(knobs=knobs, hang=True)
    before = stack.engine.identity()
    run = start_hook(stack)
    finished = wait_hook(run, HUNG_BOUND)
    if not finished:
        run.kill_tree()
    text = run.text()
    assert finished, (
        f"RED238: hook not bounded: still running {HUNG_BOUND} s after start "
        f"with COMMONPLACE_BACKUP_DEADLINE={HUNG_DEADLINE}\n{text}"
    )
    if not stack.engine.snapshot_calls() or not stack.engine.hang_engaged():
        pytest.fail(
            "SETUP: the injected hang never engaged; "
            f"rc={run.proc.returncode}\n{text}\ncalls={stack.engine.docker_calls()}"
        )
    assert run.proc.returncode != 0, f"RED238: a hung snapshot reported success:\n{text}"
    problems = serve_problems(stack, before)
    assert not problems, f"{SERVE_GONE}: {problems}"
    assert not published(stack), f"RED238: a hung run published {published(stack)}"
    assert wait_until(lambda: no_leftovers(stack), 10), (
        f"RED238: hung snapshot left processes or one-offs: {leftovers(stack)}"
    )


# --------------------------------------------------------------------------- H5


def test_hook_never_invokes_displacing_compose_verbs(make_stack):
    stack = make_stack()
    results = {}
    seen = 0

    def scenario(name):
        nonlocal seen
        rc, text = run_hook_to_end(stack)
        calls = stack.engine.docker_calls()
        results[name] = (rc, text, calls[seen:])
        seen = len(calls)

    scenario("success")
    stack.engine.set_snapshot_exit(9)
    scenario("snapshot fails")
    stack.engine.set_snapshot_exit(None)
    stack.engine.stop_serve_directly()
    scenario("serve down")

    verbs = {
        name: [verb_line(call) for call in calls if call["verb"] in DISPLACING_VERBS]
        for name, (_, _, calls) in results.items()
    }
    assert not any(verbs.values()), f"RED238: displacing verb: {verbs}"
    rc, text, _ = results["success"]
    assert rc == 0, f"RED238: hook failed with exit {rc}:\n{text}"
    for name in ("snapshot fails", "serve down"):
        rc, text, _ = results[name]
        assert rc != 0, f"RED238: scenario {name!r} reported success:\n{text}"
    assert len(published(stack)) == 1, (
        f"RED238: only the successful run may publish: {published(stack)}"
    )
    assert not stack.engine.serve_alive(), "RED238: the hook started a stopped serve"


# --------------------------------------------------------------------------- H6


def test_hook_removes_leftover_staging_then_completes(make_stack):
    stack = make_stack()
    old = "20000101T000000.000000000Z-4242"
    stack.data_staging.mkdir(mode=0o700, exist_ok=True)
    partial = stack.data_staging / f"{old}.partial"
    partial.mkdir(mode=0o700)
    (partial / "control.sqlite3").write_bytes(b"\0" * 65536)
    (stack.data_staging / f"{old}.hb").touch()
    (stack.data_staging / f"{old}.lock").touch()  # present but not held: a dead run
    finished = stack.data_staging / old
    finished.mkdir(mode=0o700)
    (finished / "manifest.json").write_text("{}\n")
    stack.backup_staging.mkdir(mode=0o700, exist_ok=True)
    (stack.backup_staging / old).mkdir(mode=0o700)
    (stack.backup_staging / old / "manifest.json").write_text("{}\n")
    before = stack.engine.identity()

    rc, text = run_hook_to_end(stack)
    if not stack.engine.snapshot_calls():
        pytest.fail(f"SETUP: the hook never made the snapshot call; rc={rc}\n{text}")

    left = staging_entries(stack, old)
    assert not left, f"RED238: leftover not removed: {[str(p) for p in left]}"
    assert rc == 0, f"RED238: hook failed with exit {rc}:\n{text}"
    problems = serve_problems(stack, before)
    assert not problems, f"{SERVE_GONE}: {problems}"
    snapshots = published(stack)
    assert len(snapshots) == 1 and (snapshots[0] / "manifest.json").is_file(), (
        f"RED238: expected one new published snapshot, found {snapshots}"
    )
    assert old not in snapshots[0].name, "RED238: a leftover was published"


# --------------------------------------------------------------------------- H7


def test_hook_refuses_encrypted_and_leaves_serve(make_stack):
    stack = make_stack(encrypted=True)
    before = stack.engine.identity()
    with HealthSampler(stack.engine, interval=1.0) as health:
        rc, text = run_hook_to_end(stack)

    problems = serve_problems(stack, before, health.samples)
    assert not problems, f"{SERVE_GONE}: {problems}"
    assert rc != 0, f"RED238: encrypted data was not refused:\n{text}"
    assert "encrypted" in text.lower(), f"RED238: refusal does not name encryption:\n{text}"
    assert not published(stack), f"RED238: refused run published {published(stack)}"
    assert wait_until(lambda: no_leftovers(stack), 10), f"RED238: leftovers {leftovers(stack)}"
    assert pid_alive(before[2]), "RED238: serve process is gone"
