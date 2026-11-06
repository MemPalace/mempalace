"""Online snapshots while the gateway serves (O-tests).

A real ``Gateway`` runs ``Gateway.serve`` in a thread of this process, so its
admin socket server starts with it. Like ``commonplace serve`` it holds only
the shared data lock. Workers are ``tests/stub_worker.py`` processes whose
palace holds a rollback-journal database plus a checksummed binary log. The
backup is the real CLI, ``commonplace backup DEST``, in a subprocess.

Test seams: with ``COMMONPLACE_BACKUP_SEAM_DIR`` set, the snapshot process
writes ``<name>.reached`` at each seam and waits for ``<name>.go``. A harness
thread releases every seam a test does not hold. Seams used: ``enumerated``
(paused and frozen, the file set listed) and ``pinned`` (the cut L: databases
pinned and thawed, workers still paused until their copy).

Every gate assertion carries the ``RED238:`` prefix. A backup that exits
before its seam with the old "data directory is in use" refusal fails as
``RED238: online backup refused while serving``; any other early exit is a
setup failure (``pytest.fail("SETUP: ...")``), never an AssertionError.

Only modules present on every supported revision are imported at module
level; newer modules are not imported here at all.
"""

from __future__ import annotations

import errno
import fcntl
import http.client
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

from commonplace.backup import BackupError, data_lock, restore, verify_backup
from commonplace.control_db import ControlDB
from commonplace.gateway import Gateway
from commonplace.policy import Policy
from commonplace.workers import WorkerSupervisor
from tests.stub_worker import BLOB_NAME, DB_NAME, StubLauncher, drawer_rows, read_blob

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="online backup runs in a Linux container"
)

SOCKET_ENV = "COMMONPLACE_ADMIN_SOCKET"
SEAM_ENV = "COMMONPLACE_BACKUP_SEAM_DIR"
STAGING = ".backup-staging"
PIN_REFUSAL = "data directory is in use"
REFUSED = "RED238: online backup refused while serving"

SEAM_ENUMERATED = "enumerated"
SEAM_PINNED = "pinned"

ROOT = Path(__file__).resolve().parent.parent
MESSAGES = json.dumps(
    [
        {"role": "user", "content": "Please summarise the release checklist."},
        {"role": "assistant", "content": "The checklist has four steps; all are done."},
    ]
)


# ── small helpers ──


