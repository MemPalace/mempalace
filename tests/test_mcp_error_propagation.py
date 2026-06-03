"""
test_mcp_error_propagation.py — Verify backend failures propagate as MCP errors.

Confirms that when a tool handler raises an unhandled exception, handle_request
returns a proper JSON-RPC error response (code -32000), not an empty result.
"""

from unittest.mock import patch
import pytest


def test_backend_exception_returns_mcp_error_not_empty():
    """A backend crash in tool_search must surface as a JSON-RPC error, not []."""
    from mempalace.mcp_server import handle_request

    with patch("mempalace.mcp_server.search_memories", side_effect=RuntimeError("disk full")):
        response = handle_request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mempalace_search",
                "arguments": {"query": "test"},
            },
        })

    assert "error" in response, "Expected MCP error response, got success"
    assert response["error"]["code"] == -32000
    assert "result" not in response


def test_backend_exception_is_not_empty_list():
    """Ensure a backend failure does not silently return an empty result set."""
    from mempalace.mcp_server import handle_request

    with patch("mempalace.mcp_server.search_memories", side_effect=Exception("backend down")):
        response = handle_request({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "mempalace_search",
                "arguments": {"query": "anything"},
            },
        })

    # Must have error key — not a result with empty memories
    assert "error" in response
    result = response.get("result")
    if result:
        content = result.get("content", [])
        for item in content:
            assert "memories" not in str(item.get("text", "")), (
                "Backend failure returned empty memories instead of an error"
            )


def test_tool_status_backend_failure_returns_error():
    """tool_status with a broken ChromaDB must not return empty wings/rooms silently."""
    from mempalace.mcp_server import handle_request

    with patch("mempalace.mcp_server._get_collection", side_effect=RuntimeError("chroma dead")):
        response = handle_request({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "mempalace_status",
                "arguments": {},
            },
        })

    assert "error" in response
    assert response["error"]["code"] == -32000
