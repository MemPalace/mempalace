"""Process liveness.

One probe so the daemon, the hook mine slots, and the server registry
agree on whether a pid is still running.

Semantics:
- A value that is not a positive ``int`` is not alive. ``bool`` is rejected
  (it is a subclass of ``int`` but never a pid).
- POSIX: ``os.kill(pid, 0)``. ``ProcessLookupError`` and any other
  ``OSError`` mean the process is gone. ``PermissionError`` means the
  process exists and this user cannot signal it, so it is alive.
- Windows: ``WaitForSingleObject`` on a ``SYNCHRONIZE`` handle. Never
  ``os.kill`` — signal 0 is ``CTRL_C_EVENT`` and can interrupt this
  process. Access denied means the process exists. If the Win32 probe
  itself fails, assume alive rather than discard a live holder.
"""

from __future__ import annotations

import os

_WIN_SYNCHRONIZE = 0x00100000
_WIN_WAIT_OBJECT_0 = 0x00000000
_WIN_ERROR_ACCESS_DENIED = 5
_WIN_MAX_PID = 0xFFFFFFFF


def kernel32():
    """``kernel32`` with the process-handle calls typed for 64-bit handles."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k32


def _pid_alive_windows(pid: int) -> bool:
    """Win32 liveness probe with no console-control side effect."""
    import ctypes
    from ctypes import wintypes

    if pid > _WIN_MAX_PID:
        return False
    k32 = kernel32()
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)

    handle = k32.OpenProcess(_WIN_SYNCHRONIZE, False, pid)
    if not handle:
        # Access denied: the process exists but isn't ours to open.
        # Any other error (invalid parameter / not found) means it's gone.
        return ctypes.get_last_error() == _WIN_ERROR_ACCESS_DENIED
    try:
        # Only an exited process is signalled. WAIT_TIMEOUT means running, and
        # WAIT_FAILED is a failed probe, which counts as alive.
        return k32.WaitForSingleObject(handle, 0) != _WIN_WAIT_OBJECT_0
    finally:
        k32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Return whether ``pid`` is a running process. See the module docstring."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            return _pid_alive_windows(pid)
        except OSError:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
