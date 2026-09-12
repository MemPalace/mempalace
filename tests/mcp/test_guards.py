"""MCP server tests — peer-writer, read-only, and sqlite integrity gates."""

import json

import pytest

from _chroma_palace_helper import make_minimal_chroma_sqlite


def test_peer_writer_guard_refuses_mutating_tool_before_handler(monkeypatch):
    from mempalace import mcp_server

    called = {"value": False}

    def handler(**kwargs):
        called["value"] = True
        return {"ok": True}

    monkeypatch.setitem(
        mcp_server.TOOLS,
        "mempalace_add_drawer",
        {
            "description": "test write tool",
            "input_schema": {
                "type": "object",
                "properties": {
                    "wing": {"type": "string"},
                    "room": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
            "handler": handler,
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_acquire_mcp_writer_lock",
        lambda: (False, "busy writer"),
    )

    response = mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "mempalace_add_drawer",
                "arguments": {
                    "wing": "wing_test",
                    "room": "room_test",
                    "content": "hello",
                },
            },
        }
    )

    assert called["value"] is False
    assert response["error"]["code"] == -32001
    assert "read-only" in response["error"]["message"]
    assert response["error"]["data"]["tool"] == "mempalace_add_drawer"


def test_peer_writer_guard_does_not_gate_read_tool(monkeypatch):
    from mempalace import mcp_server

    def forbidden_lock():
        raise AssertionError("read tools should not acquire the peer-writer lock")

    monkeypatch.setitem(
        mcp_server.TOOLS,
        "mempalace_status",
        {
            "description": "test read tool",
            "input_schema": {"type": "object", "properties": {}},
            "handler": lambda: {"ok": True},
        },
    )
    monkeypatch.setattr(mcp_server, "_acquire_mcp_writer_lock", forbidden_lock)

    response = mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "mempalace_status", "arguments": {}},
        }
    )

    assert '"ok": true' in response["result"]["content"][0]["text"]


def test_read_only_refuses_exactly_the_refused_set(monkeypatch):
    """Ask the gate which tools it refuses instead of restating the set.

    Comparing against the whole TOOLS registry also catches a stale name: a tool
    renamed or removed while the set still lists it would gate nothing, and the
    two sides would stop matching.
    """
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_READ_ONLY", True)

    refused = {
        name for name in mcp_server.TOOLS if mcp_server._mcp_read_only_refusal(1, name) is not None
    }
    assert refused == set(mcp_server._READ_ONLY_REFUSED_TOOLS)
    assert "mempalace_hook_settings" in refused
    assert "mempalace_memories_filed_away" in refused
    # Reconnect stays reachable on purpose: it is the only way a read-only
    # server picks up an external writer's changes.
    assert "mempalace_reconnect" not in refused

    # The palace-write set the peer-writer lease arbitrates stays the narrower
    # of the two; see test_peer_writer_guard_does_not_gate_hook_settings.
    assert mcp_server._MUTATING_TOOLS < mcp_server._READ_ONLY_REFUSED_TOOLS
    assert "mempalace_hook_settings" not in mcp_server._MUTATING_TOOLS


def test_read_only_refuses_every_daemon_write_tool():
    """Read-only must not be laxer than the daemon's own write classification.

    service.WRITE_TOOLS is a security allowlist: execute_job lets the generic
    mcp_tool escape hatch run write-classified tools only. A tool the daemon
    calls a write while read-only serves it is the exact gap this fixes, and
    mempalace_hook_settings was that tool.
    """
    from mempalace import mcp_server, service

    assert service.WRITE_TOOLS <= mcp_server._READ_ONLY_REFUSED_TOOLS
    assert "mempalace_hook_settings" in service.WRITE_TOOLS


