"""The daemon, hook slots, and server registry share one pid liveness probe.

PermissionError means the process exists and this user cannot signal it.
It must not be reported as dead. Windows access-denied is the same answer.
"""

from __future__ import annotations

import os

import pytest

from mempalace import daemon, hooks_cli, server_registry
from mempalace._proc import _pid_alive_windows, pid_alive

_CALL_SITES = (
    daemon._pid_alive,
    hooks_cli._pid_alive,
    server_registry._pid_alive,
)


def test_call_sites_are_the_shared_helper():
    assert daemon._pid_alive is pid_alive
    assert hooks_cli._pid_alive is pid_alive
    assert server_registry._pid_alive is pid_alive


def test_permission_error_means_alive_at_every_call_site(monkeypatch):
    def _kill(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "kill", _kill)
    assert [fn(4242) for fn in _CALL_SITES] == [True, True, True]


def test_missing_and_other_oserror_mean_dead_at_every_call_site(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")

    def _missing(pid, sig):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(os, "kill", _missing)
    assert [fn(4242) for fn in _CALL_SITES] == [False, False, False]

    def _other(pid, sig):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(os, "kill", _other)
    assert [fn(4242) for fn in _CALL_SITES] == [False, False, False]


def test_non_positive_and_non_int_are_dead_at_every_call_site():
    for value in (0, -1, None, "12", True, 1.5):
        assert [fn(value) for fn in _CALL_SITES] == [False, False, False]


def test_live_pid_agrees(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    seen = []

    def _kill(pid, sig):
        seen.append((pid, sig))

    monkeypatch.setattr(os, "kill", _kill)
    assert [fn(os.getpid()) for fn in _CALL_SITES] == [True, True, True]
    assert seen == [(os.getpid(), 0), (os.getpid(), 0), (os.getpid(), 0)]


def test_windows_probe_failure_assumes_alive_and_does_not_signal(monkeypatch):
    killed = []

    def _boom(pid):
        raise OSError("Win32 probe failed")

    def _kill(pid, sig):
        killed.append(pid)

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr("mempalace._proc._pid_alive_windows", _boom)
    monkeypatch.setattr(os, "kill", _kill)
    assert [fn(99) for fn in _CALL_SITES] == [True, True, True]
    assert killed == []


class _Kernel:
    """Plain functions, not methods: the probe assigns ``restype`` on them."""

    def __init__(self, handle, wait_result):
        self.closed = False
        self.opened_pid = None

        def open_process(access, inherit, pid):
            self.opened_pid = pid
            return handle

        def wait(handle_arg, timeout):
            assert handle_arg == handle
            assert timeout == 0
            return wait_result

        def close(handle_arg):
            self.closed = True
            return 1

        self.OpenProcess = open_process
        self.WaitForSingleObject = wait
        self.CloseHandle = close


def _install_kernel(monkeypatch, kernel, last_error):
    import ctypes

    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)
    return kernel


def test_windows_access_denied_is_alive(monkeypatch):
    kernel = _install_kernel(monkeypatch, _Kernel(handle=0, wait_result=0), last_error=5)
    assert _pid_alive_windows(77) is True
    assert kernel.closed is False


def test_windows_not_found_is_dead(monkeypatch):
    _install_kernel(monkeypatch, _Kernel(handle=0, wait_result=0), last_error=87)
    assert _pid_alive_windows(77) is False


def test_windows_wait_timeout_is_alive_and_handle_is_closed(monkeypatch):
    kernel = _install_kernel(monkeypatch, _Kernel(handle=5, wait_result=0x00000102), last_error=0)
    assert _pid_alive_windows(77) is True
    assert kernel.opened_pid == 77
    assert kernel.closed is True


def test_windows_signalled_process_is_dead(monkeypatch):
    kernel = _install_kernel(monkeypatch, _Kernel(handle=5, wait_result=0), last_error=0)
    assert _pid_alive_windows(77) is False
    assert kernel.closed is True


def test_windows_failed_wait_is_alive(monkeypatch):
    kernel = _install_kernel(monkeypatch, _Kernel(handle=5, wait_result=0xFFFFFFFF), last_error=0)
    assert _pid_alive_windows(77) is True
    assert kernel.closed is True


def test_windows_pid_beyond_dword_is_dead(monkeypatch):
    kernel = _install_kernel(monkeypatch, _Kernel(handle=5, wait_result=0x00000102), last_error=0)
    assert _pid_alive_windows(2**32 + 77) is False
    assert kernel.opened_pid is None


def test_windows_path_uses_wait_helper(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr("mempalace._proc._pid_alive_windows", lambda pid: pid == 5)
    assert pid_alive(5) is True
    assert pid_alive(6) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX os.kill(pid, 0) is the live check")
def test_current_process_is_alive_without_mocks():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(2_000_000_000) is False
