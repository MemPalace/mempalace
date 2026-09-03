#!/usr/bin/env python3
"""
MemPalace Lightweight MCP Server
================================
Consolidates 45 MemPalace tools into a high-performance 3-tool interface
(`palace_query`, `palace_exec`, `palace_coordinate`) powered by Palace Query
Language (PQL) and Command DSL, while retaining 100% of underlying features,
guarantees, and safety checks.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

# stdio protection before heavy imports
_REAL_STDOUT = sys.stdout
_REAL_STDOUT_FD = None
try:
    _REAL_STDOUT_FD = os.dup(1)
    os.dup2(2, 1)
except (OSError, AttributeError):
    pass
sys.stdout = sys.stderr

from .version import __version__  # noqa: E402
from .query_parser import (  # noqa: E402
    QueryParseError,
    parse_coordinate_input,
    parse_exec_input,
    parse_query_input,
)
from . import mcp_server  # noqa: E402

import inspect  # noqa: E402

logger = logging.getLogger("mempalace_mcp_light")

_PQL_KEYWORDS = {
    "FIND",
    "SEARCH",
    "TAXONOMY",
    "WINGS",
    "ROOMS",
    "DRAWER",
    "DRAWERS",
    "CHECK",
    "AAAK",
    "KG",
    "TRAVERSE",
    "TUNNEL",
    "TUNNELS",
    "FOLLOW",
    "HALLWAY",
    "HALLWAYS",
    "GRAPH",
    "DIARY",
    "STATUS",
    "FILED",
    "SETTINGS",
    "ADD",
    "UPDATE",
    "DELETE",
    "CREATE",
    "MINE",
    "SYNC",
    "CHECKPOINT",
    "RECONNECT",
    "TASK",
    "EVENT",
    "EVENTS",
    "LOGSTREAM",
    "ARTIFACT",
    "PATCH",
    "PEERS",
    "MESH",
}

_WRAPPER_KEYS = (
    "command",
    "input",
    "dsl",
    "pql",
    "query",
    "expression",
    "text",
    "prompt",
    "raw",
)


def _unwrap_wrapper_input(arguments: Any) -> Any:
    """Unwrap a lone DSL string; keep sibling structured fields for the parsers."""
    if not isinstance(arguments, dict):
        return arguments

    extra_keys = [k for k in arguments if k not in _WRAPPER_KEYS]
    if extra_keys:
        return arguments

    for k in _WRAPPER_KEYS:
        if k in arguments and isinstance(arguments[k], str):
            val_stripped = arguments[k].strip()
            if not val_stripped:
                continue
            first_word = val_stripped.split(None, 1)[0].upper()
            if first_word in _PQL_KEYWORDS or len(arguments) == 1:
                return val_stripped

    return arguments


def _call_handler_safe(handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke handler safely by filtering extra kwargs that the handler doesn't accept."""
    if not callable(handler):
        return {"success": False, "error": f"Handler {handler} is not callable"}
    mapped = _alias_args_for_handler(handler, params)
    return handler(**mapped)


def _resolve_fuzzy_wing(wing: Optional[str]) -> Optional[str]:
    """Resolve unique exact or '_suffix' wing names. Never substring-match."""
    if not wing:
        return wing
    try:
        wings_resp = mcp_server.tool_list_wings()
        if isinstance(wings_resp, dict) and "wings" in wings_resp:
            known = wings_resp["wings"]
            if wing in known:
                return wing
            w_low = wing.lower()
            matches = [
                kw
                for kw in known
                if str(kw).lower() == w_low or str(kw).lower().endswith(f"_{w_low}")
            ]
            if len(matches) == 1:
                return matches[0]
    except Exception:
        pass
    return wing


def _search_distance(item: Dict[str, Any]) -> Optional[float]:
    raw = item.get("distance")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _search_timestamp(item: Dict[str, Any]) -> str:
    value = item.get("filed_at") or item.get("created_at") or ""
    if not value or value == "unknown":
        return ""
    return str(value)


