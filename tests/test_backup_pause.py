"""The online-backup pause socket: path rules, protocol, run fence, worker pause."""

import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from commonplace import backup_pause as bp
from commonplace.workers import (
    DrainTimeout,
    Worker,
    WorkersPaused,
    WorkerSupervisor,
    _proc_starttime,
    process_gone,
    stop_process,
)
from tests._commonplace_helpers import hub_fixture  # noqa: F401

pytestmark = pytest.mark.skipif(not bp.supported(), reason="admin socket is Linux only")


def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def short_dir():
    # AF_UNIX paths are short; pytest's tmp_path can exceed the limit.
    path = Path(tempfile.mkdtemp(prefix="cpbp-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeAdmission:
    def __init__(self):
        self.counter = 0
        self.entries = []
        self.frozen = False
        self.freezes = 0
        self.thaws = 0
        self.fail_freeze = False
        self.freeze_delay = 0.0
        self.error = None
        self.rehydrated = 0
        self.on_lapse = None

    def freeze(self, timeout, lease_s, on_lapse):
        if self.freeze_delay:
            time.sleep(self.freeze_delay)
        if self.fail_freeze:
            raise TimeoutError("creators still running")
        self.freezes += 1
        self.frozen = True
        self.on_lapse = on_lapse
        return self.freezes

    def thaw(self):
        held, self.frozen = self.frozen, False
        self.thaws += 1
        return held

    def successors(self):
        if self.error:
            raise self.error
        return [dict(e) for e in self.entries]

    def hello_fields(self):
        if self.error:
            raise self.error
        return {
            "successor_pids": sorted(e["pid"] for e in self.entries if "pid" in e),
            "successor_sids": sorted(e["sid"] for e in self.entries if "sid" in e),
            "successors_starting": [e["handoff_id"] for e in self.entries if "pid" not in e],
            "successor_counter": self.counter,
        }

    def successor_counter(self):
        if self.error:
            raise self.error
        return self.counter

    def rehydrate(self):
        self.rehydrated += 1


class ProcLauncher:
    """Workers that are real processes, so pause must really stop them."""

    def __init__(self, ignore_term=False):
        self.ignore_term = ignore_term
        self.procs = []

    def start(self, workspace_id, workspace_dir):
        script = "trap '' TERM; while :; do sleep 0.05; done" if self.ignore_term else "sleep 60"
        proc = subprocess.Popen(["sh", "-c", script], stdin=subprocess.DEVNULL)
        self.procs.append(proc)
        time.sleep(0.05)  # let the trap be installed
        return Worker(
            workspace_id,
            "127.0.0.1",
            1,
            "t",
            stop=lambda: stop_process(proc, 0.3, 2),
            alive=lambda: proc.poll() is None,
            pid=proc.pid,
            starttime=_proc_starttime(proc.pid),
        )


@pytest.fixture
def env(tmp_path, short_dir):
    made = []

    def make(launcher=None, **knobs):
        defaults = dict(
            lease_s=2.0, freeze_lease_s=2.0, pause_max_s=30.0, drain_s=2.0, memory_wait_s=0.2
        )
        defaults.update(knobs)
        sup = WorkerSupervisor(str(tmp_path / "data"), launcher=launcher or ProcLauncher())
        sup.pause_wait_s = defaults["memory_wait_s"]
        adm = FakeAdmission()
        server = bp.PauseServer(
            sup,
            tmp_path / "data",
            path=short_dir / "s" / "gateway.sock",
            admission=adm,
            knobs=bp.Knobs(**defaults),
            tick_s=0.02,
        )
        assert server.start()
        made.append((server, sup))
        return server, sup, adm

    yield make
    for server, sup in made:
        server.stop()
        sup.close()


def _client(server):
    return bp.PauseClient(server.path, timeout=10)


# ── path rules ──
def test_socket_path_default_and_env():
    assert bp.socket_path({}) == Path(f"/tmp/commonplace-{os.getuid()}/gateway.sock")
    assert bp.socket_path({bp.SOCKET_ENV: "/run/x/a.sock"}) == Path("/run/x/a.sock")


def test_socket_path_inside_data_dir_refused(tmp_path, short_dir):
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(bp.UnsafeSocketPath):
        bp.check_socket_path(data / "gateway.sock", data)
    with pytest.raises(bp.UnsafeSocketPath):
        bp.check_socket_path(data / "sub" / "gateway.sock", data)
    link = short_dir / "via-link"
    link.symlink_to(data)
    with pytest.raises(bp.UnsafeSocketPath):
        bp.check_socket_path(link / "gateway.sock", data)
    with pytest.raises(bp.UnsafeSocketPath):
        bp.check_socket_path("relative.sock", data)
    with pytest.raises(bp.UnsafeSocketPath):
        bp.check_socket_path("/tmp/" + "x" * 120, data)
    assert bp.check_socket_path(short_dir / "gateway.sock", data) == short_dir / "gateway.sock"


def test_server_refuses_socket_under_data_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    sup = WorkerSupervisor(str(data), launcher=ProcLauncher())
    server = bp.PauseServer(sup, data, path=data / "g.sock", admission=FakeAdmission())
    assert server.start() is False
    assert not (data / "g.sock").exists()


def test_socket_dir_and_file_modes(env):
    server, _sup, adm = env()
    assert stat.S_IMODE(os.lstat(server.path.parent).st_mode) == 0o700
    st = os.lstat(server.path)
    assert stat.S_ISSOCK(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
    assert adm.rehydrated == 1


def test_socket_dir_owner_and_type_checked(short_dir):
    with pytest.raises(bp.UnsafeSocketPath):
        bp.prepare_socket_dir(short_dir, uid=os.getuid() + 1)
    real = short_dir / "real"
    real.mkdir(mode=0o755)
    link = short_dir / "link"
    link.symlink_to(real)
    with pytest.raises(bp.UnsafeSocketPath):
        bp.prepare_socket_dir(link)
    bp.prepare_socket_dir(real)
    assert stat.S_IMODE(os.lstat(real).st_mode) == 0o700


def test_stale_socket_replaced_live_socket_kept(tmp_path, short_dir):
    path = short_dir / "g.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # file stays, nobody listens
    sup = WorkerSupervisor(str(tmp_path / "data"), launcher=ProcLauncher())
    first = bp.PauseServer(sup, tmp_path / "data", path=path, admission=FakeAdmission())
    assert first.start()
    try:
        assert bp.answering(path)["gateway_pid"] == os.getpid()
        second = bp.PauseServer(sup, tmp_path / "data", path=path, admission=FakeAdmission())
        assert second.start() is False  # a live gateway answers there
        assert bp.answering(path) is not None
    finally:
        first.stop()
    assert not path.exists()
    assert bp.answering(path) is None


def test_non_socket_file_is_not_removed(tmp_path, short_dir):
    path = short_dir / "g.sock"
    path.write_text("keep")
    sup = WorkerSupervisor(str(tmp_path / "data"), launcher=ProcLauncher())
    server = bp.PauseServer(sup, tmp_path / "data", path=path, admission=FakeAdmission())
    assert server.start() is False
    assert path.read_text() == "keep"


def test_concurrent_binds_over_a_stale_socket_one_wins(tmp_path, short_dir, monkeypatch):
    """Two gateways starting at once over a stale socket: exactly one binds.

    The race is forced: each starter that found the socket stale waits (up to
    2 s) until the other one has probed too, and the second one unlinks only
    after the first has bound. Without serialization both pass the probe, the
    second unlinks the first's live socket and both serve.
    """
    path = short_dir / "g.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # file stays, nobody listens
    probed = threading.Barrier(2, timeout=2.0)
    bound = threading.Event()
    real_unlink, real_chmod = os.unlink, os.chmod

    def unlink(target, *args, **kwargs):
        if os.fspath(target) == str(path):
            try:
                second = probed.wait() == 0
            except threading.BrokenBarrierError:
                second = False  # the other starter never got past the probe
            if second:
                bound.wait(5)
        return real_unlink(target, *args, **kwargs)

    def chmod(target, *args, **kwargs):
        result = real_chmod(target, *args, **kwargs)
        if os.fspath(target) == str(path):
            bound.set()
        return result

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "chmod", chmod)
    sup = WorkerSupervisor(str(tmp_path / "data"), launcher=ProcLauncher())
    servers = [bp.PauseServer(sup, tmp_path / "data", path=path) for _ in range(2)]
    results = [None, None]

    def start(i):
        try:
            results[i] = servers[i]._bind(path)
        except Exception as exc:  # noqa: BLE001 - the loser's refusal is the result
            results[i] = exc

    threads = [threading.Thread(target=start, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    try:
        assert not any(t.is_alive() for t in threads)
        won = [r for r in results if isinstance(r, socket.socket)]
        lost = [r for r in results if not isinstance(r, socket.socket)]
        assert len(won) == 1, f"both starters bound the admin socket: {results}"
        assert isinstance(lost[0], bp.UnsafeSocketPath), lost
        assert "already answers" in str(lost[0])
    finally:
        for r in results:
            if isinstance(r, socket.socket):
                r.close()


def test_peer_with_other_uid_gets_nothing(env):
    server, _sup, _adm = env()
    server._uid = os.getuid() + 1
    with _client(server) as client:
        with pytest.raises(ConnectionError):
            client.call("hello", probe=True)


# ── protocol ──
def test_bad_requests(env):
    server, _sup, _adm = env()
    with _client(server) as client:
        client._sock.sendall(b"not json\n")
        bad = json.loads(client._reader.readline())
        assert bad == {"ok": False, "op": "", "state": bp.NO_RUN, "error": bp.E_BAD_REQUEST}
        assert client.call("nope")["error"] == bp.E_UNKNOWN_OP
        assert client.call("pause")["error"] == bp.E_BAD_STATE  # no run yet
        client._sock.sendall(b"x" * (bp.MAX_LINE_BYTES + 10) + b"\n")
        assert json.loads(client._reader.readline())["error"] == bp.E_BAD_REQUEST
        assert client._reader.readline() == b""  # closed after an oversized line


def test_hello_fields(env):
    server, sup, adm = env()
    sup.get("ws_a")
    adm.entries = [{"pid": 4242, "starttime": 7, "sid": 4242, "sid_leader_starttime": 7}]
    adm.counter = 3
    hello = bp.answering(server.path)
    assert set(bp.HELLO_FIELDS) <= set(hello)
    assert "t3_active" not in hello  # withdrawn (B6)
    assert hello["proto"] == 1 and hello["gateway_pid"] == os.getpid()
    assert hello["data_dir"] == os.path.realpath(server.data_dir)
    assert hello["worker_pids"] == sup.worker_pids() and len(hello["worker_pids"]) == 1
    assert hello["successor_pids"] == [4242] and hello["successor_sids"] == [4242]
    assert hello["successor_active"] is True and hello["successor_counter"] == 3
    assert hello["state"] == bp.NO_RUN and hello["busy"] is False
    adm.entries = [{"state": "starting", "handoff_id": "h1"}]
    hello = bp.answering(server.path)
    assert hello["successors_starting"] == ["h1"] and hello["successor_active"] is True


def test_registry_error_fails_closed(env):
    server, _sup, adm = env()
    adm.error = OSError("unreadable")
    with _client(server) as client:
        hello = client.call("hello")
        assert hello["ok"] is False and hello["successor_active"] is True
        assert hello["successor_error"] == "OSError"
        assert client.call("pause")["error"] == bp.E_BAD_STATE  # no run was opened


def test_rehydrate_failure_is_sticky(tmp_path, short_dir):
    adm = FakeAdmission()

    def broken():
        raise OSError("bad registry")

    adm.rehydrate = broken
    sup = WorkerSupervisor(str(tmp_path / "data"), launcher=ProcLauncher())
    server = bp.PauseServer(sup, tmp_path / "data", path=short_dir / "g.sock", admission=adm)
    assert server.start()
    try:
        hello = bp.answering(server.path)
        assert hello["successor_active"] is True and hello["successor_error"] == "OSError"
    finally:
        server.stop()


def test_busy_second_run_and_probe(env):
    server, _sup, _adm = env()
    with _client(server) as first, _client(server) as second:
        assert first.hello()["state"] == bp.ACTIVE
        busy = second.call("hello")
        assert busy["ok"] is False and busy["error"] == bp.E_BUSY and busy["busy"] is True
        assert second.hello(probe=True)["busy"] is True
        assert first.call("hello")["error"] == bp.E_BAD_STATE  # one run per connection
    # Both closed: the slot is free again.
    assert _until(lambda: bp.answering(server.path)["busy"] is False)


# ── run fence ──
def test_full_run_pauses_freezes_resumes_commits(env):
    server, sup, adm = env()
    worker = sup.get("ws_a")
    assert not process_gone(worker.pid, worker.starttime)
    with _client(server) as client:
        hello = client.hello()
        assert hello["state"] == bp.ACTIVE and hello["worker_pids"] == [worker.pid]
        paused = client.pause()
        assert paused["state"] == bp.PAUSED and paused["stopped_worker_pids"] == [worker.pid]
        assert process_gone(worker.pid, worker.starttime)
        assert not Path(f"/proc/{worker.pid}").exists()
        assert sup.paused and sup.worker_pids() == []
        # A memory call waits, then is refused with a retry hint; no worker starts.
        started = time.monotonic()
        with pytest.raises(WorkersPaused) as refused:
            sup.get("ws_a")
        assert refused.value.retry_after_s == 30
        assert time.monotonic() - started >= 0.15
        assert len(sup.launcher.procs) == 1
        assert client.renew()["state"] == bp.PAUSED
        assert client.freeze()["frozen"] is True and adm.frozen
        bp.record_console_drop(2)
        assert client.thaw()["frozen"] is False and not adm.frozen
        assert client.resume()["state"] == bp.RESUMED_OK
        assert not sup.paused
        committed = client.commit()
        assert committed["state"] == bp.COMMITTED and committed["console_drops"] == 2
    assert sup.get("ws_a").pid != worker.pid  # cold start after resume


def test_resume_thaws_a_frozen_gate(env):
    server, sup, adm = env()
    with _client(server) as client:
        client.hello()
        client.pause()
        client.freeze()
        assert client.resume()["state"] == bp.RESUMED_OK
        assert not adm.frozen and not sup.paused


def test_lease_lapse_invalidates_and_resumes(env):
    server, sup, _adm = env(lease_s=0.3)
    with _client(server) as client:
        client.hello()
        client.pause()
        client.renew()
        assert _until(lambda: not sup.paused, 3)
        with pytest.raises(bp.PauseError) as err:
            client.resume()
        assert err.value.error == bp.E_INVALID and err.value.reason == bp.R_LEASE
        # Permanent: commit and every later op stay refused.
        for op in ("commit", "pause", "renew", "freeze", "thaw", "resume"):
            assert client.call(op)["reason"] == bp.R_LEASE


def test_renew_keeps_the_pause(env):
    server, sup, _adm = env(lease_s=0.4)
    with _client(server) as client:
        client.hello()
        client.pause()
        for _ in range(6):
            time.sleep(0.15)
            client.renew()
        assert sup.paused
        assert client.resume()["state"] == bp.RESUMED_OK
        assert client.commit()["ok"]


def test_pause_request_can_only_shorten_lease_and_pause_max(env):
    server, sup, _adm = env(lease_s=5, pause_max_s=30)
    with _client(server) as client:
        client.hello()
        paused = client.request("pause", lease_s=0.3, pause_max_s=600)
        assert paused["lease_s"] == 0.3 and paused["pause_max_s"] == 30
        assert client.renew()["lease_s"] == 0.3
        assert _until(lambda: not sup.paused, 3), "the requested shorter lease was ignored"
        assert client.call("resume")["reason"] == bp.R_LEASE
    # The server ends the run when it sees the closed connection; wait for that.
    assert _until(lambda: bp.answering(server.path)["busy"] is False)
    with _client(server) as client:
        client.hello()
        paused = client.request("pause", lease_s=60, pause_max_s=0.3)
        assert paused["lease_s"] == 5 and paused["pause_max_s"] == 0.3
        assert _until(lambda: not sup.paused, 3), "the requested shorter pause max was ignored"
        assert client.call("resume")["reason"] == bp.R_PAUSE_MAX
    # The server ends the run when it sees the closed connection; wait for that.
    assert _until(lambda: bp.answering(server.path)["busy"] is False)
    with _client(server) as client:
        client.hello()
        paused = client.request("pause", lease_s="x", pause_max_s=-1)
        assert paused["lease_s"] == 5 and paused["pause_max_s"] == 30
        assert client.resume()["state"] == bp.RESUMED_OK


def test_client_fileno_is_the_socket(env):
    server, _sup, _adm = env()
    with _client(server) as client:
        assert client.fileno() == client._sock.fileno()


def test_freeze_lease_lapse_thaws_and_invalidates(env):
    server, sup, adm = env(freeze_lease_s=0.3, lease_s=5)
    with _client(server) as client:
        client.hello()
        client.pause()
        client.freeze()
        assert _until(lambda: not adm.frozen, 3)
        assert not sup.paused
        assert client.call("thaw")["reason"] == bp.R_FREEZE_LEASE


def test_gate_own_lapse_callback_invalidates(env):
    server, sup, adm = env(freeze_lease_s=30, lease_s=30)
    with _client(server) as client:
        client.hello()
        client.pause()
        client.freeze()
        adm.frozen = False  # the gate's own lease thawed it ...
        adm.on_lapse()  # ... and told the server
        assert not sup.paused
        assert client.call("resume")["reason"] == bp.R_FREEZE_LEASE


def test_thaw_after_gate_lapse_is_invalid(env):
    server, _sup, adm = env(freeze_lease_s=30, lease_s=30)
    with _client(server) as client:
        client.hello()
        client.pause()
        client.freeze()
        adm.frozen = False  # lapsed inside the gate, callback not yet delivered
        refused = client.call("thaw")
        assert refused["error"] == bp.E_INVALID and refused["reason"] == bp.R_FREEZE_LEASE


def test_resume_after_gate_lapse_is_invalid(env):
    server, sup, adm = env(freeze_lease_s=30, lease_s=30)
    with _client(server) as client:
        client.hello()
        client.pause()
        client.freeze()
        adm.frozen = False  # lapsed inside the gate, callback not yet delivered
        refused = client.call("resume")
        assert refused["error"] == bp.E_INVALID and refused["reason"] == bp.R_FREEZE_LEASE
        assert not sup.paused


def test_pause_max_invalidates(env):
    server, sup, _adm = env(pause_max_s=0.4, lease_s=5)
    with _client(server) as client:
        client.hello()
        client.pause()
        assert _until(lambda: not sup.paused, 3)
        assert client.call("resume")["reason"] == bp.R_PAUSE_MAX


def test_failed_freeze_invalidates_and_resumes(env):
    server, sup, adm = env()
    adm.fail_freeze = True
    with _client(server) as client:
        client.hello()
        client.pause()
        refused = client.call("freeze")
        assert refused["reason"] == bp.R_CREATOR_DRAIN
        assert not sup.paused and not adm.frozen


def test_eof_before_commit_invalidates_and_resumes(env):
    server, sup, adm = env(lease_s=30)
    client = _client(server)
    client.hello()
    client.pause()
    client.freeze()
    client.close()  # supervisor died
    assert _until(lambda: not sup.paused and not adm.frozen, 3)
    # The slot is free and the next run starts clean.
    with _client(server) as again:
        assert again.hello()["state"] == bp.ACTIVE


def test_eof_after_commit_keeps_committed(env):
    server, sup, _adm = env()
    with _client(server) as client:
        client.hello()
        client.pause()
        client.resume()
        client.commit()
    assert _until(lambda: bp.answering(server.path)["busy"] is False)
    assert not sup.paused


def test_successor_launch_mid_run_invalidates(env):
    server, sup, adm = env(lease_s=30)
    with _client(server) as client:
        client.hello()
        client.pause()
        client.resume()
        adm.counter += 1  # a launcher spawned a successor during the run
        refused = client.call("commit")
        assert refused["ok"] is False and refused["reason"] == bp.R_SUCCESSOR


def test_successor_launch_while_paused_resumes_at_once(env):
    server, sup, adm = env(lease_s=30)
    with _client(server) as client:
        client.hello()
        client.pause()
        adm.counter += 1
        assert _until(lambda: not sup.paused, 3)
        assert client.call("resume")["reason"] == bp.R_SUCCESSOR


def test_abort_undoes_everything(env):
    server, sup, adm = env(lease_s=30)
    with _client(server) as client:
        client.hello()
        client.pause()
        client.freeze()
        aborted = client.abort()
        assert aborted["state"] == bp.INVALID and aborted["reason"] == bp.R_ABORT
        assert not sup.paused and not adm.frozen
        assert client.call("commit")["error"] == bp.E_INVALID


def test_out_of_order_ops_refused(env):
    server, _sup, _adm = env()
    with _client(server) as client:
        client.hello()
        for op in ("renew", "freeze", "thaw", "resume", "commit"):
            assert client.call(op)["error"] == bp.E_BAD_STATE, op
        client.pause()
        assert client.call("thaw")["error"] == bp.E_BAD_STATE
        assert client.call("commit")["error"] == bp.E_BAD_STATE
        client.freeze()
        assert client.call("freeze")["error"] == bp.E_BAD_STATE


def test_server_stop_resumes_an_open_pause(env):
    server, sup, adm = env(lease_s=30)
    client = _client(server)
    client.hello()
    client.pause()
    client.freeze()
    server.stop()
    assert not sup.paused and not adm.frozen
    assert not server.path.exists()
    client.close()


# ── worker pause ──
def test_pause_drains_inflight_call_and_never_cuts_it(env):
    server, sup, _adm = env(drain_s=5)
    sup.get("ws_a")
    finished = threading.Event()

    def long_call():
        with sup.call():
            time.sleep(0.5)
            assert sup.worker_pids()  # the worker still runs under the call
            finished.set()

    t = threading.Thread(target=long_call)
    t.start()
    assert _until(lambda: sup.in_flight == 1)
    with _client(server) as client:
        client.hello()
        paused = client.pause()
        assert finished.is_set() and paused["drain_s"] >= 0.3
        client.resume()
    t.join()


def test_drain_timeout_reopens_and_keeps_workers(env):
    server, sup, _adm = env(drain_s=0.3)
    worker = sup.get("ws_a")
    release = threading.Event()

    def stuck_call():
        with sup.call():
            release.wait(5)

    t = threading.Thread(target=stuck_call)
    t.start()
    assert _until(lambda: sup.in_flight == 1)
    with _client(server) as client:
        client.hello()
        refused = client.call("pause")
        assert refused["error"] == bp.E_NO_DRAIN and refused["state"] == bp.ACTIVE
        assert not sup.paused and worker.alive()
        release.set()
        t.join()
        assert client.pause()["state"] == bp.PAUSED  # a retry may succeed
        client.resume()


def test_supervisor_pause_drain_error_is_raised():
    sup = WorkerSupervisor("/nonexistent-data", launcher=ProcLauncher())
    with sup.call():
        result = {}

        def pauser():
            try:
                sup.pause(drain_s=0.2)
            except DrainTimeout as exc:
                result["exc"] = exc

        p = threading.Thread(target=pauser)
        p.start()
        p.join()
    assert isinstance(result["exc"], DrainTimeout) and not sup.paused


def test_call_is_reentrant_during_drain():
    sup = WorkerSupervisor("/nonexistent-data", launcher=ProcLauncher())
    sup.pause_wait_s = 0.1
    entered = threading.Event()
    go = threading.Event()
    nested = {}

    def outer():
        with sup.call():
            entered.set()
            go.wait(5)
            with sup.call():  # same thread: never waits for the pause it blocks
                nested["ok"] = True

    t = threading.Thread(target=outer)
    t.start()
    entered.wait(5)
    p = threading.Thread(target=lambda: sup.pause(drain_s=5))
    p.start()
    assert _until(lambda: sup.paused)
    go.set()
    t.join(5)
    p.join(5)
    assert nested == {"ok": True} and sup.paused
    sup.resume()


def test_stop_process_escalates_to_kill():
    proc = subprocess.Popen(
        ["sh", "-c", "trap '' TERM; while :; do sleep 0.05; done"], stdin=subprocess.DEVNULL
    )
    time.sleep(0.1)
    starttime = _proc_starttime(proc.pid)
    started = time.monotonic()
    assert stop_process(proc, term_wait_s=0.3, kill_wait_s=5) is True
    assert time.monotonic() - started >= 0.25
    assert proc.returncode == -9
    assert process_gone(proc.pid, starttime)


def test_pause_stops_term_ignoring_worker(env):
    server, sup, _adm = env(launcher=ProcLauncher(ignore_term=True))
    worker = sup.get("ws_a")
    with _client(server) as client:
        client.hello()
        assert client.pause()["stopped_worker_pids"] == [worker.pid]
    assert process_gone(worker.pid, worker.starttime)


def test_pause_with_unstoppable_worker_invalidates(env):
    launcher = ProcLauncher()
    server, sup, _adm = env(launcher=launcher)
    worker = sup.get("ws_a")
    worker.stop = lambda: None  # survives every signal
    with _client(server) as client:
        client.hello()
        refused = client.call("pause")
        assert refused["reason"] == bp.R_WORKER_STOP and not sup.paused
    stop_process(launcher.procs[0])


def test_process_gone_rules():
    assert process_gone(None, None)
    assert not process_gone(os.getpid(), _proc_starttime(os.getpid()))
    assert process_gone(os.getpid(), (_proc_starttime(os.getpid()) or 0) + 1)  # pid reused


# ── knobs ──
def test_knobs_from_env():
    knobs = bp.Knobs.from_env(
        {
            "COMMONPLACE_BACKUP_LEASE_S": "5",
            "COMMONPLACE_BACKUP_PAUSE_MAX": "120",
            "COMMONPLACE_BACKUP_RESERVE_BYTES": "1000",
            "COMMONPLACE_BACKUP_SEAM_DIR": "/x",
        }
    )
    assert knobs.lease_s == 5 and knobs.pause_max_s == 120 and knobs.reserve_bytes == 1000
    assert knobs.seam_dir == "/x" and knobs.freeze_lease_s == 10 and knobs.deadline_s == 4500
    for bad in ("0", "-1", "nan", "inf", "x"):
        with pytest.raises(ValueError):
            bp.Knobs.from_env({"COMMONPLACE_BACKUP_LEASE_S": bad})
    assert bp.reserve_bytes(bp.Knobs(), 100 * 1000**3) == 10 * 1000**3
    assert bp.reserve_bytes(bp.Knobs(), 1000 * 1000**4) == 50 * 1000**4
    assert bp.reserve_bytes(knobs, 10**15) == 1000
    assert knobs.fence_wait_s == 60 and knobs.cli_fence_wait_s == 30
    waits = bp.Knobs.from_env(
        {"COMMONPLACE_BACKUP_FENCE_WAIT_S": "0", "COMMONPLACE_FENCE_WAIT_S": "0.5"}
    )
    assert waits.fence_wait_s == 0 and waits.cli_fence_wait_s == 0.5
    assert bp.Knobs.read("cli_fence_wait_s", {"COMMONPLACE_FENCE_WAIT_S": "2"}) == 2
    for bad in ("-1", "nan", "inf", "x"):
        with pytest.raises(ValueError):
            bp.Knobs.from_env({"COMMONPLACE_FENCE_WAIT_S": bad})


# ── gateway ──
def test_gateway_serve_owns_the_socket(tmp_path, short_dir, monkeypatch):
    from commonplace.control_db import ControlDB
    from commonplace.gateway import Gateway

    path = short_dir / "gw" / "gateway.sock"
    monkeypatch.setenv(bp.SOCKET_ENV, str(path))
    data = tmp_path / "data"
    data.mkdir()
    db = ControlDB(str(data / "control.sqlite3"))
    sup = WorkerSupervisor(str(data), launcher=ProcLauncher())
    gateway = Gateway(db, sup)
    t = threading.Thread(target=gateway.serve, args=("127.0.0.1", 0), daemon=True)
    t.start()
    try:
        assert _until(lambda: bp.answering(path) is not None, 10)
        hello = bp.answering(path)
        assert hello["gateway_pid"] == os.getpid()
        assert hello["data_dir"] == os.path.realpath(data)
        assert gateway.pause_server is not None and gateway.pause_server.serving
    finally:
        gateway.shutdown()
        t.join(10)
        db.close()
    assert not t.is_alive()
    assert not path.exists()


def test_second_gateway_on_the_same_data_dir_refuses_to_serve(tmp_path, short_dir, monkeypatch):
    import sys

    from commonplace.control_db import ControlDB, ControlError
    from commonplace.gateway import Gateway

    path = short_dir / "gw" / "gateway.sock"
    monkeypatch.setenv(bp.SOCKET_ENV, str(path))
    data = tmp_path / "data"
    data.mkdir()
    db = ControlDB(str(data / "control.sqlite3"))
    db.create_user("owner@example.com", is_admin=True)
    first = Gateway(db, WorkerSupervisor(str(data), launcher=ProcLauncher()))
    t = threading.Thread(target=first.serve, args=("127.0.0.1", 0), daemon=True)
    t.start()
    built, errors = [], []

    class Second(Gateway):
        def build_server(self, host, port):
            built.append(True)
            return super().build_server(host, port)

    second = Second(db, WorkerSupervisor(str(data), launcher=ProcLauncher()))

    def serve_second():
        try:
            second.serve("127.0.0.1", 0)
        except BaseException as exc:
            errors.append(exc)

    t2 = threading.Thread(target=serve_second, daemon=True)
    cli = None
    try:
        assert _until(lambda: bp.answering(path) is not None, 10)
        t2.start()
        t2.join(10)
        assert not t2.is_alive() and not built, "a second gateway serves the same data dir"
        assert isinstance(errors[0], ControlError), errors
        assert "one gateway per data directory" in str(errors[0])
        # The real command: a second `commonplace serve` exits non-zero without serving.
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": str(root), bp.SOCKET_ENV: str(path)}
        env.pop("COMMONPLACE_DATA", None)
        cli = subprocess.Popen(
            [sys.executable, "-m", "commonplace", "--data", str(data), "serve", "--port", "0"],
            env=env,
            cwd=str(tmp_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            out, _ = cli.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            raise AssertionError("a second `commonplace serve` kept serving") from None
        assert cli.returncode == 2, out
        assert "one gateway per data directory" in out, out
        hello = bp.answering(path)
        assert hello["gateway_pid"] == os.getpid() and first.pause_server.serving
    finally:
        if cli is not None and cli.poll() is None:
            cli.kill()
            cli.wait()
        if t2.is_alive():
            second.shutdown()
            t2.join(10)
        first.shutdown()
        t.join(10)
        db.close()
    assert not path.exists()


def test_memory_in_a_pause_gets_retry_hint_and_cached_tools(hub):
    token = hub.owner_token()
    before = hub.rpc(token, "tools/list")["result"]["tools"]
    assert before
    hub.supervisor.pause_wait_s = 0.2
    hub.supervisor.pause(drain_s=5)
    try:
        for method, params in (
            ("ping", None),
            ("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}}),
            ("tools/call", {"name": "mempalace_status", "arguments": {}}),
        ):
            error = hub.rpc(token, method, params)["error"]
            assert error["code"] == -32603, method
            assert error["data"] == {"retry_after_s": 30}, method
        listed = hub.rpc(token, "tools/list")["result"]["tools"]
        assert listed == before  # never [] during a pause
        principal = hub.gateway.authenticate(f"Bearer {token}")
        from commonplace.extensions import ToolError

        with pytest.raises(ToolError, match="paused for a backup"):
            hub.gateway.call_memory_tool(principal, "mempalace_status", {})
    finally:
        hub.supervisor.resume()
    assert "result" in hub.rpc(token, "ping")


def test_tools_list_in_a_pause_without_cache_is_an_error(hub):
    token = hub.owner_token()
    hub.supervisor.pause_wait_s = 0.1
    hub.supervisor.pause(drain_s=5)
    try:
        error = hub.rpc(token, "tools/list")["error"]
        assert error["code"] == -32603 and error["data"] == {"retry_after_s": 30}
    finally:
        hub.supervisor.resume()


def test_workspace_dir_creates_inside_the_admission_gate(tmp_path, monkeypatch):
    from commonplace import admission
    from commonplace.control_db import ControlDB
    from commonplace.gateway import Gateway

    entered = []
    real = admission.GATE.creating

    def spy(*paths, **kwargs):
        entered.append(paths)
        return real(*paths, **kwargs)

    monkeypatch.setattr(admission.GATE, "creating", spy)
    data = tmp_path / "data"
    data.mkdir()
    db = ControlDB(str(data / "control.sqlite3"))
    try:
        gateway = Gateway(db, WorkerSupervisor(str(data), launcher=ProcLauncher()))
        made = Path(gateway.workspace_dir("ws_new"))
        assert made.is_dir() and entered
    finally:
        db.close()