def test_peer_writer_guard_does_not_gate_hook_settings(monkeypatch):
    """The read-only widening must not leak into the peer-writer path.

    mempalace_hook_settings writes the config file and never the palace, so it
    stays out of _MUTATING_TOOLS and the lease has no say over it. Read-only
    refuses it through _READ_ONLY_REFUSED_TOOLS instead. Were it moved into
    _MUTATING_TOOLS, a peer holding the lease would refuse it with -32001,
    including the no-argument form that only reads the current settings.
    """
    from mempalace import mcp_server

    def forbidden_lock():
        raise AssertionError("hook_settings should not acquire the peer-writer lock")

    monkeypatch.setitem(
        mcp_server.TOOLS,
        "mempalace_hook_settings",
        {
            "description": "test config tool",
            "input_schema": {"type": "object", "properties": {}},
            "handler": lambda: {"ok": True},
        },
    )
    monkeypatch.setattr(mcp_server, "_acquire_mcp_writer_lock", forbidden_lock)

    response = mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "mempalace_hook_settings", "arguments": {}},
        }
    )

    assert '"ok": true' in response["result"]["content"][0]["text"]
    assert "mempalace_hook_settings" not in mcp_server._MUTATING_TOOLS


def test_status_tool_does_not_acquire_peer_writer_lock(monkeypatch):
    from mempalace import mcp_server

    def forbidden_lock():
        raise AssertionError("status should not acquire the peer-writer lock")

    monkeypatch.setattr(mcp_server, "_ensure_sqlite_integrity_status", lambda: None)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", None)
    monkeypatch.setattr(mcp_server, "_backend_db_exists", lambda: True)
    monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(mcp_server, "_vector_disabled", True)
    monkeypatch.setattr(
        mcp_server,
        "_tool_status_via_sqlite",
        lambda: {"total_drawers": 0, "wings": {}, "rooms": {}},
    )
    monkeypatch.setattr(mcp_server, "_acquire_mcp_writer_lock", forbidden_lock)

    assert mcp_server.tool_status()["total_drawers"] == 0


def test_peer_writer_lock_setup_failure_retries_and_recovers(monkeypatch):
    from mempalace import mcp_server, palace

    class _DummyLock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    calls = {"count": 0}

    def flaky_mine_palace_lock(palace_path):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError(f"permission denied for {palace_path}")
        return _DummyLock()

    monkeypatch.delenv(mcp_server._MCP_ALLOW_PEER_WRITER_ENV, raising=False)
    monkeypatch.setattr(palace, "mine_palace_lock", flaky_mine_palace_lock)
    monkeypatch.setattr(mcp_server, "_discard_mcp_storage_handles", lambda: None)

    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", None)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_READ_ONLY", False)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_FAILED", False)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_ERROR", "")

    ok_first, reason_first = mcp_server._acquire_mcp_writer_lock()
    ok_second, reason_second = mcp_server._acquire_mcp_writer_lock()

    assert ok_first is False
    assert "later mutating request will retry ownership" in reason_first
    assert ok_second is True
    assert reason_second == ""
    assert calls["count"] == 2
    assert mcp_server._MCP_WRITER_LOCK_FAILED is False
    assert mcp_server._MCP_WRITER_LOCK_CM is not None
    mcp_server._release_mcp_writer_lock()


def test_peer_writer_override_cannot_bypass_local_backend_lock(monkeypatch):
    from mempalace import mcp_server, palace

    class _DummyLock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    calls = {"count": 0}

    def tracked_lock(palace_path):
        calls["count"] += 1
        return _DummyLock()

    monkeypatch.setenv(mcp_server._MCP_ALLOW_PEER_WRITER_ENV, "1")
    monkeypatch.setattr(palace, "resolve_backend_name", lambda path: "sqlite_exact")
    monkeypatch.setattr(palace, "mine_palace_lock", tracked_lock)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", None)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_READ_ONLY", False)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_FAILED", False)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_ERROR", "")

    ok, reason = mcp_server._acquire_mcp_writer_lock()

    assert ok is True
    assert reason == ""
    assert calls["count"] == 1