def _enrich_search_results(res: Dict[str, Any]) -> Dict[str, Any]:
    """Tag search hits with confidence/recency; keep searcher ranking."""
    if not isinstance(res, dict) or "results" not in res:
        return res

    results_list = res.get("results") or []
    if not results_list:
        res["status"] = "no_matching_memories_found"
        res["total_found"] = 0
        res["summary"] = "No matching memories found in the palace for this query."
        return res

    distances = [d for d in (_search_distance(item) for item in results_list) if d is not None]
    if distances:
        min_dist = min(distances)
        if min_dist > 0.52:
            res["relevance_confidence"] = "low"
        elif min_dist < 0.35:
            res["relevance_confidence"] = "high"
        else:
            res["relevance_confidence"] = "moderate"

    by_recency = sorted(
        range(len(results_list)),
        key=lambda i: _search_timestamp(results_list[i]),
        reverse=True,
    )
    for recency_rank, orig_i in enumerate(by_recency, 1):
        item = results_list[orig_i]
        item["recency_rank"] = recency_rank
        if recency_rank == 1 and len(results_list) > 1 and _search_timestamp(item):
            item["is_latest_record"] = True

    res["results"] = results_list

    top_item = results_list[0]
    top_wing = top_item.get("wing")
    top_room = top_item.get("room")
    if top_wing and top_room:
        try:
            tunnel_res = mcp_server.tool_follow_tunnels(top_wing, top_room)
            if isinstance(tunnel_res, dict) and tunnel_res.get("error"):
                connections = []
            elif isinstance(tunnel_res, list):
                connections = tunnel_res
            else:
                connections = []
            if connections:
                cleaned = []
                for conn in connections[:3]:
                    if isinstance(conn, dict):
                        item = dict(conn)
                        item.pop("drawer_preview", None)
                        cleaned.append(item)
                    else:
                        cleaned.append(conn)
                res["connected_tunnels"] = cleaned
        except Exception:
            pass

    return res


def tool_palace_query(arguments: Dict[str, Any] | str) -> Dict[str, Any]:
    """Unified read tool for semantic search, taxonomy, KG, graph, diary, and status."""
    query_input = _unwrap_wrapper_input(arguments)

    try:
        target, params = parse_query_input(query_input)
    except QueryParseError as e:
        return {"success": False, "error": f"PQL query parse error: {e}"}

    # Resolve fuzzy wing names
    if "wing" in params and isinstance(params["wing"], str):
        params["wing"] = _resolve_fuzzy_wing(params["wing"])

    # Dispatch to underlying handlers in mcp_server
    if target in ("search", "find"):
        raw_res = _call_handler_safe(mcp_server.tool_search, params)
        return _enrich_search_results(raw_res)
    elif target in ("taxonomy", "get_taxonomy"):
        res = mcp_server.tool_get_taxonomy()
        wing = params.get("wing")
        if wing and isinstance(res, dict) and "taxonomy" in res:
            tax = res["taxonomy"]
            return {"taxonomy": {wing: tax.get(wing, {})}} if wing in tax else {"taxonomy": {}}
        return res
    elif target in ("wings", "list_wings"):
        return mcp_server.tool_list_wings()
    elif target in ("rooms", "list_rooms"):
        return _call_handler_safe(mcp_server.tool_list_rooms, params)
    elif target in ("drawer", "get_drawer"):
        return _call_handler_safe(mcp_server.tool_get_drawer, params)
    elif target in ("drawers", "list_drawers"):
        return _call_handler_safe(mcp_server.tool_list_drawers, params)
    elif target in ("check_duplicate", "duplicate", "check"):
        return _call_handler_safe(mcp_server.tool_check_duplicate, params)
    elif target in ("aaak_spec", "aaak", "get_aaak_spec"):
        return mcp_server.tool_get_aaak_spec()
    elif target in ("kg_query", "kg"):
        return _call_handler_safe(mcp_server.tool_kg_query, params)
    elif target in ("kg_timeline", "timeline"):
        return _call_handler_safe(mcp_server.tool_kg_timeline, params)
    elif target in ("kg_stats", "kg_statistics"):
        return mcp_server.tool_kg_stats()
    elif target in ("traverse", "traverse_graph"):
        return _call_handler_safe(mcp_server.tool_traverse_graph, params)
    elif target in ("find_tunnels",):
        return _call_handler_safe(mcp_server.tool_find_tunnels, params)
    elif target in ("list_tunnels", "tunnels"):
        return _call_handler_safe(mcp_server.tool_list_tunnels, params)
    elif target in ("follow_tunnels", "follow"):
        return _call_handler_safe(mcp_server.tool_follow_tunnels, params)
    elif target in ("list_hallways", "hallways"):
        return _call_handler_safe(mcp_server.tool_list_hallways, params)
    elif target in ("graph_stats", "stats"):
        return mcp_server.tool_graph_stats()
    elif target in ("diary_read", "diary"):
        return _call_handler_safe(mcp_server.tool_diary_read, params)
    elif target in ("status",):
        res = mcp_server.tool_status()
        return mcp_server._decorate_mcp_tool_result("mempalace_status", res)
    elif target in ("filed", "memories_filed_away"):
        return mcp_server.tool_memories_filed_away()
    elif target in ("settings", "hook_settings"):
        return mcp_server.tool_hook_settings()
    else:
        return {"success": False, "error": f"Unknown palace_query target: '{target}'"}


