#!/usr/bin/env python3
"""
MemPalace Lightweight MCP Server
================================
Consolidates 47 MemPalace tools into a high-performance 3-tool interface
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
from contextlib import nullcontext
from typing import Any, Dict, Optional

# Only the stdio entry point owns stdout. The HTTP hub imports this module.
_REAL_STDOUT = sys.stdout
_REAL_STDOUT_FD = None
if __name__ == "__main__":
    try:
        _REAL_STDOUT_FD = os.dup(1)
        os.dup2(2, 1)
    except (OSError, AttributeError):
        pass
    sys.stdout = sys.stderr

import inspect  # noqa: E402
import urllib.error  # noqa: E402

from .config import MempalaceConfig, normalize_wing_name  # noqa: E402
from .hub_client import (  # noqa: E402
    FORWARD_ERRORS,
    SEARCH_TOOL_NAMES,
    HubClient,
    proxy_error_response,
    search_settings_overridden,
    tool_result,
)
from .query_parser import (  # noqa: E402
    QueryParseError,
    parse_coordinate_input,
    parse_exec_input,
    parse_query_input,
)
from .search_util import _cosine_scale_distance  # noqa: E402
from .stored_text import dumps_client  # noqa: E402
from .timeutil import recency_key  # noqa: E402
from .tool_capabilities import (  # noqa: E402
    READ_ONLY_REFUSED_TOOLS,
    TOOL_CAPABILITIES,
)
from .version import __version__  # noqa: E402

_SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_STDOUT_RESTORED = False
_READ_ONLY = False
_MCP_SERVER_MOD: Any = None


def _import_mcp_server():
    """Import ``mempalace.mcp_server`` preserving stdout when already restored."""
    existing = sys.modules.get("mempalace.mcp_server")
    if existing is not None:
        return existing

    saved_fd = None
    saved_stdout = sys.stdout
    if _STDOUT_RESTORED:
        try:
            saved_fd = os.dup(1)
        except OSError:
            saved_fd = None

    import importlib

    try:
        mod = importlib.import_module("mempalace.mcp_server")
    finally:
        if _STDOUT_RESTORED:
            if saved_fd is not None:
                try:
                    os.dup2(saved_fd, 1)
                finally:
                    os.close(saved_fd)
            sys.stdout = saved_stdout
    if _STDOUT_RESTORED:
        dup_fd = getattr(mod, "_REAL_STDOUT_FD", None)
        if isinstance(dup_fd, int) and dup_fd >= 0:
            try:
                os.close(dup_fd)
            except OSError:
                pass
        mod._REAL_STDOUT = sys.stdout
        mod._REAL_STDOUT_FD = None
    return mod


def _get_mcp_server():
    """Return ``mempalace.mcp_server``, importing it lazily on first local dispatch."""
    global _MCP_SERVER_MOD
    if "mcp_server" in globals() and globals()["mcp_server"] is not None:
        return globals()["mcp_server"]
    if _MCP_SERVER_MOD is not None:
        globals()["mcp_server"] = _MCP_SERVER_MOD
        return _MCP_SERVER_MOD
    mod = _import_mcp_server()
    _MCP_SERVER_MOD = mod
    globals()["mcp_server"] = mod
    return mod


def _mcp_server_loaded() -> bool:
    return (
        ("mcp_server" in globals() and globals()["mcp_server"] is not None)
        or _MCP_SERVER_MOD is not None
        or "mempalace.mcp_server" in sys.modules
    )


def __getattr__(name: str):
    if name == "mcp_server":
        return _get_mcp_server()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


logger = logging.getLogger(__name__)

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
    "INBOX",
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


def _call_handler_safe(name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Run tool ``name`` with the kwargs its handler accepts, checked like any tools/call."""
    srv = _get_mcp_server()
    handler = srv.TOOLS[name]["handler"]
    try:
        mapped = srv.validate_tool_arguments(name, _alias_args_for_handler(handler, params))
    except srv.ToolArgumentError as e:
        return {"success": False, "error": f"Invalid params for tool {name}: {e}"}
    return handler(**mapped)


def _match_fuzzy_wing(wing: str, known) -> str:
    if wing in known:
        return wing
    w_low = wing.lower()
    matches = [
        kw for kw in known if str(kw).lower() == w_low or str(kw).lower().endswith(f"_{w_low}")
    ]
    return matches[0] if len(matches) == 1 else wing


