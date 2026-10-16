# tests/test_mcp_http_call_timing.py
"""
Per-call timing lines and the timestamped MEMPALACE_LOG_FILE (#2620).

Two guarantees are covered here:

* every locked palace tool call emits exactly one
  ``CALL <tool> <read|write> wait=<ms> run=<ms> <ok|error>`` line, with the
  dispatch-lock wait measured separately from the handler run, and
* the ``MEMPALACE_LOG_FILE`` handler stamps time and level on each line, so a
  server-side record can be lined up against a client-side timeout.

The timing helper is driven directly rather than through a socket: the wait is
only observable when the dispatch lock is already held by another thread, and
that is exactly the queueing the issue is about.
"""

import logging
import re
import subprocess
import sys
import threading
import time

import pytest

from mempalace import mcp_server as mcp


@pytest.fixture
def caplog_calls(caplog):
    """Capture the ``mempalace_mcp`` logger at INFO for the duration of a test."""
    caplog.set_level(logging.INFO, logger="mempalace_mcp")
    return caplog


def _call_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("CALL ")]


_CALL_RE = re.compile(
    r"^CALL (?P<tool>\S+) (?P<mode>read|write) wait=(?P<wait>\d+)ms run=(?P<run>\d+)ms (?P<out>ok|error)$"
)


class TestCallLineShape:
    """One line per locked call, carrying tool, mode, wait, run and outcome."""

    def test_read_call_emits_one_line_with_read_mode(self, caplog_calls):
        with mcp._http_call_timing("mempalace_search", "read"):
            pass
        lines = _call_lines(caplog_calls)
        assert len(lines) == 1, lines
        match = _CALL_RE.match(lines[0])
        assert match, lines[0]
        assert match["tool"] == "mempalace_search"
        assert match["mode"] == "read"
        assert match["out"] == "ok"
        assert int(match["wait"]) >= 0 and int(match["run"]) >= 0

    def test_write_call_reports_write_mode(self, caplog_calls):
        with mcp._http_call_timing("mempalace_diary_write", "write"):
            pass
        line = _call_lines(caplog_calls)[0]
        assert _CALL_RE.match(line)["mode"] == "write"

    def test_failing_call_is_logged_before_the_exception_propagates(self, caplog_calls):
        with pytest.raises(RuntimeError):
            with mcp._http_call_timing("mempalace_search", "read"):
                raise RuntimeError("boom")
        line = _call_lines(caplog_calls)[0]
        assert _CALL_RE.match(line)["out"] == "error"

    def test_lock_is_released_even_when_the_handler_raises(self):
        """A failed call must not leak the dispatch lock to the next one."""
        lock = mcp._RWLock()
        with pytest.raises(RuntimeError):
            with mcp._http_call_timing("mempalace_search", "write", (lock,)):
                raise RuntimeError("boom")
        assert not lock._writer and lock._readers == 0


class TestWaitIsMeasuredApartFromRun:
    """The line exists to separate a queued call from a slow one."""

    def test_lock_contention_lands_in_wait_not_run(self, caplog_calls):
        lock = mcp._RWLock()
        held = threading.Event()
        release = threading.Event()

        def _holder():
            with lock:
                held.set()
                release.wait(5)

        holder = threading.Thread(target=_holder, daemon=True)
        holder.start()
        assert held.wait(5)
        try:
            with mcp._http_call_timing("mempalace_search", "read", (lock.read_lock(),)):
                pass
        finally:
            release.set()
            holder.join(timeout=5)

        match = _CALL_RE.match(_call_lines(caplog_calls)[0])
        # Only the wait can be long here: the handler body is empty.
        assert int(match["wait"]) > 0, match.group(0)
        assert int(match["run"]) == 0, match.group(0)

    def test_slow_handler_lands_in_run_not_wait(self, caplog_calls):
        lock = mcp._RWLock()
        with mcp._http_call_timing("mempalace_search", "read", (lock.read_lock(),)):
            time.sleep(0.05)
        match = _CALL_RE.match(_call_lines(caplog_calls)[0])
        assert int(match["wait"]) == 0, match.group(0)
        assert int(match["run"]) >= 40, match.group(0)


class TestSlowCallEscalation:
    """``MEMPALACE_SLOW_CALL_WARN_SECS`` escalates the line to WARNING."""

    def test_default_threshold_is_20s(self):
        assert mcp._http_slow_call_warn_secs() == mcp._HTTP_SLOW_CALL_WARN_DEFAULT
        assert mcp._HTTP_SLOW_CALL_WARN_DEFAULT == 20.0

    def test_fast_call_stays_at_info(self, caplog_calls):
        with mcp._http_call_timing("mempalace_search", "read"):
            pass
        record = next(r for r in caplog_calls.records if r.getMessage().startswith("CALL "))
        assert record.levelno == logging.INFO

    def test_slow_run_escalates_to_warning(self, caplog_calls, monkeypatch):
        monkeypatch.setenv(mcp._HTTP_SLOW_CALL_WARN_ENV, "0.05")
        with mcp._http_call_timing("mempalace_search", "read"):
            time.sleep(0.08)
        record = next(r for r in caplog_calls.records if r.getMessage().startswith("SLOW CALL "))
        assert record.levelno == logging.WARNING
        assert _CALL_RE.match(record.getMessage().removeprefix("SLOW "))

    def test_zero_disables_the_escalation(self, caplog_calls, monkeypatch):
        monkeypatch.setenv(mcp._HTTP_SLOW_CALL_WARN_ENV, "0")
        with mcp._http_call_timing("mempalace_search", "read"):
            time.sleep(0.08)
        assert not any(r.getMessage().startswith("SLOW CALL ") for r in caplog_calls.records)

    def test_malformed_value_warns_and_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv(mcp._HTTP_SLOW_CALL_WARN_ENV, "not-a-number")
        assert mcp._http_slow_call_warn_secs() == mcp._HTTP_SLOW_CALL_WARN_DEFAULT

    def test_negative_value_is_clamped_to_disabled(self, monkeypatch):
        monkeypatch.setenv(mcp._HTTP_SLOW_CALL_WARN_ENV, "-5")
        assert mcp._http_slow_call_warn_secs() == 0.0