def tool_palace_exec(arguments: Dict[str, Any] | str) -> Dict[str, Any]:
    """Unified write tool for drawers, KG facts, tunnels, diaries, and maintenance."""
    exec_input = _unwrap_wrapper_input(arguments)

    try:
        action, params = parse_exec_input(exec_input)
    except QueryParseError as e:
        return {"success": False, "error": f"PQL exec parse error: {e}"}

    # Dispatch to underlying handlers
    if action == "add_drawer":
        if "wing" in params:
            params["wing"] = _resolve_fuzzy_wing(params["wing"])
        return _call_handler_safe(mcp_server.tool_add_drawer, params)
    elif action == "update_drawer":
        if "wing" in params:
            params["wing"] = _resolve_fuzzy_wing(params["wing"])
        return _call_handler_safe(mcp_server.tool_update_drawer, params)
    elif action == "delete_drawer":
        return _call_handler_safe(mcp_server.tool_delete_drawer, params)
    elif action == "delete_by_source":
        return _call_handler_safe(mcp_server.tool_delete_by_source, params)
    elif action == "checkpoint":
        return _call_handler_safe(mcp_server.tool_checkpoint, params)
    elif action == "mine":
        return _call_handler_safe(mcp_server.tool_mine, params)
    elif action == "sync":
        return _call_handler_safe(mcp_server.tool_sync, params)
    elif action == "kg_add":
        return _call_handler_safe(mcp_server.tool_kg_add, params)
    elif action == "kg_invalidate":
        return _call_handler_safe(mcp_server.tool_kg_invalidate, params)
    elif action == "kg_supersede":
        return _call_handler_safe(mcp_server.tool_kg_supersede, params)
    elif action == "create_tunnel":
        if "source_wing" in params:
            params["source_wing"] = _resolve_fuzzy_wing(params["source_wing"])
        if "target_wing" in params:
            params["target_wing"] = _resolve_fuzzy_wing(params["target_wing"])
        return _call_handler_safe(mcp_server.tool_create_tunnel, params)
    elif action == "delete_tunnel":
        return _call_handler_safe(mcp_server.tool_delete_tunnel, params)
    elif action == "delete_hallway":
        return _call_handler_safe(mcp_server.tool_delete_hallway, params)
    elif action == "diary_write":
        if "wing" in params:
            params["wing"] = _resolve_fuzzy_wing(params["wing"])
        return _call_handler_safe(mcp_server.tool_diary_write, params)
    elif action == "reconnect":
        return mcp_server.tool_reconnect()
    elif action == "hook_settings":
        return _call_handler_safe(mcp_server.tool_hook_settings, params)
    else:
        return {"success": False, "error": f"Unknown palace_exec action: '{action}'"}


def tool_palace_coordinate(arguments: Dict[str, Any] | str) -> Dict[str, Any]:
    """Unified multi-agent coordination tool (RFC 003 logstream, tasks, artifacts, mesh)."""
    coord_input = _unwrap_wrapper_input(arguments)

    try:
        action, params = parse_coordinate_input(coord_input)
    except QueryParseError as e:
        return {"success": False, "error": f"PQL coordinate parse error: {e}"}

    if action == "task_create":
        return _call_handler_safe(mcp_server.tool_task_create, params)
    elif action == "event_append":
        return _call_handler_safe(mcp_server.tool_event_append, params)
    elif action == "event_list":
        return _call_handler_safe(mcp_server.tool_event_list, params)
    elif action == "event_wait":
        return _call_handler_safe(mcp_server.tool_event_wait, params)
    elif action == "event_ack":
        return _call_handler_safe(mcp_server.tool_event_ack, params)
    elif action == "artifact_put":
        return _call_handler_safe(mcp_server.tool_artifact_put, params)
    elif action == "artifact_get":
        return _call_handler_safe(mcp_server.tool_artifact_get, params)
    elif action == "patch_submit":
        return _call_handler_safe(mcp_server.tool_patch_submit, params)
    elif action in ("mesh_peers", "peers"):
        return mcp_server.tool_mesh_peers()
    else:
        return {"success": False, "error": f"Unknown palace_coordinate action: '{action}'"}


