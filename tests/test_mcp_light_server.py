"""
test_mcp_light_server.py — Integration tests for Lightweight MemPalace MCP Server.
"""

import json
from mempalace import mcp_light_server, mcp_server
from mempalace.palace_graph import invalidate_graph_cache


def _patch_light_server(monkeypatch, config, kg):
    """Patch mcp_server and mcp_light_server state for fixtures."""
    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda *a, **kw: kg)
    monkeypatch.setattr(mcp_server, "_taxonomy_cache", None)
    monkeypatch.setattr(mcp_server, "_taxonomy_cache_time", 0.0)
    monkeypatch.setattr(mcp_server, "_READ_ONLY", False)
    monkeypatch.setattr(mcp_server, "_vector_disabled", False)
    invalidate_graph_cache()


class TestLightMcpProtocol:
    def test_initialize(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }
        res = mcp_light_server.handle_light_request(req)
        assert res["id"] == 1
        assert res["result"]["serverInfo"]["name"] == "mempalace-light"
        assert "tools" in res["result"]["capabilities"]

    def test_tools_list_default(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        res = mcp_light_server.handle_light_request(req)
        assert res["id"] == 2
        tools = res["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        assert tool_names == ["palace_query", "palace_exec", "palace_coordinate"]

    def test_tools_list_read_only(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp_server, "_READ_ONLY", True)
        req = {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
        res = mcp_light_server.handle_light_request(req)
        tools = res["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        assert tool_names == ["palace_query"]


class TestPalaceQuery:
    def test_status_query(self, monkeypatch, config, collection, kg):
        _patch_light_server(monkeypatch, config, kg)
        req = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "palace_query", "arguments": "STATUS"},
        }
        res = mcp_light_server.handle_light_request(req)
        assert res["id"] == 10
        payload = json.loads(res["result"]["content"][0]["text"])
        assert "total_drawers" in payload or "wings" in payload

    def test_aaak_spec_query(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        req = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "palace_query", "arguments": "AAAK SPEC"},
        }
        res = mcp_light_server.handle_light_request(req)
        payload = json.loads(res["result"]["content"][0]["text"])
        assert "aaak_spec" in payload

    def test_taxonomy_and_wings(self, monkeypatch, config, seeded_collection, kg):
        _patch_light_server(monkeypatch, config, kg)
        req = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "palace_query", "arguments": "TAXONOMY"},
        }
        res = mcp_light_server.handle_light_request(req)
        payload = json.loads(res["result"]["content"][0]["text"])
        assert "taxonomy" in payload

    def test_kg_stats_query(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        req = {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {"name": "palace_query", "arguments": "KG STATS"},
        }
        res = mcp_light_server.handle_light_request(req)
        payload = json.loads(res["result"]["content"][0]["text"])
        assert "entities" in payload or "triples" in payload

    def test_check_dup_query(self, monkeypatch, config, collection, kg):
        _patch_light_server(monkeypatch, config, kg)
        req = {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {
                "name": "palace_query",
                "arguments": 'CHECK DUP "some memory content" THRESHOLD 0.85',
            },
        }
        res = mcp_light_server.handle_light_request(req)
        payload = json.loads(res["result"]["content"][0]["text"])
        assert "is_duplicate" in payload


class TestPalaceExec:
    def test_add_and_get_drawer(self, monkeypatch, config, collection, kg):
        _patch_light_server(monkeypatch, config, kg)
        # 1. Add drawer
        add_req = {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "palace_exec",
                "arguments": 'ADD IN test_wing/test_room "OAuth2 token rotation guide" SOURCE auth.md',
            },
        }
        res = mcp_light_server.handle_light_request(add_req)
        assert res["id"] == 20
        payload = json.loads(res["result"]["content"][0]["text"])
        assert payload.get("success") is True
        drawer_id = payload["drawer_id"]
        assert drawer_id

        # 2. Get drawer via palace_query
        get_req = {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {"name": "palace_query", "arguments": f"DRAWER {drawer_id}"},
        }
        get_res = mcp_light_server.handle_light_request(get_req)
        get_payload = json.loads(get_res["result"]["content"][0]["text"])
        assert get_payload.get("drawer_id") == drawer_id
        assert get_payload.get("wing") == "test_wing"
        assert get_payload.get("room") == "test_room"
        assert "OAuth2 token rotation" in get_payload.get("content", "")

    def test_kg_add_and_query(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        # 1. KG ADD
        add_req = {
            "jsonrpc": "2.0",
            "id": 30,
            "method": "tools/call",
            "params": {
                "name": "palace_exec",
                "arguments": "KG ADD Arthur -> leads -> Camelot FROM 2026-01-01",
            },
        }
        res = mcp_light_server.handle_light_request(add_req)
        payload = json.loads(res["result"]["content"][0]["text"])
        assert payload.get("success") is True

        # 2. KG Query
        query_req = {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {"name": "palace_query", "arguments": "KG Arthur"},
        }
        q_res = mcp_light_server.handle_light_request(query_req)
        q_payload = json.loads(q_res["result"]["content"][0]["text"])
        assert q_payload.get("entity") == "Arthur"
        facts = q_payload.get("facts", [])
        assert any(f.get("predicate") == "leads" and f.get("object") == "Camelot" for f in facts)

    def test_diary_write_and_read(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        # 1. Diary Write
        write_req = {
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {
                "name": "palace_exec",
                "arguments": 'DIARY WRITE antigravity TOPIC proto "SESSION:built lightweight mcp prototype"',
            },
        }
        res = mcp_light_server.handle_light_request(write_req)
        payload = json.loads(res["result"]["content"][0]["text"])
        assert payload.get("success") is True

        # 2. Diary Read
        read_req = {
            "jsonrpc": "2.0",
            "id": 41,
            "method": "tools/call",
            "params": {"name": "palace_query", "arguments": "DIARY antigravity LAST 5"},
        }
        read_res = mcp_light_server.handle_light_request(read_req)
        read_payload = json.loads(read_res["result"]["content"][0]["text"])
        assert read_payload.get("agent") == "antigravity"
        assert len(read_payload.get("entries", [])) >= 1


class TestPalaceCoordinate:
    def test_task_create_and_event_list(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        cmd = (
            "TASK CREATE project:mempalace from:windows:antigravity:mempalace "
            'to:windows:claude:mempalace goal:"Implement PQL query engine" '
            'branch:feat/pql base:e4f5a6b7 done:"All unit tests pass"'
        )
        req = {
            "jsonrpc": "2.0",
            "id": 50,
            "method": "tools/call",
            "params": {"name": "palace_coordinate", "arguments": cmd},
        }
        res = mcp_light_server.handle_light_request(req)
        assert res["id"] == 50
        payload = json.loads(res["result"]["content"][0]["text"])
        assert payload.get("success") is True
        task_event = payload.get("task", {})
        assert task_event.get("type") == "task.request"

        # List events
        list_req = {
            "jsonrpc": "2.0",
            "id": 51,
            "method": "tools/call",
            "params": {
                "name": "palace_coordinate",
                "arguments": "EVENT LIST stream:project/mempalace limit:10",
            },
        }
        list_res = mcp_light_server.handle_light_request(list_req)
        list_payload = json.loads(list_res["result"]["content"][0]["text"])
        events = list_payload.get("events", [])
        assert len(events) >= 1

    def test_artifact_put_and_get(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        put_cmd = 'ARTIFACT PUT kind:note created_by:antigravity content:"Architecture decision record: PQL 3-tool triad"'
        put_req = {
            "jsonrpc": "2.0",
            "id": 60,
            "method": "tools/call",
            "params": {"name": "palace_coordinate", "arguments": put_cmd},
        }
        put_res = mcp_light_server.handle_light_request(put_req)
        put_payload = json.loads(put_res["result"]["content"][0]["text"])
        assert put_payload.get("success") is True
        art_id = put_payload["artifact"]["id"]

        get_req = {
            "jsonrpc": "2.0",
            "id": 61,
            "method": "tools/call",
            "params": {"name": "palace_coordinate", "arguments": f"ARTIFACT GET {art_id}"},
        }
        get_res = mcp_light_server.handle_light_request(get_req)
        get_payload = json.loads(get_res["result"]["content"][0]["text"])
        assert (
            get_payload["artifact"]["content"] == "Architecture decision record: PQL 3-tool triad"
        )


class TestLightMcpPreflightAndGates:
    def test_read_only_blocks_palace_exec_direct_call(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp_server, "_READ_ONLY", True)
        req = {
            "jsonrpc": "2.0",
            "id": 70,
            "method": "tools/call",
            "params": {
                "name": "palace_exec",
                "arguments": 'ADD IN test_wing/test_room "content" SOURCE test.md',
            },
        }
        res = mcp_light_server.handle_light_request(req)
        assert res["id"] == 70
        assert "error" in res
        assert res["error"]["code"] == -32003
        assert "read-only mode" in res["error"]["message"].lower()

    def test_sync_project_dir_translation(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        called_args = {}

        def mock_tool_sync(project_dir=None, dry_run=True, apply=False):
            called_args["project_dir"] = project_dir
            called_args["apply"] = apply
            return {"synced": True}

        monkeypatch.setattr(mcp_server, "tool_sync", mock_tool_sync)
        req = {
            "jsonrpc": "2.0",
            "id": 71,
            "method": "tools/call",
            "params": {
                "name": "palace_exec",
                "arguments": "SYNC PROJECT /custom/repo APPLY",
            },
        }
        res = mcp_light_server.handle_light_request(req)
        assert res["id"] == 71
        assert called_args["project_dir"] == "/custom/repo"
        assert called_args["apply"] is True

    def test_read_only_blocks_filed_on_palace_query(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp_server, "_READ_ONLY", True)
        req = {
            "jsonrpc": "2.0",
            "id": 72,
            "method": "tools/call",
            "params": {"name": "palace_query", "arguments": "FILED"},
        }
        res = mcp_light_server.handle_light_request(req)
        assert res["error"]["code"] == -32003
        assert res["error"]["data"]["tool"] == "mempalace_memories_filed_away"

    def test_unknown_notification_has_no_response(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        req = {"jsonrpc": "2.0", "id": None, "method": "no/such/method", "params": {}}
        assert mcp_light_server.handle_light_request(req) is None

    def test_classify_filed_and_settings(self):
        assert (
            mcp_light_server._classify_underlying_tool("palace_query", "FILED")
            == "mempalace_memories_filed_away"
        )
        assert (
            mcp_light_server._classify_underlying_tool("palace_query", "SETTINGS")
            == "mempalace_hook_settings"
        )
        assert (
            mcp_light_server._classify_underlying_tool("palace_query", "TRAVERSE auth-flow")
            == "mempalace_traverse"
        )

    def test_alias_valid_to_to_ended_for_invalidate(self):
        mapped = mcp_light_server._alias_args_for_handler(
            mcp_server.tool_kg_invalidate,
            {
                "subject": "Max",
                "predicate": "plays",
                "object": "soccer",
                "valid_to": "2020-01-01",
            },
        )
        assert mapped["ended"] == "2020-01-01"
        assert "valid_to" not in mapped


class TestUnwrapAndStructuredMerge:
    def test_unwrap_keeps_sibling_limit(self):
        raw = {"query": "hello", "limit": 5}
        assert mcp_light_server._unwrap_wrapper_input(raw) == raw

    def test_unwrap_keeps_mixed_dsl_and_fields(self):
        raw = {"query": "FIND auth IN backend", "limit": 10, "wing": "core"}
        assert mcp_light_server._unwrap_wrapper_input(raw) == raw

    def test_unwrap_lone_dsl_string(self):
        assert mcp_light_server._unwrap_wrapper_input({"query": "STATUS"}) == "STATUS"

    def test_structured_search_limit_reaches_handler(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        captured = {}

        def fake_search(**kwargs):
            captured.update(kwargs)
            return {"results": []}

        monkeypatch.setattr(mcp_server, "tool_search", fake_search)
        req = {
            "jsonrpc": "2.0",
            "id": 80,
            "method": "tools/call",
            "params": {
                "name": "palace_query",
                "arguments": {"query": "hello", "limit": 5},
            },
        }
        mcp_light_server.handle_light_request(req)
        assert captured.get("query") == "hello"
        assert captured.get("limit") == 5


class TestSearchEnrichment:
    def test_keeps_searcher_order_and_tags_recency(self):
        res = {
            "results": [
                {"text": "old-high", "distance": 0.1, "created_at": "2024-01-01T00:00:00"},
                {"text": "new-low", "distance": 0.8, "created_at": "2026-01-01T00:00:00"},
            ]
        }
        out = mcp_light_server._enrich_search_results(res)
        assert [r["text"] for r in out["results"]] == ["old-high", "new-low"]
        assert out["results"][0]["recency_rank"] == 2
        assert out["results"][1]["recency_rank"] == 1
        assert out["results"][1].get("is_latest_record") is True
        assert "is_latest_record" not in out["results"][0]

    def test_unknown_timestamp_is_oldest_not_latest(self):
        res = {
            "results": [
                {"text": "dated", "distance": 0.2, "created_at": "2026-01-01T00:00:00"},
                {"text": "undated", "distance": 0.3, "created_at": "unknown"},
            ]
        }
        out = mcp_light_server._enrich_search_results(res)
        undated = [r for r in out["results"] if r["text"] == "undated"][0]
        dated = [r for r in out["results"] if r["text"] == "dated"][0]
        assert dated["recency_rank"] == 1
        assert undated["recency_rank"] == 2
        assert undated.get("is_latest_record") is not True

    def test_none_distance_does_not_raise(self):
        res = {
            "results": [
                {"text": "bm25", "distance": None, "created_at": "2026-01-01T00:00:00"},
                {"text": "vec", "distance": 0.4, "created_at": "2025-01-01T00:00:00"},
            ]
        }
        out = mcp_light_server._enrich_search_results(res)
        assert out["relevance_confidence"] == "moderate"
        assert "warning" not in out

    def test_tunnel_expansion_consumes_list(self, monkeypatch):
        monkeypatch.setattr(
            mcp_server,
            "tool_follow_tunnels",
            lambda wing, room: [
                {
                    "connected_wing": "guidelines",
                    "connected_room": "rx",
                    "drawer_id": "d1",
                    "drawer_preview": "outside-wing text that must not leak",
                }
            ],
        )
        res = {
            "results": [
                {
                    "text": "hit",
                    "distance": 0.2,
                    "wing": "patients",
                    "room": "allergies",
                    "created_at": "2026-01-01T00:00:00",
                }
            ]
        }
        out = mcp_light_server._enrich_search_results(res)
        assert out["connected_tunnels"][0]["connected_wing"] == "guidelines"
        assert "drawer_preview" not in out["connected_tunnels"][0]
        assert "connected_room_context" not in out


class TestFuzzyWing:
    def test_substring_does_not_steal_writes(self, monkeypatch):
        monkeypatch.setattr(
            mcp_server,
            "tool_list_wings",
            lambda: {"wings": {"oauth_notes": 1, "project_auth": 2}},
        )
        assert mcp_light_server._resolve_fuzzy_wing("auth") == "project_auth"

    def test_ambiguous_suffix_left_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            mcp_server,
            "tool_list_wings",
            lambda: {"wings": {"core_auth": 1, "project_auth": 2}},
        )
        assert mcp_light_server._resolve_fuzzy_wing("auth") == "auth"

    def test_substring_match_ignored(self, monkeypatch):
        monkeypatch.setattr(
            mcp_server,
            "tool_list_wings",
            lambda: {"wings": {"oauth_notes": 1}},
        )
        assert mcp_light_server._resolve_fuzzy_wing("auth") == "auth"


class TestHubDispatch:
    def test_tools_call_rewrites_add_and_forwards_to_hub(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        captured = {}

        monkeypatch.setattr(mcp_server, "_hub_proxy_target", lambda: ("http://127.0.0.1:9", {}))

        def fake_forward(base_url, headers, request, palace_path):
            captured["name"] = request["params"]["name"]
            captured["arguments"] = request["params"]["arguments"]
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]},
            }

        monkeypatch.setattr(mcp_server, "_forward_request_to_hub", fake_forward)
        req = {
            "jsonrpc": "2.0",
            "id": 90,
            "method": "tools/call",
            "params": {
                "name": "palace_exec",
                "arguments": 'ADD IN test_wing/test_room "verbatim" SOURCE a.md',
            },
        }
        res = mcp_light_server.dispatch_light_stdio_request(req)
        assert res["id"] == 90
        assert captured["name"] == "mempalace_add_drawer"
        assert captured["arguments"]["wing"] == "test_wing"
        assert captured["arguments"]["content"] == "verbatim"

    def test_tools_call_maps_sync_project_to_project_dir(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        captured = {}
        monkeypatch.setattr(mcp_server, "_hub_proxy_target", lambda: ("http://127.0.0.1:9", {}))

        def fake_forward(base_url, headers, request, palace_path):
            captured["name"] = request["params"]["name"]
            captured["arguments"] = request["params"]["arguments"]
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"content": [{"type": "text", "text": "{}"}]},
            }

        monkeypatch.setattr(mcp_server, "_forward_request_to_hub", fake_forward)
        req = {
            "jsonrpc": "2.0",
            "id": 91,
            "method": "tools/call",
            "params": {
                "name": "palace_exec",
                "arguments": "SYNC PROJECT /custom/repo APPLY",
            },
        }
        mcp_light_server.dispatch_light_stdio_request(req)
        assert captured["name"] == "mempalace_sync"
        assert captured["arguments"]["project_dir"] == "/custom/repo"
        assert captured["arguments"].get("apply") is True
        assert "project" not in captured["arguments"]

    def test_taxonomy_in_wing_is_filtered_after_hub_rewrite(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp_server, "_hub_proxy_target", lambda: ("http://127.0.0.1:9", {}))

        def fake_forward(base_url, headers, request, palace_path):
            assert request["params"]["name"] == "mempalace_get_taxonomy"
            assert "wing" not in request["params"]["arguments"]
            payload = {"taxonomy": {"backend": {"auth": 2}, "secrets": {"keys": 9}}}
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
            }

        monkeypatch.setattr(mcp_server, "_forward_request_to_hub", fake_forward)
        req = {
            "jsonrpc": "2.0",
            "id": 92,
            "method": "tools/call",
            "params": {"name": "palace_query", "arguments": "TAXONOMY IN backend"},
        }
        res = mcp_light_server.dispatch_light_stdio_request(req)
        payload = json.loads(res["result"]["content"][0]["text"])
        assert payload == {"taxonomy": {"backend": {"auth": 2}}}

    def test_exec_add_resolves_unique_suffix_wing_before_forward(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp_server, "_hub_proxy_target", lambda: ("http://127.0.0.1:9", {}))
        monkeypatch.setattr(
            mcp_server,
            "tool_list_wings",
            lambda: {"wings": {"project_auth": 3, "oauth_notes": 1}},
        )
        captured = {}

        def fake_forward(base_url, headers, request, palace_path):
            captured["name"] = request["params"]["name"]
            captured["arguments"] = request["params"]["arguments"]
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"content": [{"type": "text", "text": "{}"}]},
            }

        monkeypatch.setattr(mcp_server, "_forward_request_to_hub", fake_forward)
        req = {
            "jsonrpc": "2.0",
            "id": 93,
            "method": "tools/call",
            "params": {
                "name": "palace_exec",
                "arguments": 'ADD IN auth/room "verbatim words" SOURCE a.md',
            },
        }
        mcp_light_server.dispatch_light_stdio_request(req)
        assert captured["name"] == "mempalace_add_drawer"
        assert captured["arguments"]["wing"] == "project_auth"
        assert captured["arguments"]["room"] == "room"

    def test_read_only_blocks_exec_before_hub_forward(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        monkeypatch.setattr(mcp_server, "_READ_ONLY", True)
        forwarded = []
        monkeypatch.setattr(mcp_server, "_hub_proxy_target", lambda: ("http://127.0.0.1:9", {}))

        def fake_forward(base_url, headers, request, palace_path):
            forwarded.append(request)
            return {"jsonrpc": "2.0", "id": request["id"], "result": {}}

        monkeypatch.setattr(mcp_server, "_forward_request_to_hub", fake_forward)
        req = {
            "jsonrpc": "2.0",
            "id": 94,
            "method": "tools/call",
            "params": {
                "name": "palace_exec",
                "arguments": 'ADD IN test_wing/test_room "content" SOURCE test.md',
            },
        }
        res = mcp_light_server.dispatch_light_stdio_request(req)
        assert res["error"]["code"] == -32003
        assert forwarded == []

    def test_malformed_limit_does_not_raise(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        req = {
            "jsonrpc": "2.0",
            "id": 95,
            "method": "tools/call",
            "params": {"name": "palace_query", "arguments": "FIND x LIMIT nope"},
        }
        res = mcp_light_server.dispatch_light_stdio_request(req)
        payload = json.loads(res["result"]["content"][0]["text"])
        assert payload["success"] is False
        assert (
            "nope" in payload["error"]
            or "invalid" in payload["error"].lower()
            or "PQL" in payload["error"]
        )

    def test_tools_call_maps_kg_invalidate_valid_to_to_ended(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        captured = {}
        monkeypatch.setattr(mcp_server, "_hub_proxy_target", lambda: ("http://127.0.0.1:9", {}))

        def fake_forward(base_url, headers, request, palace_path):
            captured["name"] = request["params"]["name"]
            captured["arguments"] = request["params"]["arguments"]
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"content": [{"type": "text", "text": "{}"}]},
            }

        monkeypatch.setattr(mcp_server, "_forward_request_to_hub", fake_forward)
        req = {
            "jsonrpc": "2.0",
            "id": 96,
            "method": "tools/call",
            "params": {
                "name": "palace_exec",
                "arguments": {
                    "action": "kg_invalidate",
                    "subject": "Max",
                    "predicate": "plays",
                    "object": "soccer",
                    "valid_to": "2020-01-01",
                },
            },
        }
        mcp_light_server.dispatch_light_stdio_request(req)
        assert captured["name"] == "mempalace_kg_invalidate"
        assert captured["arguments"]["ended"] == "2020-01-01"
        assert "valid_to" not in captured["arguments"]

    def test_tools_call_maps_traverse_to_registered_tool(self, monkeypatch, config, kg):
        _patch_light_server(monkeypatch, config, kg)
        captured = {}
        monkeypatch.setattr(mcp_server, "_hub_proxy_target", lambda: ("http://127.0.0.1:9", {}))

        def fake_forward(base_url, headers, request, palace_path):
            captured["name"] = request["params"]["name"]
            captured["arguments"] = request["params"]["arguments"]
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"content": [{"type": "text", "text": "{}"}]},
            }

        monkeypatch.setattr(mcp_server, "_forward_request_to_hub", fake_forward)
        req = {
            "jsonrpc": "2.0",
            "id": 97,
            "method": "tools/call",
            "params": {"name": "palace_query", "arguments": "TRAVERSE auth-flow HOPS 3"},
        }
        mcp_light_server.dispatch_light_stdio_request(req)
        assert captured["name"] == "mempalace_traverse"
        assert captured["arguments"]["start_room"] == "auth-flow"
        assert captured["arguments"]["max_hops"] == 3
