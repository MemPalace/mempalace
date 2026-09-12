"""
test_query_parser.py — Unit tests for MemPalace Query and Command Parser.
"""

import pytest
from mempalace.query_parser import (
    QueryParseError,
    parse_coordinate_input,
    parse_exec_input,
    parse_query_input,
    tokenize_dsl,
)


class TestTokenizeDsl:
    def test_basic_tokens(self):
        tokens = tokenize_dsl("FIND auth tokens IN backend/auth LIMIT 5")
        assert tokens == ["FIND", "auth", "tokens", "IN", "backend/auth", "LIMIT", "5"]

    def test_quoted_strings_and_escapes(self):
        tokens = tokenize_dsl('FIND "exact phrase \\"with quotes\\"" IN wing_code')
        assert tokens == ["FIND", 'exact phrase "with quotes"', "IN", "wing_code"]

    def test_arrows_and_symbols(self):
        tokens = tokenize_dsl("KG ADD Alice -> loves -> chess")
        assert tokens == ["KG", "ADD", "Alice", "->", "loves", "->", "chess"]

        tokens2 = tokenize_dsl("KG SUPERSEDE Max -> grade: 6 => 7 AT 2026-09-01")
        assert tokens2 == [
            "KG",
            "SUPERSEDE",
            "Max",
            "->",
            "grade",
            ":",
            "6",
            "=>",
            "7",
            "AT",
            "2026-09-01",
        ]


class TestPalaceQueryParser:
    def test_search_shorthand(self):
        target, params = parse_query_input(
            "FIND oauth token refresh IN backend/auth LIMIT 5 SINCE 2026-01-01"
        )
        assert target == "search"
        assert params["query"] == "oauth token refresh"
        assert params["wing"] == "backend"
        assert params["room"] == "auth"
        assert params["limit"] == 5
        assert params["since"] == "2026-01-01"

    def test_search_with_quotes(self):
        target, params = parse_query_input(
            'SEARCH "sqlite concurrency deadlock" IN wing_core MAX_DIST 1.2'
        )
        assert target == "search"
        assert params["query"] == "sqlite concurrency deadlock"
        assert params["wing"] == "wing_core"
        assert params["max_distance"] == 1.2

        target, params = parse_query_input('FIND "status: active"')
        assert target == "search"
        assert params["query"] == "status: active"

    def test_taxonomy_and_wings(self):
        target, params = parse_query_input("TAXONOMY")
        assert target == "taxonomy"
        assert params == {}

        target, params = parse_query_input("TAXONOMY IN backend")
        assert target == "taxonomy"
        assert params == {"wing": "backend"}

        target, params = parse_query_input("WINGS")
        assert target == "wings"

        target, params = parse_query_input("ROOMS IN backend")
        assert target == "rooms"
        assert params == {"wing": "backend"}

    def test_drawer_and_drawers(self):
        target, params = parse_query_input("DRAWER drw_abc123")
        assert target == "drawer"
        assert params == {"drawer_id": "drw_abc123"}

        target, params = parse_query_input("DRAWERS IN backend/auth LIMIT 10 OFFSET 20")
        assert target == "drawers"
        assert params == {"wing": "backend", "room": "auth", "limit": 10, "offset": 20}

    def test_check_dup(self):
        target, params = parse_query_input('CHECK DUP "exact memory content" THRESHOLD 0.85')
        assert target == "check_duplicate"
        assert params == {"content": "exact memory content", "threshold": 0.85}

    def test_kg_query(self):
        target, params = parse_query_input("KG Alice AS OF 2026-04-01 DIRECTION outgoing")
        assert target == "kg_query"
        assert params == {"entity": "Alice", "as_of": "2026-04-01", "direction": "outgoing"}

        target, params = parse_query_input("KG Alice Smith AS OF 2026-04-01 DIRECTION outgoing")
        assert target == "kg_query"
        assert params == {"entity": "Alice Smith", "as_of": "2026-04-01", "direction": "outgoing"}

        target, params = parse_query_input("KG TIMELINE Bob")
        assert target == "kg_timeline"
        assert params == {"entity": "Bob"}

        target, params = parse_query_input("KG TIMELINE Alice Smith")
        assert target == "kg_timeline"
        assert params == {"entity": "Alice Smith"}

        target, params = parse_query_input("KG STATS")
        assert target == "kg_stats"

    def test_traverse_and_tunnels(self):
        target, params = parse_query_input("TRAVERSE auth-flow HOPS 3")
        assert target == "traverse"
        assert params == {"start_room": "auth-flow", "max_hops": 3}

        target, params = parse_query_input("TUNNELS BETWEEN wing_backend AND wing_frontend")
        assert target == "find_tunnels"
        assert params == {"wing_a": "wing_backend", "wing_b": "wing_frontend"}

        target, params = parse_query_input("FOLLOW backend/auth")
        assert target == "follow_tunnels"
        assert params == {"wing": "backend", "room": "auth"}

        target, params = parse_query_input("HALLWAYS IN wing_backend")
        assert target == "list_hallways"
        assert params == {"wing": "wing_backend"}

    def test_diary_read(self):
        target, params = parse_query_input("DIARY antigravity LAST 5 IN wing_proj")
        assert target == "diary_read"
        assert params == {"agent_name": "antigravity", "last_n": 5, "wing": "wing_proj"}

    def test_status_filed_settings_aaak(self):
        assert parse_query_input("STATUS")[0] == "status"
        assert parse_query_input("FILED")[0] == "filed"
        assert parse_query_input("SETTINGS")[0] == "settings"
        assert parse_query_input("AAAK SPEC")[0] == "aaak_spec"

    def test_structured_dict_passthrough(self):
        payload = {"target": "search", "query": "hello", "limit": 3}
        target, params = parse_query_input(payload)
        assert target == "search"
        assert params == {"query": "hello", "limit": 3}


