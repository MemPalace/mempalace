# MemPalace — Antigravity examples

Two standalone configs for users who don't want to use the
`hooks/antigravity/install.sh` installer.

## Files

| File              | Purpose                                                                           |
|-------------------|-----------------------------------------------------------------------------------|
| `hooks.json`      | Standalone `hooks.json` registering the Stop and PreInvocation hooks.             |
| `mcp_config.json` | Standalone MCP entry registering the `mempalace-mcp` stdio server.                |

## Wire up `hooks.json`

The example uses placeholder absolute paths (`/ABSOLUTE/PATH/TO/mempalace/...`).
You must rewrite both `command` fields to the actual absolute paths to
the hook scripts in your cloned repo, or to whichever location holds
them. Antigravity will not resolve relative paths reliably.

Then drop the file at one of:

- `~/.gemini/config/hooks.json` (global, applies to every workspace)
- `<workspace>/.agents/hooks.json` (workspace-scoped)

Restart Antigravity to pick the file up.

If you'd rather have the paths absolutized automatically, run the
installer:

```bash
bash hooks/antigravity/install.sh
```

That writes a fully rendered `hooks.json` to
`~/.gemini/config/plugins/mempalace/hooks.json`.

## Wire up `mcp_config.json`

The example registers the `mempalace-mcp` stdio server. Two options:

### Option A — merge into the user-level Antigravity MCP config

Antigravity's user-level MCP config lives at
`~/.gemini/antigravity/mcp_config.json`. Merge the `mcpServers.mempalace`
entry from this example into that file, then restart Antigravity.

### Option B — drop into a plugin directory

If you've created a custom plugin folder (per the [Antigravity plugins docs](https://antigravity.google/docs/plugins)),
copy this `mcp_config.json` directly into the plugin root:

```
<plugin-root>/mcp_config.json
```

Antigravity merges plugin-level MCP entries with the user-level config
on launch.

## Verify

After wiring up either or both:

```bash
mempalace-mcp --version          # confirm binary is on PATH
ls ~/.mempalace/                 # confirm palace exists (run `mempalace init` if not)
```

Restart Antigravity. The `mempalace` MCP server should appear in the
MCP store; the Stop and PreInvocation hooks fire automatically.

See [`hooks/antigravity/STDIN_SHAPE.md`](../../hooks/antigravity/STDIN_SHAPE.md)
for the exact wire format Antigravity uses, and
[`website/guide/antigravity.md`](../../website/guide/antigravity.md)
for the full user-facing guide.
