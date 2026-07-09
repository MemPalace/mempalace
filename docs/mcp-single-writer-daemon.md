# MCP single-writer daemon bridge

Issue #1963 tracks the recurring HNSW divergence cluster caused by multiple
local MCP/CLI/hook processes opening the same ChromaDB `PersistentClient`.
The safe local topology is one long-lived MemPalace process owning ChromaDB,
with every MCP client talking to that owner through a small stdio bridge.

## Commands

Default MCP command:

    mempalace-mcp

`mempalace-mcp` is now the safe bridge. It auto-starts one private daemon per
palace and relays JSON-RPC over a Unix domain socket.

Raw legacy stdio server:

    mempalace-mcp-stdio

Use this only for debugging or single-session scenarios. It opens the palace
directly in the client-owned process.

Explicit bridge and daemon commands:

    mempalace-mcp-bridge
    mempalace-mcp-daemon

## Environment

`MEMPALACE_MCP_DAEMON_STATE_ROOT` changes where per-palace socket state is
stored. By default it is `~/.mempalace/mcp`.

`MEMPALACE_MCP_SOCKET` overrides the socket path.

`MEMPALACE_MCP_DISABLE_DAEMON=1` makes `mempalace-mcp` delegate to the raw
stdio server for emergency rollback.

## Concurrency contract

The daemon imports `mempalace.mcp_server.handle_request()` once. Protocol
messages such as `initialize`, `ping`, notifications, and `tools/list` remain
lock-free. Every `tools/call` request is serialized through one process-wide
lock, so lazy reads, writes, and maintenance calls all observe the same single
ChromaDB owner.
