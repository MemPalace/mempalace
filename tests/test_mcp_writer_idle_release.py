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


def test_inflight_counter_leaves_clock_alone(monkeypatch):
    monkeypatch.setattr(m, "_MCP_WRITER_INFLIGHT", 0)
    before = time.monotonic() - 999
    monkeypatch.setattr(m, "_last_mutating_time", before)
    mutating = sorted(m._MUTATING_TOOLS - m._HTTP_LOCK_FREE_TOOLS)[0]
    with m._writer_inflight(mutating):
        assert m._MCP_WRITER_INFLIGHT == 1
    assert m._MCP_WRITER_INFLIGHT == 0
    assert m._last_mutating_time == before


def test_idle_clock_refreshed_after_mutating_dispatch(monkeypatch):
    before = time.monotonic() - 999
    monkeypatch.setattr(m, "_last_mutating_time", before)
    mutating = sorted(m._MUTATING_TOOLS - m._HTTP_LOCK_FREE_TOOLS)[0]
    with m._writer_idle_clock(mutating):
        assert m._last_mutating_time == before
    assert m._last_mutating_time > before


def test_read_tool_counts_inflight_but_not_clock(monkeypatch):
    monkeypatch.setattr(m, "_MCP_WRITER_INFLIGHT", 0)
    before = time.monotonic() - 999
    monkeypatch.setattr(m, "_last_mutating_time", before)
    read_tool = "mempalace_search"
    assert read_tool not in m._MUTATING_TOOLS
    with m._writer_inflight(read_tool), m._writer_idle_clock(read_tool):
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


# --- dispatch-path race (review on #2305) ---------------------------------

_PREFLIGHT_GATES = (
    "_mcp_read_only_refusal",
    "_mcp_sqlite_integrity_refusal",
    "_mcp_stale_library_refusal",
    "_mcp_diverged_index_refusal",
)


def _pass_earlier_gates(monkeypatch):
    for gate in _PREFLIGHT_GATES:
        monkeypatch.setattr(m, gate, lambda req_id, tool_name: None)


def _install_write_tool(monkeypatch, handler):
    tool = "mempalace_add_drawer"
    monkeypatch.setitem(
        m.TOOLS,
        tool,
        {
            "description": "test write tool",
            "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}},
            "handler": handler,
        },
    )
    return tool


def _call(tool):
    return m.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {"content": "hello"}},
        }
    )


def test_watchdog_tick_after_peer_preflight_keeps_lease_through_dispatch(monkeypatch):
    """A held-but-write-idle lease passes the peer-writer check without a
    fresh acquire, so the clock is still past the threshold. A watchdog tick
    between that check and dispatch must not drop the lease the call was
    just cleared to write under."""
    cm = _hold_lease(monkeypatch)
    monkeypatch.setattr(m, "_last_mutating_time", time.monotonic() - 3600)
    _pass_earlier_gates(monkeypatch)

    original_refusal = m._mcp_peer_writer_refusal
    ticks = []

    def refusal_then_watchdog_tick(req_id, tool_name):
        refused = original_refusal(req_id, tool_name)
        assert refused is None
        ticks.append(m._maybe_release_idle_writer(600))
        return refused

    monkeypatch.setattr(m, "_mcp_peer_writer_refusal", refusal_then_watchdog_tick)

    seen = {}

    def handler(**kwargs):
        seen["lease"] = m._MCP_WRITER_LOCK_CM
        return {"ok": True}

    tool = _install_write_tool(monkeypatch, handler)
    response = _call(tool)

    assert "result" in response, response
    assert ticks == [False]
    assert seen["lease"] is cm
    assert m._MCP_WRITER_LOCK_CM is cm
    assert not cm.exited
    assert m._MCP_WRITER_INFLIGHT == 0


def test_refusals_do_not_refresh_write_idle_clock(monkeypatch):
    """Refused mutating calls must not keep an idle holder alive: a server
    refusing at a preflight gate would otherwise never release."""
    _hold_lease(monkeypatch)
    before = time.monotonic() - 3600
    monkeypatch.setattr(m, "_last_mutating_time", before)
    _pass_earlier_gates(monkeypatch)

    def handler(**kwargs):
        raise AssertionError("refused call reached the handler")

    tool = _install_write_tool(monkeypatch, handler)
    refusal = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32002, "message": "refused"}}

    for gate in (*_PREFLIGHT_GATES, "_mcp_peer_writer_refusal"):
        monkeypatch.setattr(m, gate, lambda req_id, tool_name: refusal)
        for _ in range(3):
            assert _call(tool) is refusal
        monkeypatch.setattr(m, gate, lambda req_id, tool_name: None)

    assert m._last_mutating_time == before
    assert m._MCP_WRITER_INFLIGHT == 0
    assert m._maybe_release_idle_writer(600) is True
