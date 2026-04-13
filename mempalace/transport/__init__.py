"""Transport layer — how the MCP JSON-RPC dispatcher receives and returns bytes.

Two concrete transports:
  - stdio  (default, backward compatible): sys.stdin / sys.stdout
  - http   (new): FastAPI + uvicorn

Both share the same handle_request() dispatcher from mempalace.mcp_server.
"""