def test_peer_writer_override_remains_available_for_remote_backend(monkeypatch):
    from mempalace import mcp_server, palace

    monkeypatch.setenv(mcp_server._MCP_ALLOW_PEER_WRITER_ENV, "1")
    monkeypatch.setattr(palace, "resolve_backend_name", lambda path: "qdrant")
    monkeypatch.setattr(
        palace,
        "mine_palace_lock",
        lambda path: pytest.fail("remote backend should not take the local writer lease"),
    )
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", None)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_READ_ONLY", False)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_FAILED", False)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_ERROR", "")

    assert mcp_server._acquire_mcp_writer_lock() == (True, "")


def test_peer_writer_readonly_self_heals_after_peer_exits(monkeypatch):
    """A server that came up read-only must retry the flock and promote itself
    to writer once the peer holding the lease exits — no restart required."""
    from mempalace import mcp_server, palace

    class _DummyLock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    calls = {"count": 0}

    def flaky_mine_palace_lock(palace_path):
        calls["count"] += 1
        if calls["count"] == 1:
            # First attempt: a live peer still holds the lease.
            raise palace.MineAlreadyRunning(f"palace {palace_path} is held by pid=999")
        # Second attempt: peer has exited, flock is free.
        return _DummyLock()

    monkeypatch.delenv(mcp_server._MCP_ALLOW_PEER_WRITER_ENV, raising=False)
    monkeypatch.setattr(palace, "mine_palace_lock", flaky_mine_palace_lock)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", None)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_READ_ONLY", False)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_FAILED", False)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_ERROR", "")

    # First call: refused, latched read-only for reporting.
    ok_first, reason_first = mcp_server._acquire_mcp_writer_lock()
    assert ok_first is False
    assert mcp_server._MCP_WRITER_READ_ONLY is True
    assert "already holds" in reason_first

    # Second call: the sticky latch must NOT short-circuit — retry succeeds.
    ok_second, reason_second = mcp_server._acquire_mcp_writer_lock()
    assert ok_second is True
    assert reason_second == ""
    assert calls["count"] == 2  # retried, not stranded read-only
    assert mcp_server._MCP_WRITER_LOCK_CM is not None
    assert mcp_server._MCP_WRITER_READ_ONLY is False


def test_sqlite_integrity_gate_refuses_non_status_tool(monkeypatch):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", True)
    monkeypatch.setattr(
        mcp_server,
        "_sqlite_integrity_errors",
        ["malformed inverted index for FTS5 table main.embedding_fulltext_search"],
    )
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")

    response = mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1818,
            "method": "tools/call",
            "params": {"name": "mempalace_list_wings", "arguments": {}},
        }
    )

    assert response["error"]["code"] == mcp_server._SQLITE_INTEGRITY_ERROR_CODE
    assert "integrity check failed" in response["error"]["message"]
    assert response["error"]["data"]["tool"] == "mempalace_list_wings"
    assert "malformed inverted index" in response["error"]["data"]["errors"][0]


def test_sqlite_integrity_status_surfaces_payload_without_chroma(monkeypatch):
    import json

    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", True)
    monkeypatch.setattr(
        mcp_server,
        "_sqlite_integrity_errors",
        ["malformed inverted index for FTS5 table main.embedding_fulltext_search"],
    )
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")
    monkeypatch.setattr(
        mcp_server,
        "_tool_status_via_sqlite",
        lambda: {"total_drawers": 123, "backend": "chroma"},
    )

    response = mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1819,
            "method": "tools/call",
            "params": {"name": "mempalace_status", "arguments": {}},
        }
    )

    payload = json.loads(response["result"]["content"][0]["text"])

    assert payload["total_drawers"] == 123
    assert payload["sqlite_integrity_failed"] is True
    assert payload["sqlite_integrity"]["ok"] is False
    assert payload["sqlite_integrity"]["error_count"] == 1
    assert "malformed inverted index" in payload["sqlite_integrity"]["errors"][0]