# ==================== LIGHTWEIGHT MCP TOOL DEFINITIONS ====================

LIGHT_TOOLS = {
    "palace_query": {
        "description": (
            "Unified Palace Query Engine. Retrieve memories, taxonomy, knowledge graph facts/timelines, "
            "tunnels, hallways, agent diaries, and palace status. "
            "Accepts a concise PQL DSL query string (e.g. 'FIND \"terms\" IN wing/room LIMIT 5', "
            "'TAXONOMY', 'KG Max AS OF 2026-04-01', 'TRAVERSE auth-flow HOPS 2', 'DIARY agent LAST 5', "
            "'STATUS') or a structured dict payload."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "maxLength": 250,
                    "description": "PQL query DSL string (e.g. 'FIND auth IN backend/auth LIMIT 5' or 'STATUS')",
                },
                "target": {
                    "type": "string",
                    "description": (
                        "Target domain: search, taxonomy, wings, rooms, drawer, drawers, check_duplicate, "
                        "aaak_spec, kg_query, kg_timeline, kg_stats, traverse, find_tunnels, list_tunnels, "
                        "follow_tunnels, list_hallways, graph_stats, diary_read, status, filed, settings"
                    ),
                },
                "wing": {"type": "string", "description": "Wing filter (optional)"},
                "room": {"type": "string", "description": "Room filter (optional)"},
                "limit": {"type": "integer", "description": "Max results (optional)"},
                "offset": {"type": "integer", "description": "Pagination offset (optional)"},
                "since": {"type": "string", "description": "Start ISO date/datetime (optional)"},
                "before": {"type": "string", "description": "End ISO date/datetime (optional)"},
                "entity": {"type": "string", "description": "Entity for KG queries (optional)"},
                "as_of": {
                    "type": "string",
                    "description": "Point-in-time date for KG queries (optional)",
                },
                "direction": {
                    "type": "string",
                    "description": "outgoing, incoming, or both for KG (optional)",
                },
                "start_room": {
                    "type": "string",
                    "description": "Starting room for graph traversal (optional)",
                },
                "max_hops": {"type": "integer", "description": "Max traversal hops (default 2)"},
                "agent_name": {
                    "type": "string",
                    "description": "Agent name for diary read (optional)",
                },
                "content": {
                    "type": "string",
                    "description": "Content string for duplicate check (optional)",
                },
                "drawer_id": {
                    "type": "string",
                    "description": "Drawer ID for get_drawer (optional)",
                },
                "last_n": {"type": "integer", "description": "Diary entry count (optional)"},
                "max_distance": {
                    "type": "number",
                    "description": "Max cosine distance for search (optional)",
                },
                "candidate_strategy": {
                    "type": "string",
                    "enum": ["vector", "union"],
                    "description": "Search candidate strategy (optional)",
                },
                "source_file": {
                    "type": "string",
                    "description": "Exact source_file filter (optional)",
                },
                "wing_a": {
                    "type": "string",
                    "description": "First wing for find_tunnels (optional)",
                },
                "wing_b": {
                    "type": "string",
                    "description": "Second wing for find_tunnels (optional)",
                },
            },
        },
        "handler": tool_palace_query,
    },
    "palace_exec": {
        "description": (
            "Unified Palace Execution Engine. Add/update/delete drawers, batch checkpoint, knowledge graph "
            "fact lifecycle (add/invalidate/supersede), cross-wing tunnels, hallways, mining, sync, agent "
            "diaries, and maintenance. "
            "Accepts a concise command DSL string (e.g. 'ADD IN backend/auth \"content\"', "
            "'DELETE DRAWER drw_123', 'KG ADD Max -> loves -> chess', 'KG SUPERSEDE Max -> grade: 6 => 7', "
            "'MINE /path MODE projects', 'SYNC APPLY', 'RECONNECT') or a structured dict payload."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command DSL string (e.g. 'ADD IN backend/auth \"content\"')",
                },
                "action": {
                    "type": "string",
                    "description": (
                        "Action: add_drawer, update_drawer, delete_drawer, delete_by_source, checkpoint, "
                        "mine, sync, kg_add, kg_invalidate, kg_supersede, create_tunnel, delete_tunnel, "
                        "delete_hallway, diary_write, reconnect, hook_settings"
                    ),
                },
                "wing": {"type": "string", "description": "Target wing (optional)"},
                "room": {"type": "string", "description": "Target room (optional)"},
                "content": {
                    "type": "string",
                    "description": "Verbatim content to store/update (optional)",
                },
                "drawer_id": {
                    "type": "string",
                    "description": "Drawer ID for update/delete (optional)",
                },
                "items": {"type": "array", "description": "Batch items for checkpoint (optional)"},
                "diary": {
                    "type": "object",
                    "description": "Diary entry object for checkpoint (optional)",
                },
                "source": {"type": "string", "description": "Source path for mine (optional)"},
                "subject": {
                    "type": "string",
                    "description": "Subject for KG operations (optional)",
                },
                "predicate": {
                    "type": "string",
                    "description": "Predicate for KG operations (optional)",
                },
                "object": {"type": "string", "description": "Object for KG operations (optional)"},
                "old_object": {
                    "type": "string",
                    "description": "Old object for KG supersede (optional)",
                },
                "new_object": {
                    "type": "string",
                    "description": "New object for KG supersede (optional)",
                },
                "source_file": {
                    "type": "string",
                    "description": "Source path for mine/delete_by_source (optional)",
                },
                "source_wing": {
                    "type": "string",
                    "description": "Source wing for create_tunnel (optional)",
                },
                "target_wing": {
                    "type": "string",
                    "description": "Target wing for create_tunnel (optional)",
                },
                "valid_from": {"type": "string", "description": "KG fact start (optional)"},
                "valid_to": {
                    "type": "string",
                    "description": "KG fact end / invalidate ended (optional)",
                },
                "ended": {"type": "string", "description": "KG invalidate end date (optional)"},
                "project": {
                    "type": "string",
                    "description": "Project directory for mine/sync (optional)",
                },
            },
        },
        "handler": tool_palace_exec,
    },
    "palace_coordinate": {
        "description": (
            "Unified Multi-Agent Coordination Engine (RFC 003 / RFC 005). Immutable task delegation, "
            "logstream event append/list/wait/ack, artifact put/get, patch submission, and mesh estate snapshot. "
            "Accepts a concise coordination DSL string (e.g. 'TASK CREATE project:mempalace from:agent1 "
            'to:agent2 goal:"fix" branch:b base:c done:"done"\', \'EVENT APPEND type:task.request ...\', '
            "'EVENT LIST stream:project/x ...', 'EVENT WAIT correlation:task_1', 'EVENT ACK id:evt_1 "
            "from:agent1 status:applied', 'ARTIFACT PUT kind:patch ...', 'PATCH SUBMIT ...', 'MESH PEERS') "
            "or a structured dict payload."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Coordination DSL string (e.g. 'TASK CREATE project:x ...')",
                },
                "action": {
                    "type": "string",
                    "description": (
                        "Action: task_create, event_append, event_list, event_wait, event_ack, "
                        "artifact_put, artifact_get, patch_submit, mesh_peers"
                    ),
                },
                "project": {"type": "string", "description": "Project routing name (optional)"},
                "stream": {"type": "string", "description": "Logical stream (optional)"},
                "room": {"type": "string", "description": "Sub-channel (optional)"},
                "from_agent": {"type": "string", "description": "Writer agent identity (optional)"},
                "to_agent": {"type": "string", "description": "Target agent identity (optional)"},
                "goal": {"type": "string", "description": "Task goal (optional)"},
                "branch": {"type": "string", "description": "Git branch (optional)"},
                "base_commit": {"type": "string", "description": "Git commit SHA (optional)"},
                "done": {"type": "string", "description": "Definition of done (optional)"},
                "type": {"type": "string", "description": "Event type (optional)"},
                "correlation_id": {"type": "string", "description": "Correlation ID (optional)"},
                "status": {"type": "string", "description": "Status string (optional)"},
                "body": {"type": "string", "description": "Event/ack body content (optional)"},
                "content": {"type": "string", "description": "Artifact / Patch content (optional)"},
                "artifact_id": {"type": "string", "description": "Artifact ID to get (optional)"},
                "event_id": {"type": "string", "description": "Event ID to ack (optional)"},
                "since_event_id": {
                    "type": "string",
                    "description": "Resume cursor: events after this id (optional)",
                },
                "before_event_id": {
                    "type": "string",
                    "description": "Page backward: events before this id (optional)",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "EVENT WAIT timeout in milliseconds (optional)",
                },
                "kind": {"type": "string", "description": "Artifact kind (optional)"},
                "created_by": {"type": "string", "description": "Artifact author (optional)"},
                "metadata": {"type": "object", "description": "Event/artifact metadata (optional)"},
                "topic": {"type": "string", "description": "Event topic (optional)"},
            },
        },
        "handler": tool_palace_coordinate,
    },
}