def _resolve_fuzzy_wing(wing: Optional[str]) -> Optional[str]:
    """Resolve unique exact or '_suffix' wing names. Never substring-match."""
    if not wing:
        return wing
    try:
        wings_resp = _get_mcp_server().tool_list_wings()
        if isinstance(wings_resp, dict) and "wings" in wings_resp:
            return _match_fuzzy_wing(wing, wings_resp["wings"])
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


def _follow_tunnels_from_disk(palace_path: str, wing: str, room: str) -> list:
    """Read explicit tunnel connections from ``tunnels.json`` without loading ``mcp_server``."""
    tunnels_file = MempalaceConfig(palace_path=palace_path).tunnel_file
    try:
        with open(tunnels_file, encoding="utf-8") as f:
            tunnels = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(tunnels, list):
        return []
    norm_wing = normalize_wing_name(wing) or wing
    connections = []
    for t in tunnels:
        if not isinstance(t, dict):
            continue
        src = t.get("source") or {}
        tgt = t.get("target") or {}
        if normalize_wing_name(str(src.get("wing") or "")) == norm_wing and src.get("room") == room:
            connections.append(
                {
                    "direction": "outgoing",
                    "connected_wing": tgt.get("wing", ""),
                    "connected_room": tgt.get("room", ""),
                    "label": t.get("label", ""),
                    "drawer_id": tgt.get("drawer_id"),
                    "tunnel_id": t.get("id", ""),
                }
            )
        elif (
            normalize_wing_name(str(tgt.get("wing") or "")) == norm_wing and tgt.get("room") == room
        ):
            connections.append(
                {
                    "direction": "incoming",
                    "connected_wing": src.get("wing", ""),
                    "connected_room": src.get("room", ""),
                    "label": t.get("label", ""),
                    "drawer_id": src.get("drawer_id"),
                    "tunnel_id": t.get("id", ""),
                }
            )
    return connections


def _enrich_search_results(
    res: Dict[str, Any], *, palace_path: Optional[str] = None
) -> Dict[str, Any]:
    """Tag search hits with confidence/recency; keep searcher ranking."""
    if not isinstance(res, dict) or "results" not in res:
        return res

    results_list = res.get("results") or []
    if not results_list:
        res["status"] = "no_matching_memories_found"
        res["total_found"] = 0
        res["summary"] = "No matching memories found in the palace for this query."
        return res

    # The bands are on the cosine scale; the raw distance may be l2 or ip.
    metric = res.get("distance_metric") or "cosine"
    distances = [
        _cosine_scale_distance(d, metric)
        for d in (_search_distance(item) for item in results_list)
        if d is not None
    ]
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
        key=lambda i: recency_key(_search_timestamp(results_list[i])),
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
            # Decoration of a search hit, not an agent crossing the tunnel.
            if palace_path and not _mcp_server_loaded():
                tunnel_res = _follow_tunnels_from_disk(palace_path, top_wing, top_room)
            else:
                tunnel_res = _get_mcp_server().tool_follow_tunnels(top_wing, top_room, record=False)
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

    _resolve_wing_params(params)
    name = _allowlisted_tool(_QUERY_TARGET_TO_TOOL, target)
    if name is None:
        return {"success": False, "error": f"Unknown palace_query target: '{target}'"}
    res = _call_handler_safe(name, _query_arguments(name, params))
    if name == "mempalace_search":
        return _enrich_search_results(res)
    if name == "mempalace_get_taxonomy" and params.get("wing") and isinstance(res, dict):
        return _filter_taxonomy_payload(res, params["wing"]) if "taxonomy" in res else res
    if name == "mempalace_status":
        return _get_mcp_server()._decorate_mcp_tool_result(name, res)
    return res


def tool_palace_exec(arguments: Dict[str, Any] | str) -> Dict[str, Any]:
    """Unified write tool for drawers, KG facts, tunnels, diaries, and maintenance."""
    exec_input = _unwrap_wrapper_input(arguments)

    try:
        action, params = parse_exec_input(exec_input)
    except QueryParseError as e:
        return {"success": False, "error": f"PQL exec parse error: {e}"}

    _resolve_wing_params(params)
    name = _allowlisted_tool(_EXEC_ACTION_TO_TOOL, action)
    if name is None:
        return {"success": False, "error": f"Unknown palace_exec action: '{action}'"}
    return _call_handler_safe(name, params)