def test_sqlite_integrity_payload_not_applicable_on_non_chroma_backend(monkeypatch):
    """#1931: a non-chroma backend runs no sqlite quick_check, so status must
    report the check as not-applicable rather than implying it passed.

    Before the fix the payload reported ``checked=True``/``ok=True`` and a
    ``chroma.sqlite3`` path that does not exist for the active backend.
    """
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_selected_backend_name", lambda: "qdrant")
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", True)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")

    payload = mcp_server._sqlite_integrity_payload()

    assert payload["checked"] is False
    assert payload["ok"] is None
    assert "qdrant" in payload["reason"]
    # No chroma.sqlite3 reference and a shape stable with the chroma payload.
    assert payload["sqlite_path"] == ""
    assert payload["error_count"] == 0
    assert payload["errors"] == []


def test_sqlite_integrity_payload_reports_no_verdict_when_the_database_is_absent(
    monkeypatch, tmp_path
):
    """A chroma palace with no database file gets the same not-applicable shape.

    #1931 introduced that shape for a backend the check does not apply to, and
    the gate reaches it by backend name. A chroma palace whose chroma.sqlite3
    was never created therefore still answered ``checked``/``ok`` true, stating
    a quick_check that never ran.
    """
    from mempalace import mcp_server

    monkeypatch.setattr(
        type(mcp_server._config), "palace_path", property(lambda self: str(tmp_path))
    )
    monkeypatch.setattr(mcp_server, "_selected_backend_name", lambda: "chroma")
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", True)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")
    monkeypatch.setattr(
        mcp_server,
        "_sqlite_integrity_no_verdict_reason",
        f"no quick_check ran: {tmp_path / 'chroma.sqlite3'} does not exist",
    )

    payload = mcp_server._sqlite_integrity_payload()

    assert payload["checked"] is False
    assert payload["ok"] is None
    assert "does not exist" in payload["reason"]
    assert payload["error_count"] == 0
    assert payload["errors"] == []
    # The path is still named: it is the file the operator is missing.
    assert payload["sqlite_path"].endswith("chroma.sqlite3")


def test_refresh_sqlite_integrity_status_records_absence_not_a_clean_verdict(monkeypatch, tmp_path):
    """The palace directory exists and holds no database: no verdict, no errors."""
    from mempalace import mcp_server

    monkeypatch.setattr(
        type(mcp_server._config), "palace_path", property(lambda self: str(tmp_path))
    )
    monkeypatch.setattr(mcp_server, "_is_chroma_backend", lambda: True)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", False)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_no_verdict_reason", "")

    mcp_server._refresh_sqlite_integrity_status()

    assert mcp_server._sqlite_integrity_errors == []
    assert "chroma.sqlite3" in mcp_server._sqlite_integrity_no_verdict_reason
    # The probe attempt is still recorded, so the lazy consumers do not re-run it.
    assert mcp_server._sqlite_integrity_checked is True


def test_refresh_sqlite_integrity_status_clears_absence_once_a_database_exists(
    monkeypatch, tmp_path
):
    """A stale "no database" must not outlive a probe that found one.

    Scoped to the path where the probe ran: the size-limit exit records no
    reason of its own, and clears the previous one rather than inheriting it.

    The probe is the real one, against a real minimal database, so the clearing
    is observed rather than arranged.
    """
    from mempalace import mcp_server

    make_minimal_chroma_sqlite(tmp_path)
    # The payload call below resolves the backend for real, and resolution
    # reads MEMPALACE_BACKEND. A developer machine that sets it would send
    # this test down the not-applicable branch instead.
    monkeypatch.delenv("MEMPALACE_BACKEND", raising=False)
    monkeypatch.delenv("MEMPALACE_BACKEND_EXPLICIT", raising=False)
    monkeypatch.setattr(
        type(mcp_server._config), "palace_path", property(lambda self: str(tmp_path))
    )
    monkeypatch.setattr(mcp_server, "_is_chroma_backend", lambda: True)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", False)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")
    monkeypatch.setattr(
        mcp_server, "_sqlite_integrity_no_verdict_reason", "no quick_check ran: stale"
    )

    mcp_server._refresh_sqlite_integrity_status()

    assert mcp_server._sqlite_integrity_no_verdict_reason == ""
    assert mcp_server._sqlite_integrity_payload()["ok"] is True


