"""MCP list_hallways pages strongest-first instead of returning the whole store."""

import pytest

from mempalace import hallways, mcp_server


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(hallways, "_get_hallway_file", lambda *a, **k: str(tmp_path / "h.json"))
    monkeypatch.setattr(hallways, "_legacy_hallway_file", lambda: str(tmp_path / "legacy.json"))

    def seed(rows):
        hallways._save_hallways(rows)

    return seed


def test_list_hallways_pages_strongest_first(seeded):
    seeded([{"id": str(i), "wing": "w", "co_occurrence_count": i} for i in range(7)])
    page = mcp_server.tool_list_hallways(limit=3)
    assert [h["id"] for h in page["hallways"]] == ["6", "5", "4"]
    assert page["total"] == 7 and page["count"] == 3 and page["offset"] == 0
    page2 = mcp_server.tool_list_hallways(limit=3, offset=3)
    assert [h["id"] for h in page2["hallways"]] == ["3", "2", "1"]


def test_equal_counts_keep_list_order_and_a_wing_pages_alone(seeded):
    rows = [
        {"id": "a", "wing": "w", "co_occurrence_count": 2},
        {"id": "b", "wing": "other", "co_occurrence_count": 9},
        {"id": "c", "wing": "w", "co_occurrence_count": 5},
        {"id": "d", "wing": "w", "co_occurrence_count": 2},
        {"id": "e", "wing": "w"},
    ]
    seeded(rows)
    expected = sorted(
        [h for h in rows if h["wing"] == "w"], key=lambda h: -int(h.get("co_occurrence_count") or 0)
    )
    page = mcp_server.tool_list_hallways(wing="w", limit=10)
    assert page["hallways"] == expected
    assert [h["id"] for h in page["hallways"]] == ["c", "a", "d", "e"]
    assert page["total"] == 4


def _call(arguments):
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_list_hallways", "arguments": arguments},
    }
    return mcp_server.handle_request(request)


def test_list_hallways_rejects_a_page_outside_its_schema():
    for bad in ({"limit": 9999}, {"limit": "x"}, {"offset": -1}):
        response = _call(bad)
        assert response["error"]["code"] == -32602, bad