class TestPalaceExecParser:
    def test_add_drawer(self):
        target, params = parse_exec_input(
            'ADD IN backend/auth "use jwt tokens" SOURCE auth.py ADDED_BY agent1'
        )
        assert target == "add_drawer"
        assert params["wing"] == "backend"
        assert params["room"] == "auth"
        assert params["content"] == "use jwt tokens"
        assert params["source_file"] == "auth.py"
        assert params["added_by"] == "agent1"

        target, params = parse_exec_input('ADD IN notes/day "status: active"')
        assert target == "add_drawer"
        assert params["content"] == "status: active"
        assert params["wing"] == "notes"
        assert params["room"] == "day"

    def test_update_and_delete_drawer(self):
        target, params = parse_exec_input(
            'UPDATE drw_123 CONTENT "new content" WING backend ROOM auth'
        )
        assert target == "update_drawer"
        assert params["drawer_id"] == "drw_123"
        assert params["content"] == "new content"

        target, params = parse_exec_input("DELETE DRAWER drw_123")
        assert target == "delete_drawer"
        assert params == {"drawer_id": "drw_123"}

        target, params = parse_exec_input("DELETE SOURCE /path/to/file COMMIT")
        assert target == "delete_by_source"
        assert params == {"source_file": "/path/to/file", "dry_run": False}

        target, params = parse_exec_input("UPDATE drw_1 CONTENT NASA")
        assert target == "update_drawer"
        assert params["drawer_id"] == "drw_1"
        assert params["content"] == "NASA"

        for quoted in ("00123", "true", "null"):
            target, params = parse_exec_input(f'UPDATE drw_1 CONTENT "{quoted}"')
            assert target == "update_drawer"
            assert params["content"] == quoted
            assert isinstance(params["content"], str)

    def test_structured_drawer_id_plus_content_is_update(self):
        action, params = parse_exec_input({"drawer_id": "drw_1", "content": "replacement"})
        assert action == "update_drawer"
        assert params["drawer_id"] == "drw_1"
        assert params["content"] == "replacement"

    def test_mine_and_sync(self):
        target, params = parse_exec_input("MINE /path/to/repo MODE projects WING core LIMIT 100")
        assert target == "mine"
        assert params["source"] == "/path/to/repo"
        assert params["mode"] == "projects"
        assert params["wing"] == "core"
        assert params["limit"] == 100

        target, params = parse_exec_input("SYNC PROJECT /path/to/repo WING core APPLY")
        assert target == "sync"
        assert params["project"] == "/path/to/repo"
        assert params["wing"] == "core"
        assert params["apply"] is True

    def test_checkpoint(self):
        target, params = parse_exec_input(
            'CHECKPOINT {"items": [{"wing": "w", "room": "r", "content": "c"}]}'
        )
        assert target == "checkpoint"
        assert params == {"items": [{"wing": "w", "room": "r", "content": "c"}]}

        target, params = parse_exec_input({"items": [{"wing": "w", "room": "r", "content": "c"}]})
        assert target == "checkpoint"
        assert params == {"items": [{"wing": "w", "room": "r", "content": "c"}]}

    def test_kg_add_invalidate_supersede(self):
        target, params = parse_exec_input("KG ADD Max -> loves -> chess FROM 2026-01-01")
        assert target == "kg_add"
        assert params == {
            "subject": "Max",
            "predicate": "loves",
            "object": "chess",
            "valid_from": "2026-01-01",
        }

        target, params = parse_exec_input("KG INVALIDATE Max -> plays -> soccer ENDED 2026-05-01")
        assert target == "kg_invalidate"
        assert params == {
            "subject": "Max",
            "predicate": "plays",
            "object": "soccer",
            "ended": "2026-05-01",
        }

        target, params = parse_exec_input(
            {
                "action": "kg_invalidate",
                "subject": "Max",
                "predicate": "plays",
                "object": "soccer",
                "valid_to": "2020-01-01",
            }
        )
        assert target == "kg_invalidate"
        assert params["ended"] == "2020-01-01"
        assert "valid_to" not in params

        target, params = parse_exec_input("KG SUPERSEDE Max -> grade: 6 => 7 AT 2026-09-01")
        assert target == "kg_supersede"
        assert params == {
            "subject": "Max",
            "predicate": "grade",
            "old_object": "6",
            "new_object": "7",
            "at": "2026-09-01",
        }

        target, params = parse_exec_input("KG ADD Alice -> website -> https://example.com")
        assert target == "kg_add"
        assert params["object"] == "https://example.com"

        target, params = parse_exec_input('KG ADD Alice -> note -> "status: active"')
        assert target == "kg_add"
        assert params["object"] == "status: active"

        target, params = parse_exec_input('KG ADD Alice -> note -> "FROM"')
        assert target == "kg_add"
        assert params["object"] == "FROM"
        assert "valid_from" not in params

        target, params = parse_exec_input(
            "KG SUPERSEDE Max -> website: old => https://new.example AT 2026-01-01"
        )
        assert target == "kg_supersede"
        assert params["old_object"] == "old"
        assert params["new_object"] == "https://new.example"
        assert params["at"] == "2026-01-01"

        target, params = parse_exec_input(
            'KG SUPERSEDE Max -> note: stale => "status: active" AT 2026-01-01'
        )
        assert target == "kg_supersede"
        assert params["new_object"] == "status: active"

    def test_tunnel_create_and_delete(self):
        target, params = parse_exec_input(
            'TUNNEL CREATE backend/api -> db/schema LABEL "API to DB"'
        )
        assert target == "create_tunnel"
        assert params == {
            "source_wing": "backend",
            "source_room": "api",
            "target_wing": "db",
            "target_room": "schema",
            "label": "API to DB",
        }

        target, params = parse_exec_input("TUNNEL DELETE tun_456")
        assert target == "delete_tunnel"
        assert params == {"tunnel_id": "tun_456"}

        target, params = parse_exec_input("HALLWAY DELETE hlw_789")
        assert target == "delete_hallway"
        assert params == {"hallway_id": "hlw_789"}

    def test_diary_write(self):
        target, params = parse_exec_input(
            'DIARY WRITE antigravity TOPIC auth "SESSION|added auth module"'
        )
        assert target == "diary_write"
        assert params == {
            "agent_name": "antigravity",
            "entry": "SESSION|added auth module",
            "topic": "auth",
        }

        target, params = parse_exec_input('DIARY WRITE bot "topic: exact observation"')
        assert target == "diary_write"
        assert params == {
            "agent_name": "bot",
            "entry": "topic: exact observation",
        }

    def test_reconnect_and_settings(self):
        assert parse_exec_input("RECONNECT")[0] == "reconnect"
        target, params = parse_exec_input("SETTINGS SILENT_SAVE true DESKTOP_TOAST false")
        assert target == "hook_settings"
        assert params == {"silent_save": True, "desktop_toast": False}


