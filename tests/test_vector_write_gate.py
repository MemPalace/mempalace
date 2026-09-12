"""Tests for the write-side half of the #1222 HNSW divergence guard.

The capacity probe already routed *reads* to the BM25 fallback when the flushed
HNSW segment lags sqlite. These cover the two gaps that left writes exposed:

* ``_mcp_diverged_index_refusal`` — a vector write into a diverged index is
  refused at dispatch instead of reaching chromadb, where an upsert can block
  for the life of the process.
* the write-stall watchdog — when a write does stop coming back anyway, a
  thread that the stuck call is not blocking says so on stderr, and an operator
  can opt into turning the wedge into a restartable exit.
"""

from __future__ import annotations

import threading

import pytest


REASON = "HNSW index holds 803 elements but sqlite has 820 embeddings"


@pytest.fixture
def diverged(monkeypatch):
    """A palace whose vector index is known-diverged, probe counted."""
    from mempalace import mcp_server

    probes = {"n": 0}

    def _probe():
        probes["n"] += 1

    monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", _probe)
    monkeypatch.setattr(mcp_server, "_vector_disabled", True)
    monkeypatch.setattr(mcp_server, "_vector_disabled_reason", REASON)
    return probes


# ── Dispatch gate ─────────────────────────────────────────────────────────


def test_vector_write_refused_while_index_diverged(diverged):
    from mempalace import mcp_server

    err = mcp_server._mcp_diverged_index_refusal(req_id=7, tool_name="mempalace_add_drawer")

    assert err is not None
    assert err["id"] == 7
    assert err["error"]["code"] == mcp_server._DIVERGED_INDEX_ERROR_CODE
    data = err["error"]["data"]
    assert data["tool"] == "mempalace_add_drawer"
    assert data["vector_disabled_reason"] == REASON
    # The refusal has to carry the way out, not just the verdict.
    assert "rebuild-index" in data["hint"]


@pytest.mark.parametrize(
    "tool",
    sorted(
        {
            "mempalace_add_drawer",
            "mempalace_update_drawer",
            "mempalace_delete_drawer",
            "mempalace_delete_by_source",
            "mempalace_diary_write",
            "mempalace_checkpoint",
            "mempalace_mine",
            "mempalace_sync",
        }
    ),
)
def test_every_vector_write_tool_is_gated(diverged, tool):
    from mempalace import mcp_server

    assert tool in mcp_server._MUTATING_TOOLS, "the vector set must stay a subset"
    assert mcp_server._mcp_diverged_index_refusal(req_id=1, tool_name=tool) is not None


@pytest.mark.parametrize(
    "tool",
    [
        "mempalace_kg_add",
        "mempalace_kg_invalidate",
        "mempalace_kg_supersede",
        "mempalace_create_tunnel",
        "mempalace_delete_tunnel",
        "mempalace_delete_hallway",
    ],
)
def test_non_vector_writes_are_not_gated(diverged, tool):
    """The knowledge graph and hallways keep their own state — a broken HNSW
    segment has no say over them, and must not even cost them a probe."""
    from mempalace import mcp_server

    assert mcp_server._mcp_diverged_index_refusal(req_id=1, tool_name=tool) is None
    assert diverged["n"] == 0


def test_read_tools_are_not_gated(diverged):
    """Reads have their own fallback (BM25); refusing them here would break it."""
    from mempalace import mcp_server

    assert mcp_server._mcp_diverged_index_refusal(req_id=1, tool_name="mempalace_search") is None


def test_healthy_index_allows_the_write(monkeypatch):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(mcp_server, "_vector_disabled", False)

    assert (
        mcp_server._mcp_diverged_index_refusal(req_id=1, tool_name="mempalace_add_drawer") is None
    )


def test_gate_re_probes_so_a_repair_un_gates_without_restart(diverged):
    """The gate must consult the probe on every call: a long-lived stdio server
    has to notice `mempalace repair` finishing in another process."""
    from mempalace import mcp_server

    mcp_server._mcp_diverged_index_refusal(req_id=1, tool_name="mempalace_add_drawer")
    mcp_server._mcp_diverged_index_refusal(req_id=2, tool_name="mempalace_add_drawer")

    assert diverged["n"] == 2


def test_preflight_reports_divergence_ahead_of_the_peer_writer_lock(diverged, monkeypatch):
    """Both gates can be up at once — a peer holds the lease *because* this
    palace is wedged. The diverged verdict is the actionable one."""
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_mcp_read_only_refusal", lambda req_id, tool_name: None)
    monkeypatch.setattr(mcp_server, "_mcp_sqlite_integrity_refusal", lambda req_id, tool_name: None)
    monkeypatch.setattr(
        mcp_server,
        "_mcp_peer_writer_refusal",
        lambda req_id, tool_name: {"error": {"code": -32001}},
    )

    err = mcp_server._mcp_tool_preflight_refusal(req_id=3, tool_name="mempalace_add_drawer")

    assert err["error"]["code"] == mcp_server._DIVERGED_INDEX_ERROR_CODE


