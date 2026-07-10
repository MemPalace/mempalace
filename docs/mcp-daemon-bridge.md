# Daemon-backed MCP bridge

This PR turns the daemon + bridge direction from #1270 into a package-level
integration that reuses the existing `mempalace.daemon` queue/server.

## Scope

This is a production integration slice for the Tier 3 work tracked in #1963.

It does not rewrite `mine_palace_lock()`. External direct writers still fail
fast when they collide, preserving the existing anti-corruption contract.

Instead, routed clients share one local daemon owner:

- MCP stdio clients use `mempalace-mcp`, which now bridges to the daemon.
- CLI writes can already use the daemon with the existing daemon-backed CLI paths.
- Hook writes can already use daemon-backed execution when hook daemon mode is enabled.

Together with #1270's daemon + bridge direction, this provides the package-level
path toward one ChromaDB-owning process.

## Commands

Default MCP command:

    mempalace-mcp

Emergency direct stdio fallback:

    MEMPALACE_MCP_DISABLE_DAEMON=1 mempalace-mcp

Raw stdio command:

    mempalace-mcp-stdio

## Safety properties

- The bridge does not import ChromaDB or `mempalace.mcp_server`.
- The existing daemon owns the MCP server import and the ChromaDB client.
- Daemon queued jobs and MCP `tools/call` requests share one in-process writer lock.
- Protocol traffic such as `initialize`, `ping`, and notifications remains outside the writer lock.
- The daemon sets `MEMPALACE_MCP_ALLOW_PEER_WRITER=1` before importing `mempalace.mcp_server`, so the daemon does not hold the legacy server-lifetime peer-writer lease merely by existing.
- The bridge validates daemon palace/backend identity before attaching.
- MCP read-only identity is fixed on first daemon MCP use and mismatches are refused.

## Notes for #1963

This PR contributes to #1963; it should not close the tracking epic by itself.
The final Tier 3 state also requires installer/config follow-through so hooks
and CLI workflows consistently use the daemon-backed paths.
