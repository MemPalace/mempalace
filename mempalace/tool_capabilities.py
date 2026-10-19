"""What each MCP tool does to palace state, and the gate sets derived from it.

Kept free of imports on purpose: the daemon, the CLI, and the HTTP transport
classify tools without importing ``mempalace.mcp_server``, whose import
redirects stdout and loads the vector stack.

Each tool is ``read``, ``write``, or ``maintenance``, plus flags:

* ``mutating``: changes palace state, so it needs the peer-writer lease.
* ``peer_exempt``: mutating, but writes only logstream.sqlite3 or the knowledge
  graph, never Chroma, so the lease that serialises two Chroma clients has no
  claim on it. Read-only mode still refuses it.
* ``vector_write``: mutating and reaches the HNSW segment, where a write into a
  diverged index blocks inside chromadb with no timeout.
* ``read_only_allowed``: not a read, but read-only mode keeps it. Reconnect is
  the only way to pick up an external writer's changes.
* ``integrity_allowed``: callable while chroma.sqlite3 is corrupt, because it
  never touches it (logstream, mesh state, status, reconnect).
* ``lock_free``: the HTTP transport runs it outside the request lock (its own
  database or no palace state); a five-minute event_wait must not hold it.
* ``embedding_release``: HTTP drops the request lock around its one embedding call.
* ``yielding``: HTTP lets queued requests run between its steps (a mine between files).

Read-only mode refuses every tool that is not a read, unless it is
``read_only_allowed``. That covers hook_settings, which writes config.json, and
memories_filed_away, which unlinks the checkpoint ack: writes the peer-writer
lease does not arbitrate.
"""

TOOL_CAPABILITIES = {
    "mempalace_status": ("read", "integrity_allowed"),
    "mempalace_list_wings": ("read", ""),
    "mempalace_list_rooms": ("read", ""),
    "mempalace_get_taxonomy": ("read", ""),
    "mempalace_get_aaak_spec": ("read", "lock_free"),
    "mempalace_kg_query": ("read", "lock_free"),
    "mempalace_kg_add": ("write", "mutating peer_exempt lock_free"),
    "mempalace_kg_invalidate": ("write", "mutating peer_exempt lock_free"),
    "mempalace_kg_supersede": ("write", "mutating peer_exempt lock_free"),
    "mempalace_kg_timeline": ("read", "lock_free"),
    "mempalace_kg_stats": ("read", "lock_free"),
    "mempalace_traverse": ("read", ""),
    "mempalace_find_tunnels": ("read", ""),
    "mempalace_graph_stats": ("read", ""),
    "mempalace_mesh_peers": ("read", "lock_free integrity_allowed"),
    "mempalace_create_tunnel": ("write", "mutating"),
    "mempalace_list_tunnels": ("read", ""),
    "mempalace_delete_tunnel": ("write", "mutating"),
    "mempalace_list_hallways": ("read", ""),
    "mempalace_delete_hallway": ("write", "mutating"),
    "mempalace_follow_tunnels": ("read", ""),
    "mempalace_search": ("read", "embedding_release"),
    "mempalace_check_duplicate": ("read", "embedding_release"),
    "mempalace_add_drawer": ("write", "mutating vector_write"),
    "mempalace_checkpoint": ("write", "mutating vector_write"),
    "mempalace_delete_drawer": ("write", "mutating vector_write"),
    "mempalace_delete_drawers": ("write", "mutating vector_write"),
    "mempalace_mine": ("maintenance", "mutating vector_write yielding"),
    "mempalace_delete_by_source": ("write", "mutating vector_write"),
    "mempalace_sync": ("maintenance", "mutating vector_write"),
    "mempalace_get_drawer": ("read", ""),
    "mempalace_get_drawers": ("read", ""),
    "mempalace_list_drawers": ("read", ""),
    "mempalace_update_drawer": ("write", "mutating vector_write"),
    "mempalace_diary_write": ("write", "mutating vector_write embedding_release"),
    "mempalace_diary_read": ("read", ""),
    "mempalace_hook_settings": ("write", ""),
    "mempalace_memories_filed_away": ("write", ""),
    "mempalace_reconnect": ("maintenance", "integrity_allowed read_only_allowed"),
    "mempalace_event_append": ("write", "mutating peer_exempt lock_free integrity_allowed"),
    "mempalace_task_create": ("write", "mutating peer_exempt lock_free integrity_allowed"),
    "mempalace_event_list": ("read", "lock_free integrity_allowed"),
    "mempalace_event_wait": ("read", "lock_free integrity_allowed"),
    "mempalace_event_ack": ("write", "mutating peer_exempt lock_free integrity_allowed"),
    "mempalace_artifact_put": ("write", "mutating peer_exempt lock_free integrity_allowed"),
    "mempalace_artifact_get": ("read", "lock_free integrity_allowed"),
    "mempalace_patch_submit": ("write", "mutating peer_exempt lock_free integrity_allowed"),
}

_KNOWN_FLAGS = {
    "mutating",
    "peer_exempt",
    "vector_write",
    "read_only_allowed",
    "integrity_allowed",
    "lock_free",
    "embedding_release",
    "yielding",
}
_FLAGS = {name: frozenset(flags.split()) for name, (_, flags) in TOOL_CAPABILITIES.items()}


def _check_registry() -> None:
    for name, (capability, _) in TOOL_CAPABILITIES.items():
        flags = _FLAGS[name]
        if capability not in ("read", "write", "maintenance"):
            raise RuntimeError(f"{name}: unknown capability {capability!r}")
        if flags - _KNOWN_FLAGS:
            raise RuntimeError(f"{name}: unknown flags {sorted(flags - _KNOWN_FLAGS)}")
        if flags & {"peer_exempt", "vector_write"} and "mutating" not in flags:
            raise RuntimeError(f"{name}: peer_exempt and vector_write imply mutating")


_check_registry()


def _tools(capability=None, flag=None) -> frozenset:
    return frozenset(
        name
        for name, (cap, _) in TOOL_CAPABILITIES.items()
        if (capability is None or cap == capability) and (flag is None or flag in _FLAGS[name])
    )


READ_TOOLS = _tools(capability="read")
WRITE_TOOLS = _tools(capability="write")
MUTATING_TOOLS = _tools(flag="mutating")
PEER_WRITER_EXEMPT_TOOLS = _tools(flag="peer_exempt")
VECTOR_WRITE_TOOLS = _tools(flag="vector_write")
READ_ONLY_REFUSED_TOOLS = (
    frozenset(TOOL_CAPABILITIES) - READ_TOOLS - _tools(flag="read_only_allowed")
)
SQLITE_INTEGRITY_ALLOWED_TOOLS = _tools(flag="integrity_allowed")
HTTP_LOCK_FREE_TOOLS = _tools(flag="lock_free")
HTTP_EMBEDDING_RELEASE_TOOLS = _tools(flag="embedding_release")
HTTP_YIELDING_TOOLS = _tools(flag="yielding")


def classify_tool(name: str) -> str:
    """Return ``read``, ``write``, ``maintenance``, or ``unknown`` for an MCP tool."""
    entry = TOOL_CAPABILITIES.get(name)
    return entry[0] if entry else "unknown"
