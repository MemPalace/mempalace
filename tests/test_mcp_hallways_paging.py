"""MCP list_hallways pages strongest-first instead of returning the whole sidecar."""

from unittest.mock import patch

from mempalace import mcp_server


def test_list_hallways_pages_strongest_first():
    rows = [{"id": str(i), "wing": "w", "co_occurrence_count": i} for i in range(7)]
    with patch.object(mcp_server, "list_hallways", lambda wing=None: list(rows)):
        page = mcp_server.tool_list_hallways(limit=3)
        assert [h["id"] for h in page["hallways"]] == ["6", "5", "4"]
        assert page["total"] == 7 and page["count"] == 3 and page["offset"] == 0
        page2 = mcp_server.tool_list_hallways(limit=3, offset=3)
        assert [h["id"] for h in page2["hallways"]] == ["3", "2", "1"]
        assert mcp_server.tool_list_hallways(limit=9999)["limit"] == 500
        assert "error" in mcp_server.tool_list_hallways(limit="x")