def _wait(predicate, timeout, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _alive(pid) -> bool:
    """Live means /proc/<pid> exists and is neither a zombie nor dead."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return text.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def _processes_naming(marker: str) -> list[int]:
    """Live processes of this user whose command line contains ``marker``."""
    found, needle = [], marker.encode()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            if needle in (proc / "cmdline").read_bytes() and _alive(proc.name):
                found.append(int(proc.name))
        except OSError:
            continue
    return found


def _lock_free(path: Path) -> bool:
    if not path.exists():
        return True
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return False
        raise
    finally:
        os.close(fd)
    return True


def _immutable(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _thread_messages():
    from commonplace.handoff import transcripts

    return transcripts.parse(MESSAGES, "messages")


class _Call(threading.Thread):
    """Runs ``fn`` once; keeps its result or exception and its return time."""

    def __init__(self, fn):
        super().__init__(daemon=True)
        self.fn, self.result, self.error, self.returned_at = fn, None, None, None

    def run(self):
        try:
            self.result = self.fn()
        except BaseException as exc:  # Reported by the test, never swallowed.
            self.error = exc
        finally:
            self.returned_at = time.time()


class _CapturingGateway(Gateway):
    """The production gateway; only remembers the server ``serve`` builds."""

    def build_server(self, host, port):
        server = super().build_server(host, port)
        self.captured_server = server
        return server


# ── one backup run ──


class Run:
    def __init__(self, harness, stamp, env, heartbeat=True):
        self.harness = harness
        self.stamp = stamp
        self.staging = harness.staging
        self.dest = self.staging / stamp
        self.seams = harness.tmp / "seams" / stamp
        self.seams.mkdir(parents=True)
        self.log = harness.tmp / f"backup-{stamp}.log"
        self.held: set = set()
        self.hb = self.staging / f"{stamp}.hb"
        self.hb.touch()
        self._beating = threading.Event()
        if heartbeat:
            self._beating.set()
        self._stop = threading.Event()
        env = {**harness.cli_env(), SEAM_ENV: str(self.seams), **env}
        with open(self.log, "wb") as out:
            self.proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "commonplace",
                    "--data",
                    str(harness.data),
                    "backup",
                    str(self.dest),
                ],
                env=env,
                cwd=str(harness.tmp),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._helper = threading.Thread(target=self._tick, daemon=True)
        self._helper.start()

    def _tick(self):
        """Keep the heartbeat fresh (as the hook does) and release free seams."""
        last_beat = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if self._beating.is_set() and now - last_beat >= 1.0:
                try:
                    os.utime(self.hb)
                except OSError:
                    pass
                last_beat = now
            for reached in self.seams.glob("*.reached"):
                name = reached.name[: -len(".reached")]
                if name not in self.held:
                    (self.seams / f"{name}.go").touch()
            self._stop.wait(0.05)

    def hold(self, *names):
        self.held.update(names)
        return self

    def release(self, name):
        self.held.discard(name)
        (self.seams / f"{name}.go").touch()

    def stop_heartbeat(self):
        self._beating.clear()

    def reached(self, name) -> bool:
        return (self.seams / f"{name}.reached").exists()

    def output(self) -> str:
        try:
            return self.log.read_text(errors="replace")
        except OSError:
            return ""

    def _early_exit(self, seam):
        output = self.output()
        if PIN_REFUSAL in output:
            raise AssertionError(REFUSED)
        pytest.fail(
            f"SETUP: backup exited {self.proc.returncode} before seam {seam!r}: {output[-2000:]}"
        )

    def wait_seam_or_exit(self, seam, timeout=60.0, *, required=True) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.reached(seam):
                return True
            if self.proc.poll() is not None:
                if self.reached(seam):
                    return True
                self._early_exit(seam)
            time.sleep(0.02)
        if self.reached(seam):
            return True
        if required:
            pytest.fail(
                f"SETUP: seam {seam!r} not reached in {timeout:.0f}s: {self.output()[-2000:]}"
            )
        return False

    def finish(self, timeout=120.0) -> int:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"RED238: backup did not finish in {timeout:.0f}s: {self.output()[-2000:]}"
            ) from None

    def published(self) -> bool:
        return (self.dest / "manifest.json").is_file()

    def nothing_published(self) -> bool:
        return not self.dest.exists() and not self.dest.is_symlink()

    def close(self):
        self._stop.set()
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass
        for pid in _processes_naming(self.stamp):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        self._helper.join(timeout=5)


# ── the serving gateway ──


class Harness:
    def __init__(self, tmp: Path, monkeypatch):
        self.tmp = tmp
        self.data = tmp / "data"
        self.data.mkdir(mode=0o700)
        self.staging = self.data / STAGING
        self.staging.mkdir(mode=0o700)
        # AF_UNIX paths are short; keep the admin socket out of the data tree.
        self.socket_dir = Path(tempfile.mkdtemp(prefix="cpb-", dir="/tmp"))
        self.socket_dir.chmod(0o700)
        self.socket = self.socket_dir / "gateway.sock"
        for name in list(os.environ):
            if name.startswith("COMMONPLACE_"):
                monkeypatch.delenv(name)
        monkeypatch.setenv(SOCKET_ENV, str(self.socket))
        monkeypatch.setenv("COMMONPLACE_RULES_CHECK", "off")
        monkeypatch.setenv("COMMONPLACE_RULES_INJECT", "off")
        monkeypatch.setenv("COMMONPLACE_HANDOFF_LAUNCHERS", "command")
        self.monkeypatch = monkeypatch
        self.stack = ExitStack()
        self.locked = False
        self.runs: list = []
        self.sessions: list = []
        self.closers: list = []
        self.gateway = None
        self.thread = None
        self.launcher = StubLauncher()
        self.db = ControlDB(str(self.data / "control.sqlite3"))
        self.admin = self.db.create_user("owner@example.com", is_admin=True)
        self.ws1 = self.db.create_workspace("home", self.admin, personal=True)
        self._stamps = 0

    # gateway lifecycle

    def start(self):
        if not self.locked:
            # As `commonplace serve` (cli.py dispatch): the shared data lock only.
            self.stack.enter_context(data_lock(self.data))
            self.locked = True
        supervisor = WorkerSupervisor(str(self.data), launcher=self.launcher)
        gateway = _CapturingGateway(self.db, supervisor)
        errors = []

        def serve():
            try:
                gateway.serve("127.0.0.1", 0, idle_check_s=3600)
            except BaseException as exc:
                errors.append(exc)

        self.gateway = gateway
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        if not _wait(lambda: getattr(gateway, "captured_server", None) or errors, 30):
            pytest.fail("SETUP: gateway did not build its server")
        if errors:
            pytest.fail(f"SETUP: gateway failed to start: {errors[0]!r}")
        self.port = gateway.captured_server.server_address[1]
        if not _wait(lambda: self.healthz() == 200, 30):
            pytest.fail("SETUP: gateway /healthz never answered")
        self.token = self.owner_token()
        self.principal = gateway.authenticate(f"Bearer {self.token}")
        return gateway

    def stop(self):
        gateway, self.gateway = self.gateway, None
        if gateway is None:
            return
        if callable(getattr(gateway, "shutdown", None)):
            gateway.shutdown()
        elif getattr(gateway, "captured_server", None) is not None:
            gateway.captured_server.shutdown()
        self.thread.join(timeout=60)
        if self.thread.is_alive():
            pytest.fail("SETUP: gateway serve did not stop")

    def restart(self):
        self.stop()
        old, self.db = self.db, ControlDB(str(self.data / "control.sqlite3"))
        old.close()
        return self.start()

    def close(self):
        for run in self.runs:
            run.close()
        for closer in reversed(self.closers):
            try:
                closer()
            except Exception:
                pass
        try:
            self.stop()
        except BaseException:
            pass
        for sid in self.sessions:
            try:
                os.killpg(sid, signal.SIGKILL)
            except OSError:
                pass
        # Anything still naming this test's directory (successors, children).
        for pid in _processes_naming(str(self.tmp)):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.launcher.kill_all()
        self.stack.close()
        self.db.close()
        shutil.rmtree(self.socket_dir, ignore_errors=True)

    # environment of the CLI

    def cli_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("COMMONPLACE_")}
        env[SOCKET_ENV] = str(self.socket)
        # A tiny disk reserve: the preflight must not depend on the test host's free space.
        env["COMMONPLACE_BACKUP_RESERVE_BYTES"] = str(1 << 20)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]
        )
        return env

    def stamp(self) -> str:
        self._stamps += 1
        now = time.time_ns()
        base = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now // 1_000_000_000))
        return f"{base}.{now % 1_000_000_000:09d}{self._stamps}Z-{os.getpid()}"

    def backup(self, *, env=None, hold=(), heartbeat=True) -> Run:
        run = Run(self, self.stamp(), env or {}, heartbeat=heartbeat)
        run.hold(*hold)
        self.runs.append(run)
        return run

    # HTTP

    def healthz(self) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/healthz")
            response = conn.getresponse()
            response.read()
            return response.status
        except OSError:
            return 0
        finally:
            conn.close()

    def owner_token(self, workspace=None) -> str:
        policy = Policy.build(
            profile="full",
            scopes=[
                "memory:read",
                "memory:write",
                "memory:delete",
                "kg:read",
                "kg:write",
                "events:read",
                "events:write",
                "private:read",
                "private:write",
                "threads:read",
                "handoff:write",
            ],
            read_wings=["*"],
            write_wings=["*"],
            trust="trusted",
        )
        _cid, token = self.db.create_token(
            workspace_ref=workspace or self.ws1,
            user_ref=self.admin,
            name=f"owner-{time.monotonic_ns()}",
            client="claude-code",
            policy=policy,
            actor=self.admin,
        )
        return token

    def rpc(self, method, params=None, *, token=None, timeout=60) -> dict:
        body = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(
                "POST",
                "/mcp",
                json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token or self.token}",
                },
            )
            response = conn.getresponse()
            raw = response.read()
        finally:
            conn.close()
        if response.status != 200:
            return {"http_status": response.status, "body": raw[:500].decode(errors="replace")}
        return json.loads(raw)

    def tool(self, name, **arguments) -> dict:
        return self.rpc("tools/call", {"name": name, "arguments": arguments})

    def add_drawer(self, content) -> dict:
        return self.tool("mempalace_add_drawer", wing="notes", room="general", content=content)

    def memory_ok(self) -> bool:
        response = self.tool("mempalace_status")
        return "result" in response and not response["result"].get("isError")

    # stores

    @property
    def ctx(self):
        return self.gateway.handoff

    def workspace_dir(self, workspace_id) -> Path:
        from commonplace.workspace_store import workspace_path

        return workspace_path(self.data, workspace_id)

    def worker_palace(self, root: Path, workspace_id) -> Path:
        from commonplace.workspace_store import resolve_workspace

        return resolve_workspace(root / "workspaces" / workspace_id) / "palace"

    def store_thread(self, workspace_id, thread_id):
        return self.ctx.store_for(workspace_id).store(
            workspace_id, thread_id, _thread_messages(), actor="test"
        )

    def handoff_settings(self, *, launcher=None):
        from commonplace.handoff.models import LauncherConfig, SettingRow

        row = SettingRow(
            workspace_id=self.ws1,
            scope="workspace",
            ref="",
            enabled=True,
            launcher=launcher if launcher is not None else LauncherConfig(),
        )
        self.ctx.settings.set(row, actor=self.admin, is_owner=True)

    def run_handoff(self, thread_id):
        from commonplace.handoff.models import HandoffRequest, ThreadMeta

        request = HandoffRequest(
            meta=ThreadMeta(thread_id=thread_id, client="claude-code"),
            transcript=MESSAGES,
            transcript_format="messages",
        )
        return self.ctx.service.run(self.principal, request)

    def restore(self, run: Run) -> Path:
        target = self.tmp / f"restored-{run.stamp}"
        try:
            restore(run.dest, target)
        except BackupError as exc:
            raise AssertionError(f"RED238: the snapshot does not restore: {exc}") from None
        return target


@pytest.fixture
def harness(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    try:
        yield h
    finally:
        h.close()


def _verify(snapshot: Path):
    try:
        verify_backup(snapshot)
    except BackupError as exc:
        raise AssertionError(f"RED238: the snapshot fails verification: {exc}") from None


_NOT_SNAPSHOT = {".commonplace.lock", "manifest.json", ".commonplace-fence.lock"}


def _cut_listing(root: Path) -> set:
    """Regular files a snapshot of ``root`` must hold, listed while frozen.

    Same walk as the offline inventory (no symlinks followed), minus the
    snapshot's exclusions: locks, a previous manifest, SQLite sidecars, the
    staging directory and ELF core dumps.
    """
    names = set()
    for directory, subdirectories, files in os.walk(root):
        relative = Path(directory).relative_to(root)
        if relative.parts[:1] == (STAGING,):
            subdirectories[:] = []
            continue
        for name in files:
            path = Path(directory) / name
            entry = (relative / name).as_posix()
            if entry in _NOT_SNAPSHOT or name.endswith(("-wal", "-shm", "-journal")):
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if name.split(".")[0] == "core" and path.open("rb").read(4) == b"\x7fELF":
                    continue
            except OSError:
                continue
            names.add(entry)
    return names


def _healthz_sampler(harness, samples, stop):
    while not stop.is_set():
        samples.append(harness.healthz())
        stop.wait(0.2)


def _assert_worker_files_consistent(palace: Path, expected_min: int, label: str):
    rows = drawer_rows(palace / DB_NAME)
    records = [payload.decode("utf-8") for payload in read_blob(palace / BLOB_NAME)]
    assert rows == records, f"RED238: {label}: worker database and blob log disagree"
    assert len(rows) >= expected_min, f"RED238: {label}: {len(rows)} drawers < {expected_min}"
    return rows


def _handoff_rows(control: Path, workspace_id: str) -> list:
    conn = _immutable(control)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT id, source_thread_id, state, stored_thread_id FROM handoffs "
                "WHERE workspace_id=?",
                (workspace_id,),
            )
        ]
    finally:
        conn.close()


def _restored_thread(root: Path, workspace_id: str, thread_id: str):
    from commonplace.workspace_store import open_threads, thread_path, workspace_path

    directory = workspace_path(root, workspace_id)
    if not thread_path(directory).exists():
        return None
    store = open_threads(directory)
    try:
        return store.by_source(workspace_id, thread_id)
    finally:
        store.close()


def _no_dangling_handoffs(root: Path, workspace_id: str):
    for row in _handoff_rows(root / "control.sqlite3", workspace_id):
        if row["state"] in ("stored", "launched", "done", "launch_failed"):
            assert row["stored_thread_id"], f"RED238: handoff {row['id']} has no stored thread"
            found = _restored_thread(root, workspace_id, row["source_thread_id"])
            assert found is not None and found.id == row["stored_thread_id"], (
                f"RED238: dangling handoff {row['id']} in the restored snapshot"
            )


# ── O1 ──


def test_backup_succeeds_while_gateway_serves(harness):
    harness.start()
    for index in range(3):
        added = harness.add_drawer(f"drawer before backup {index}")
        if "result" not in added:
            pytest.fail(f"SETUP: stub worker write failed: {added}")
    samples, stop = [], threading.Event()
    sampler = threading.Thread(target=_healthz_sampler, args=(harness, samples, stop), daemon=True)
    sampler.start()
    try:
        run = harness.backup()
        run.wait_seam_or_exit(SEAM_PINNED)
        code = run.finish()
    finally:
        stop.set()
        sampler.join(timeout=10)
    assert code == 0, f"RED238: online backup failed: {run.output()[-2000:]}"
    assert run.published(), "RED238: no published snapshot"
    assert not Path(str(run.dest) + ".partial").exists(), "RED238: partial snapshot left"
    _verify(run.dest)
    restored = harness.restore(run)
    rows = _assert_worker_files_consistent(
        harness.worker_palace(restored, harness.ws1), 3, "restored worker"
    )
    assert rows[:3] == [f"drawer before backup {i}" for i in range(3)], "RED238: drawers lost"
    assert samples and all(status == 200 for status in samples), (
        f"RED238: /healthz not 200 during the backup: {sorted(set(samples))}"
    )
    assert harness.healthz() == 200, "RED238: gateway stopped serving"
    assert harness.memory_ok(), "RED238: memory did not resume after the backup"


# ── O2 ──


def test_cut_consistent_between_store_and_mark_stored(harness, monkeypatch):
    from commonplace.handoff.thread_store import ThreadStore
    from commonplace.handoff.thread_store_v2 import ThreadStoreV2

    thread_id = "cut-between-store-and-mark"
    stored, release = threading.Event(), threading.Event()

    def blocking(original):
        def store(self, workspace_id, source_thread_id, *args, **kwargs):
            result = original(self, workspace_id, source_thread_id, *args, **kwargs)
            if source_thread_id == thread_id and not release.is_set():
                stored.set()
                release.wait(120)
            return result

        return store

    monkeypatch.setattr(ThreadStore, "store", blocking(ThreadStore.store))
    monkeypatch.setattr(ThreadStoreV2, "store", blocking(ThreadStoreV2.store))
    harness.closers.append(release.set)
    harness.start()
    harness.handoff_settings()
    harness.store_thread(harness.ws1, "already-stored")
    handoff = _Call(lambda: harness.run_handoff(thread_id))
    handoff.start()
    if not stored.wait(30):
        pytest.fail(f"SETUP: the handoff never stored its thread: {handoff.error!r}")
    live = {row["source_thread_id"]: row for row in _handoff_rows_live(harness)}
    if live.get(thread_id, {}).get("state") != "pending":
        pytest.fail(f"SETUP: expected a pending handoff before the cut: {live}")

    run = harness.backup(hold=(SEAM_PINNED,))
    run.wait_seam_or_exit(SEAM_PINNED)
    release.set()
    handoff.join(timeout=60)
    if handoff.is_alive() or handoff.error is not None:
        pytest.fail(f"SETUP: the handoff did not finish after the cut: {handoff.error!r}")
    live = {row["source_thread_id"]: row for row in _handoff_rows_live(harness)}
    assert live[thread_id]["state"] != "pending", "RED238: mark_stored blocked by the snapshot"
    run.release(SEAM_PINNED)
    code = run.finish()
    assert code == 0, f"RED238: online backup failed: {run.output()[-2000:]}"
    restored = harness.restore(run)
    rows = {
        row["source_thread_id"]: row
        for row in _handoff_rows(restored / "control.sqlite3", harness.ws1)
    }
    # Both pre-barrier writes (control `pending`, threads `store`) or neither.
    assert thread_id in rows, "RED238: the pending handoff row is missing from the cut"
    assert rows[thread_id]["state"] == "pending", (
        f"RED238: mark_stored after the cut leaked into it: {rows[thread_id]['state']}"
    )
    assert _restored_thread(restored, harness.ws1, thread_id) is not None, (
        "RED238: control row in the cut without its stored thread"
    )
    assert _restored_thread(restored, harness.ws1, "already-stored") is not None, (
        "RED238: earlier thread missing"
    )
    _no_dangling_handoffs(restored, harness.ws1)


def _handoff_rows_live(harness):
    with harness.db._lock:
        return [
            dict(row)
            for row in harness.db._conn.execute(
                "SELECT source_thread_id, state, stored_thread_id FROM handoffs "
                "WHERE workspace_id=?",
                (harness.ws1,),
            )
        ]


# ── O3 ──


def test_creator_blocked_after_enumeration_restores_one_cut(harness):
    from commonplace.workspace_store import thread_path

    harness.start()
    harness.store_thread(harness.ws1, "thread-a")
    ws2 = harness.db.create_workspace("second", harness.admin)
    w2_root = harness.data / "workspaces" / ws2
    run = harness.backup(hold=(SEAM_ENUMERATED,))
    run.wait_seam_or_exit(SEAM_ENUMERATED)
    creator = _Call(lambda: harness.store_thread(ws2, "thread-x"))
    creator.start()
    creator.join(timeout=1.0)
    blocked = creator.is_alive()
    created = thread_path(w2_root).exists() if w2_root.is_dir() else False
    run.release(SEAM_ENUMERATED)
    assert blocked, f"RED238: a creator ran while the cut was frozen: {creator.error!r}"
    assert not created, "RED238: a new thread database appeared while frozen"
    creator.join(timeout=60)
    assert not creator.is_alive() and creator.error is None, (
        f"RED238: the creator did not complete after the thaw: {creator.error!r}"
    )
    code = run.finish()
    assert code == 0, f"RED238: online backup failed: {run.output()[-2000:]}"
    restored = harness.restore(run)
    assert _restored_thread(restored, harness.ws1, "thread-a") is not None, (
        "RED238: thread A missing from the cut"
    )
    assert _restored_thread(restored, ws2, "thread-x") is None, (
        "RED238: thread X stored after the cut is in it"
    )
    conn = _immutable(restored / "control.sqlite3")
    try:
        found = conn.execute("SELECT id FROM workspaces WHERE id=?", (ws2,)).fetchone()
    finally:
        conn.close()
    assert found is not None, "RED238: W2's control row missing from the cut"


# ── O4 ──


def _generation_fixture(harness) -> str:
    """A workspace whose active generation is compact, with a retained v1 one."""
    ws = harness.db.create_workspace("generations", harness.admin)
    root = harness.data / "workspaces" / ws
    root.mkdir(parents=True, mode=0o700)
    active = f"{ws}.generation-{'a' * 24}"
    retained = f"{ws}.generation-{'b' * 24}"
    (root.parent / active).mkdir(mode=0o700)
    (root.parent / active / "workspace.json").write_text(json.dumps({"store": "compact"}))
    (root.parent / retained).mkdir(mode=0o700)
    old = sqlite3.connect(root.parent / retained / "threads.sqlite3")
    old.execute("CREATE TABLE retired (id INTEGER PRIMARY KEY, note TEXT)")
    old.execute("INSERT INTO retired (note) VALUES ('kept')")
    old.commit()
    old.close()
    (root / ".store-pointer.json").write_text(
        json.dumps({"active": active, "retained": [{"name": retained, "retired_at": 1.0}]})
    )
    return ws


def _runner_insert(store, job_id, task):
    from commonplace.runner import jobs

    now = jobs._now()
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO runner_jobs (id, task_number, board_slug, machine_id, project, topic, "
            "grant_version, runtime_mode, requested_by, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                task,
                "ops",
                "machine-1",
                "proj",
                "build",
                1,
                "auto",
                "test",
                "cancelled",
                now,
                now,
            ),
        )


def _memory_sample(harness, method) -> dict:
    started = time.monotonic()
    if method == "tools/call":
        response = harness.tool("mempalace_status")
    elif method == "initialize":
        response = harness.rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "backup-test", "version": "1"},
            },
        )
    else:
        response = harness.rpc("ping")
    error = response.get("error") if isinstance(response, dict) else None
    ok = isinstance(response, dict) and "result" in response and not error
    if ok and method == "tools/call" and response["result"].get("isError"):
        ok = False
    return {"method": method, "started": started, "ok": ok, "error": error, "raw": response}


def test_concurrent_writes_restore_and_verify(harness):
    from commonplace.chat.store import open_replies
    from commonplace.runner.jobs import JobStore

    harness.start()
    gen_ws = _generation_fixture(harness)
    gen_token = harness.owner_token(gen_ws)
    if "result" not in harness.add_drawer("first drawer"):
        pytest.fail("SETUP: stub worker write failed")
    if "result" not in harness.rpc("tools/call", {"name": "mempalace_status"}, token=gen_token):
        pytest.fail("SETUP: generation workspace worker did not start")
    harness.store_thread(harness.ws1, "v1-thread")
    harness.store_thread(gen_ws, "v2-thread")
    runner = JobStore(harness.gateway, harness.ws1)
    _runner_insert(runner, "job-before", 1)
    replies = open_replies(harness.gateway, harness.ws1)
    replies.add(
        board="ops",
        message_id=0,
        for_user="u1",
        kind="answer",
        code="ok",
        text="before",
        redacted=[],
        model="local/test",
    )
    board = harness.tool("task_add", title="card before", board="ops")
    if "result" not in board or board["result"].get("isError"):
        pytest.fail(f"SETUP: board write failed: {board}")
    # Fixture files: a .bak database, an ELF core, a growing prompt log.
    bak = sqlite3.connect(harness.data / "control.sqlite3.20260101.bak")
    bak.execute("CREATE TABLE t (x)")
    bak.commit()
    bak.close()
    (harness.data / "core.123").write_bytes(b"\x7fELF" + b"\0" * 4092)
    prompts = harness.data / "handoff-prompts"
    prompts.mkdir(mode=0o700, exist_ok=True)
    log = prompts / "x.log"
    log.write_text("line 0\n")

    failures, counts, stop = [], {}, threading.Event()

    def writer(kind, fn):
        index = 0
        while not stop.is_set():
            index += 1
            try:
                fn(index)
                counts[kind] = counts.get(kind, 0) + 1
            except Exception as exc:
                failures.append(f"{kind}#{index}: {exc!r}")
            stop.wait(0.1)

    def board_write(i):
        response = harness.tool("task_add", title=f"card {i}", board="ops")
        if "result" not in response or response["result"].get("isError"):
            raise RuntimeError(f"board write refused: {str(response)[:200]}")

    def chat_write(i):
        replies.add(
            board="ops",
            message_id=i,
            for_user="u1",
            kind="answer",
            code="ok",
            text=f"reply {i}",
            redacted=[],
            model="local/test",
        )

    def log_write(i):
        with open(log, "a") as out:
            out.write(f"line {i}\n")

    writers = {
        "board": board_write,
        "chat": chat_write,
        "runner": lambda i: _runner_insert(runner, f"job-{i}", 100 + i),
        "threads": lambda i: harness.store_thread(harness.ws1, f"during-{i}"),
        "threads-v2": lambda i: harness.store_thread(gen_ws, f"during-v2-{i}"),
        "control": lambda i: harness.db.create_user(f"user{i}@example.com"),
        "log": log_write,
    }
    samples = []

    def memory():
        methods = ("tools/call", "ping", "initialize")
        index = 0
        while not stop.is_set():
            samples.append(_memory_sample(harness, methods[index % 3]))
            index += 1
            stop.wait(0.1)

    threads = [
        threading.Thread(target=writer, args=(kind, fn), daemon=True)
        for kind, fn in writers.items()
    ]
    threads.append(threading.Thread(target=memory, daemon=True))
    for thread in threads:
        thread.start()
    try:
        time.sleep(0.5)
        run = harness.backup(hold=(SEAM_ENUMERATED,))
        run.wait_seam_or_exit(SEAM_ENUMERATED)
        # Frozen: nothing is created, deleted or renamed until the cut.
        at_cut = _cut_listing(harness.data)
        run.release(SEAM_ENUMERATED)
        code = run.finish()
        time.sleep(0.5)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=90)
    assert code == 0, f"RED238: online backup failed: {run.output()[-2000:]}"
    assert not failures, f"RED238: writes failed during the online backup: {failures[:10]}"
    for kind in writers:
        assert counts.get(kind), f"RED238: no {kind} write committed during the run"
    # Memory: fails only inside the pause window W, with -32603, never elsewhere.
    samples.sort(key=lambda sample: sample["started"])
    seen_failure = recovered = False
    for sample in samples:
        if not sample["ok"]:
            assert not recovered, f"RED238: memory failed outside the pause window: {sample['raw']}"
            error = sample["error"] or {}
            assert error.get("code") == -32603, (
                f"RED238: memory failure not -32603: {sample['raw']}"
            )
            assert (error.get("data") or {}).get("retry_after_s") == 30, (
                f"RED238: memory failure without retry_after_s: {sample['raw']}"
            )
            seen_failure = True
        elif seen_failure and sample["method"] == "tools/call":
            recovered = True
    assert samples and samples[0]["ok"], "RED238: memory failed before the backup started"
    assert harness.memory_ok(), "RED238: memory did not resume after the backup"

    restored = harness.restore(run)
    _assert_worker_files_consistent(harness.worker_palace(restored, harness.ws1), 1, "W1 worker")
    assert _restored_thread(restored, harness.ws1, "v1-thread"), "RED238: v1 thread lost"
    assert _restored_thread(restored, gen_ws, "v2-thread"), "RED238: compact thread lost"
    restored_log = (restored / "handoff-prompts" / "x.log").read_bytes()
    assert log.read_bytes().startswith(restored_log) and restored_log.endswith(b"\n"), (
        "RED238: growing log is not a prefix captured at the cut"
    )
    # Online file set = the offline inventory at the cut minus the exclusions.
    manifest = json.loads((run.dest / "manifest.json").read_text())
    online = {entry["path"] for entry in manifest["files"]}
    assert "core.123" not in online, "RED238: ELF core copied into the snapshot"
    assert online == at_cut, (
        f"RED238: online file set differs from the inventory at the cut: "
        f"extra {sorted(online - at_cut)} missing {sorted(at_cut - online)}"
    )
    for name in (
        "control.sqlite3",
        "control.sqlite3.20260101.bak",
        f"workspaces/{harness.ws1}/threads.sqlite3",
        f"workspaces/{harness.ws1}/chat.sqlite3",
        f"workspaces/{harness.ws1}/runner.sqlite3",
        f"workspaces/{gen_ws}/.store-pointer.json",
        f"workspaces/{gen_ws}.generation-{'a' * 24}/workspace.json",
        f"workspaces/{gen_ws}.generation-{'a' * 24}/threads.v2.sqlite3",
        f"workspaces/{gen_ws}.generation-{'b' * 24}/threads.sqlite3",
        "handoff-prompts/x.log",
    ):
        assert name in online, f"RED238: {name} missing from the online snapshot"


# ── O5 ──


def test_pause_drains_inflight_never_interrupts(harness):
    harness.start()
    if "result" not in harness.add_drawer("warm up"):
        pytest.fail("SETUP: stub worker write failed")
    release = harness.tmp / "release-held-call"
    harness.closers.append(release.touch)
    call = _Call(lambda: harness.add_drawer(f"HOLD:{release}"))
    call.start()
    if not _wait(lambda: Path(str(release) + ".started").exists(), 30):
        pytest.fail(f"SETUP: the held call never reached the worker: {call.error!r}")
    run = harness.backup(hold=(SEAM_ENUMERATED,))
    early = run.wait_seam_or_exit(SEAM_ENUMERATED, timeout=2.0, required=False)
    assert not early, "RED238: the pause did not wait for the in-flight call"
    assert call.is_alive(), "RED238: the in-flight call was cut off by the pause"
    release.touch()
    call.join(timeout=60)
    assert not call.is_alive(), "RED238: the held call never completed"
    assert call.error is None and "result" in call.result, (
        f"RED238: the in-flight call was interrupted: {call.error!r} {call.result}"
    )
    assert not call.result["result"].get("isError"), f"RED238: held call failed: {call.result}"
    run.wait_seam_or_exit(SEAM_ENUMERATED)
    reached_at = (run.seams / f"{SEAM_ENUMERATED}.reached").stat().st_mtime
    assert reached_at >= call.returned_at - 0.5, "RED238: the cut began before the drain"
    run.release(SEAM_ENUMERATED)
    code = run.finish()
    assert code == 0, f"RED238: online backup failed: {run.output()[-2000:]}"
    restored = harness.restore(run)
    rows = _assert_worker_files_consistent(
        harness.worker_palace(restored, harness.ws1), 2, "restored worker"
    )
    assert f"HOLD:{release}" in rows, "RED238: the drained call's write is not in the snapshot"


# ── O6 ──


def test_lease_lapse_mid_copy_invalidates_run(harness):
    harness.start()
    if "result" not in harness.add_drawer("before"):
        pytest.fail("SETUP: stub worker write failed")
    run = harness.backup(env={"COMMONPLACE_BACKUP_LEASE_S": "2"}, hold=(SEAM_PINNED,))
    run.wait_seam_or_exit(SEAM_PINNED)
    # Still paused (resume follows the worker copy); the held child makes no
    # progress, so the 2 s pause lease lapses.
    time.sleep(6)
    assert _wait(harness.memory_ok, 30, interval=0.5), (
        "RED238: workers did not resume after the lease lapsed"
    )
    run.release(SEAM_PINNED)
    code = run.finish()
    output = run.output()
    assert code != 0, "RED238: a run whose lease lapsed succeeded"
    assert "invalid" in output.lower() or "lease lapsed" in output.lower(), (
        f"RED238: lease lapse not reported INVALID: {output[-1000:]}"
    )
    assert run.nothing_published(), "RED238: an INVALID run published a snapshot"
    assert _lock_free(harness.staging / f"{run.stamp}.lock"), "RED238: run lock still held"
    assert harness.memory_ok(), "RED238: memory unavailable after the INVALID run"


# ── O7 ──


@pytest.mark.parametrize("dead", ["client", "supervisor"])
def test_dead_parent_kills_child_nothing_published(harness, dead):
    stale = 3
    harness.start()
    if "result" not in harness.add_drawer("before"):
        pytest.fail("SETUP: stub worker write failed")
    run = harness.backup(
        env={"COMMONPLACE_BACKUP_HEARTBEAT_STALE_S": str(stale)}, hold=(SEAM_PINNED,)
    )
    run.wait_seam_or_exit(SEAM_PINNED)
    if dead == "client":
        run.stop_heartbeat()
    else:
        os.kill(run.proc.pid, signal.SIGKILL)
    bound = stale + 25

    def settled():
        return (
            run.proc.poll() is not None
            and not _processes_naming(run.stamp)
            and _lock_free(harness.staging / f"{run.stamp}.lock")
        )

    _wait(settled, bound, interval=0.2)
    assert run.proc.poll() is not None, "RED238: the supervisor outlived its dead client"
    assert not _processes_naming(run.stamp), (
        f"RED238: snapshot child still alive {bound}s after its parent died"
    )
    assert _lock_free(harness.staging / f"{run.stamp}.lock"), "RED238: run lock still held"
    assert run.nothing_published(), "RED238: a killed run published a snapshot"
    if dead == "client":
        assert run.proc.returncode != 0, "RED238: a run with a stale heartbeat exited 0"
    assert _wait(harness.memory_ok, 30, interval=0.5), "RED238: workers did not resume"
    # The next run cleans up after the dead one and completes.
    following = harness.backup()
    following.wait_seam_or_exit(SEAM_PINNED)
    code = following.finish()
    assert code == 0, f"RED238: the next run failed: {following.output()[-2000:]}"
    assert following.published(), "RED238: the next run published nothing"
    _verify(following.dest)


# ── O8 ──


def test_rollback_journal_writer_not_blocked(harness):
    from commonplace.runner.jobs import JobStore

    harness.start()
    runner = JobStore(harness.gateway, harness.ws1)
    _runner_insert(runner, "job-before", 1)
    conn = sqlite3.connect(runner.path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    if mode.lower() == "wal":
        pytest.fail("SETUP: runner database is expected to use a rollback journal")
    run = harness.backup(hold=(SEAM_PINNED,))
    run.wait_seam_or_exit(SEAM_PINNED)
    write = _Call(lambda: _runner_insert(runner, "job-during", 2))
    started = time.monotonic()
    write.start()
    run.release(SEAM_PINNED)
    write.join(timeout=20)
    elapsed = time.monotonic() - started
    assert not write.is_alive(), "RED238: runner write still blocked after 20 s"
    assert write.error is None, f"RED238: runner write failed during copy-first: {write.error!r}"
    assert elapsed < 10, f"RED238: runner write waited {elapsed:.1f}s (its timeout is 10 s)"
    code = run.finish()
    assert code == 0, f"RED238: online backup failed: {run.output()[-2000:]}"
    restored = harness.restore(run)
    conn = _immutable(restored / "workspaces" / harness.ws1 / "runner.sqlite3")
    try:
        ids = {row["id"] for row in conn.execute("SELECT id FROM runner_jobs")}
    finally:
        conn.close()
    assert "job-before" in ids, "RED238: runner row from before the cut lost"
    assert "job-during" not in ids, "RED238: runner write after the cut leaked into it"


# ── O9 ──

SUCCESSOR = """#!/bin/sh
# Successor stand-in: leaves one session member writing the data tree, exits.
state="$1"; db="$2"; pidfile="$3"
(
  while :; do
    printf 'x' | dd of="$state" bs=1 seek=0 count=1 conv=notrunc 2>/dev/null
    dd if="$db" of="$db" bs=512 count=1 conv=notrunc 2>/dev/null
    sleep 0.2
  done
) </dev/null >/dev/null 2>&1 &
echo $! > "$pidfile"
exit 0
"""


def _successor_script(harness, name="claude", body=SUCCESSOR) -> Path:
    bin_dir = harness.tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / name
    script.write_text(body)
    script.chmod(stat.S_IRWXU)
    return script


def _successor_ref(result) -> str:
    successor = getattr(getattr(result, "handoff", None), "successor", None)
    return getattr(successor, "agent_ref", "") or ""


@pytest.mark.parametrize("case", ["non_python", "after_gateway_restart"])
def test_live_successor_refuses_backup(harness, case):
    from commonplace.handoff.models import LauncherConfig

    harness.start()
    if "result" not in harness.add_drawer("before"):
        pytest.fail("SETUP: stub worker write failed")
    w1 = harness.workspace_dir(harness.ws1)
    state = w1 / "state.bin"
    state.write_bytes(b"\0" * 64)
    db = harness.worker_palace(harness.data, harness.ws1) / DB_NAME
    pidfile = harness.tmp / "descendant.pid"
    script = _successor_script(harness)
    harness.handoff_settings(
        launcher=LauncherConfig(
            kind="command", argv=(str(script), str(state), str(db), str(pidfile))
        )
    )
    result = harness.run_handoff(f"successor-{case}")
    successor = _successor_ref(result)
    if not successor.startswith("pid:"):
        pytest.fail(f"SETUP: the command successor did not start: {result}")
    leader = int(successor[4:])
    harness.sessions.append(leader)
    if not _wait(lambda: pidfile.exists() and pidfile.read_text().strip(), 10):
        pytest.fail("SETUP: the successor left no descendant")
    descendant = int(pidfile.read_text())
    if not _wait(lambda: not _alive(leader), 10) or not _alive(descendant):
        pytest.fail("SETUP: expected an exited leader and a live session descendant")
    if os.getsid(descendant) != leader:
        pytest.fail("SETUP: the descendant left the successor's session")
    if case == "after_gateway_restart":
        harness.restart()  # The registry comes back from its persisted file.
    run = harness.backup()
    try:
        code = run.proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "RED238: expected stage successor active (the backup kept running)"
        ) from None
    output = run.output()
    if code != 0 and PIN_REFUSAL in output:
        raise AssertionError("RED238: expected stage successor active")
    assert code != 0, "RED238: expected stage successor active (the backup succeeded)"
    if "successor active" not in output:
        pytest.fail(f"SETUP: backup failed at another stage: {output[-2000:]}")
    assert run.nothing_published(), "RED238: a refused run published a snapshot"
    assert _alive(descendant), "RED238: the backup must refuse, not kill, a live successor"


# ── O10 ──


def test_successor_launch_mid_run_invalidates(harness):
    from commonplace.handoff.models import LauncherConfig

    harness.start()
    harness.store_thread(harness.ws1, "before-launch")
    script = _successor_script(harness, body="#!/bin/sh\nexec sleep 3\n")
    harness.handoff_settings(launcher=LauncherConfig(kind="command", argv=(str(script),)))
    run = harness.backup(hold=(SEAM_PINNED,))
    run.wait_seam_or_exit(SEAM_PINNED)
    launch = _Call(lambda: harness.run_handoff("launched-mid-run"))
    launch.start()
    launch.join(timeout=60)
    run.release(SEAM_PINNED)
    if launch.is_alive() or launch.error is not None:
        pytest.fail(f"SETUP: the mid-run handoff did not finish: {launch.error!r}")
    successor = _successor_ref(launch.result)
    if not successor.startswith("pid:"):
        pytest.fail(f"SETUP: no successor launched mid-run: {launch.result}")
    harness.sessions.append(int(successor[4:]))
    code = run.finish()
    output = run.output()
    assert code != 0, "RED238: a run with a successor launched mid-run succeeded"
    assert "successor launched" in output, (
        f"RED238: expected INVALID (successor launched): {output[-1000:]}"
    )
    assert run.nothing_published(), "RED238: an INVALID run published a snapshot"


# ── O11 ──


def test_backup_succeeds_while_real_serve_runs(harness):
    from commonplace.workers import _free_loopback_port

    port = _free_loopback_port()
    env = harness.cli_env()
    log = open(harness.tmp / "serve.log", "wb")
    harness.closers.append(log.close)
    serve = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "commonplace",
            "--data",
            str(harness.data),
            "serve",
            "--port",
            str(port),
        ],
        env=env,
        cwd=str(harness.tmp),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    def stop_serve():
        if serve.poll() is None:
            serve.terminate()
            try:
                serve.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(serve.pid, signal.SIGKILL)
                serve.wait(timeout=10)

    harness.closers.append(stop_serve)
    harness.port = port
    if not _wait(lambda: serve.poll() is not None or harness.healthz() == 200, 60, 0.2):
        pytest.fail("SETUP: commonplace serve did not answer /healthz")
    if serve.poll() is not None:
        pytest.fail(f"SETUP: commonplace serve exited: {(harness.tmp / 'serve.log').read_text()}")
    run = harness.backup()
    run.wait_seam_or_exit(SEAM_PINNED)
    code = run.finish()
    assert code == 0, f"RED238: backup failed while serve ran: {run.output()[-2000:]}"
    assert run.published(), "RED238: no published snapshot"
    _verify(run.dest)
    assert serve.poll() is None, "RED238: the backup stopped commonplace serve"
    assert harness.healthz() == 200, "RED238: serve stopped answering"


# ── backup through cli.main(): exempt from main()'s shared fence and data lock ──


def test_backup_commands_run_through_cli_main(harness, capsys):
    """``backup DEST`` with nothing serving, then ``backup --online`` beside a gateway.

    ``cli.main`` runs one-shot commands under a shared fence and data lock;
    ``backup`` is exempt because the offline copy takes the data lock exclusive
    and the snapshot child takes the fence exclusive. Holding either shared in
    main() would refuse both runs.
    """
    from commonplace import cli

    offline = harness.tmp / "offline-snapshot"
    code = cli.main(["--data", str(harness.data), "backup", str(offline)])
    err = capsys.readouterr().err
    assert code == 0, f"offline `backup DEST` through cli.main failed: {err}"
    _verify(offline)

    harness.start()
    stamp = harness.stamp()
    dest = harness.staging / stamp
    (harness.staging / f"{stamp}.hb").touch()  # as the hook does
    result = subprocess.run(
        [sys.executable, "-m", "commonplace", "--data", str(harness.data), "backup"]
        + ["--online", "--deadline", str(time.time() + 600), str(dest)],
        env=harness.cli_env(),
        cwd=str(harness.tmp),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"`backup --online` failed: {result.stderr[-2000:]}"
    assert result.stdout.strip() == str(dest / "manifest.json")
    _verify(dest)
    assert _lock_free(harness.data / ".commonplace-fence.lock")
    assert harness.healthz() == 200


def test_lock_held_probe_modes(tmp_path):
    from commonplace.backup_online import _lock_held

    path = tmp_path / "probe.lock"
    path.touch()
    assert not _lock_held(path) and not _lock_held(path, only_exclusive=True)
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        assert _lock_held(path), "a shared holder counts by default"
        assert not _lock_held(path, only_exclusive=True), "a shared holder is not exclusive"
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert _lock_held(path) and _lock_held(path, only_exclusive=True)
    finally:
        os.close(fd)