def tool_palace_coordinate(arguments: Dict[str, Any] | str) -> Dict[str, Any]:
    """Unified multi-agent coordination tool (RFC 003 logstream, tasks, artifacts, mesh)."""
    coord_input = _unwrap_wrapper_input(arguments)

    try:
        action, params = parse_coordinate_input(coord_input)
    except QueryParseError as e:
        return {"success": False, "error": f"PQL coordinate parse error: {e}"}

    name = _allowlisted_tool(_COORD_ACTION_TO_TOOL, action)
    if name is None:
        return {"success": False, "error": f"Unknown palace_coordinate action: '{action}'"}
    return _call_handler_safe(name, params)


# ==================== LIGHTWEIGHT MCP TOOL DEFINITIONS ====================

LIGHT_TOOLS = {
    "palace_query": {
        "description": (
            "Unified Palace Query Engine. Retrieve memories, taxonomy, knowledge graph facts/timelines, "
            "tunnels, hallways, agent diaries, palace status, and graph statistics. "
            "Use palace_query for ALL read-only queries, inspections, status checks, and diary reads. "
            "NEVER use palace_coordinate for reads or queries. "
            "Accepts a concise PQL DSL query string (e.g. 'FIND \"terms\" IN wing/room LIMIT 5', "
            "'TAXONOMY', 'KG Max AS OF 2026-04-01', 'TRAVERSE auth-flow HOPS 2', 'DIARY agent LAST 5', "
            "'STATUS') or a structured dict payload (e.g. {'target': 'search', 'query': 'terms', 'wing': 'patient_1042', 'room': 'labs', 'limit': 5})."
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
                    "enum": ["vector", "union", "lexical"],
                    "description": "Search candidate strategy: vector, union, or lexical (optional)",
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
            "IMPORTANT: Use palace_exec ONLY for intentional, authorized mutations. Drafting, proposing, "
            "checking, reading, or explaining must NEVER call palace_exec. "
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
                    "description": "Verbatim content to store/update (optional; also accepted for diary_write)",
                },
                "agent_name": {
                    "type": "string",
                    "description": "Agent name for diary_write (required when action is diary_write)",
                },
                "entry": {
                    "type": "string",
                    "description": "Diary entry text for diary_write (required when action is diary_write; content also accepted)",
                },
                "topic": {
                    "type": "string",
                    "description": "Topic category for diary_write (optional, default: 'general')",
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
                    "description": "Subject for KG operations (required for kg_add, kg_invalidate, kg_supersede)",
                },
                "predicate": {
                    "type": "string",
                    "description": "Predicate for KG operations (required for kg_add, kg_invalidate, kg_supersede)",
                },
                "object": {
                    "type": "string",
                    "description": "Object for KG operations (required for kg_add, kg_invalidate)",
                },
                "old_object": {
                    "type": "string",
                    "description": "Old object being replaced for KG supersede (required for kg_supersede)",
                },
                "new_object": {
                    "type": "string",
                    "description": "New replacement object for KG supersede (required for kg_supersede)",
                },
                "at": {
                    "type": "string",
                    "description": "Boundary instant for KG supersede (ISO date or datetime, optional; defaults to now UTC)",
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
            "DO NOT use for reading memories, viewing status, checking graph stats, or querying diaries (use palace_query instead). "
            "Accepts a concise coordination DSL string (e.g. 'TASK CREATE project:mempalace from:agent1 "
            'to:agent2 goal:"fix" branch:b base:c done:"done"\', \'EVENT APPEND type:task.request ...\', '
            "'EVENT INBOX to:agent', 'EVENT LIST stream:project/x [DESC|ASC] [LIMIT n] [PREVIEW]', "
            "'EVENT WAIT correlation:task_1', 'EVENT ACK id:evt_1 from:agent1 status:applied', "
            "'ARTIFACT PUT kind:patch ...', 'PATCH SUBMIT ...', 'MESH PEERS') "
            "or a structured dict payload. EVENT LIST defaults to newest-first (order='desc') when no cursor is passed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Coordination DSL string (e.g. 'TASK CREATE project:x ...', 'EVENT INBOX to:agent', 'EVENT LIST stream:project/x DESC')",
                },
                "action": {
                    "type": "string",
                    "description": (
                        "Action: task_create, event_append, event_list, event_wait, event_ack, "
                        "artifact_put, artifact_get, patch_submit, mesh_peers, inbox"
                    ),
                },
                "project": {"type": "string", "description": "Project routing name (optional)"},
                "stream": {"type": "string", "description": "Logical stream (optional)"},
                "room": {"type": "string", "description": "Sub-channel (optional)"},
                "from_agent": {
                    "type": "string",
                    "description": "Your identity (never filters; use writer)",
                },
                "writer": {"type": "string", "description": "List/wait: filter by event writer"},
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
                    "description": "Resume cursor: events strictly after this id in append order (optional)",
                },
                "before_event_id": {
                    "type": "string",
                    "description": "Page backward: events strictly before this id in append order (optional)",
                },
                "since_created_at": {
                    "type": "string",
                    "description": "Time window filter, inclusive: events created at or after this timestamp (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, optional). NOT a resume cursor — use since_event_id for that.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max events to return (default 50, max 500, optional)",
                },
                "order": {
                    "type": "string",
                    "description": "'desc' (newest first, default without cursor) or 'asc' (forward chronological, default with since_event_id, optional)",
                },
                "preview": {
                    "type": "boolean",
                    "description": "Truncate each event body to short excerpt (marks body_truncated + body_length) so scanning stays cheap (default false)",
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
    global _REAL_STDOUT_FD, _STDOUT_RESTORED
    if _REAL_STDOUT_FD is not None:
        try:
            os.dup2(_REAL_STDOUT_FD, 1)
            os.close(_REAL_STDOUT_FD)
            _REAL_STDOUT_FD = None
        except OSError:
            pass
    sys.stdout = _REAL_STDOUT
    _STDOUT_RESTORED = True
    # mcp_server dup'd fd 1 after we had already redirected it to stderr.
    # Keep it from later restoring that captured stderr onto protocol stdout.
    if _mcp_server_loaded():
        srv = _get_mcp_server()
        srv._REAL_STDOUT = sys.stdout
        srv._REAL_STDOUT_FD = None


_QUERY_TARGET_TO_TOOL = {
    "search": "mempalace_search",
    "find": "mempalace_search",
    "kg_query": "mempalace_kg_query",
    "kg": "mempalace_kg_query",
    "kg_timeline": "mempalace_kg_timeline",
    "timeline": "mempalace_kg_timeline",
    "kg_stats": "mempalace_kg_stats",
    "kg_statistics": "mempalace_kg_stats",
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

# palace_exec / palace_coordinate actions the production rewrite may dispatch.
# Anything absent is an unknown target (-32602), never ``mempalace_<action>``.
_EXEC_ACTION_TO_TOOL = {
    "add_drawer": "mempalace_add_drawer",
    "update_drawer": "mempalace_update_drawer",
    "delete_drawer": "mempalace_delete_drawer",
    "delete_by_source": "mempalace_delete_by_source",
    "checkpoint": "mempalace_checkpoint",
    "mine": "mempalace_mine",
    "sync": "mempalace_sync",
    "kg_add": "mempalace_kg_add",
    "kg_invalidate": "mempalace_kg_invalidate",
    "kg_supersede": "mempalace_kg_supersede",
    "create_tunnel": "mempalace_create_tunnel",
    "delete_tunnel": "mempalace_delete_tunnel",
    "delete_hallway": "mempalace_delete_hallway",
    "diary_write": "mempalace_diary_write",
    "reconnect": "mempalace_reconnect",
    "hook_settings": "mempalace_hook_settings",
}

_COORD_ACTION_TO_TOOL = {
    "task_create": "mempalace_task_create",
    "event_append": "mempalace_event_append",
    "event_list": "mempalace_event_list",
    "event_wait": "mempalace_event_wait",
    "event_ack": "mempalace_event_ack",
    "artifact_put": "mempalace_artifact_put",
    "artifact_get": "mempalace_artifact_get",
    "patch_submit": "mempalace_patch_submit",
    "mesh_peers": "mempalace_mesh_peers",
    # Structured ``{"action": "peers"}`` is not rewritten by the PQL parser.
    "peers": "mempalace_mesh_peers",
}


def _allowlisted_tool(mapping: Dict[str, str], key: str) -> Optional[str]:
    """Return the mempalace_* name for an allowlisted key, else None."""
    name = mapping.get(key)
    if not isinstance(name, str):
        return None
    if _mcp_server_loaded():
        return name if name in _get_mcp_server().TOOLS else None
    return name if name in TOOL_CAPABILITIES else None


def _query_arguments(name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """palace_query reads settings; the arguments that would change them are dropped."""
    return {} if name == "mempalace_hook_settings" else params


def _resolve_wing_params(params: Dict[str, Any], *, read_lock=nullcontext) -> None:
    keys = [
        key for key in ("wing", "source_wing", "target_wing") if isinstance(params.get(key), str)
    ]
    if keys:
        with read_lock():
            for key in keys:
                params[key] = _resolve_fuzzy_wing(params[key])


def _classify_underlying_tool(tool_name: str, arguments: Any) -> Optional[str]:
    """Map a palace_* call to an allowlisted mempalace_* tool.

    Unknown targets are None. They are not invented as ``mempalace_<target>``.
    A parse failure still names a real mutating tool so preflight keeps
    treating an unreadable exec or coordinate call as a write.
    """
    unwrapped = _unwrap_wrapper_input(arguments)
    if tool_name == "palace_exec":
        try:
            action, _ = parse_exec_input(unwrapped)
        except Exception:
            return "mempalace_add_drawer"
        return _allowlisted_tool(_EXEC_ACTION_TO_TOOL, action)

    if tool_name == "palace_coordinate":
        try:
            action, _ = parse_coordinate_input(unwrapped)
        except Exception:
            return "mempalace_event_append"
        return _allowlisted_tool(_COORD_ACTION_TO_TOOL, action)

    if tool_name == "palace_query":
        try:
            target, _ = parse_query_input(unwrapped)
        except Exception:
            return "mempalace_search"
        return _allowlisted_tool(_QUERY_TARGET_TO_TOOL, target)

    if isinstance(tool_name, str):
        known_tools = _get_mcp_server().TOOLS if _mcp_server_loaded() else TOOL_CAPABILITIES
        if tool_name in known_tools:
            return tool_name
    return None


def _unknown_target_error(req_id) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32602, "message": "unknown target"},
    }


def _is_read_only() -> bool:
    if _mcp_server_loaded():
        return bool(_get_mcp_server()._READ_ONLY)
    return _READ_ONLY


def _supported_protocol_versions():
    if _mcp_server_loaded():
        return _get_mcp_server().SUPPORTED_PROTOCOL_VERSIONS
    return _SUPPORTED_PROTOCOL_VERSIONS


def handle_light_request(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Process a single JSON-RPC request for the lightweight MCP server."""
    if _mcp_server_loaded():
        _get_mcp_server()._last_request_time = time.monotonic()
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }

    method = request.get("method")
    if not isinstance(method, str):
        method = ""
    params = request.get("params")
    if not isinstance(params, dict):
        params = {}
    req_id = request.get("id")

    if method == "initialize":
        versions = _supported_protocol_versions()
        client_version = params.get("protocolVersion", versions[-1])
        negotiated = client_version if client_version in versions else versions[0]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mempalace-light", "version": __version__},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        # In read-only mode, only palace_query is exposed
        tools_list = []
        read_only = _is_read_only()
        for name, tool_def in LIGHT_TOOLS.items():
            if read_only and name in ("palace_exec", "palace_coordinate"):
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
    if method == "tools/call":
        if not isinstance(params.get("name"), str):
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

        # Same allowlist the production rewrite uses. An unknown palace_*
        # target is -32602 here too, so this path cannot dispatch a write
        # the rewrite would refuse.
        underlying_name = _classify_underlying_tool(tool_name, arguments)
        if not underlying_name:
            return _unknown_target_error(req_id)
        srv = _get_mcp_server()
        # A writer lease the preflight takes lasts this call only (see call_tool).
        lease_held_before = srv._MCP_WRITER_LOCK_CM is not None
        refusal = srv._mcp_tool_preflight_refusal(req_id, underlying_name)
        if refusal is not None:
            return refusal

        try:
            handler = LIGHT_TOOLS[tool_name]["handler"]
            with srv._write_stall_watch(underlying_name):
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
            return srv._internal_tool_error(
                req_id, tool_name, e, include_message=isinstance(e, ValueError)
            )
        finally:
            srv._end_call_writer_lease(lease_held_before)

    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _alias_args_for_names(
    names, params: Dict[str, Any], *, has_var_keyword: bool = False
) -> Dict[str, Any]:
    """Translate PQL field names to an underlying tool's parameter names."""
    mapped = dict(params)
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
    if "old" in mapped and "old_object" in names and "old_object" not in mapped:
        mapped["old_object"] = mapped.pop("old")
    if "new" in mapped and "new_object" in names and "new_object" not in mapped:
        mapped["new_object"] = mapped.pop("new")
    if has_var_keyword:
        return mapped
    return {k: v for k, v in mapped.items() if k in names}


def _alias_args_for_handler(handler, params: Dict[str, Any]) -> Dict[str, Any]:
    """Translate PQL field names to the underlying handler's parameter names."""
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return dict(params)
    names = sig.parameters
    has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in names.values())
    return _alias_args_for_names(names, params, has_var_keyword=has_var_keyword)


def _rewrite_tools_call_for_hub(request: Dict[str, Any], *, read_lock=nullcontext):
    """Translate palace_* tools/call into the underlying mempalace_* JSON-RPC."""
    params = request.get("params") or {}
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
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
        _resolve_wing_params(parsed, read_lock=read_lock)
        if tool_name == "palace_query" and query_target in ("taxonomy", "get_taxonomy"):
            taxonomy_wing = parsed.get("wing")

    underlying = _classify_underlying_tool(tool_name, arguments)
    if not underlying:
        return {"_unknown_target": True}
    if tool_name == "palace_query":
        parsed = _query_arguments(underlying, parsed)
    tool_def = _get_mcp_server().TOOLS.get(underlying) or {}
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


def _postprocess_light_response(
    rewritten: Dict[str, Any],
    resp: Optional[Dict[str, Any]],
    *,
    palace_path: Optional[str] = None,
    read_lock=nullcontext,
):
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
        with read_lock():
            payload = _enrich_search_results(payload, palace_path=palace_path)
    elif underlying == "mempalace_get_taxonomy":
        payload = _filter_taxonomy_payload(payload, taxonomy_wing)
    content[0]["text"] = json.dumps(payload, ensure_ascii=False, indent=2)
    return resp


def _stdio_reply_light(line: str, dispatch) -> Optional[str]:
    """Parse one JSON-RPC line and serialize the response without importing ``mcp_server``."""
    try:
        request = json.loads(line)
    except Exception as exc:
        logger.error("Server error: %s: %s", type(exc).__name__, exc)
        return dumps_client(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        )
    try:
        response = dispatch(request)
    except Exception:
        logger.exception("Server error")
        req_id = request.get("id") if isinstance(request, dict) else None
        if req_id is None:
            return None
        return dumps_client(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": "Internal error"},
            }
        )
    return None if response is None else dumps_client(response)


def _hub_for_light_session(args) -> Optional[HubClient]:
    """Return the live hub for a plain stdio light session, or None."""
    if (
        getattr(args, "read_only", False)
        or getattr(args, "backend", None)
        or getattr(args, "collection", None)
    ):
        return None
    palace_path = getattr(args, "palace", None)
    if not palace_path:
        try:
            palace_path = MempalaceConfig().palace_path
        except Exception:
            return None
    return HubClient.discover(palace_path)


class _LightHubProxy:
    """Thin hub-forwarding dispatcher for ``mempalace-light-mcp``.

    Keeps ``mempalace.mcp_server`` (and with it ``chromadb`` and ``numpy``)
    unloaded while a live hub is serving the palace.
    """

    def __init__(self, hub: HubClient, palace_path: str) -> None:
        self.hub: Optional[HubClient] = hub
        self.base_url = hub.base_url
        self.palace_path = palace_path
        self._tool_schemas: Optional[Dict[str, set]] = None
        self._wings_cache: Optional[list] = None
        self._local_initialized = False

    def _set_hub(self, hub: HubClient) -> None:
        self.hub = hub
        self.base_url = hub.base_url
        self._tool_schemas = None
        self._wings_cache = None

    def _forward(self, request: Dict[str, Any]):
        if self.hub is None:
            raise urllib.error.URLError("hub unavailable")
        return self.hub.post(request)

    def _known_wings(self) -> list:
        if self._wings_cache is not None:
            return self._wings_cache
        resp = self._forward(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "mempalace_list_wings", "arguments": {}},
            }
        )
        try:
            payload = tool_result(resp)
        except (KeyError, IndexError, TypeError, ValueError):
            return []
        wings = payload.get("wings") if isinstance(payload, dict) else None
        if isinstance(wings, dict):
            self._wings_cache = list(wings.keys())
            return self._wings_cache
        if isinstance(wings, list):
            self._wings_cache = list(wings)
            return self._wings_cache
        return []

    def _param_names_for(self, tool_name: str) -> Optional[set]:
        if self._tool_schemas is None:
            resp = self._forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            tools = ((resp or {}).get("result") or {}).get("tools") or []
            schemas: Dict[str, set] = {}
            for entry in tools:
                if not isinstance(entry, dict) or "name" not in entry:
                    continue
                props = ((entry.get("inputSchema") or {}).get("properties")) or {}
                if isinstance(props, dict):
                    schemas[entry["name"]] = set(props.keys())
            self._tool_schemas = schemas
        return self._tool_schemas.get(tool_name)

    def _resolve_wings(self, params: Dict[str, Any]) -> None:
        wing_keys = [
            k for k in ("wing", "source_wing", "target_wing") if isinstance(params.get(k), str)
        ]
        if not wing_keys:
            return
        known = self._known_wings()
        if not known:
            return
        for key in wing_keys:
            params[key] = _match_fuzzy_wing(params[key], known)

    def _fallback_local(self, request: Dict[str, Any], *, degraded: bool = False):
        if not self._local_initialized:
            srv = _get_mcp_server()
            srv._apply_server_flags(palace=self.palace_path or None)
            for start in (srv._start_idle_exit_watchdog, srv._start_write_stall_watchdog):
                try:
                    start()
                except Exception:
                    logger.debug("watchdog did not start", exc_info=True)
            self._local_initialized = True
        resp = dispatch_light_stdio_request(request)
        if degraded:
            from .mcp_proxy import _annotate_degraded

            resp = _annotate_degraded(resp)
        return resp

    def _dispatch_to_hub(
        self,
        request: Dict[str, Any],
        tool_name: str,
        query_target: Optional[str],
        underlying: str,
        raw_parsed: Dict[str, Any],
    ):
        parsed = dict(raw_parsed)
        taxonomy_wing = None
        if tool_name in ("palace_query", "palace_exec"):
            self._resolve_wings(parsed)
            if tool_name == "palace_query" and query_target in ("taxonomy", "get_taxonomy"):
                taxonomy_wing = parsed.get("wing")
        if tool_name == "palace_query":
            parsed = _query_arguments(underlying, parsed)

        schema_names = self._param_names_for(underlying)
        if schema_names is not None:
            parsed = _alias_args_for_names(schema_names, parsed)
        forwarded = {
            "jsonrpc": request.get("jsonrpc", "2.0"),
            "id": request.get("id"),
            "method": "tools/call",
            "params": {"name": underlying, "arguments": parsed},
        }
        resp = self._forward(forwarded)
        return forwarded, taxonomy_wing, resp

    def handle(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            }
        method = request.get("method") or ""
        if method != "tools/call":
            return handle_light_request(request)

        params = request.get("params") or {}
        if not isinstance(params, dict) or "name" not in params:
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32602, "message": "Invalid params: name required"},
            }
        tool_name = params["name"]
        if tool_name not in LIGHT_TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }

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
            payload = {"success": False, "error": f"PQL parse error: {exc}"}
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}
                    ]
                },
            }

        underlying = _classify_underlying_tool(tool_name, arguments)
        if not underlying:
            return _unknown_target_error(request.get("id"))

        if underlying in SEARCH_TOOL_NAMES and search_settings_overridden():
            return self._fallback_local(request, degraded=False)

        if self.hub is None:
            rediscovered = HubClient.discover(self.palace_path) if self.palace_path else None
            if rediscovered is None:
                return self._fallback_local(request, degraded=True)
            self._set_hub(rediscovered)

        try:
            forwarded, taxonomy_wing, resp = self._dispatch_to_hub(
                request, tool_name, query_target, underlying, parsed
            )
        except FORWARD_ERRORS as exc:
            failed_url = self.base_url
            if isinstance(exc, urllib.error.HTTPError):
                return proxy_error_response(request, failed_url, exc)
            self.hub = None
            self._tool_schemas = None
            self._wings_cache = None
            if underlying in READ_ONLY_REFUSED_TOOLS:
                return proxy_error_response(request, failed_url, exc)
            restarted = HubClient.discover(self.palace_path) if self.palace_path else None
            if restarted is not None and restarted.base_url != failed_url:
                self._set_hub(restarted)
                try:
                    forwarded, taxonomy_wing, resp = self._dispatch_to_hub(
                        request, tool_name, query_target, underlying, parsed
                    )
                except FORWARD_ERRORS as retry_exc:
                    self.hub = None
                    if isinstance(retry_exc, urllib.error.HTTPError):
                        return proxy_error_response(request, self.base_url, retry_exc)
                    logger.warning(
                        "Hub at %s unreachable (%s); handling request locally",
                        self.base_url,
                        retry_exc,
                    )
                    return self._fallback_local(request, degraded=True)
            else:
                logger.warning(
                    "Hub at %s unreachable (%s); handling request locally", failed_url, exc
                )
                return self._fallback_local(request, degraded=True)

        if underlying == "mempalace_status":
            from .mcp_proxy import _annotate_forwarded_update_status

            resp = _annotate_forwarded_update_status(forwarded, resp)
        rewritten = {
            "params": {"name": underlying},
            "_taxonomy_wing": taxonomy_wing,
        }
        return _postprocess_light_response(rewritten, resp, palace_path=self.palace_path)