# ==================== JSON-RPC REQUEST HANDLING ====================


def _restore_stdout():
    """Restore stdout descriptor for the JSON-RPC stdio transport."""
    global _REAL_STDOUT_FD
    if _REAL_STDOUT_FD is not None:
        try:
            os.dup2(_REAL_STDOUT_FD, 1)
            os.close(_REAL_STDOUT_FD)
            _REAL_STDOUT_FD = None
        except OSError:
            pass
    sys.stdout = _REAL_STDOUT
    # mcp_server dup'd fd 1 after we had already redirected it to stderr.
    # Keep it from later restoring that captured stderr onto protocol stdout.
    mcp_server._REAL_STDOUT = sys.stdout
    mcp_server._REAL_STDOUT_FD = None


_QUERY_TARGET_TO_TOOL = {
    "search": "mempalace_search",
    "find": "mempalace_search",
    "kg_query": "mempalace_kg_query",
    "kg": "mempalace_kg_query",
    "kg_timeline": "mempalace_kg_timeline",
    "timeline": "mempalace_kg_timeline",
    "kg_stats": "mempalace_kg_stats",
    "rooms": "mempalace_list_rooms",
    "list_rooms": "mempalace_list_rooms",
    "wings": "mempalace_list_wings",
    "list_wings": "mempalace_list_wings",
    "drawers": "mempalace_list_drawers",
    "list_drawers": "mempalace_list_drawers",
    "drawer": "mempalace_get_drawer",
    "get_drawer": "mempalace_get_drawer",
    "status": "mempalace_status",
    "filed": "mempalace_memories_filed_away",
    "memories_filed_away": "mempalace_memories_filed_away",
    "settings": "mempalace_hook_settings",
    "hook_settings": "mempalace_hook_settings",
    "find_tunnels": "mempalace_find_tunnels",
    "list_tunnels": "mempalace_list_tunnels",
    "tunnels": "mempalace_list_tunnels",
    "follow_tunnels": "mempalace_follow_tunnels",
    "follow": "mempalace_follow_tunnels",
    "diary_read": "mempalace_diary_read",
    "diary": "mempalace_diary_read",
    "check_duplicate": "mempalace_check_duplicate",
    "duplicate": "mempalace_check_duplicate",
    "check": "mempalace_check_duplicate",
    "aaak_spec": "mempalace_get_aaak_spec",
    "aaak": "mempalace_get_aaak_spec",
    "get_aaak_spec": "mempalace_get_aaak_spec",
    "taxonomy": "mempalace_get_taxonomy",
    "get_taxonomy": "mempalace_get_taxonomy",
    "traverse": "mempalace_traverse",
    "traverse_graph": "mempalace_traverse",
    "list_hallways": "mempalace_list_hallways",
    "hallways": "mempalace_list_hallways",
    "graph_stats": "mempalace_graph_stats",
    "stats": "mempalace_graph_stats",
}


