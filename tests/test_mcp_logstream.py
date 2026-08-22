"""
test_mcp_logstream.py — MCP surface tests for the RFC 003 logstream tools.

Covers handle_request dispatch for the seven logstream tools, read-only
mode (hidden from tools/list AND refused at dispatch), the peer-writer
exemption, the Chroma-integrity-gate exemption, and a cross-thread
append/wait round trip through the dispatch layer.
"""

import json
import threading

import pytest

from mempalace import mcp_server


LOGSTREAM_TOOLS = frozenset(
    {
        "mempalace_event_append",
        "mempalace_event_list",
        "mempalace_event_wait",
        "mempalace_event_ack",
        "mempalace_artifact_put",
        "mempalace_artifact_get",
        "mempalace_patch_submit",
    }
)
LOGSTREAM_MUTATING = frozenset(
    {
        "mempalace_event_append",
        "mempalace_event_ack",
        "mempalace_artifact_put",
        "mempalace_patch_submit",
    }
)
LOGSTREAM_READ_ONLY_OK = LOGSTREAM_TOOLS - LOGSTREAM_MUTATING


@pytest.fixture
def patched_server(monkeypatch, config, palace_path):
    """Point the MCP server at a temp palace with a fresh logstream cache."""
    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_logstream_by_path", {})
    yield mcp_server
    for ls in mcp_server._logstream_by_path.values():
        ls.close()


def _call(server, name, arguments, req_id=1):
    return server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )


def _result(response):
    """Unwrap a tools/call response into the handler's dict result."""
    assert "error" not in response, response
    return json.loads(response["result"]["content"][0]["text"])


APPEND_ARGS = {
    "type": "task.request",
    "stream": "project/mempalace",
    "room": "delegation",
    "from_agent": "mac-codex",
    "to_agent": "windows-codex",
    "correlation_id": "task_mcp",
    "body": "Please fix search echo ranking.",
}


# ── Registration invariants ───────────────────────────────────────────────


class TestRegistration:
    def test_all_logstream_tools_registered(self):
        assert LOGSTREAM_TOOLS <= set(mcp_server.TOOLS)

    def test_mutating_logstream_tools_flagged(self):
        assert LOGSTREAM_MUTATING <= mcp_server._MUTATING_TOOLS
        assert not (LOGSTREAM_READ_ONLY_OK & mcp_server._MUTATING_TOOLS)

    def test_logstream_tools_exempt_from_chroma_integrity_gate(self):
        assert LOGSTREAM_TOOLS <= mcp_server._SQLITE_INTEGRITY_ALLOWED_TOOLS

    def test_mutating_logstream_tools_exempt_from_peer_writer_gate(self):
        assert LOGSTREAM_MUTATING == mcp_server._PEER_WRITER_EXEMPT_TOOLS


# ── Dispatch round trips ──────────────────────────────────────────────────


