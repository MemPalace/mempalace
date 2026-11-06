"""``backup --online``: the fence, the data lock and the supervisor.

The CLI runs in a subprocess with a fake ``backup_pause`` client, a fake
snapshot (``backup_online``) and a fake gateway on a Unix socket, so only the
CLI dispatch, the fence and the supervisor are under test. A "serve" here is a
real process that holds the shared ``data_lock`` and no fence, as ``serve``
does.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

from commonplace import cli
from commonplace.backup import FENCE_NAME, LOCK_NAME, STAGING_NAME

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")

ROOT = Path(__file__).resolve().parents[1]
STAMP = "20261107T030253.123456789Z-4242"

RUNNER = textwrap.dedent(
    r'''
    import fcntl, json, os, signal, socket, sys, time, types

    cfg = json.loads(os.environ["FENCE_TEST_CFG"])

    def note(**fields):
        with open(cfg["probe"], "a", encoding="utf-8") as f:
            f.write(json.dumps(fields) + "\n")

    def fence_fds(pid):
        """Descriptors of ``pid`` open on the fence file."""
        found = 0
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            found += target.endswith("/.commonplace-fence.lock")
        return found

    def held(path, exclusive_probe=False):
        """True if another holder blocks a new lock on ``path`` (new open file)."""
        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            return False
        try:
            mode = fcntl.LOCK_EX if exclusive_probe else fcntl.LOCK_SH
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)

    # fake commonplace.backup_pause (client side only)
    bp = types.ModuleType("commonplace.backup_pause")

    class PauseError(RuntimeError):
        def __init__(self, response):
            self.response = response
            self.error = response.get("error")
            self.reason = response.get("reason")
            self.state = response.get("state")
            super().__init__(f"{response.get('op')}: {self.error}")

    class Knobs:
        def __init__(self):
            env = os.environ
            self.heartbeat_stale_s = float(env.get("COMMONPLACE_BACKUP_HEARTBEAT_STALE_S", 60))
            self.deadline_s = float(env.get("COMMONPLACE_BACKUP_DEADLINE", 4500))
            self.fence_wait_s = float(env.get("COMMONPLACE_BACKUP_FENCE_WAIT_S", 60))

        @classmethod
        def from_env(cls):
            return cls()

        @staticmethod
        def read(name):
            assert name == "cli_fence_wait_s", name
            return float(os.environ.get("COMMONPLACE_FENCE_WAIT_S", 30))

    class PauseClient:
        def __init__(self, path=None, timeout=30.0):
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.settimeout(timeout)
            self._sock.connect(cfg["socket"])
            self._reader = self._sock.makefile("rb")

        def call(self, op, **fields):
            self._sock.sendall(json.dumps({"op": op, **fields}).encode() + b"\n")
            line = self._reader.readline()
            if not line:
                raise ConnectionError("gateway closed")
            return json.loads(line)

        def close(self):
            self._reader.close()
            self._sock.close()

    def answering(path=None, timeout=2.0):
        if not cfg.get("socket") or not os.path.exists(cfg["socket"]):
            return None
        try:
            client = PauseClient()
        except OSError:
            return None
        try:
            return client.call("hello", probe=True)
        finally:
            client.close()

    bp.PauseError, bp.Knobs, bp.PauseClient, bp.answering = PauseError, Knobs, PauseClient, answering
    sys.modules["commonplace.backup_pause"] = bp

    # fake commonplace.backup_online
    bo = types.ModuleType("commonplace.backup_online")

    def backup_online(data_dir, dest, *, client, deadline, knobs):
        data_dir, dest = str(data_dir), str(dest)
        note(
            event="snapshot",
            pid=os.getpid(),
            ppid=os.getppid(),
            sid=os.getsid(0),
            pgid=os.getpgid(0),
            client=type(client).__name__ if client is not None else None,
            fence_exclusive=held(os.path.join(data_dir, ".commonplace-fence.lock")),
            fence_fds=fence_fds(os.getpid()),
            supervisor_fence_fds=fence_fds(os.getppid()),
            data_lock_exclusive=held(os.path.join(data_dir, ".commonplace.lock")),
            stamp_locked=held(dest + ".lock", exclusive_probe=True),
            deadline=deadline,
        )
        mode = cfg.get("snapshot", "ok")
        if mode == "drop_hb":
            # The caller's cleanup removed the heartbeat before the supervisor saw it.
            os.unlink(dest + ".hb")
        if client is not None:
            client.hello()
            client.pause()
        if mode in ("sleep", "drop_hb"):
            while True:
                time.sleep(0.05)
        if mode == "ignore_term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                time.sleep(0.05)
        if mode == "fail":
            err = RuntimeError("refused")
            err.stage = "successor active"
            raise err
        if client is not None:
            client.freeze()
            client.progress(bytes=1)
            client.thaw()
            client.resume()
            if mode != "no_commit":
                client.commit()
        os.mkdir(dest + ".partial")
        with open(os.path.join(dest + ".partial", "manifest.json"), "w") as f:
            f.write('{"version": 1, "files": []}\n')
        os.rename(dest + ".partial", dest)
        return {"version": 1, "files": []}

    bo.backup_online = backup_online
    sys.modules["commonplace.backup_online"] = bo

    # lock spy: every data_lock request is recorded, in any process
    from commonplace import backup as _backup

    real_data_lock = _backup.data_lock

    def spy(data_dir, *, exclusive=False):
        with open(cfg["spy"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"pid": os.getpid(), "exclusive": exclusive}) + "\n")
        return real_data_lock(data_dir, exclusive=exclusive)

    _backup.data_lock = spy

    from commonplace import backup_supervisor, cli

    backup_supervisor.TERM_GRACE_S = backup_supervisor.KILL_GRACE_S = cfg.get("grace", 2.0)
    sys.exit(cli.main(sys.argv[1:]))
    '''
)


class FakeGateway:
    """JSON-lines gateway: records ops, tracks the run state, can drop the line."""

    TRANSITIONS = {
        "hello": "ACTIVE",
        "pause": "PAUSED",
        "resume": "RESUMED_OK",
        "commit": "COMMITTED",
        "abort": "INVALID",
    }

    def __init__(self, path: Path, data: Path, close_after=None):
        self.path, self.data, self.close_after = path, data, close_after
        self.ops, self.eof_states = [], []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        state = "NONE"
        with conn, conn.makefile("rb") as reader:
            for line in reader:
                message = json.loads(line)
                op, probe = message["op"], message.get("probe")
                self.ops.append(op + (":probe" if probe else ""))
                if not probe:
                    state = self.TRANSITIONS.get(op, state)
                response = {"ok": True, "op": op, "state": "NONE" if probe else state}
                if op == "hello":
                    response.update(
                        proto=1,
                        gateway_pid=os.getpid(),
                        data_dir=os.path.realpath(self.data),
                        worker_pids=[],
                        successor_active=False,
                        busy=False,
                    )
                conn.sendall(json.dumps(response).encode() + b"\n")
                if op == self.close_after and not probe:
                    break
        if state not in ("NONE", "COMMITTED"):
            self.eof_states.append(state)

    def close(self):
        self.server.close()


@pytest.fixture
def env(tmp_path):
    short = Path(tempfile.mkdtemp(prefix="cpf-", dir="/tmp"))
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    runner = tmp_path / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    box = {
        "data": data,
        "staging": data / STAGING_NAME,
        "dest": data / STAGING_NAME / STAMP,
        "runner": runner,
        "probe": tmp_path / "probe.jsonl",
        "spy": tmp_path / "spy.jsonl",
        "socket": short / "gw.sock",
        "procs": [],
        "gateway": None,
    }
    # As the backup hook does: the heartbeat exists before every online run.
    box["staging"].mkdir(mode=0o700)
    (box["staging"] / f"{STAMP}.hb").touch()
    yield box
    for proc in box["procs"]:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if box["gateway"] is not None:
        box["gateway"].close()
    shutil.rmtree(short, ignore_errors=True)


def gateway(env, **kwargs):
    env["gateway"] = FakeGateway(env["socket"], env["data"], **kwargs)
    return env["gateway"]


def serve_holder(env):
    """A process holding the shared data_lock and no fence, as ``serve`` does."""
    code = (
        "import sys, time; from commonplace.backup import data_lock; "
        "lock = data_lock(sys.argv[1]); lock.__enter__(); print('ready', flush=True); "
        "time.sleep(600)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(env["data"])],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    env["procs"].append(proc)
    assert proc.stdout.readline().strip() == "ready"
    return proc


def _cmd(env, *args, snapshot="ok", extra_env=None, grace=2.0):
    cfg = {
        "probe": str(env["probe"]),
        "spy": str(env["spy"]),
        "socket": str(env["socket"]) if env["gateway"] is not None else None,
        "snapshot": snapshot,
        "grace": grace,
    }
    run_env = {**os.environ, "PYTHONPATH": str(ROOT), "FENCE_TEST_CFG": json.dumps(cfg)}
    run_env.pop("COMMONPLACE_DATA", None)
    run_env.update(extra_env or {})
    argv = [sys.executable, str(env["runner"]), "--data", str(env["data"]), "backup", *args]
    return argv, run_env


def run_cli(env, *args, timeout=60, **kwargs):
    argv, run_env = _cmd(env, *args, **kwargs)
    return subprocess.run(
        argv, cwd=ROOT, env=run_env, capture_output=True, text=True, timeout=timeout
    )


def records(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def wait_for(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def alive(pid) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, IndexError):
        return False
    return state not in ("Z", "X")


def lock_free(path, exclusive=True) -> bool:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def assert_nothing_published(env):
    assert not env["dest"].exists()
    assert not Path(str(env["dest"]) + ".partial").exists()


# ── B11 head test: fence exclusive, data_lock never requested ──
def test_online_backup_beside_serve_takes_fence_and_never_data_lock(env):
    serve = serve_holder(env)
    gw = gateway(env)
    result = run_cli(env, "--online", "--deadline", str(time.time() + 60), str(env["dest"]))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(env["dest"] / "manifest.json")
    assert (env["dest"] / "manifest.json").is_file()
    assert records(env["spy"]) == [], "online backup requested data_lock while a gateway answered"
    (snap,) = [r for r in records(env["probe"]) if r["event"] == "snapshot"]
    assert snap["fence_exclusive"], "fence not held exclusive during the snapshot"
    assert snap["fence_fds"] == 1, "the snapshot child must take the fence exactly once"
    assert snap["supervisor_fence_fds"] == 0, "the supervisor parent must not hold the fence"
    assert not snap["data_lock_exclusive"]
    assert snap["stamp_locked"]
    assert snap["client"] == "RelayClient"
    assert snap["sid"] == snap["pid"] == snap["pgid"], "child is not in its own session"
    assert gw.ops == [
        "hello:probe",
        "hello",
        "pause",
        "freeze",
        "thaw",
        "resume",
        "commit",
    ]
    assert gw.eof_states == []
    assert serve.poll() is None, "serve was displaced"
    assert lock_free(env["data"] / FENCE_NAME)


def test_online_without_gateway_refuses_while_serve_holds_lock(env):
    serve_holder(env)
    result = run_cli(env, "--online", str(env["dest"]))
    assert result.returncode == 2
    assert "gateway not answering" in result.stderr
    assert [r["exclusive"] for r in records(env["spy"])] == [True]
    assert records(env["probe"]) == []
    assert_nothing_published(env)
    assert lock_free(env["data"] / FENCE_NAME)


def test_online_without_gateway_holds_exclusive_data_lock_for_whole_run(env):
    result = run_cli(env, "--online", str(env["dest"]))
    assert result.returncode == 0, result.stderr
    (snap,) = records(env["probe"])
    assert snap["client"] is None
    assert snap["data_lock_exclusive"], "a serve start would not be refused during the run"
    assert snap["fence_exclusive"]
    assert (env["dest"] / "manifest.json").is_file()
    assert lock_free(env["data"] / LOCK_NAME)


# ── one-shot CLIs and serve ──
def test_one_shot_cli_holds_fence_shared_and_serve_takes_none(tmp_path, monkeypatch):
    from commonplace import gateway as gateway_module

    data = tmp_path / "data"
    seen = {}

    def audit(args):
        seen["exclusive_blocked"] = not lock_free(data / FENCE_NAME, exclusive=True)
        seen["shared_ok"] = lock_free(data / FENCE_NAME, exclusive=False)
        return 0

    class FakeGateway:
        def __init__(self, db, supervisor):
            pass

        def serve(self, host, port):
            seen["serve"] = True

    monkeypatch.setattr(cli, "cmd_audit", audit)
    monkeypatch.setattr(gateway_module, "Gateway", FakeGateway)
    monkeypatch.setenv("COMMONPLACE_FENCE_WAIT_S", "0.3")
    assert cli.main(["--data", str(data), "audit"]) == 0
    assert seen == {"exclusive_blocked": True, "shared_ok": True}

    assert cli.main(["--data", str(data), "init", "--admin-email", "owner@example.com"]) == 0
    fd = os.open(data / FENCE_NAME, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a backup holds the fence
        started = time.monotonic()
        assert cli.main(["--data", str(data), "audit"]) == 2
        assert time.monotonic() - started >= 0.25
        assert cli.main(["--data", str(data), "serve"]) == 0
        assert seen.get("serve"), "serve waited on or was refused by the fence"
    finally:
        os.close(fd)


def test_one_shot_cli_refused_message(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("COMMONPLACE_FENCE_WAIT_S", "0")
    fd = os.open(data / FENCE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cli.main(["--data", str(data), "user", "list"]) == 2
    finally:
        os.close(fd)
    assert "a backup is running" in capsys.readouterr().err
    assert not (data / "control.sqlite3").exists(), "the CLI opened the DB before the fence"


def test_backup_waits_for_cli_fence_then_fails(env):
    gateway(env)
    fd = os.open(env["data"] / FENCE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # a one-shot CLI is running
        result = run_cli(
            env, "--online", str(env["dest"]), extra_env={"COMMONPLACE_BACKUP_FENCE_WAIT_S": "0.3"}
        )
    finally:
        os.close(fd)
    assert result.returncode == 2
    assert "fence busy" in result.stderr
    assert records(env["probe"]) == []
    assert_nothing_published(env)


# ── supervisor ──
def test_stale_heartbeat_stops_child_and_invalidates_run(env):
    gw = gateway(env)
    hb = env["staging"] / f"{STAMP}.hb"
    env["staging"].mkdir(mode=0o700, exist_ok=True)
    hb.touch()
    started = time.monotonic()
    result = run_cli(
        env,
        "--online",
        str(env["dest"]),
        snapshot="sleep",
        extra_env={"COMMONPLACE_BACKUP_HEARTBEAT_STALE_S": "1"},
    )
    assert result.returncode == 2
    assert "heartbeat stale" in result.stderr
    assert time.monotonic() - started < 15
    (snap,) = records(env["probe"])
    assert not alive(snap["pid"])
    assert "abort" in gw.ops or gw.eof_states
    assert lock_free(Path(str(env["dest"]) + ".lock"))
    assert_nothing_published(env)


def test_online_refuses_without_heartbeat_before_fork(env):
    gw = gateway(env)
    (env["staging"] / f"{STAMP}.hb").unlink()
    result = run_cli(env, "--online", str(env["dest"]))
    assert result.returncode == 2, result.stderr
    assert "(stage: heartbeat missing)" in result.stderr
    assert records(env["probe"]) == [], "a snapshot child ran without a heartbeat"
    assert "hello" not in gw.ops and "pause" not in gw.ops
    assert not Path(str(env["dest"]) + ".lock").exists()
    assert lock_free(env["data"] / FENCE_NAME)
    assert_nothing_published(env)


def test_heartbeat_removed_before_first_tick_aborts_within_a_tick(env):
    """With the default stale limit (60 s) a tick is 5 s; the heartbeat is gone
    before the supervisor first looks, so it is never "seen". The run must still
    abort at once instead of running to its deadline."""
    gw = gateway(env)
    started = time.monotonic()
    result = run_cli(
        env,
        "--online",
        "--deadline",
        str(time.time() + 25),
        str(env["dest"]),
        snapshot="drop_hb",
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 2, result.stderr
    assert "(stage: heartbeat gone)" in result.stderr, result.stderr
    assert elapsed < 15, f"abort took {elapsed:.1f} s"
    (snap,) = records(env["probe"])
    assert not alive(snap["pid"])
    assert "abort" in gw.ops or gw.eof_states
    assert lock_free(Path(str(env["dest"]) + ".lock"))
    assert lock_free(env["data"] / FENCE_NAME)
    assert_nothing_published(env)


def test_bad_cli_fence_knob_is_an_error_not_a_traceback(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("COMMONPLACE_FENCE_WAIT_S", "soon")
    assert cli.main(["--data", str(data), "user", "list"]) == 2
    err = capsys.readouterr().err
    assert "COMMONPLACE_FENCE_WAIT_S" in err and "Traceback" not in err


REAPED_RUNNER = textwrap.dedent(
    r"""
    import json, os, sys, time, types
    from pathlib import Path

    from commonplace import backup_supervisor as bs

    calls = []
    looked = set()
    real_waitpid = os.waitpid

    def child(data, target, channel, *rest):
        os.write(channel._w, b"not json\n")  # a protocol error, then a clean exit
        return 0

    def waitpid(pid, options):
        if options == os.WNOHANG and pid not in looked:
            looked.add(pid)
            return real_waitpid(pid, 0)  # the child is gone before the first look
        return real_waitpid(pid, options)

    def recorder(name):
        def kill(pid, sig):
            calls.append([name, pid, int(sig)])
            raise ProcessLookupError(pid)  # never signal for real: the pid is free
        return kill

    bs._child_main = child
    os.waitpid = waitpid
    os.killpg, os.kill = recorder("killpg"), recorder("kill")

    root = Path(sys.argv[1])
    target = root / "staging" / "20261107T030253.123456789Z-4242"
    target.parent.mkdir(parents=True)
    bs._sidecar(target, ".hb").touch()
    knobs = types.SimpleNamespace(heartbeat_stale_s=60)
    supervisor = bs._Supervisor(root, target, None, time.time() + 60, knobs)
    out = {"stage": None, "reaped_pid": None}
    try:
        supervisor.run()
    except bs.OnlineBackupError as exc:
        out["stage"] = exc.stage
    out["reaped_pid"] = sorted(looked)
    out["calls"] = calls
    print(json.dumps(out))
    """
)


def test_supervisor_never_signals_a_reaped_child(tmp_path):
    """A child reaped by the watch loop is never signalled: its pid may be reused.

    The child writes one non-JSON request and exits; the supervisor reaps it
    before it reads the request, so the abort reason is set by the drain after
    the reap ("relay protocol error"). Stopping the child then must not call
    ``killpg``/``kill`` on that pid.
    """
    runner = tmp_path / "reaped_runner.py"
    runner.write_text(REAPED_RUNNER)
    run_env = {**os.environ, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, str(runner), str(tmp_path)],
        cwd=ROOT,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["stage"] == "relay protocol error", out
    assert len(out["reaped_pid"]) == 1, out
    assert out["calls"] == [], f"signals sent to the reaped child pid: {out['calls']}"


def test_term_ignored_escalates_to_kill(env):
    gateway(env)
    env["staging"].mkdir(mode=0o700, exist_ok=True)
    (env["staging"] / f"{STAMP}.hb").touch()
    result = run_cli(
        env,
        "--online",
        str(env["dest"]),
        snapshot="ignore_term",
        grace=0.5,
        extra_env={"COMMONPLACE_BACKUP_HEARTBEAT_STALE_S": "1"},
    )
    assert result.returncode == 2
    assert "(stage: heartbeat stale)" in result.stderr
    assert "uninterruptible IO" not in result.stderr, "KILL never reached the child"
    (snap,) = records(env["probe"])
    assert not alive(snap["pid"])
    assert_nothing_published(env)


@pytest.mark.parametrize("with_gateway", [True, False], ids=["relay", "no_gateway"])
def test_killed_supervisor_takes_child_down(env, with_gateway):
    """``no_gateway``: the child sleeps outside any relay read, so only the
    parent-death signal or the lifeline pipe can stop it."""
    gw = gateway(env) if with_gateway else None
    argv, run_env = _cmd(env, "--online", str(env["dest"]), snapshot="sleep")
    proc = subprocess.Popen(
        argv, cwd=ROOT, env=run_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    env["procs"].append(proc)
    assert wait_for(lambda: records(env["probe"]) and (gw is None or "pause" in gw.ops))
    (snap,) = records(env["probe"])
    assert snap["ppid"] == proc.pid
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    assert wait_for(lambda: not alive(snap["pid"]), timeout=10), "child outlived its supervisor"
    if gw is not None:
        assert wait_for(lambda: gw.eof_states == ["PAUSED"]), "gateway never saw the run end"
    # A dying multi-threaded child shows Z before its last thread drops the files.
    assert wait_for(lambda: lock_free(Path(str(env["dest"]) + ".lock")), timeout=5)
    assert wait_for(lambda: lock_free(env["data"] / FENCE_NAME), timeout=5)
    assert wait_for(lambda: lock_free(env["data"] / LOCK_NAME), timeout=5)
    assert_nothing_published(env)


def test_deadline_stops_the_run(env):
    started = time.monotonic()
    result = run_cli(
        env, "--online", "--deadline", str(time.time() + 1.5), str(env["dest"]), snapshot="sleep"
    )
    assert result.returncode == 2
    assert "deadline" in result.stderr
    assert time.monotonic() - started < 15
    (snap,) = records(env["probe"])
    assert not alive(snap["pid"])
    assert_nothing_published(env)


def test_gateway_eof_aborts_the_run(env):
    gateway(env, close_after="pause")
    result = run_cli(env, "--online", str(env["dest"]), snapshot="sleep")
    assert result.returncode == 2
    assert "gateway socket closed" in result.stderr
    (snap,) = records(env["probe"])
    assert not alive(snap["pid"])
    assert_nothing_published(env)


def test_child_failure_reports_stage_and_aborts(env):
    gw = gateway(env)
    result = run_cli(env, "--online", str(env["dest"]), snapshot="fail")
    assert result.returncode == 2
    assert "stage: successor active" in result.stderr
    assert "Traceback" not in result.stderr
    assert gw.ops[-1] == "abort"
    assert_nothing_published(env)


def test_publish_without_commit_is_a_failure(env):
    gw = gateway(env)
    result = run_cli(env, "--online", str(env["dest"]), snapshot="no_commit")
    assert result.returncode == 2
    assert "stage: commit" in result.stderr
    assert "commit" not in gw.ops


# ── plain backup routing, check, cleanup, DEST rules ──
def test_plain_backup_goes_online_only_when_serving_and_answering(env):
    serve_holder(env)
    no_gateway = run_cli(env, str(env["dest"]))
    assert no_gateway.returncode == 2
    assert "data directory is in use" in no_gateway.stderr
    gw = gateway(env)
    outside = run_cli(env, str(env["data"].parent / "elsewhere"))
    assert outside.returncode == 2
    assert "in use" in outside.stderr and STAGING_NAME in outside.stderr
    result = run_cli(env, str(env["dest"]))
    assert result.returncode == 0, result.stderr
    assert "commit" in gw.ops
    assert (env["dest"] / "manifest.json").is_file()


def test_check_online(env):
    absent = run_cli(env, "--check-online")
    assert absent.returncode == 2
    assert "gateway not answering" in absent.stderr
    gateway(env)
    result = run_cli(env, "--check-online")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["online"] is True


def test_online_cleanup_is_lock_checked_and_never_signals(env):
    env["staging"].mkdir(mode=0o700, exist_ok=True)
    partial = Path(str(env["dest"]) + ".partial")
    partial.mkdir()
    (partial / "control.sqlite3").write_bytes(b"x" * 10)
    (env["staging"] / f"{STAMP}.hb").touch()
    lock = Path(str(env["dest"]) + ".lock")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
            "fcntl.flock(fd, fcntl.LOCK_EX); print('ready', flush=True); time.sleep(600)",
            str(lock),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    env["procs"].append(holder)
    assert holder.stdout.readline().strip() == "ready"
    busy = run_cli(env, "--online-cleanup", str(env["dest"]))
    assert busy.returncode == 2
    assert "staging locked" in busy.stderr
    assert partial.is_dir() and holder.poll() is None
    holder.kill()
    holder.wait()
    done = run_cli(env, "--online-cleanup", str(env["dest"]))
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert sorted(report["removed"]) == sorted([f"{STAMP}.partial", f"{STAMP}.hb", f"{STAMP}.lock"])
    assert report["bytes"] == 10
    assert sorted(os.listdir(env["staging"])) == []


@pytest.mark.parametrize(
    "dest",
    ["outside", "bad-stamp", "existing", "existing-partial"],
)
def test_online_dest_rules(env, dest):
    gateway(env)
    env["staging"].mkdir(mode=0o700, exist_ok=True)
    target = {
        "outside": env["data"].parent / STAMP,
        "bad-stamp": env["staging"] / "not a stamp",
        "existing": env["dest"],
        "existing-partial": env["dest"],
    }[dest]
    if dest == "existing":
        env["dest"].mkdir()
    if dest == "existing-partial":
        Path(str(env["dest"]) + ".partial").mkdir()
    result = run_cli(env, "--online", str(target))
    assert result.returncode == 2
    assert records(env["probe"]) == []


def test_backup_argument_rules(capsys):
    with pytest.raises(SystemExit) as missing:
        cli.main(["backup", "--online"])
    assert missing.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["backup", "--check-online", "--deadline", "123"])
    with pytest.raises(SystemExit):
        cli.main(["backup", "--online", "--online-cleanup", "x"])
    capsys.readouterr()
