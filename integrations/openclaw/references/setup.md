# MemPalace setup for OpenClaw

Install and initialize MemPalace, then connect the MCP server from your host.

```bash
uv tool install mempalace   # or: pip install mempalace
mempalace init ~/my-convos
mempalace mine ~/my-convos
```

OpenClaw MCP config:

```json
{
  "mcpServers": {
    "mempalace": {
      "command": "python3",
      "args": ["-m", "mempalace.mcp_server"]
    }
  }
}
```

Equivalent CLI form:

```bash
openclaw mcp set mempalace '{"command":"python3","args":["-m","mempalace.mcp_server"]}'
```

Other MCP hosts can run the same server command, for example:

```bash
claude mcp add mempalace -- python -m mempalace.mcp_server
```
