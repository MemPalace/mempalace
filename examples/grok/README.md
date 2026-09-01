# Grok Examples for MemPalace

Examples only. Do not treat these as active root config.

Recommended MCP setup:

```bash
grok mcp add --scope project mempalace -- uv run mempalace-mcp
```

This creates `.grok/config.toml` in your current project.

Copy `hooks.mempal.json.example` into your project `.grok/hooks/mempal.json` only when locally testing.

Run `/hooks-trust` in Grok before expecting project hooks to execute.

Useful checks:

```bash
grok inspect
grok mcp doctor mempalace
```