@pytest.mark.parametrize(
    "palace_path, is_chroma",
    [("", True), (None, True), ("/nonexistent/palace", False)],
    ids=["palace-path-empty", "palace-path-none", "non-chroma-backend"],
)
def test_refresh_sqlite_integrity_status_clears_absence_on_the_exits_that_never_probe(
    monkeypatch, palace_path, is_chroma
):
    """The exit taken before the probe owns no reason, so it may not keep one.

    All three parameters reach the same ``if not _config.palace_path or not
    _is_chroma_backend()`` return, which fires before any quick_check. What is
    asserted here is the gate's own state after that return, not the payload:
    a server that saw a palace with no database and was then pointed elsewhere
    must not still be holding that palace's reason. The empty-palace-path route
    is why it matters, since ``_sqlite_integrity_payload`` would publish a kept
    reason there while naming no palace at all.
    """
    from mempalace import mcp_server

    monkeypatch.setattr(type(mcp_server._config), "palace_path", property(lambda self: palace_path))
    monkeypatch.setattr(mcp_server, "_is_chroma_backend", lambda: is_chroma)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", False)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", ["stale"])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "stale probe error")
    monkeypatch.setattr(
        mcp_server,
        "_sqlite_integrity_no_verdict_reason",
        "no quick_check ran: /gone/chroma.sqlite3 does not exist",
    )

    mcp_server._refresh_sqlite_integrity_status()

    assert mcp_server._sqlite_integrity_no_verdict_reason == ""
    assert mcp_server._sqlite_integrity_errors == []
    assert mcp_server._sqlite_integrity_check_error == ""
    assert mcp_server._sqlite_integrity_checked is True


def test_absence_reason_is_written_first_entering_and_last_leaving():
    """Order the two globals so entering "no verdict" has no clean-looking gap.

    ``_sqlite_integrity_payload`` reads both globals without the refresh lock.
    Recording the reason before the errors closes the window on the way in: a
    reader cannot catch an empty list that nothing explains. The way out keeps
    the reason until the errors are in place, which is the best available
    there, not a guarantee; no write order makes both directions safe, because
    the writer sets the pair in opposite orders in the two branches. Both
    pairs are adjacent assignments on one thread, which no behavioural test
    can observe, so assert the order structurally.
    """
    import ast
    import inspect

    from mempalace import mcp_server

    tree = ast.parse(inspect.getsource(mcp_server._refresh_sqlite_integrity_status_locked))

    def _assigned_name(node):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                return target.id
        return None

    branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Attribute)
        and node.test.attr == "checked"
        and node.orelse
    ]
    assert len(branches) == 1, "expected exactly one `if status.checked:` branch"

    checked, absent = branches[0].body, branches[0].orelse
    assert _assigned_name(absent[0]) == "_sqlite_integrity_no_verdict_reason"
    assert _assigned_name(absent[1]) == "_sqlite_integrity_errors"
    assert _assigned_name(checked[0]) == "_sqlite_integrity_errors"
    assert _assigned_name(checked[-1]) == "_sqlite_integrity_no_verdict_reason"

    # The probe-failed exit is the third writer this test covers. Its error
    # list is non-empty by construction, so no reader can catch an unexplained
    # empty one there; the order is asserted anyway, so the branch cannot
    # drift into a shape where that stops being true.
    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and any(_assigned_name(stmt) == "_sqlite_integrity_errors" for stmt in node.body)
    ]
    assert len(handlers) == 1, "expected exactly one except branch recording the errors"
    names = [_assigned_name(stmt) for stmt in handlers[0].body]
    assert "_sqlite_integrity_errors" in names
    assert "_sqlite_integrity_no_verdict_reason" in names
    assert names.index("_sqlite_integrity_errors") < names.index(
        "_sqlite_integrity_no_verdict_reason"
    )


