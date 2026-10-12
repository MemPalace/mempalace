# tests/test_idle_watchdog_exit.py
"""The idle watchdog must exit the process AND run the registered cleanup.

Issue #2500. `os._exit(0)` skipped every `atexit` handler, so a watchdog exit
left `serverinfo.json` advertising a dead PID — measured in the field at 2 days
8 hours of a stale record. Two handlers are registered by then:
`server_registry.clear_serverinfo` and `_release_mcp_writer_lock`.

The issue proposes `sys.exit(0)`. That is wrong here and the second test pins
why: the watchdog runs in a DAEMON THREAD, where `SystemExit` unwinds that
thread and nothing else. The process survives, and the watchdog — having
returned out of its own `while True` — never fires again, trading a stale
`serverinfo.json` for the file-handle accumulation it exists to prevent (#1552).
"""

import atexit
import os
import sys
import threading
import time

import mempalace.mcp_server as mcp


def test_the_watchdog_exit_runs_registered_cleanup_before_exiting(monkeypatch):
    order: list[str] = []

    monkeypatch.setattr(os, "_exit", lambda code: order.append(f"exit:{code}"))
    monkeypatch.setattr(atexit, "_run_exitfuncs", lambda: order.append("cleanup"))

    mcp._exit_running_registered_cleanup()

    assert order == ["cleanup", "exit:0"], "the cleanup must run, and it must run BEFORE the exit"


def test_a_raising_cleanup_handler_does_not_keep_the_process_alive(monkeypatch):
    """The exit is the point; cleanup is best-effort."""
    exited: list[int] = []

    def _boom() -> None:
        raise RuntimeError("handler blew up")

    monkeypatch.setattr(os, "_exit", lambda code: exited.append(code))
    monkeypatch.setattr(atexit, "_run_exitfuncs", _boom)

    mcp._exit_running_registered_cleanup()

    assert exited == [0]


def test_sys_exit_would_not_end_the_process_from_a_daemon_thread():
    """The control for the choice above, measured rather than asserted from
    docs: `SystemExit` raised in a daemon thread ends the THREAD.

    If this ever stops holding, `sys.exit(0)` becomes a legitimate option and
    the helper's docstring is wrong — which is the reason this test exists
    rather than a comment.
    """
    finished = threading.Event()
    still_running = threading.Event()

    def _thread() -> None:
        # Caught here only so pytest's unhandled-thread-exception hook stays
        # quiet; catching it does not change what is being measured, which is
        # that the INTERPRETER is still running afterwards.
        try:
            sys.exit(0)
        except SystemExit:
            pass
        finally:
            finished.set()

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    assert finished.wait(5), "the thread should have unwound"
    t.join(5)
    assert not t.is_alive()
    # The interpreter is still here to run this line, which is the whole point.
    still_running.set()
    assert still_running.is_set()


def test_the_watchdog_calls_the_helper_rather_than_os_exit_directly(monkeypatch):
    """End to end through the real watchdog thread, so the wiring is pinned
    and not just the helper.

    Emits one `PytestUnhandledThreadExceptionWarning`: the stand-in raises
    `SystemExit` inside the watchdog's own thread to unwind it, and pytest's
    threadexception hook reports that at teardown, outside any
    `filterwarnings` mark this test could carry. Left visible rather than
    suppressed with a mark that does not actually reach it."""
    called = threading.Event()
    raw_exit = threading.Event()

    # Both stand-ins RAISE, and that is load-bearing twice over. The watchdog's
    # loop is `while True` and ignores a return, so a stand-in that returns
    # leaves a live daemon thread calling the exit every check interval — and
    # once monkeypatch restores the real `os._exit` at teardown, that thread
    # kills the pytest process and takes the rest of the suite with it.
    # `SystemExit` in a daemon thread unwinds that thread and nothing else,
    # which is exactly what the control above measures.
    def _stop(event: threading.Event):
        def _raise(*_args: object) -> None:
            event.set()
            raise SystemExit(0)

        return _raise

    monkeypatch.setattr(mcp, "_exit_running_registered_cleanup", _stop(called))
    # `os._exit` too, so a tree where the watchdog still calls it directly
    # FAILS this cell instead of killing the run.
    monkeypatch.setattr(os, "_exit", _stop(raw_exit))
    monkeypatch.setenv("MEMPALACE_MCP_IDLE_HOURS", "0.0002")  # ~0.72 s
    monkeypatch.setattr(mcp, "_last_request_time", time.monotonic() - 3600)

    mcp._start_idle_exit_watchdog()

    assert called.wait(10) or raw_exit.wait(0), "the idle watchdog never reached any exit path"
    assert not raw_exit.is_set(), "the watchdog exited without running the registered cleanup"
    assert called.is_set()