class TestLoggingNeverFailsTheCall:
    """A diagnostic must not be able to fail the tool call it describes."""

    def test_logging_failure_does_not_propagate(self, monkeypatch, caplog_calls):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("logging is broken")

        monkeypatch.setattr(mcp.logger, "log", _boom)
        with mcp._http_call_timing("mempalace_search", "read"):
            pass  # must not raise

    def test_logging_failure_does_not_swallow_a_handler_error(self, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("logging is broken")

        monkeypatch.setattr(mcp.logger, "log", _boom)
        with pytest.raises(ValueError, match="handler"):
            with mcp._http_call_timing("mempalace_search", "read"):
                raise ValueError("handler")


class TestDispatchEmitsOneLinePerLockedCall:
    """The lines come from ``_http_dispatch``, not only from direct use."""

    @staticmethod
    def _tools_call(name):
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}

    def test_read_tool_call_is_timed(self, caplog_calls, monkeypatch):
        monkeypatch.setattr(mcp, "handle_request", lambda _request: {"jsonrpc": "2.0", "id": 1})
        mcp._http_dispatch(self._tools_call("mempalace_search"))
        lines = _call_lines(caplog_calls)
        assert len(lines) == 1, lines
        assert _CALL_RE.match(lines[0])["mode"] == "read"

    def test_write_tool_call_is_timed(self, caplog_calls, monkeypatch):
        monkeypatch.setattr(mcp, "handle_request", lambda _request: {"jsonrpc": "2.0", "id": 1})
        mcp._http_dispatch(self._tools_call("mempalace_add_drawer"))
        assert _CALL_RE.match(_call_lines(caplog_calls)[0])["mode"] == "write"

    def test_unknown_tool_fails_closed_and_is_still_timed(self, caplog_calls, monkeypatch):
        """Unclassified tools take the exclusive side — and still get a line."""
        monkeypatch.setattr(mcp, "handle_request", lambda _request: {"jsonrpc": "2.0", "id": 1})
        mcp._http_dispatch(self._tools_call("mempalace_not_a_tool"))
        lines = _call_lines(caplog_calls)
        assert len(lines) == 1, lines
        assert _CALL_RE.match(lines[0])["mode"] == "write"

    def test_lock_free_tool_is_not_timed(self, caplog_calls, monkeypatch):
        """A tool that never takes the lock has no wait to report."""
        monkeypatch.setattr(mcp, "handle_request", lambda _request: {"jsonrpc": "2.0", "id": 1})
        mcp._http_dispatch(self._tools_call("mempalace_kg_stats"))
        assert _call_lines(caplog_calls) == []

    def test_protocol_method_is_not_timed(self, caplog_calls, monkeypatch):
        monkeypatch.setattr(mcp, "handle_request", lambda _request: {"jsonrpc": "2.0", "id": 1})
        mcp._http_dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert _call_lines(caplog_calls) == []

    def test_failing_tool_call_is_timed_as_error(self, caplog_calls, monkeypatch):
        def _boom(_request):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(mcp, "handle_request", _boom)
        with pytest.raises(RuntimeError):
            mcp._http_dispatch(self._tools_call("mempalace_search"))
        assert _CALL_RE.match(_call_lines(caplog_calls)[0])["out"] == "error"

    def test_dispatch_still_releases_the_lock_after_a_failing_call(self, caplog_calls, monkeypatch):
        def _boom(_request):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(mcp, "handle_request", _boom)
        with pytest.raises(RuntimeError):
            mcp._http_dispatch(self._tools_call("mempalace_add_drawer"))
        assert not mcp._HTTP_REQUEST_LOCK._writer
        assert mcp._HTTP_REQUEST_LOCK._readers == 0


class TestLogFileCarriesTimestampAndLevel:
    """The file is the only durable record; it must be lineable-up-able.

    ``_init_logging`` runs at import time, so each case needs a fresh
    interpreter — the same subprocess pattern ``tests/mcp/test_protocol.py``
    uses for ``MEMPALACE_LOG_FILE``.
    """

    @staticmethod
    def _run_main(env_overrides: dict, extra_code: str = ""):
        import os

        env = {
            k: v
            for k, v in os.environ.items()
            if k not in env_overrides or env_overrides[k] is not None
        }
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        code = extra_code + "from mempalace.mcp_server import main\nmain()\n"
        return subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            input="",
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_file_handler_stamps_time_and_level(self, tmp_path):
        log_path = tmp_path / "mcp.log"
        result = self._run_main({"MEMPALACE_LOG_FILE": str(log_path)})
        assert result.returncode == 0, result.stderr
        lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert lines, "log file is empty"
        # Every line starts with an ISO-ish timestamp then a level name, so two
        # lines can be ordered and matched against a client-side timeout.
        pattern = re.compile(
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL) "
        )
        for line in lines:
            assert pattern.match(line), line