def test_sqlite_integrity_payload_reports_unknown_when_backend_unresolvable(monkeypatch):
    """#1931: if backend resolution raises, status still must not claim an
    integrity pass; it reports not-applicable for an unknown backend.
    """
    from mempalace import mcp_server

    def _boom():
        raise RuntimeError("backend registry unavailable")

    monkeypatch.setattr(mcp_server, "_selected_backend_name", _boom)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", True)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")

    payload = mcp_server._sqlite_integrity_payload()

    assert payload["checked"] is False
    assert payload["ok"] is None
    assert "unknown" in payload["reason"]


def test_sqlite_integrity_payload_full_shape_on_chroma_backend(monkeypatch):
    """#1931 guard: a chroma backend with no recorded errors must still return
    the full integrity payload; the not-applicable branch must not swallow the
    chroma path.
    """
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_selected_backend_name", lambda: "chroma")
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", True)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")
    # A verdict exists, so no absence is recorded. Stated because this is the
    # one payload test that reaches the reason branch at all: the other two
    # return earlier on a non-chroma backend. Nothing in the module is known
    # to leave the global set, but the gate writes it directly rather than
    # through a fixture, so the input is pinned rather than assumed.
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_no_verdict_reason", "")

    payload = mcp_server._sqlite_integrity_payload()

    assert payload["checked"] is True
    assert payload["ok"] is True
    assert "sqlite_path" in payload
    assert payload["error_count"] == 0
    assert "reason" not in payload


def test_sqlite_integrity_reconnect_allowed_when_corrupt(monkeypatch):
    from mempalace import mcp_server

    called = {"value": False}

    def fake_reconnect():
        called["value"] = True
        return {"success": True}

    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", True)
    monkeypatch.setattr(
        mcp_server,
        "_sqlite_integrity_errors",
        ["malformed inverted index for FTS5 table main.embedding_fulltext_search"],
    )
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")
    monkeypatch.setitem(
        mcp_server.TOOLS,
        "mempalace_reconnect",
        {
            "description": "test reconnect",
            "input_schema": {"type": "object", "properties": {}},
            "handler": fake_reconnect,
        },
    )

    response = mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1820,
            "method": "tools/call",
            "params": {"name": "mempalace_reconnect", "arguments": {}},
        }
    )

    assert called["value"] is True
    assert '"success": true' in response["result"]["content"][0]["text"]


def test_refresh_sqlite_integrity_status_records_quick_check_errors(monkeypatch, tmp_path):
    from mempalace import mcp_server, repair

    # The database path resolves: the probe reports a verdict wherever the file
    # is not provably absent, and this test is about the verdict's contents.
    make_minimal_chroma_sqlite(tmp_path)
    monkeypatch.setattr(
        type(mcp_server._config), "palace_path", property(lambda self: str(tmp_path))
    )
    monkeypatch.setattr(mcp_server, "_is_chroma_backend", lambda: True)
    # sqlite_integrity_status is the seam the gate reads; sqlite_integrity_errors
    # is no longer on that path, so patching it here would inject nothing.
    monkeypatch.setattr(
        repair,
        "sqlite_integrity_status",
        lambda palace_path: repair.SqliteIntegrityStatus(
            checked=True,
            errors=("malformed inverted index for FTS5 table main.embedding_fulltext_search",),
            reason="",
        ),
    )
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", False)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")

    mcp_server._refresh_sqlite_integrity_status()

    assert mcp_server._sqlite_integrity_checked is True
    assert len(mcp_server._sqlite_integrity_errors) == 1
    assert "malformed inverted index" in mcp_server._sqlite_integrity_errors[0]


