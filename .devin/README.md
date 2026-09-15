# Devin integration for MemPalace

This directory contains hooks and configuration for using MemPalace with the
[Devin](https://devin.ai) agent platform.

## What it does

Devin surfaces every `mcp_call_tool` invocation to PreToolUse hooks before the
call reaches an MCP server. If the arguments are invalid, the MCP server returns
a JSON-RPC error, but Devin's client currently reports that as:

```
Failed to connect to MCP server 'mempalace'. Please try again.
```

That message is a connectivity error, not a parameter validation error, so it is
hard to debug. The `mcp_validate.py` hook validates tool arguments against the
server's declared JSON Schema **before** Devin tries to connect, and returns a
human-readable validation message instead.

## Installing the PreToolUse hook

1. Copy `hooks/mcp_validate.py` to your Devin hooks directory:

   ```bash
   mkdir -p ~/.devin/hooks
   cp .devin/hooks/mcp_validate.py ~/.devin/hooks/mcp_validate.py
   ```

2. Add it to `~/.config/devin/config.json` under `hooks.PreToolUse`:

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "mcp_call_tool",
           "hooks": [
             {
               "type": "command",
               "command": "python3 ~/.devin/hooks/mcp_validate.py",
               "timeout": 30
             }
           ]
         }
       ]
     }
   }
   ```

   Make sure `mcp_validate.py` runs **before** any other `mcp_call_tool` hooks
   (such as server whitelists or research guards), so validation happens first.

## How it works

- The hook reads your Devin MCP server configuration from the usual locations:
  - `.devin/config.json` and `.devin/config.local.json`
  - `.devin/mcp_config.json` and `.devin/mcp_config.local.json`
  - `~/.config/devin/config.json` and `~/.config/devin/config.local.json`
  - `~/.config/devin/mcp_config.json` and `~/.config/devin/mcp_config.local.json`

  Later files override earlier ones, matching Devin's own config loading.

- It calls `tools/list` on the target server, caches the result under
  `~/.devin/cache/mcp_schemas/` for five minutes, and validates the pending
  tool call against the tool's `inputSchema`.

- For **HTTP/SSE servers** (such as the MemPalace HTTP transport), it POSTs the
  JSON-RPC `initialize` and `tools/list` requests directly. For **stdio servers**,
  it spawns the configured command once, fetches the tool list, and then stops
  the process.

- If the server is unreachable or its tool list cannot be fetched, the hook
  approves the call and lets Devin surface the real connectivity error — it does
  not mask connection problems with synthetic validation failures.

## What gets validated

- Missing `required` fields.
- Unknown fields (unless the handler accepts `**kwargs`).
- Basic JSON-Schema types: `string`, `number`, `integer`, `boolean`, `array`,
  `object`.

Errors are reported as, for example:

```
Invalid arguments for mempalace/mempalace_kg_query: missing required field: entity; unknown field: entitty
```

## Project-level vs. user-level hooks

You can place the hook in a project at `.devin/hooks/mcp_validate.py` and point
`command` at the project-local path. For a cross-project setup, keep it in
`~/.devin/hooks/mcp_validate.py` and reference it from `~/.config/devin/config.json`.