class TestPalaceCoordinateParser:
    def test_task_create(self):
        cmd = (
            "TASK CREATE project:mempalace from:agent1 to:agent2 "
            'goal:"Fix memory leak" branch:fix/leak base:a1b2c3d4 done:"All tests pass"'
        )
        target, params = parse_coordinate_input(cmd)
        assert target == "task_create"
        assert params["project"] == "mempalace"
        assert params["from_agent"] == "agent1"
        assert params["to_agent"] == "agent2"
        assert params["goal"] == "Fix memory leak"
        assert params["branch"] == "fix/leak"
        assert params["base_commit"] == "a1b2c3d4"
        assert params["done"] == "All tests pass"

    def test_event_append_list_wait_ack(self):
        cmd = 'EVENT APPEND type:task.request stream:project/mempalace room:delegation from:agent1 to:agent2 body:"hello"'
        target, params = parse_coordinate_input(cmd)
        assert target == "event_append"
        assert params["type"] == "task.request"
        assert params["stream"] == "project/mempalace"
        assert params["room"] == "delegation"
        assert params["from_agent"] == "agent1"
        assert params["to_agent"] == "agent2"
        assert params["body"] == "hello"

        target, params = parse_coordinate_input(
            "EVENT LIST stream:project/mempalace to:agent1 since_id:evt_100 limit:20"
        )
        assert target == "event_list"
        assert params["stream"] == "project/mempalace"
        assert params["to_agent"] == "agent1"
        assert params["since_event_id"] == "evt_100"
        assert params["limit"] == 20

        target, params = parse_coordinate_input(
            "EVENT WAIT stream:project/mempalace correlation:task_1 timeout:5000"
        )
        assert target == "event_wait"
        assert params["stream"] == "project/mempalace"
        assert params["correlation_id"] == "task_1"
        assert params["timeout_ms"] == 5000

        target, params = parse_coordinate_input(
            'EVENT ACK id:evt_123 from:agent1 status:applied body:"Done"'
        )
        assert target == "event_ack"
        assert params["event_id"] == "evt_123"
        assert params["from_agent"] == "agent1"
        assert params["status"] == "applied"
        assert params["body"] == "Done"

    def test_artifact_put_get_patch(self):
        target, params = parse_coordinate_input(
            'ARTIFACT PUT kind:patch created_by:agent1 content:"diff --git a/b"'
        )
        assert target == "artifact_put"
        assert params["kind"] == "patch"
        assert params["created_by"] == "agent1"
        assert params["content"] == "diff --git a/b"

        target, params = parse_coordinate_input("ARTIFACT GET art_999")
        assert target == "artifact_get"
        assert params["artifact_id"] == "art_999"

        target, params = parse_coordinate_input(
            'PATCH SUBMIT stream:project/mempalace from:agent1 diff:"diff content"'
        )
        assert target == "patch_submit"
        assert params["stream"] == "project/mempalace"
        assert params["from_agent"] == "agent1"
        assert params["content"] == "diff content"

    def test_mesh_peers(self):
        assert parse_coordinate_input("MESH PEERS")[0] == "mesh_peers"
        assert parse_coordinate_input("PEERS")[0] == "mesh_peers"

    def test_structured_dict_with_embedded_dsl_keywords(self):
        for kw in [
            "DRAWERS IN backend/auth",
            "FOLLOW backend/auth",
            "FILED",
            "SETTINGS",
            "AAAK SPEC",
        ]:
            target, params = parse_query_input({"query": kw})
            assert target in ("drawers", "follow_tunnels", "filed", "settings", "aaak_spec")

    def test_structured_target_aliases(self):
        assert parse_query_input({"target": "rooms", "wing": "core"}) == ("rooms", {"wing": "core"})
        assert parse_query_input({"target": "drawers", "wing": "core"}) == (
            "drawers",
            {"wing": "core"},
        )
        assert parse_query_input({"target": "graph_stats"}) == ("graph_stats", {})
        assert parse_query_input({"target": "kg_stats"}) == ("kg_stats", {})
        assert parse_query_input({"target": "list_tunnels", "wing": "core"}) == (
            "list_tunnels",
            {"wing": "core"},
        )
        assert parse_query_input({"target": "find_tunnels", "wing_a": "a", "wing_b": "b"}) == (
            "find_tunnels",
            {"wing_a": "a", "wing_b": "b"},
        )
        assert parse_query_input({"target": "list_hallways"}) == ("list_hallways", {})

    def test_structured_search_keeps_limit(self):
        target, params = parse_query_input({"query": "hello", "limit": 5})
        assert target == "search"
        assert params["query"] == "hello"
        assert params["limit"] == 5

    def test_event_list_since_timestamp_is_not_an_event_id(self):
        target, params = parse_coordinate_input("EVENT LIST stream:project/x since:2026-09-01")
        assert target == "event_list"
        assert params["since_created_at"] == "2026-09-01"
        assert "since_event_id" not in params

    def test_event_list_since_id_still_maps(self):
        target, params = parse_coordinate_input("EVENT LIST since_id:evt_100")
        assert params["since_event_id"] == "evt_100"

    def test_event_list_bare_since_rejects_non_id_non_date(self):
        with pytest.raises(QueryParseError, match="since"):
            parse_coordinate_input("EVENT LIST since:yesterday")

    def test_coordinate_dict_merges_command_and_limit(self):
        action, params = parse_coordinate_input(
            {"command": "EVENT LIST stream:project/x", "limit": 10}
        )
        assert action == "event_list"
        assert params["stream"] == "project/x"
        assert params["limit"] == 10

    def test_event_type_alias_becomes_type(self):
        action, params = parse_coordinate_input(
            {
                "event_type": "task.request",
                "stream": "project/x",
                "room": "tasks",
                "from_agent": "a",
            }
        )
        assert action == "event_append"
        assert params["type"] == "task.request"
        assert "event_type" not in params

    def test_structured_patch_diff_maps_to_content(self):
        action, params = parse_coordinate_input(
            {
                "action": "patch_submit",
                "diff": "diff --git a/b",
                "from_agent": "a",
                "stream": "project/x",
            }
        )
        assert action == "patch_submit"
        assert params["content"] == "diff --git a/b"
        assert "diff" not in params
