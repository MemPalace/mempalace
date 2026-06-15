"""Regression tests for the parent-death watchdog (MCP stdio orphan fix).

The MCP stdio protocol carries no application-level liveness signal:
when a client (Claude / Codex / etc.) crashes, force-quits, or restarts
without closing stdio cleanly, the child ``mempalace-mcp`` server can
outlive its parent indefinitely — still holding ChromaDB / HNSW /
SQLite file handles. The 8 h idle watchdog (#1552) catches it
eventually; the parent-death watchdog catches it within ~5 s by polling
``os.getppid()`` and exiting as soon as the child is reparented to
PID 1.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest


WATCHDOG_THREAD_NAME = "mcp-parent-watchdog"


def _run(code, env=None, timeout=60):
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", code],
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def test_parent_watchdog_starts_thread_by_default():
    """No env override → ``_start_parent_death_watchdog()`` spawns a
    daemon thread named ``mcp-parent-watchdog``."""
    code = textwrap.dedent(
        f"""
        import sys, threading
        from mempalace.mcp_server import _start_parent_death_watchdog
        _start_parent_death_watchdog()
        names = [t.name for t in threading.enumerate()]
        sys.stderr.write("THREADS: " + ",".join(names) + "\\n")
        assert {WATCHDOG_THREAD_NAME!r} in names, names
        """
    )
    # Remove the override in case the harness sets one for development.
    env = {"MEMPALACE_MCP_PARENT_WATCHDOG": ""}
    result = _run(code, env=env)
    assert result.returncode == 0, f"stderr={result.stderr!r}"


@pytest.mark.parametrize("disable_value", ["0", "false", "False", "no", "off"])
def test_parent_watchdog_respects_disable_env(disable_value):
    """``MEMPALACE_MCP_PARENT_WATCHDOG=0/false/no/off`` skips the
    thread — escape hatch for embedded / supervised contexts where the
    host already provides liveness."""
    code = textwrap.dedent(
        f"""
        import sys, threading
        from mempalace.mcp_server import _start_parent_death_watchdog
        _start_parent_death_watchdog()
        names = [t.name for t in threading.enumerate()]
        sys.stderr.write("THREADS: " + ",".join(names) + "\\n")
        assert {WATCHDOG_THREAD_NAME!r} not in names, names
        """
    )
    result = _run(code, env={"MEMPALACE_MCP_PARENT_WATCHDOG": disable_value})
    assert result.returncode == 0, f"stderr={result.stderr!r}"


@pytest.mark.skipif(
    not os.path.exists("/dev/zero"),
    reason="end-to-end orphan test relies on POSIX /dev/zero to keep stdin blocking",
)
def test_parent_watchdog_exits_orphaned_process(tmp_path):
    """End-to-end live regression: spawn mempalace-mcp under a
    short-lived driver, let the driver exit, assert the orphaned child
    self-exits within 20 s and writes the expected log line to stderr.

    The child's stdin is bound to ``/dev/zero`` so ``readline()`` blocks
    forever — simulating an idle-but-connected client. Without the
    watchdog this would hang until the 8 h idle timeout.
    """
    stderr_path = tmp_path / "child.err"
    pid_path = tmp_path / "child.pid"

    driver_code = textwrap.dedent(
        f"""
        import sys, subprocess
        proc = subprocess.Popen(
            [sys.executable, "-c", "from mempalace.mcp_server import main; main()"],
            stdin=open("/dev/zero"),
            stdout=subprocess.DEVNULL,
            stderr=open({str(stderr_path)!r}, "w"),
        )
        with open({str(pid_path)!r}, "w") as f:
            f.write(str(proc.pid))
        # driver exits → child reparented to PID 1
        """
    )
    driver = subprocess.run(
        [sys.executable, "-c", driver_code],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert driver.returncode == 0, f"driver failed: stderr={driver.stderr!r}"

    child_pid = int(pid_path.read_text().strip())

    # Give heavy imports (chromadb / hnsw) a moment to finish so the
    # watchdog thread is actually running when we start polling.
    time.sleep(2.0)

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)  # existence check
        except ProcessLookupError:
            err = stderr_path.read_text()
            # Watchdog log line covers both "died (PPID was N)" and
            # "absent at startup" (driver may exit before child runs).
            assert "exiting orphan to release file handles" in err, (
                f"orphan exited but watchdog log line missing: stderr={err!r}"
            )
            return
        time.sleep(0.5)

    # Timeout — kill leaked child and fail.
    try:
        os.kill(child_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    pytest.fail(
        f"orphan child {child_pid} did not exit within 20 s; stderr={stderr_path.read_text()!r}"
    )