def _classify_underlying_tool(tool_name: str, arguments: Any) -> str:
    """Map a consolidated lightweight tool call to its underlying legacy tool name for preflight gates."""
    unwrapped = _unwrap_wrapper_input(arguments)
    if tool_name == "palace_exec":
        try:
            action, _ = parse_exec_input(unwrapped)
            return f"mempalace_{action}"
        except Exception:
            return "mempalace_add_drawer"

    if tool_name == "palace_coordinate":
        try:
            action, _ = parse_coordinate_input(unwrapped)
            return f"mempalace_{action}"
        except Exception:
            return "mempalace_event_append"

    if tool_name == "palace_query":
        try:
            target, _ = parse_query_input(unwrapped)
            return _QUERY_TARGET_TO_TOOL.get(target, f"mempalace_{target}")
        except Exception:
            return "mempalace_search"

    return tool_name


def handle_light_request(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Process a single JSON-RPC request for the lightweight MCP server."""
    mcp_server._last_request_time = time.monotonic()
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }

    method = request.get("method") or ""
    params = request.get("params") or {}
    req_id = request.get("id")

    if method == "initialize":
        client_version = params.get("protocolVersion", mcp_server.SUPPORTED_PROTOCOL_VERSIONS[-1])
        negotiated = (
            client_version
            if client_version in mcp_server.SUPPORTED_PROTOCOL_VERSIONS
            else mcp_server.SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mempalace-light", "version": __version__},
            },
        }
    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    elif method.startswith("notifications/"):
        return None
    elif method == "tools/list":
        # In read-only mode, only palace_query is exposed
        tools_list = []
        for name, tool_def in LIGHT_TOOLS.items():
            if mcp_server._READ_ONLY and name in ("palace_exec", "palace_coordinate"):
                continue
            tools_list.append(
                {
                    "name": name,
                    "description": tool_def["description"],
                    "inputSchema": tool_def["input_schema"],
                }
            )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tools_list},
        }
    elif method == "tools/call":
        if not isinstance(params, dict) or "name" not in params:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "Invalid params: name required"},
            }

        tool_name = params["name"]
        if tool_name not in LIGHT_TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }

        arguments = params.get("arguments") or {}

        # Classify underlying tool for preflight gates (read-only, peer-writer, stale-library, diverged-index)
        underlying_name = _classify_underlying_tool(tool_name, arguments)
        refusal = mcp_server._mcp_tool_preflight_refusal(req_id, underlying_name)
        if refusal is not None:
            return refusal

        try:
            handler = LIGHT_TOOLS[tool_name]["handler"]
            with mcp_server._write_stall_watch(underlying_name):
                result = handler(arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, ensure_ascii=False, indent=2)
                            if not isinstance(result, str)
                            else result,
                        }
                    ]
                },
            }
        except Exception as e:
            return mcp_server._internal_tool_error(req_id, tool_name, e)

    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _alias_args_for_handler(handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Translate PQL field names to the underlying handler's parameter names."""
    mapped = dict(params)
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return mapped
    names = sig.parameters
    if "entity" in mapped and "entity_name" in names and "entity_name" not in mapped:
        mapped["entity_name"] = mapped.pop("entity")
    if "agent" in mapped and "agent_name" in names and "agent_name" not in mapped:
        mapped["agent_name"] = mapped.pop("agent")
    if "content" in mapped and "entry" in names and "entry" not in mapped:
        mapped["entry"] = mapped.pop("content")
    if "source" in mapped and "source_file" in names and "source_file" not in mapped:
        mapped["source_file"] = mapped.pop("source")
    if "source" in mapped and "source_wing" in names and "source_wing" not in mapped:
        mapped["source_wing"] = mapped.pop("source")
    if "target" in mapped and "target_wing" in names and "target_wing" not in mapped:
        mapped["target_wing"] = mapped.pop("target")
    if "project" in mapped and "project_dir" in names and "project_dir" not in mapped:
        mapped["project_dir"] = mapped.pop("project")
    if "valid_to" in mapped and "ended" in names and "ended" not in mapped:
        mapped["ended"] = mapped.pop("valid_to")
    has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in names.values())
    if has_var_keyword:
        return mapped
    return {k: v for k, v in mapped.items() if k in names}


