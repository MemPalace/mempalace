"""Exercise shutdown in disposable interpreters, including after Python is gone."""

import os
import subprocess
import sys
import textwrap

import psutil
import pytest

# The forced-hang fixtures use a tiny grace so a missed deadline fails fast.
# Normal interpreter teardown of a child that imported mempalace.miner takes
# 0.1 to 0.5 s on a loaded runner, so a tiny grace must never bound a clean
# shutdown; tests that expect a clean exit use CLEAN_EXIT_GRACE instead.
FORCED_HANG_GRACE = "0.2"
CLEAN_EXIT_GRACE = "60"
# Hang guard for the whole child, including a cold import under xdist load.
# It is not what any assertion measures.
CHILD_TIMEOUT = 60


def _shutdown_env(tmp_path, grace):
    pid_file = tmp_path / "mine.pid"
    env = os.environ.copy()
    env["MEMPALACE_MINE_PID_FILE"] = str(pid_file)
    env["MEMPALACE_HOOK_MINE_EXIT_GRACE_SECONDS"] = grace
    return pid_file, env


def _wait_helper_exit(pid_file):
    helper_pid = int(pid_file.with_suffix(".pid.helper").read_text())
    try:
        psutil.Process(helper_pid).wait(timeout=CHILD_TIMEOUT)
    except psutil.NoSuchProcess:
        pass


def _run_shutdown(tmp_path, body, exit_code=0, after_arm="", grace=FORCED_HANG_GRACE):
    pid_file, env = _shutdown_env(tmp_path, grace)
    script = (
        "import os, pathlib, sys, threading, time\n"
        "from mempalace.miner import _arm_hook_mine_exit_watchdog\n"
        "pathlib.Path(os.environ['MEMPALACE_MINE_PID_FILE']).write_text(str(os.getpid()))\n"
        + textwrap.dedent(body)
        + f"\nhandle = _arm_hook_mine_exit_watchdog({exit_code})\n"
        + "pathlib.Path(os.environ['MEMPALACE_MINE_PID_FILE'] + '.helper').write_text(str(handle.process.pid))\n"
        + textwrap.dedent(after_arm)
        + f"sys.exit({exit_code})\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, timeout=CHILD_TIMEOUT
    )
    assert not pid_file.exists()
    _wait_helper_exit(pid_file)
    return proc


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_shutdown_deadline_survives_blocked_stream(tmp_path, stream):
    proc = _run_shutdown(
        tmp_path,
        f"""
        class BlockedStream:
            def write(self, _text):
                time.sleep(30)
            def flush(self):
                time.sleep(30)
        sys.{stream} = BlockedStream()
        threading.Thread(target=time.sleep, args=(30,), daemon=False).start()
        """,
        exit_code=7,
    )
    # POSIX signal termination is reported as -SIGKILL; Windows preserves the code.
    assert proc.returncode == (7 if os.name == "nt" else -9)


def test_shutdown_deadline_survives_native_finalizer(tmp_path):
    import platform

    if os.name == "nt" and platform.machine().lower() not in ("amd64", "x86_64"):
        pytest.skip("Windows native loop fixture requires x64")
    proc = _run_shutdown(
        tmp_path,
        """
        import ctypes
        # A native `void(void)` callback that loops forever without entering
        # Python. A ctypes/Python callback would itself need the torn-down GIL
        # and would not test Py_AtExit. No compiler is needed for this fixture.
        if os.name == 'nt':
            code = b'\\xeb\\xfe'  # x64 jmp to self
            alloc = ctypes.windll.kernel32.VirtualAlloc
            alloc.restype = ctypes.c_void_p
            alloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong, ctypes.c_ulong]
            address = alloc(None, len(code), 0x3000, 0x40)
            assert address
            ctypes.memmove(address, code, len(code))
        else:
            # pause(void) is a native indefinite wait; its unused int return
            # value needs no Python trampoline and works on macOS ARM64 too.
            libc = ctypes.CDLL(None)
            address = ctypes.cast(libc.pause, ctypes.c_void_p).value
        register = ctypes.pythonapi.Py_AtExit
        register.argtypes = [ctypes.c_void_p]
        register.restype = ctypes.c_int
        assert register(address) == 0
        """,
        exit_code=7,
    )
    assert proc.returncode == (7 if os.name == "nt" else -9)