def test_refresh_sqlite_integrity_status_skips_oversized_db(monkeypatch, tmp_path):
    """Oversized chroma.sqlite3 must NOT run the O(size) startup quick_check."""
    from mempalace import mcp_server, repair

    (tmp_path / "chroma.sqlite3").write_bytes(b"\0" * (2 * 1024 * 1024))  # 2 MB
    monkeypatch.setattr(mcp_server, "_is_chroma_backend", lambda: True)
    monkeypatch.setattr(
        type(mcp_server._config), "palace_path", property(lambda self: str(tmp_path))
    )
    monkeypatch.setenv("MEMPALACE_STARTUP_INTEGRITY_MAX_MB", "1")  # limit 1 MB < 2 MB

    called = {"n": 0}

    def _boom(palace_path):
        called["n"] += 1
        raise AssertionError("quick_check must not run for oversized DB")

    monkeypatch.setattr(repair, "sqlite_integrity_status", _boom)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", False)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", ["stale"])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")
    # Every piece of stale state, including a previous probe's reason for
    # having no verdict: a palace that had no database at startup and an
    # oversized one now must not still be described as having none.
    monkeypatch.setattr(
        mcp_server, "_sqlite_integrity_no_verdict_reason", "no quick_check ran: stale"
    )

    mcp_server._refresh_sqlite_integrity_status()

    assert called["n"] == 0
    assert mcp_server._sqlite_integrity_checked is True
    assert mcp_server._sqlite_integrity_errors == []
    assert mcp_server._sqlite_integrity_no_verdict_reason == ""
    assert "does not exist" not in json.dumps(mcp_server._sqlite_integrity_payload())


def test_refresh_sqlite_integrity_status_runs_when_under_limit(monkeypatch, tmp_path):
    """A DB under the limit still runs the quick_check (behaviour preserved)."""
    from mempalace import mcp_server, repair

    (tmp_path / "chroma.sqlite3").write_bytes(b"\0" * (512 * 1024))  # 0.5 MB
    monkeypatch.setattr(mcp_server, "_is_chroma_backend", lambda: True)
    monkeypatch.setattr(
        type(mcp_server._config), "palace_path", property(lambda self: str(tmp_path))
    )
    monkeypatch.setenv("MEMPALACE_STARTUP_INTEGRITY_MAX_MB", "1")  # limit 1 MB > 0.5 MB

    called = {"n": 0}

    def _spy(palace_path):
        called["n"] += 1
        return repair.SqliteIntegrityStatus(checked=True, errors=(), reason="")

    monkeypatch.setattr(repair, "sqlite_integrity_status", _spy)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", False)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")

    mcp_server._refresh_sqlite_integrity_status()

    assert called["n"] == 1
    assert mcp_server._sqlite_integrity_checked is True


def test_startup_integrity_size_gate_disabled_with_zero(monkeypatch, tmp_path):
    """MEMPALACE_STARTUP_INTEGRITY_MAX_MB=0 disables the gate: check always runs."""
    from mempalace import mcp_server, repair

    (tmp_path / "chroma.sqlite3").write_bytes(b"\0" * (4 * 1024 * 1024))  # 4 MB
    monkeypatch.setattr(mcp_server, "_is_chroma_backend", lambda: True)
    monkeypatch.setattr(
        type(mcp_server._config), "palace_path", property(lambda self: str(tmp_path))
    )
    monkeypatch.setenv("MEMPALACE_STARTUP_INTEGRITY_MAX_MB", "0")

    called = {"n": 0}

    def _spy(palace_path):
        called["n"] += 1
        return repair.SqliteIntegrityStatus(checked=True, errors=(), reason="")

    monkeypatch.setattr(repair, "sqlite_integrity_status", _spy)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", False)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")

    mcp_server._refresh_sqlite_integrity_status()

    assert called["n"] == 1