def test_dispatch_refuses_before_the_handler_runs(diverged, monkeypatch):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_mcp_sqlite_integrity_refusal", lambda req_id, tool_name: None)
    monkeypatch.setattr(mcp_server, "_mcp_peer_writer_refusal", lambda req_id, tool_name: None)

    def _must_not_run(**kwargs):  # pragma: no cover - the point is that it never runs
        raise AssertionError("handler reached chromadb despite the diverged index")

    monkeypatch.setitem(mcp_server.TOOLS["mempalace_add_drawer"], "handler", _must_not_run)

    resp = mcp_server.handle_request(
        {
            "method": "tools/call",
            "id": 11,
            "params": {
                "name": "mempalace_add_drawer",
                "arguments": {"wing": "w", "room": "r", "content": "c"},
            },
        }
    )

    assert resp["error"]["code"] == mcp_server._DIVERGED_INDEX_ERROR_CODE


# ── Write-stall watchdog ──────────────────────────────────────────────────


def test_stall_action_warns_once_then_stays_quiet():
    from mempalace import mcp_server

    assert mcp_server._write_stall_action(59.0, 60.0, 0.0, False) is None
    assert mcp_server._write_stall_action(60.0, 60.0, 0.0, False) == "warn"
    assert mcp_server._write_stall_action(600.0, 60.0, 0.0, True) is None


def test_stall_action_escalates_to_exit_when_opted_in():
    from mempalace import mcp_server

    assert mcp_server._write_stall_action(299.0, 60.0, 300.0, True) is None
    assert mcp_server._write_stall_action(300.0, 60.0, 300.0, True) == "exit"
    # A single tick may cross both lines; exit wins over an unsent warning.
    assert mcp_server._write_stall_action(300.0, 60.0, 300.0, False) == "exit"


def test_stall_action_respects_disabled_thresholds():
    from mempalace import mcp_server

    assert mcp_server._write_stall_action(10_000.0, 0.0, 0.0, False) is None


def test_stall_secs_falls_back_on_garbage(monkeypatch):
    from mempalace import mcp_server

    monkeypatch.setenv(mcp_server._WRITE_STALL_WARN_ENV, "soon")
    assert mcp_server._write_stall_secs(mcp_server._WRITE_STALL_WARN_ENV, 60.0) == 60.0

    monkeypatch.setenv(mcp_server._WRITE_STALL_WARN_ENV, "-5")
    assert mcp_server._write_stall_secs(mcp_server._WRITE_STALL_WARN_ENV, 60.0) == 0.0

    monkeypatch.delenv(mcp_server._WRITE_STALL_WARN_ENV)
    assert mcp_server._write_stall_secs(mcp_server._WRITE_STALL_WARN_ENV, 60.0) == 60.0


def test_stall_watch_registers_the_write_and_clears_it():
    from mempalace import mcp_server

    with mcp_server._write_stall_watch("mempalace_add_drawer"):
        inflight = mcp_server._write_stall_inflight
        assert inflight is not None
        assert inflight["tool"] == "mempalace_add_drawer"
        assert inflight["warned"] is False

    assert mcp_server._write_stall_inflight is None


def test_stall_watch_clears_on_failure():
    """A raising handler must not leave a phantom write in flight — the next
    write would inherit its clock and trip the watchdog."""
    from mempalace import mcp_server

    with pytest.raises(RuntimeError):
        with mcp_server._write_stall_watch("mempalace_checkpoint"):
            raise RuntimeError("boom")

    assert mcp_server._write_stall_inflight is None


def test_stall_watch_ignores_tools_that_never_reach_chromadb():
    from mempalace import mcp_server

    with mcp_server._write_stall_watch("mempalace_search"):
        assert mcp_server._write_stall_inflight is None


def test_watchdog_thread_not_started_when_both_thresholds_are_zero(monkeypatch):
    from mempalace import mcp_server

    monkeypatch.setenv(mcp_server._WRITE_STALL_WARN_ENV, "0")
    monkeypatch.setenv(mcp_server._WRITE_STALL_EXIT_ENV, "0")
    before = [t.name for t in threading.enumerate()]

    mcp_server._start_write_stall_watchdog()

    after = [t.name for t in threading.enumerate()]
    assert after == before