def _rewrite_tools_call_for_hub(request: Dict[str, Any]):
    """Translate palace_* tools/call into the underlying mempalace_* JSON-RPC."""
    params = request.get("params") or {}
    if not isinstance(params, dict) or "name" not in params:
        return None
    tool_name = params["name"]
    if tool_name not in LIGHT_TOOLS:
        return None
    arguments = params.get("arguments") or {}
    unwrapped = _unwrap_wrapper_input(arguments)
    query_target = None
    try:
        if tool_name == "palace_query":
            query_target, parsed = parse_query_input(unwrapped)
        elif tool_name == "palace_exec":
            _, parsed = parse_exec_input(unwrapped)
        else:
            _, parsed = parse_coordinate_input(unwrapped)
    except (QueryParseError, ValueError, TypeError) as exc:
        return {"_parse_error": True, "message": str(exc)}

    taxonomy_wing = None
    if tool_name in ("palace_query", "palace_exec"):
        for key in ("wing", "source_wing", "target_wing"):
            if isinstance(parsed.get(key), str):
                parsed[key] = _resolve_fuzzy_wing(parsed[key])
        if tool_name == "palace_query" and query_target in ("taxonomy", "get_taxonomy"):
            taxonomy_wing = parsed.get("wing")

    underlying = _classify_underlying_tool(tool_name, arguments)
    tool_def = mcp_server.TOOLS.get(underlying) or {}
    handler = tool_def.get("handler")
    if handler is not None:
        parsed = _alias_args_for_handler(handler, parsed)
    return {
        "jsonrpc": request.get("jsonrpc", "2.0"),
        "id": request.get("id"),
        "method": "tools/call",
        "params": {"name": underlying, "arguments": parsed},
        "_light_tool": tool_name,
        "_taxonomy_wing": taxonomy_wing,
    }


