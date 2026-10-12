"""Regression coverage for MCP writer-state test isolation (#2334)."""

import pytest


class _TrackedWriterLock:
    def __init__(self, events: list[str], label: str):
        self.events = events
        self.label = label

    def __exit__(self, exc_type, exc, traceback):
        self.events.append(self.label)
        return False


@pytest.fixture(scope="module")
def _seeded_mcp_writer_state():
    """Seed state before function fixtures and inspect final teardown afterward."""
    from mempalace import mcp_server

    # Start from a known state even when this test is selected after another
    # MCP test in a long-lived local pytest process.
    if mcp_server._MCP_WRITER_LOCK_CM is not None:
        mcp_server._release_mcp_writer_lock()

    events = []
    original_atexit_registered = mcp_server._MCP_WRITER_ATEXIT_REGISTERED

    # Module-scoped fixtures are established before function-scoped autouse
    # fixtures. This state must therefore be removed by _reset_mcp_cache setup.
    mcp_server._MCP_WRITER_LOCK_CM = _TrackedWriterLock(events, "setup")
    mcp_server._MCP_WRITER_READ_ONLY = True
    mcp_server._MCP_WRITER_LOCK_FAILED = True
    mcp_server._MCP_WRITER_LOCK_ERROR = "stale setup failure"
    mcp_server._MCP_WRITER_ATEXIT_REGISTERED = True

    try:
        yield mcp_server, events

        # This code runs after the function-scoped autouse fixture teardown.
        # The second context installed by the test must have been exited once.
        assert events == ["setup", "teardown"]
        assert mcp_server._MCP_WRITER_LOCK_CM is None
        assert mcp_server._MCP_WRITER_READ_ONLY is False
        assert mcp_server._MCP_WRITER_LOCK_FAILED is False
        assert mcp_server._MCP_WRITER_LOCK_ERROR == ""
        assert mcp_server._MCP_WRITER_ATEXIT_REGISTERED is True
    finally:
        # Leave later modules clean even when an assertion above fails.
        if mcp_server._MCP_WRITER_LOCK_CM is not None:
            mcp_server._release_mcp_writer_lock()
        mcp_server._MCP_WRITER_LOCK_CM = None
        mcp_server._MCP_WRITER_READ_ONLY = False
        mcp_server._MCP_WRITER_LOCK_FAILED = False
        mcp_server._MCP_WRITER_LOCK_ERROR = ""
        mcp_server._MCP_WRITER_ATEXIT_REGISTERED = original_atexit_registered


def test_autouse_fixture_resets_writer_state_at_both_boundaries(
    _seeded_mcp_writer_state,
    monkeypatch,
):
    """Autouse setup and teardown both release stale MCP writer state."""
    mcp_server, events = _seeded_mcp_writer_state

    # Fixture setup must have released the module-seeded context and cleared
    # every per-test status field without changing process-lifetime atexit state.
    assert events == ["setup"]
    assert mcp_server._MCP_WRITER_LOCK_CM is None
    assert mcp_server._MCP_WRITER_READ_ONLY is False
    assert mcp_server._MCP_WRITER_LOCK_FAILED is False
    assert mcp_server._MCP_WRITER_LOCK_ERROR == ""
    assert mcp_server._MCP_WRITER_ATEXIT_REGISTERED is True

    # Register clean values with monkeypatch, then install the dirty
    # context by direct assignment. If monkeypatch finalizes before the
    # autouse cleanup, it erases the context reference before ``__exit__`` can
    # run, and the module fixture's final event assertion fails.
    for name, value in (
        ("_MCP_WRITER_LOCK_CM", None),
        ("_MCP_WRITER_READ_ONLY", False),
        ("_MCP_WRITER_LOCK_FAILED", False),
        ("_MCP_WRITER_LOCK_ERROR", ""),
    ):
        monkeypatch.setattr(mcp_server, name, value)

    # Deliberately leave a second dirty state behind. The module fixture's
    # post-yield assertions run only after _reset_mcp_cache teardown and prove
    # that teardown exits this context exactly once and resets all status fields.
    mcp_server._MCP_WRITER_LOCK_CM = _TrackedWriterLock(events, "teardown")
    mcp_server._MCP_WRITER_READ_ONLY = True
    mcp_server._MCP_WRITER_LOCK_FAILED = True
    mcp_server._MCP_WRITER_LOCK_ERROR = "stale teardown failure"