def test_armed_watchdog_waits_for_interpreter_shutdown(tmp_path):
    """While the owner is alive, the helper never starts its grace period.

    The child arms a FORCED_HANG_GRACE watchdog, reports that it is armed and
    whether the shutdown trigger was sent, then blocks until the test releases
    it. The test holds the child live for many grace periods: a trigger sent at
    arming time would kill it. Load can only make this check pass late, never
    fail it. The child then cancels the helper before exiting, so the tiny
    grace never races normal interpreter teardown (see the clean-exit test).
    """
    pid_file, env = _shutdown_env(tmp_path, FORCED_HANG_GRACE)
    script = (
        "import os, pathlib, sys\n"
        "from mempalace.miner import _arm_hook_mine_exit_watchdog\n"
        "handle = _arm_hook_mine_exit_watchdog(0)\n"
        "pathlib.Path(os.environ['MEMPALACE_MINE_PID_FILE'] + '.helper')"
        ".write_text(str(handle.process.pid))\n"
        "os.write(1, b'T' if handle._started else b'A')\n"
        "if os.read(0, 1) != b'G':\n"
        "    sys.exit(3)\n"
        "os.write(1, b'T' if handle._started else b'L')\n"
        "handle.cancel()\n"
        "sys.exit(0)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        assert child.stdout.read(1) == b"A", "trigger sent at arming time"
        helper = psutil.Process(int(pid_file.with_suffix(".pid.helper").read_text()))
        live_for = 10 * float(FORCED_HANG_GRACE)
        try:
            child.wait(timeout=live_for)
        except subprocess.TimeoutExpired:
            pass
        assert child.poll() is None, "watchdog killed the owner during its live period"
        assert helper.is_running() and helper.status() != psutil.STATUS_ZOMBIE
        child.stdin.write(b"G")
        child.stdin.flush()
        assert child.stdout.read(1) == b"L", "trigger sent during the live period"
        _out, err = child.communicate(timeout=CHILD_TIMEOUT)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert child.returncode == 0, err
    assert not pid_file.exists()
    _wait_helper_exit(pid_file)


def test_armed_watchdog_lets_clean_shutdown_finish(tmp_path):
    """A normal exit sends the trigger at shutdown and is not killed.

    An atexit handler registered before arming runs after the helper was
    triggered (threading atexits run first, then atexit in LIFO order) and
    reports whether the trigger was sent. The grace is CLEAN_EXIT_GRACE so the
    assertion does not depend on how long finalization takes under load.
    """
    proc = _run_shutdown(
        tmp_path,
        """
        import atexit
        atexit.register(lambda: os.write(1, b'S' if handle._started else b'N'))
        """,
        grace=CLEAN_EXIT_GRACE,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b"S"


def test_watchdog_rearming_reaps_old_helper(tmp_path):
    proc = _run_shutdown(
        tmp_path,
        "",
        after_arm="""
        old = handle
        handle = _arm_hook_mine_exit_watchdog(7)
        assert old.process.poll() is not None
        handle.cancel()
        assert handle.process.poll() is not None
        time.sleep(0.6)
        """,
    )
    assert proc.returncode == 0


@pytest.mark.parametrize("raw", ["inf", "nan", "0", "-1", "bad"])
def test_invalid_shutdown_grace_uses_bounded_default(monkeypatch, raw):
    from mempalace.miner import _hook_mine_exit_grace_seconds

    monkeypatch.setenv("MEMPALACE_HOOK_MINE_EXIT_GRACE_SECONDS", raw)
    assert _hook_mine_exit_grace_seconds() == 60.0