def _filter_taxonomy_payload(payload: Dict[str, Any], wing: str) -> Dict[str, Any]:
    tax = payload.get("taxonomy") if isinstance(payload.get("taxonomy"), dict) else {}
    if wing in tax:
        return {"taxonomy": {wing: tax.get(wing, {})}}
    return {"taxonomy": {}}


def _postprocess_light_response(rewritten: Dict[str, Any], resp: Optional[Dict[str, Any]]):
    if not resp or "result" not in resp:
        return resp
    underlying = ((rewritten.get("params") or {}).get("name")) or ""
    taxonomy_wing = rewritten.get("_taxonomy_wing")
    if underlying not in ("mempalace_search", "mempalace_get_taxonomy"):
        return resp
    if underlying == "mempalace_get_taxonomy" and not taxonomy_wing:
        return resp
    content = (resp.get("result") or {}).get("content") or []
    if not content or not isinstance(content[0], dict):
        return resp
    raw = content[0].get("text")
    if not isinstance(raw, str):
        return resp
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return resp
    if not isinstance(payload, dict):
        return resp
    if underlying == "mempalace_search":
        payload = _enrich_search_results(payload)
    elif underlying == "mempalace_get_taxonomy":
        payload = _filter_taxonomy_payload(payload, taxonomy_wing)
    content[0]["text"] = json.dumps(payload, ensure_ascii=False, indent=2)
    return resp


def dispatch_light_stdio_request(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Hub-first stdio dispatch: translate palace_* calls, keep the 3-tool handshake local."""
    method = request.get("method") if isinstance(request, dict) else None
    if method == "tools/call":
        rewritten = _rewrite_tools_call_for_hub(request)
        if isinstance(rewritten, dict) and rewritten.get("_parse_error"):
            req_id = request.get("id")
            payload = {"success": False, "error": f"PQL parse error: {rewritten['message']}"}
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(payload, ensure_ascii=False, indent=2),
                        }
                    ]
                },
            }
        if rewritten is not None:
            forwarded = {k: v for k, v in rewritten.items() if not k.startswith("_")}
            underlying = ((forwarded.get("params") or {}).get("name")) or ""
            refusal = mcp_server._mcp_tool_preflight_refusal(request.get("id"), underlying)
            if refusal is not None:
                return refusal
            try:
                resp = mcp_server._dispatch_stdio_request(forwarded)
            except Exception as exc:
                return mcp_server._internal_tool_error(request.get("id"), underlying, exc)
            return _postprocess_light_response(rewritten, resp)
    return handle_light_request(request)


def main():
    parser = argparse.ArgumentParser(description="MemPalace Lightweight MCP Server")
    parser.add_argument("--palace", help="Path to palace directory")
    parser.add_argument("--collection", help="Chroma collection name")
    parser.add_argument(
        "--backend",
        help="Storage backend to use (default: config/env/detected/chroma)",
    )
    parser.add_argument("--read-only", action="store_true", help="Run in read-only mode")
    args = parser.parse_args()

    if args.palace:
        mcp_server._config.palace_path = args.palace
        os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(args.palace)
    if args.collection:
        mcp_server._config.collection_name = args.collection
    if args.backend:
        backend_name = str(args.backend).strip().lower()
        os.environ["MEMPALACE_BACKEND_EXPLICIT"] = backend_name
        os.environ["MEMPALACE_BACKEND"] = backend_name
    if args.read_only:
        mcp_server._READ_ONLY = True

    # Run stdio protocol loop with lightweight dispatcher
    _restore_stdout()
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass
    logger.info("MemPalace Lightweight MCP Server starting (3 consolidated tools)...")
    mcp_server._maybe_eager_warmup_embedder()
    mcp_server._start_idle_exit_watchdog()
    mcp_server._start_write_stall_watchdog()

    while True:
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            break
        except OSError as exc:
            logger.info("stdin read failed (%s) -- client disconnected, shutting down", exc)
            break
        if not line:
            logger.info("stdin EOF -- client disconnected, shutting down")
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            resp = dispatch_light_stdio_request(req)
        except Exception as exc:
            logger.error("Server error: %s", exc)
            continue
        if resp is not None:
            try:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                break


if __name__ == "__main__":
    main()