class TestDispatch:
    def test_append_then_list(self, patched_server):
        appended = _result(_call(patched_server, "mempalace_event_append", APPEND_ARGS))
        assert appended["success"] is True
        event = appended["event"]
        assert event["id"].startswith("evt_")

        listed = _result(
            _call(
                patched_server,
                "mempalace_event_list",
                {"stream": "project/mempalace", "correlation_id": "task_mcp"},
            )
        )
        assert listed["count"] == 1
        assert listed["events"][0]["body"] == APPEND_ARGS["body"]

    def test_preview_truncates_long_bodies(self, patched_server):
        long_body = "x" * 5000
        _result(
            _call(
                patched_server,
                "mempalace_event_append",
                {**APPEND_ARGS, "body": long_body},
            )
        )
        # Full (default): the whole verbatim body comes back.
        full = _result(
            _call(patched_server, "mempalace_event_list", {"correlation_id": "task_mcp"})
        )
        assert full["events"][0]["body"] == long_body
        assert "body_truncated" not in full["events"][0]
        # Preview: body trimmed to the excerpt, with the length marker.
        prev = _result(
            _call(
                patched_server,
                "mempalace_event_list",
                {"correlation_id": "task_mcp", "preview": True},
            )
        )
        ev = prev["events"][0]
        assert ev["body"] == long_body[: mcp_server._PREVIEW_BODY_CHARS]
        assert ev["body_truncated"] is True
        assert ev["body_length"] == 5000
        # Routing fields survive the preview so the stream stays scannable.
        assert ev["correlation_id"] == "task_mcp" and ev["from_agent"] == "mac-codex"

    def test_preview_leaves_short_bodies_intact(self, patched_server):
        _result(_call(patched_server, "mempalace_event_append", APPEND_ARGS))
        prev = _result(
            _call(
                patched_server,
                "mempalace_event_list",
                {"correlation_id": "task_mcp", "preview": True},
            )
        )
        assert prev["events"][0]["body"] == APPEND_ARGS["body"]
        assert "body_truncated" not in prev["events"][0]

    def test_wait_returns_existing_event(self, patched_server):
        _result(_call(patched_server, "mempalace_event_append", APPEND_ARGS))
        result = _result(
            _call(
                patched_server,
                "mempalace_event_wait",
                {"correlation_id": "task_mcp", "timeout_ms": 5000},
            )
        )
        assert result["timed_out"] is False
        assert result["count"] == 1

    def test_wait_times_out_cleanly(self, patched_server):
        result = _result(
            _call(
                patched_server,
                "mempalace_event_wait",
                {"correlation_id": "task_never", "timeout_ms": 100},
            )
        )
        assert result["timed_out"] is True
        assert result["events"] == []

    def test_wait_accepts_limit_like_list(self, patched_server):
        """wait and list accept the same filter set — windows-codex hit a
        -32602 'Unknown parameter limit' calling wait with list's filters."""
        for i in range(3):
            _result(
                _call(
                    patched_server,
                    "mempalace_event_append",
                    dict(APPEND_ARGS, body=f"event {i}"),
                )
            )
        result = _result(
            _call(
                patched_server,
                "mempalace_event_wait",
                {"correlation_id": "task_mcp", "timeout_ms": 5000, "limit": 2},
            )
        )
        assert result["timed_out"] is False
        assert result["count"] == 2

    def test_artifact_put_get_round_trip(self, patched_server):
        patch = "diff --git a/x b/x\n+1\n"
        put = _result(
            _call(
                patched_server,
                "mempalace_artifact_put",
                {"kind": "patch", "content": patch, "created_by": "windows-codex"},
            )
        )
        assert put["success"] is True
        got = _result(
            _call(
                patched_server,
                "mempalace_artifact_get",
                {"artifact_id": put["artifact"]["id"]},
            )
        )
        assert got["artifact"]["content"] == patch
        assert got["artifact"]["sha256"] == put["artifact"]["sha256"]

    def test_artifact_get_missing_returns_error_payload(self, patched_server):
        got = _result(_call(patched_server, "mempalace_artifact_get", {"artifact_id": "art_nope"}))
        assert "not found" in got["error"]

    def test_patch_submit_then_ack(self, patched_server):
        submitted = _result(
            _call(
                patched_server,
                "mempalace_patch_submit",
                {
                    "content": "diff --git a/y b/y\n+2\n",
                    "from_agent": "windows-codex",
                    "stream": "project/mempalace",
                    "to_agent": "mac-codex",
                    "correlation_id": "task_mcp",
                },
            )
        )
        assert submitted["success"] is True
        event = submitted["event"]
        assert event["type"] == "patch.ready"
        assert event["artifact_ids"] == [submitted["artifact"]["id"]]

        acked = _result(
            _call(
                patched_server,
                "mempalace_event_ack",
                {"event_id": event["id"], "from_agent": "mac-codex", "status": "applied"},
            )
        )
        assert acked["success"] is True
        assert acked["event"]["type"] == "event.ack"
        assert acked["event"]["to_agent"] == "windows-codex"
        assert acked["event"]["correlation_id"] == "task_mcp"

    def test_validation_error_surfaces_in_result(self, patched_server):
        bad = dict(APPEND_ARGS, status="bogus")
        result = _result(_call(patched_server, "mempalace_event_append", bad))
        assert result["success"] is False
        assert "status" in result["error"]

    def test_append_from_one_request_visible_to_waiting_request(self, patched_server):
        """Two dispatch threads sharing the server: waiter sees the append."""
        results = {}

        def waiter():
            results["wait"] = _result(
                _call(
                    patched_server,
                    "mempalace_event_wait",
                    {
                        "correlation_id": "task_cross",
                        "type": "patch.ready",
                        "timeout_ms": 10000,
                    },
                    req_id=2,
                )
            )

        t = threading.Thread(target=waiter)
        t.start()
        _result(
            _call(
                patched_server,
                "mempalace_event_append",
                dict(APPEND_ARGS, type="patch.ready", correlation_id="task_cross"),
            )
        )
        t.join(timeout=15)
        assert not t.is_alive()
        assert results["wait"]["timed_out"] is False
        assert results["wait"]["events"][0]["correlation_id"] == "task_cross"


# ── Read-only mode (#1877 semantics) ──────────────────────────────────────


class TestReadOnlyMode:
    def test_mutating_logstream_tools_refused(self, patched_server, monkeypatch):
        monkeypatch.setattr(mcp_server, "_READ_ONLY", True)
        for name in sorted(LOGSTREAM_MUTATING):
            response = _call(patched_server, name, {})
            assert response["error"]["code"] == -32003, name

    def test_read_logstream_tools_still_served(self, patched_server, monkeypatch):
        _result(_call(patched_server, "mempalace_event_append", APPEND_ARGS))
        monkeypatch.setattr(mcp_server, "_READ_ONLY", True)
        listed = _result(
            _call(patched_server, "mempalace_event_list", {"correlation_id": "task_mcp"})
        )
        assert listed["count"] == 1

    def test_mutating_logstream_tools_hidden_from_tools_list(self, patched_server, monkeypatch):
        monkeypatch.setattr(mcp_server, "_READ_ONLY", True)
        response = patched_server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        advertised = {t["name"] for t in response["result"]["tools"]}
        assert not (LOGSTREAM_MUTATING & advertised)
        assert LOGSTREAM_READ_ONLY_OK <= advertised


# ── Gate exemptions ───────────────────────────────────────────────────────


class TestGateExemptions:
    def test_peer_writer_lock_does_not_block_event_append(self, patched_server, monkeypatch):
        monkeypatch.setattr(
            mcp_server, "_acquire_mcp_writer_lock", lambda: (False, "peer writer active")
        )
        # Chroma-backed mutating tool is refused...
        assert mcp_server._mcp_peer_writer_refusal(1, "mempalace_add_drawer") is not None
        # ...but logstream mutating tools pass the gate and dispatch fine.
        for name in sorted(LOGSTREAM_MUTATING):
            assert mcp_server._mcp_peer_writer_refusal(1, name) is None, name
        appended = _result(_call(patched_server, "mempalace_event_append", APPEND_ARGS))
        assert appended["success"] is True

    def test_chroma_integrity_failure_does_not_block_logstream(self, patched_server, monkeypatch):
        monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", True)
        monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", ["chroma.sqlite3: malformed"])
        for name in sorted(LOGSTREAM_TOOLS):
            assert mcp_server._mcp_sqlite_integrity_refusal(1, name) is None, name
        appended = _result(_call(patched_server, "mempalace_event_append", APPEND_ARGS))
        assert appended["success"] is True
