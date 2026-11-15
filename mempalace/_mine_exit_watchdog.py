"""Stdlib-only hook-mine supervisor, also executable with ``python -I -S``.

No diagnostic or buffered I/O belongs on the termination path. This process
outlives the miner's Python threads and native Py_AtExit callbacks.
"""

import os
import signal
import subprocess
import sys
import time


def _windows_process_api():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    return kernel


def start_watchdog(grace: float, exit_code: int):
    """Return a helper with a one-byte shutdown trigger on its stdin pipe."""
    from pathlib import Path

    options = {}
    handle = None
    target = os.getpid()
    if os.name == "nt":
        kernel = _windows_process_api()
        # Inherit a handle to this exact process, avoiding PID reuse races.
        handle = kernel.OpenProcess(0x00100001, True, target)  # SYNCHRONIZE | TERMINATE
        if not handle:
            import ctypes

            raise ctypes.WinError(ctypes.get_last_error())
        target = handle
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpAttributeList = {"handle_list": [handle]}
        options.update(startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        options["start_new_session"] = True
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                str(Path(__file__).resolve()),
                str(target),
                str(grace),
                str(exit_code),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            close_fds=True,
            **options,
        )
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)


def _supervise(target: int, grace: float, exit_code: int) -> None:
    # EOF before the shutdown marker means the owner exited or cancelled.
    if os.read(sys.stdin.fileno(), 1) != b"S":
        return
    if os.name == "nt":
        kernel = _windows_process_api()
        try:
            # Wait on the inherited handle, never open a possibly reused PID.
            deadline = time.monotonic() + grace
            while kernel.WaitForSingleObject(target, 50) == 0x102:  # WAIT_TIMEOUT
                if time.monotonic() >= deadline:
                    kernel.TerminateProcess(target, exit_code & 0xFFFFFFFF)
                    break
        finally:
            kernel.CloseHandle(target)
    else:
        deadline = time.monotonic() + grace
        while os.getppid() == target:
            if time.monotonic() >= deadline:
                # A helper is reparented when its owner exits, so a reused PID
                # is not treated as our owner. SIGKILL also covers native hangs.
                try:
                    os.kill(target, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    _supervise(int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3]))
