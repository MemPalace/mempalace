"""
palace_graph.py — Graph traversal layer for MemPalace
======================================================

Builds a navigable graph from the palace structure:
  - Nodes = rooms (named ideas)
  - Edges = shared rooms across wings (tunnels)
  - Edge types = halls (the corridors)

Enables queries like:
  "Start at chromadb-setup in wing_code, walk to wing_myproject"
  "Find all rooms connected to riley-college-apps"
  "What topics bridge wing_hardware and wing_myproject?"

No external graph DB needed — built from ChromaDB metadata.
"""

import hashlib
import json
import os
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .config import MempalaceConfig
from .palace import get_collection as _get_palace_collection
from .palace import mine_lock

# Module-level graph cache with TTL and write-invalidation.
# Warm cache serves build_graph() in O(1); invalidate_graph_cache() clears on writes.
_graph_cache_lock = threading.Lock()
_graph_cache_nodes = None
_graph_cache_edges = None
_graph_cache_time = 0.0
_GRAPH_CACHE_TTL = 60.0  # seconds — graph changes less often than metadata


def invalidate_graph_cache():
    """Clear the graph cache. Called from mcp_server.py on writes."""
    global _graph_cache_nodes, _graph_cache_edges, _graph_cache_time
    with _graph_cache_lock:
        _graph_cache_nodes = None
        _graph_cache_edges = None
        _graph_cache_time = 0.0


def _get_collection(config=None):
    config = config or MempalaceConfig()
    try:
        return _get_palace_collection(
            config.palace_path,
            collection_name=config.collection_name,
            create=False,
        )
    except Exception:
        return None


def build_graph(col=None, config=None):
    """
    Build the palace graph from ChromaDB metadata.

    Returns cached result if fresh (within TTL). Cache is invalidated
    on writes via invalidate_graph_cache(). Thread-safe via _graph_cache_lock.

    Note: warm cache ignores ``col`` and ``config`` arguments — this is
    intentional for the MCP server's single-palace use case. Callers
    switching collections should call ``invalidate_graph_cache()`` first.

    Returns:
        nodes: dict of {room: {wings: set, halls: set, count: int}}
        edges: list of {room, wing_a, wing_b, hall} — one per tunnel crossing
    """
    global _graph_cache_nodes, _graph_cache_edges, _graph_cache_time
    now = time.time()
    # NOTE: warm cache ignores col/config args — intentional for the MCP server's
    # single-palace use case. Callers switching collections must invalidate first.
    with _graph_cache_lock:
        if _graph_cache_nodes is not None and (now - _graph_cache_time) < _GRAPH_CACHE_TTL:
            return _graph_cache_nodes, _graph_cache_edges

    if col is None:
        col = _get_collection(config)
    if not col:
        return {}, []

    total = col.count()
    room_data = defaultdict(lambda: {"wings": set(), "halls": set(), "count": 0, "dates": set()})

    offset = 0
    while offset < total:
        batch = col.get(limit=1000, offset=offset, include=["metadatas"])
        for meta in batch["metadatas"]:
            room = meta.get("room", "")
            wing = meta.get("wing", "")
            hall = meta.get("hall", "")
            date = meta.get("date", "")
            if room and room != "general" and wing:
                room_data[room]["wings"].add(wing)
                if hall:
                    room_data[room]["halls"].add(hall)
                if date:
                    room_data[room]["dates"].add(date)
                room_data[room]["count"] += 1
        if not batch["ids"]:
            break
        offset += len(batch["ids"])

    # Build edges from rooms that span multiple wings
    edges = []
    for room, data in room_data.items():
        wings = sorted(data["wings"])
        if len(wings) >= 2:
            for i, wa in enumerate(wings):
                for wb in wings[i + 1 :]:
                    for hall in data["halls"]:
                        edges.append(
                            {
                                "room": room,
                                "wing_a": wa,
                                "wing_b": wb,
                                "hall": hall,
                                "count": data["count"],
                            }
                        )

    # Convert sets to lists for JSON serialization
    nodes = {}
    for room, data in room_data.items():
        nodes[room] = {
            "wings": sorted(data["wings"]),
            "halls": sorted(data["halls"]),
            "count": data["count"],
            "dates": sorted(data["dates"])[-5:] if data["dates"] else [],
        }

    # Only cache non-empty graphs so new data is picked up immediately
    # when the palace is first populated.
    if nodes:
        with _graph_cache_lock:
            _graph_cache_nodes = nodes
            _graph_cache_edges = edges
            _graph_cache_time = time.time()

    return nodes, edges


