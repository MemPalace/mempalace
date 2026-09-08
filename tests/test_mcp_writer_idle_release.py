"""Writer-lease idle release (fork patch 2026-08-19).

The lease is dropped by a watchdog after a period with no completed mutating
call, so an orphaned-but-alive stdio server cannot keep peer sessions
read-only. These tests exercise the release decision and the in-flight
guard directly; the acquire path's self-healing retry is covered by the
existing peer-writer tests.
"""

import time

import mempalace.mcp_server as m


class _FakeLockCM:
    def __init__(self):
        self.exited = False

    def __exit__(self, *args):
        self.exited = True


def _hold_lease(monkeypatch):
    cm = _FakeLockCM()
    monkeypatch.setattr(m, "_MCP_WRITER_LOCK_CM", cm)
    monkeypatch.setattr(m, "_MCP_WRITER_INFLIGHT", 0)
    monkeypatch.setattr(m, "_discard_mcp_storage_handles", lambda: None)
    return cm


def test_releases_lease_idle_past_threshold(monkeypatch):
    cm = _hold_lease(monkeypatch)
    monkeypatch.setattr(m, "_last_mutating_time", time.monotonic() - 3600)
    assert m._maybe_release_idle_writer(600) is True
    assert m._MCP_WRITER_LOCK_CM is None
    assert cm.exited


def test_keeps_lease_within_threshold(monkeypatch):
    cm = _hold_lease(monkeypatch)
    monkeypatch.setattr(m, "_last_mutating_time", time.monotonic())
    assert m._maybe_release_idle_writer(600) is False
    assert m._MCP_WRITER_LOCK_CM is cm
    assert not cm.exited


def test_keeps_lease_with_inflight_call(monkeypatch):
    cm = _hold_lease(monkeypatch)
    monkeypatch.setattr(m, "_last_mutating_time", time.monotonic() - 3600)
    monkeypatch.setattr(m, "_MCP_WRITER_INFLIGHT", 1)
    assert m._maybe_release_idle_writer(600) is False
    assert m._MCP_WRITER_LOCK_CM is cm
    assert not cm.exited


def test_noop_without_lease(monkeypatch):
    monkeypatch.setattr(m, "_MCP_WRITER_LOCK_CM", None)
    monkeypatch.setattr(m, "_MCP_WRITER_INFLIGHT", 0)
    assert m._maybe_release_idle_writer(600) is False


def test_inflight_counter_and_mutating_clock(monkeypatch):
    monkeypatch.setattr(m, "_MCP_WRITER_INFLIGHT", 0)
    before = time.monotonic() - 999
    monkeypatch.setattr(m, "_last_mutating_time", before)
    mutating = sorted(m._MUTATING_TOOLS - m._HTTP_LOCK_FREE_TOOLS)[0]
    with m._writer_inflight(mutating):
        assert m._MCP_WRITER_INFLIGHT == 1
    assert m._MCP_WRITER_INFLIGHT == 0
    assert m._last_mutating_time > before


def test_read_tool_counts_inflight_but_not_clock(monkeypatch):
    monkeypatch.setattr(m, "_MCP_WRITER_INFLIGHT", 0)
    before = time.monotonic() - 999
    monkeypatch.setattr(m, "_last_mutating_time", before)
    read_tool = "mempalace_search"
    assert read_tool not in m._MUTATING_TOOLS
    with m._writer_inflight(read_tool):
        assert m._MCP_WRITER_INFLIGHT == 1
    assert m._MCP_WRITER_INFLIGHT == 0
    assert m._last_mutating_time == before


def test_lock_free_tool_skips_counter(monkeypatch):
    monkeypatch.setattr(m, "_MCP_WRITER_INFLIGHT", 0)
    tool = sorted(m._HTTP_LOCK_FREE_TOOLS)[0]
    with m._writer_inflight(tool):
        assert m._MCP_WRITER_INFLIGHT == 0


def test_idle_secs_env_parsing(monkeypatch):
    monkeypatch.setenv(m._MCP_WRITER_IDLE_MINUTES_ENV, "5")
    assert m._writer_idle_release_secs() == 300
    monkeypatch.setenv(m._MCP_WRITER_IDLE_MINUTES_ENV, "0")
    assert m._writer_idle_release_secs() == 0
    monkeypatch.setenv(m._MCP_WRITER_IDLE_MINUTES_ENV, "lixo")
    assert m._writer_idle_release_secs() == m._MCP_WRITER_IDLE_MINUTES_DEFAULT * 60
    monkeypatch.delenv(m._MCP_WRITER_IDLE_MINUTES_ENV)
    assert m._writer_idle_release_secs() == m._MCP_WRITER_IDLE_MINUTES_DEFAULT * 60
