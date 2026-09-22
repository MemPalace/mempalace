# Devin integration for MemPalace

Hooks and configuration for using MemPalace with the [Devin](https://devin.ai)
agent platform. Layout follows the sibling `hooks/cursor/` and
`hooks/antigravity/` integrations.

## What it does

Devin surfaces every MCP invocation to PreToolUse hooks before the call reaches
the server — both the `mcp_call_tool` wrapper and direct
`mcp__<server>__<tool>` tool names. If the arguments are invalid, the server
returns a JSON-RPC `-32602` error, but Devin's client currently reports that as:

```
Failed to connect to MCP server 'mempalace'. Please try again.
```

That message is a connectivity error, not a parameter validation error, so it is
hard to debug. The `mcp_validate.py` hook validates tool arguments against the
server's declared JSON Schema **before** Devin tries to connect, and returns a
human-readable validation message instead.

## Installing the PreToolUse hook

1. Copy `mcp_validate.py` to your Devin hooks directory:

   ```bash
   mkdir -p ~/.devin/hooks
   cp hooks/devin/mcp_validate.py ~/.devin/hooks/mcp_validate.py
   ```

2. Add it to `~/.config/devin/config.json` under `hooks.PreToolUse`:

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "mcp",
           "hooks": [
             {
               "type": "command",
               "command": "python3 ~/.devin/hooks/mcp_validate.py",
               "timeout": 2
             }
           ]
         }
       ]
     }
   }
   ```

   The `mcp` matcher catches both `mcp_call_tool` and `mcp__<server>__<tool>`.
   Keep the timeout tight — the hook's HTTP budget is under 500ms and it fails
   open, so a short timeout cannot strand a tool call.

## How it works

- **Scope**: only servers that resolve to MemPalace (server name, `url`,
  `command`, or `args` matching `mempalace`) are validated. All other MCP
  servers are approved untouched.

- **Config resolution** mirrors Devin's precedence: `$DEVIN_PROJECT_DIR/.devin/`
  first, then the user directory (`~/.config/devin`, or `%APPDATA%\devin` on
  Windows). Within each level: `config.local.json`, `config.json`,
  `mcp_config.local.json`, `mcp_config.json`. JSONC comments are tolerated.

- **Schema fetch**: for HTTP-configured servers the hook POSTs `initialize` /
  `notifications/initialized` / `tools/list` over the streamable-HTTP transport,
  forwarding the negotiated `mcp-session-id` and protocol version. Results are
  cached for five minutes under `~/.cache/devin/mcp_schemas/`
  (`%APPDATA%\devin\mcp_schemas` on Windows), keyed by a fingerprint of the
  resolved server config and project root — so two projects defining
  `mempalace` differently never share a cache entry.

- **stdio configs are never spawned.** A hook must not start server processes;
  command-based MemPalace configs are approved unvalidated.

- **Fail open**: unreachable server, missing config, malformed schema, timeouts
  — all approve the call and let Devin surface the real error.

## What gets validated

Deliberately narrower than full JSON Schema, matching what the MemPalace server
itself rejects:

- Missing `required` parameters.
- `enum` violations.
- Unknown parameters **only** when the tool's schema sets
  `additionalProperties: false`.

Scalar coercion (`limit: "5"` → `5`) and server-accepted extras like
`wait_for_previous` are intentionally allowed — the server handles them.

Blocked calls exit `2` (Devin's blocking exit code) and print:

```
{"decision": "block", "reason": "invalid arguments for mempalace/mempalace_kg_query: missing required parameter: entity"}
```

## Project-level vs. user-level

You can point `command` at this repo's `hooks/devin/mcp_validate.py` directly
for a project-local install, or copy it to `~/.devin/hooks/` for a
cross-project setup.