def traverse(start_room: str, col=None, config=None, max_hops: int = 2):
    """
    Walk the graph from a starting room. Find connected rooms
    through shared wings.

    Returns list of paths: [{room, wing, hall, hop_distance}]
    """
    nodes, edges = build_graph(col, config)

    if start_room not in nodes:
        return {
            "error": f"Room '{start_room}' not found",
            "suggestions": _fuzzy_match(start_room, nodes),
        }

    start = nodes[start_room]
    visited = {start_room}
    results = [
        {
            "room": start_room,
            "wings": start["wings"],
            "halls": start["halls"],
            "count": start["count"],
            "hop": 0,
        }
    ]

    # BFS traversal
    frontier = [(start_room, 0)]
    while frontier:
        current_room, depth = frontier.pop(0)
        if depth >= max_hops:
            continue

        current = nodes.get(current_room, {})
        current_wings = set(current.get("wings", []))

        # Find all rooms that share a wing with current room
        for room, data in nodes.items():
            if room in visited:
                continue
            shared_wings = current_wings & set(data["wings"])
            if shared_wings:
                visited.add(room)
                results.append(
                    {
                        "room": room,
                        "wings": data["wings"],
                        "halls": data["halls"],
                        "count": data["count"],
                        "hop": depth + 1,
                        "connected_via": sorted(shared_wings),
                    }
                )
                if depth + 1 < max_hops:
                    frontier.append((room, depth + 1))

    # Sort by relevance (hop distance, then count)
    results.sort(key=lambda x: (x["hop"], -x["count"]))
    return results[:50]  # cap results


def find_tunnels(wing_a: str = None, wing_b: str = None, col=None, config=None):
    """
    Find rooms that connect two wings (or all tunnel rooms if no wings specified).
    These are the "hallways" — same named idea appearing in multiple domains.
    """
    nodes, edges = build_graph(col, config)

    tunnels = []
    for room, data in nodes.items():
        wings = data["wings"]
        if len(wings) < 2:
            continue

        if wing_a and wing_a not in wings:
            continue
        if wing_b and wing_b not in wings:
            continue

        tunnels.append(
            {
                "room": room,
                "wings": wings,
                "halls": data["halls"],
                "count": data["count"],
                "recent": data["dates"][-1] if data["dates"] else "",
            }
        )

    tunnels.sort(key=lambda x: -x["count"])
    return tunnels[:50]


def graph_stats(col=None, config=None):
    """Summary statistics about the palace graph."""
    nodes, edges = build_graph(col, config)

    tunnel_rooms = sum(1 for n in nodes.values() if len(n["wings"]) >= 2)
    wing_counts = Counter()
    for data in nodes.values():
        for w in data["wings"]:
            wing_counts[w] += 1

    return {
        "total_rooms": len(nodes),
        "tunnel_rooms": tunnel_rooms,
        "total_edges": len(edges),
        "rooms_per_wing": dict(wing_counts.most_common()),
        "top_tunnels": [
            {"room": r, "wings": d["wings"], "count": d["count"]}
            for r, d in sorted(nodes.items(), key=lambda x: -len(x[1]["wings"]))[:10]
            if len(d["wings"]) >= 2
        ],
    }


