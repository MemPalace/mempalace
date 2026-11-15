"""Exercise shutdown in disposable interpreters, including after Python is gone."""

import os
import subprocess
import sys
import textwrap

import psutil
import pytest


def _run_shutdown(tmp_path, body, exit_code=0, after_arm=""):
    pid_file = tmp_path / "mine.pid"
    env = os.environ.copy()
    env["MEMPALACE_MINE_PID_FILE"] = str(pid_file)
    env["MEMPALACE_HOOK_MINE_EXIT_GRACE_SECONDS"] = "0.2"
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
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, timeout=5)
    assert not pid_file.exists()
    helper_pid = int(pid_file.with_suffix(".pid.helper").read_text())
    try:
        psutil.Process(helper_pid).wait(timeout=5)
    except psutil.NoSuchProcess:
        pass
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
    proc = _run_shutdown(tmp_path, "", after_arm="time.sleep(0.6)\n")
    assert proc.returncode == 0


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
