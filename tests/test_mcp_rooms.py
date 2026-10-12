"""MCP surface tests for the RFC 006 room tools: registration, guard sets,
dispatch, transcript filing on close, and the light server's coordinate path."""

import json

import pytest

from _mcp_server_helpers import _get_collection, _patch_mcp_server
from mempalace import mcp_server

ROOM_TOOLS = frozenset(
    {"mempalace_room_open", "mempalace_room_read", "mempalace_room_say", "mempalace_room_close"}
)
# Logstream-only: never touch Chroma.
ROOM_LOGSTREAM_ONLY = frozenset(
    {"mempalace_room_open", "mempalace_room_read", "mempalace_room_say"}
)


@pytest.fixture
def server(monkeypatch, config, kg):
    _patch_mcp_server(monkeypatch, config, kg)
    monkeypatch.setattr(mcp_server, "_logstream_by_path", {})
    yield mcp_server
    for ls in mcp_server._logstream_by_path.values():
        ls.close()


def _call(name, arguments):
    response = mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert "error" not in response, response
    return json.loads(response["result"]["content"][0]["text"])


def _open(agenda="Should BM25 move into the Rust engine?"):
    result = _call(
        "mempalace_room_open",
        {
            "project": "myapp",
            "from_agent": "operator",
            "name": "Search Brainstorm",
            "agenda": agenda,
        },
    )
    assert result["success"] is True, result
    return result


class TestRegistration:
    def test_room_tools_registered(self):
        assert ROOM_TOOLS <= set(mcp_server.TOOLS)

    def test_writes_are_flagged_and_read_is_not(self):
        assert ROOM_TOOLS - {"mempalace_room_read"} <= mcp_server._MUTATING_TOOLS
        assert "mempalace_room_read" not in mcp_server._MUTATING_TOOLS

    def test_logstream_only_tools_bypass_chroma_gates(self):
        assert ROOM_LOGSTREAM_ONLY <= mcp_server._SQLITE_INTEGRITY_ALLOWED_TOOLS
        assert ROOM_LOGSTREAM_ONLY <= mcp_server._HTTP_LOCK_FREE_TOOLS
        assert {"mempalace_room_open", "mempalace_room_say"} <= mcp_server._PEER_WRITER_EXEMPT_TOOLS

    def test_close_files_drawers_so_it_keeps_every_chroma_gate(self):
        close = "mempalace_room_close"
        assert close in mcp_server._VECTOR_WRITE_TOOLS
        assert close not in mcp_server._SQLITE_INTEGRITY_ALLOWED_TOOLS
        assert close not in mcp_server._PEER_WRITER_EXEMPT_TOOLS
        assert close not in mcp_server._HTTP_LOCK_FREE_TOOLS

    def test_room_tools_are_never_advertised_read_only(self):
        for name in ROOM_TOOLS:
            assert not mcp_server.TOOLS[name].get("read_only"), name


class TestDispatch:
    def test_open_read_say_round_trip(self, server):
        opened = _open()
        room_id = opened["room"]["room_id"]
        assert room_id in opened["handoff"]

        first = _call("mempalace_room_read", {"room_id": room_id, "agent": "a"})
        assert first["count"] == 1 and first["more"] is False
        assert first["room"]["closed"] is False

        said = _call(
            "mempalace_room_say", {"room_id": room_id, "from_agent": "b", "body": "an objection"}
        )
        assert said["success"] is True and said["unread"] == 1

        again = _call("mempalace_room_read", {"room_id": room_id, "agent": "a"})
        assert [e["body"] for e in again["events"]] == ["an objection"]

    def test_errors_come_back_as_results(self, server):
        assert "error" in _call("mempalace_room_read", {"room_id": "room_nope_00", "agent": "a"})
        result = _call(
            "mempalace_room_say", {"room_id": "room_nope_00", "from_agent": "a", "body": "x"}
        )
        assert result["success"] is False


class TestCloseFiling:
    def test_close_files_each_turn_verbatim_and_retries_idempotently(self, server, palace_path):
        room_id = _open()["room"]["room_id"]
        _call("mempalace_room_say", {"room_id": room_id, "from_agent": "a", "body": "same"})
        _call("mempalace_room_say", {"room_id": room_id, "from_agent": "b", "body": "same"})

        closed = _call(
            "mempalace_room_close",
            {"room_id": room_id, "from_agent": "operator", "outcome": "Decision: keep it."},
        )
        assert closed["success"] is True, closed
        assert closed["already_closed"] is False
        assert closed["filed"] == 4  # agenda, two identical turns, outcome
        assert closed["wing"] == "myapp" and closed["palace_room"] == "search-brainstorm"

        client, col = _get_collection(palace_path)
        try:
            got = col.get(where={"source_file": room_id}, include=["documents", "metadatas"])
        finally:
            client.close()
        assert len(got["ids"]) == 4
        assert {m["wing"] for m in got["metadatas"]} == {"myapp"}
        assert {m["room"] for m in got["metadatas"]} == {"search-brainstorm"}
        assert sum(doc.endswith("\nsame") for doc in got["documents"]) == 2
        assert any(doc.endswith("\nDecision: keep it.") for doc in got["documents"])
        # A relative source_file records no directory identity, so sync
        # classifies these drawers as having no source rather than a file.
        assert not any("source_dir_ino" in m for m in got["metadatas"])

        # The room id is the one-call handle for recalling the discussion.
        found = _call(
            "mempalace_search", {"query": "Decision keep it", "source_file": room_id, "limit": 10}
        )
        assert found.get("results"), found
        assert all(r["source_path"] == room_id for r in found["results"])

        retry = _call("mempalace_room_close", {"room_id": room_id, "from_agent": "operator"})
        assert retry["success"] is True
        assert retry["already_closed"] is True
        assert retry["filed"] == 0 and retry["already_filed"] == 4

    def test_filing_failure_is_reported_and_room_stays_closed(self, server, monkeypatch):
        room_id = _open()["room"]["room_id"]
        monkeypatch.setattr(
            mcp_server, "tool_add_drawer", lambda **kw: {"success": False, "error": "boom"}
        )
        closed = _call("mempalace_room_close", {"room_id": room_id, "from_agent": "operator"})
        assert closed["success"] is False
        assert closed["errors"] == ["boom"]
        assert "mempalace_room_close again" in closed["error"]
        assert closed["room"]["closed"] is True


class TestLightCoordinate:
    def test_room_dsl_round_trip(self, server):
        from mempalace.mcp_light_server import tool_palace_coordinate

        opened = tool_palace_coordinate(
            {"command": 'ROOM OPEN project:myapp from:operator name:ideas agenda:"What next?"'}
        )
        assert opened["success"] is True, opened
        room_id = opened["room"]["room_id"]

        said = tool_palace_coordinate(f'ROOM SAY {room_id} from:a body:"A proposal."')
        assert said["success"] is True, said

        read = tool_palace_coordinate({"action": "room_read", "id": room_id, "from_agent": "b"})
        assert [e["body"] for e in read["events"]] == ["What next?", "A proposal."]

    def test_room_dsl_requires_fields(self):
        from mempalace.query_parser import QueryParseError, parse_coordinate_input

        with pytest.raises(QueryParseError, match="agent"):
            parse_coordinate_input("ROOM READ room_x_00")
        with pytest.raises(QueryParseError, match="OPEN, READ, SAY or CLOSE"):
            parse_coordinate_input("ROOM DANCE")