def _fuzzy_match(query: str, nodes: dict, n: int = 5):
    """Find rooms that approximately match a query string."""
    query_lower = query.lower()
    scored = []
    for room in nodes:
        # Simple substring matching
        if query_lower in room:
            scored.append((room, 1.0))
        elif any(word in room for word in query_lower.split("-")):
            scored.append((room, 0.5))
    scored.sort(key=lambda x: -x[1])
    return [r for r, _ in scored[:n]]


# =============================================================================
# EXPLICIT TUNNELS — agent-created cross-wing links
# =============================================================================
# Passive tunnels are discovered from shared room names across wings.
# Explicit tunnels are created by agents when they notice a connection
# between two specific drawers or rooms in different wings/projects.
#
# Stored as a JSON file at ~/.mempalace/tunnels.json so they persist
# across palace rebuilds (not in ChromaDB which can be recreated).


_TUNNEL_FILE = os.path.join(os.path.expanduser("~"), ".mempalace", "tunnels.json")


def _load_tunnels():
    """Load explicit tunnels from disk.

    Returns an empty list if the file is missing or corrupt (e.g. truncated
    by a crash mid-write on a system that lacks atomic-rename semantics).
    """
    if not os.path.exists(_TUNNEL_FILE):
        return []
    try:
        with open(_TUNNEL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _save_tunnels(tunnels):
    """Persist explicit tunnels atomically.

    Writes to ``tunnels.json.tmp`` then ``os.replace``s it into place, so
    a crash mid-write can never leave a partial/empty tunnels.json that
    silently wipes every tunnel on next read.
    """
    os.makedirs(os.path.dirname(_TUNNEL_FILE), exist_ok=True)
    tmp_path = _TUNNEL_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(tunnels, f, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            # Not all filesystems (or Windows file handles) support fsync — tolerate.
            pass
    os.replace(tmp_path, _TUNNEL_FILE)


def _endpoint_key(wing: str, room: str) -> str:
    return f"{wing}/{room}"


def _canonical_tunnel_id(
    source_wing: str, source_room: str, target_wing: str, target_room: str
) -> str:
    """Compute a symmetric tunnel ID.

    Tunnels are conceptually undirected — "auth relates to users" is the
    same connection as "users relates to auth". Sort the two endpoints
    before hashing so ``create_tunnel(A, B)`` and ``create_tunnel(B, A)``
    resolve to the same ID and dedup into one record.
    """
    src = _endpoint_key(source_wing, source_room)
    tgt = _endpoint_key(target_wing, target_room)
    a, b = sorted((src, tgt))
    return hashlib.sha256(f"{a}↔{b}".encode()).hexdigest()[:16]


def _require_name(value: str, field: str) -> str:
    """Reject empty / non-string endpoint identifiers."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def create_tunnel(
    source_wing: str,
    source_room: str,
    target_wing: str,
    target_room: str,
    label: str = "",
    source_drawer_id: str = None,
    target_drawer_id: str = None,
):
    """Create an explicit (symmetric) tunnel between two locations in the palace.

    Tunnels are undirected: ``create_tunnel(A, B)`` and ``create_tunnel(B, A)``
    resolve to the same canonical ID. A second call with the same endpoints
    updates the stored label (and drawer IDs, if provided) rather than
    creating a duplicate.

    The ``source`` / ``target`` fields on the returned dict preserve the
    argument order the caller used, so callers can display it directionally
    if they like. The ID and dedup are symmetric.

    Args:
        source_wing: Wing of the source (e.g., "project_api").
        source_room: Room in the source wing.
        target_wing: Wing of the target (e.g., "project_database").
        target_room: Room in the target wing.
        label: Description of the connection.
        source_drawer_id: Optional specific drawer ID.
        target_drawer_id: Optional specific drawer ID.

    Returns:
        The stored tunnel dict.

    Raises:
        ValueError: if any wing or room is empty or non-string.
    """
    source_wing = _require_name(source_wing, "source_wing")
    source_room = _require_name(source_room, "source_room")
    target_wing = _require_name(target_wing, "target_wing")
    target_room = _require_name(target_room, "target_room")

    tunnel_id = _canonical_tunnel_id(source_wing, source_room, target_wing, target_room)

    tunnel = {
        "id": tunnel_id,
        "source": {"wing": source_wing, "room": source_room},
        "target": {"wing": target_wing, "room": target_room},
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if source_drawer_id:
        tunnel["source"]["drawer_id"] = source_drawer_id
    if target_drawer_id:
        tunnel["target"]["drawer_id"] = target_drawer_id

    # Serialize the load → mutate → save cycle. Without this, two concurrent
    # create_tunnel calls can both read the same snapshot and the later
    # writer silently drops the earlier writer's tunnel.
    with mine_lock(_TUNNEL_FILE):
        tunnels = _load_tunnels()
        for existing in tunnels:
            if existing.get("id") == tunnel_id:
                # Preserve original creation timestamp on label updates.
                tunnel["created_at"] = existing.get("created_at", tunnel["created_at"])
                tunnel["updated_at"] = datetime.now(timezone.utc).isoformat()
                existing.clear()
                existing.update(tunnel)
                _save_tunnels(tunnels)
                return existing
        tunnels.append(tunnel)
        _save_tunnels(tunnels)
    return tunnel


def list_tunnels(wing: str = None):
    """List all explicit tunnels, optionally filtered by wing.

    Returns tunnels where ``wing`` appears as either source or target
    (tunnels are symmetric, so either endpoint is a valid filter match).
    """
    tunnels = _load_tunnels()
    if wing:
        tunnels = [t for t in tunnels if t["source"]["wing"] == wing or t["target"]["wing"] == wing]
    return tunnels


def delete_tunnel(tunnel_id: str):
    """Delete an explicit tunnel by ID. Returns ``{"deleted": <id>}``."""
    with mine_lock(_TUNNEL_FILE):
        tunnels = _load_tunnels()
        tunnels = [t for t in tunnels if t.get("id") != tunnel_id]
        _save_tunnels(tunnels)
    return {"deleted": tunnel_id}


def follow_tunnels(wing: str, room: str, col=None, config=None):
    """Follow explicit tunnels from a room — returns connected drawers.

    Given a location (wing/room), finds all tunnels leading from or to it,
    and optionally fetches the connected drawer content.
    """
    tunnels = _load_tunnels()
    connections = []

    for t in tunnels:
        src = t["source"]
        tgt = t["target"]

        if src["wing"] == wing and src["room"] == room:
            connections.append(
                {
                    "direction": "outgoing",
                    "connected_wing": tgt["wing"],
                    "connected_room": tgt["room"],
                    "label": t.get("label", ""),
                    "drawer_id": tgt.get("drawer_id"),
                    "tunnel_id": t["id"],
                }
            )
        elif tgt["wing"] == wing and tgt["room"] == room:
            connections.append(
                {
                    "direction": "incoming",
                    "connected_wing": src["wing"],
                    "connected_room": src["room"],
                    "label": t.get("label", ""),
                    "drawer_id": src.get("drawer_id"),
                    "tunnel_id": t["id"],
                }
            )

    # If we have a collection, fetch drawer content for connected items
    if col and connections:
        drawer_ids = [c["drawer_id"] for c in connections if c.get("drawer_id")]
        if drawer_ids:
            try:
                results = col.get(ids=drawer_ids, include=["documents", "metadatas"])
                drawer_map = dict(zip(results["ids"], results["documents"]))
                for c in connections:
                    did = c.get("drawer_id")
                    if did and did in drawer_map:
                        c["drawer_preview"] = drawer_map[did][:300]
            except Exception:
                pass

    return connections