def test_sqlite_integrity_refusal_handles_none_palace_path(monkeypatch):
    """
    Regression test for Gemini review feedback on PR #1823 (lines 433-455).

    _mcp_sqlite_integrity_refusal() must not raise TypeError when
    _config.palace_path is None — os.path.join(None, "chroma.sqlite3")
    would otherwise crash the server on every mutating tool call while
    the palace is unconfigured and integrity errors are present.
    """
    from mempalace import mcp_server

    # palace_path is a read-only @property on MempalaceConfig (no setter),
    # so monkeypatch.setattr on the instance fails. Patch the class-level
    # property instead -- monkeypatch restores it automatically on teardown.
    monkeypatch.setattr(type(mcp_server._config), "palace_path", property(lambda self: None))
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", True)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", ["malformed inverted index"])
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_check_error", "")

    # Must not raise
    result = mcp_server._mcp_sqlite_integrity_refusal(req_id=1, tool_name="mempalace_kg_add")

    assert result is not None
    assert result["error"]["data"]["palace"] == ""
    assert result["error"]["data"]["sqlite_path"] == ""
    assert result["error"]["data"]["tool"] == "mempalace_kg_add"


def test_startup_preflight_does_not_block_initialize(monkeypatch):
    """The startup integrity probe is O(database size) (PRAGMA quick_check
    reads every page of chroma.sqlite3 — 20s+ on multi-GB palaces) and used
    to run before the protocol loop, starving the client's initialize
    timeout. It now runs on the mcp-startup-preflight thread; the handshake
    must answer immediately while the probe is still in flight."""
    import threading
    import time

    from mempalace import mcp_server

    probe_started = threading.Event()
    release_probe = threading.Event()

    def slow_probe():
        probe_started.set()
        release_probe.wait(10)
        mcp_server._sqlite_integrity_checked = True

    monkeypatch.setattr(mcp_server, "_refresh_sqlite_integrity_status_locked", slow_probe)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", False)
    monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)

    preflight = threading.Thread(target=mcp_server._startup_preflight, daemon=True)
    preflight.start()
    try:
        assert probe_started.wait(5), "preflight thread never started the probe"

        started = time.monotonic()
        response = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        elapsed = time.monotonic() - started

        assert response["result"]["serverInfo"]["name"] == "mempalace"
        assert elapsed < 1.0, f"initialize blocked {elapsed:.2f}s behind the startup probe"
    finally:
        release_probe.set()
        preflight.join(5)


def test_ensure_sqlite_integrity_status_joins_inflight_probe(monkeypatch):
    """A lazy consumer (tool-call integrity gate) arriving while the startup
    preflight probe is still running must wait for that probe's verdict on
    _sqlite_integrity_refresh_lock — not run a second O(database size)
    quick_check concurrently, and not proceed without a verdict."""
    import threading

    from mempalace import mcp_server

    probe_calls = []
    probe_started = threading.Event()
    release_probe = threading.Event()

    def slow_probe():
        probe_calls.append(1)
        probe_started.set()
        release_probe.wait(10)
        mcp_server._sqlite_integrity_checked = True

    monkeypatch.setattr(mcp_server, "_refresh_sqlite_integrity_status_locked", slow_probe)
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_checked", False)

    background = threading.Thread(target=mcp_server._refresh_sqlite_integrity_status, daemon=True)
    background.start()
    assert probe_started.wait(5), "background probe never started"

    consumer_done = threading.Event()

    def consumer():
        mcp_server._ensure_sqlite_integrity_status()
        consumer_done.set()

    consumer_thread = threading.Thread(target=consumer, daemon=True)
    consumer_thread.start()
    try:
        assert not consumer_done.wait(0.3), "consumer bypassed the in-flight probe"
        release_probe.set()
        assert consumer_done.wait(5), "consumer never unblocked after the probe finished"
        assert probe_calls == [1], "quick_check probe ran more than once"
    finally:
        release_probe.set()
        background.join(5)
        consumer_thread.join(5)
