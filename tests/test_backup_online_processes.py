"""Online writer scans distinguish gateway supervisors from foreign writers."""

import os
import sys

import pytest

from commonplace import backup
from commonplace.backup_online import OnlineBackupError, _Cut, _Gateway
from commonplace.backup_pause import Knobs

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc scan")


@pytest.fixture
def process_tree(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()

    def add(pid, parent, args, *, environment=True):
        entry = proc / str(pid)
        entry.mkdir()
        (entry / "stat").write_text(f"{pid} (worker (wrapper)) S {parent} 0 0\n")
        (entry / "cmdline").write_bytes(b"\0".join(os.fsencode(arg) for arg in args) + b"\0")
        (entry / "environ").write_bytes(
            b"COMMONPLACE_DATA=" + os.fsencode(root) + b"\0" if environment else b""
        )
        (entry / "cwd").symlink_to(root, target_is_directory=True)

    # An exec-created backup has its own ancestry, separate from the serve init.
    add(90, 0, ["commonplace", "backup", "--online"])
    (proc / "self").symlink_to("90", target_is_directory=True)
    monkeypatch.setattr(backup, "_PROC_ROOT", proc)
    monkeypatch.setattr(backup.os, "getpid", lambda: 90)

    def cut(gateway_pid, worker_pids=()):
        control = _Gateway(None, root, Knobs())
        control.info = {"gateway_pid": gateway_pid, "worker_pids": worker_pids}
        return _Cut(root, root / "unused", control, Knobs(), None, None, None)

    return root, add, cut


@pytest.mark.parametrize("workers", [True, False], ids=["preflight", "paused"])
@pytest.mark.parametrize("init_pid,gateway_pid", [(1, 7), (41, 67)])
def test_gateway_supervisor_is_not_a_foreign_writer(process_tree, workers, init_pid, gateway_pid):
    root, add, cut = process_tree
    wrapper = [
        "/sbin/docker-init",
        "--",
        "commonplace",
        "serve",
        "--host",
        "0.0.0.0",
        "--port",
        "8765",
    ]
    add(init_pid, 0, wrapper)
    parent = init_pid
    if init_pid != 1:
        # Multiple arbitrary supervisors: neither PID 1 nor a name is special.
        add(51, init_pid, ["/sbin/tini", "--", "commonplace", "serve", "--data", str(root)])
        parent = 51
    add(gateway_pid, parent, ["/usr/local/bin/python3.12", "/usr/local/bin/commonplace", "serve"])

    error = None
    try:
        cut(gateway_pid)._scan(workers=workers)
    except OnlineBackupError as exc:
        error = exc
    assert error is None, f"gateway supervisor was mistaken for a foreign writer: {error}"


@pytest.mark.parametrize("workers", [True, False], ids=["preflight", "paused"])
@pytest.mark.parametrize("executable", ["/sbin/docker-init", "/usr/bin/python3"])
@pytest.mark.parametrize("environment", [True, False], ids=["environment", "data-argument"])
def test_foreign_writer_is_still_refused(process_tree, workers, executable, environment):
    root, add, cut = process_tree
    wrapper_args = ["--", "commonplace", "serve", "--data", str(root)]
    add(1, 0, ["/sbin/docker-init", *wrapper_args])
    add(7, 1, ["python3", "commonplace", "serve"])
    # Same argv shape as the exempt supervisor, but not on the gateway's chain.
    add(99, 1, [executable, *wrapper_args], environment=environment)
    with pytest.raises(OnlineBackupError, match="process 99 outside the gateway"):
        cut(7)._scan(workers=workers)


def test_worker_exemption_ends_after_pause(process_tree):
    _root, add, cut = process_tree
    add(1, 0, ["/sbin/docker-init", "--", "commonplace", "serve"])
    add(7, 1, ["python3", "commonplace", "serve"])
    add(12, 7, ["python3", "commonplace", "worker"])
    scanner = cut(7, worker_pids=[12])
    error = None
    try:
        scanner._scan(workers=True)
    except OnlineBackupError as exc:
        error = exc
    assert error is None, f"preflight refused gateway processes: {error}"
    with pytest.raises(OnlineBackupError, match="process 12 outside the gateway"):
        scanner._scan(workers=False)