def dispatch_light_stdio_request(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate palace_* calls and dispatch them through the stdio transport."""
    return _dispatch_light_request(request, _dispatch_light_stdio_tool)


def _dispatch_light_stdio_tool(request):
    srv = _get_mcp_server()
    req_id = request.get("id")
    tool_name = request["params"]["name"]
    # The destination owns the lease. Check local restrictions before forwarding
    # without competing for the hub's writer lock.
    refusal = srv._mcp_tool_preflight_refusal(req_id, tool_name, check_writer=False)
    if refusal is not None:
        return refusal
    try:
        return srv._dispatch_stdio_request(request)
    except Exception as exc:
        return srv._internal_tool_error(
            req_id, tool_name, exc, include_message=isinstance(exc, ValueError)
        )


def _dispatch_light_request(request, dispatch, *, read_lock=nullcontext):
    """Translate the light catalog to core tools before dispatching through a transport."""
    if (
        isinstance(request, dict)
        and request.get("jsonrpc") == "2.0"
        and request.get("method") == "tools/call"
    ):
        rewritten = _rewrite_tools_call_for_hub(request, read_lock=read_lock)
        if isinstance(rewritten, dict) and rewritten.get("_unknown_target"):
            return _unknown_target_error(request.get("id"))
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
            return _postprocess_light_response(rewritten, dispatch(forwarded), read_lock=read_lock)
    return handle_light_request(request)


def _run_light_hub_proxy(hub: HubClient, palace_path: str) -> None:
    proxy = _LightHubProxy(hub, palace_path)
    logger.info(
        "MemPalace Lightweight MCP Server proxying to hub %s (3 consolidated tools)...",
        hub.base_url,
    )
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
        payload = _stdio_reply_light(line, proxy.handle)
        if payload is None:
            continue
        try:
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            break


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

    _restore_stdout()
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass

    hub = _hub_for_light_session(args)
    if hub is not None:
        _run_light_hub_proxy(hub, hub.palace_path)
        return

    srv = _get_mcp_server()
    srv._apply_server_flags(palace=args.palace, backend=args.backend, read_only=args.read_only)
    if args.collection:
        srv._config.collection_name = args.collection

    # Run stdio protocol loop with lightweight dispatcher
    logger.info("MemPalace Lightweight MCP Server starting (3 consolidated tools)...")
    srv._maybe_eager_warmup_embedder()
    srv._start_idle_exit_watchdog()
    srv._start_write_stall_watchdog()

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
        payload = srv._stdio_reply(line, dispatch_light_stdio_request)
        if payload is None:
            continue
        try:
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            break


if __name__ == "__main__":
    main()
