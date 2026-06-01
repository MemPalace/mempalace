# MemPalace — Kiro IDE Integration

Give your AI a persistent memory inside the [Kiro IDE](https://kiro.dev): mine
projects and conversations into a searchable palace backed by ChromaDB, recall
them with MCP tools, and let MemPalace read Kiro's own session transcripts so
nothing is lost.

Kiro integrates through two first-class extension points (no plugin runtime or
hooks required):

- **MCP server** — registered in `~/.kiro/settings/mcp.json`, exposing
  MemPalace's read/write memory tools.
- **Steering** — a Markdown file in `~/.kiro/steering/` that tells the agent to
  recall memory proactively and record what matters.

## Prerequisites

- Python 3.9+
- `uv tool install mempalace` (recommended) or `pip install mempalace`

Verify the CLI and MCP server are on your PATH:

```bash
mempalace --version
which mempalace-mcp
```

## Install (recommended)

One command wires everything up — it merges the MCP entry (never clobbering
your other servers) and writes the steering file:

```bash
mempalace kiro install
```

Then reload Kiro (**Command Palette → "Developer: Reload Window"**). The
`mempalace` MCP server starts automatically on the next session.

Scope it to a single workspace instead of your home config with `--local`:

```bash
mempalace kiro install --local           # writes ./.kiro/...
mempalace kiro install --local /path/to/repo
```

Point the server at a custom palace location:

```bash
mempalace kiro install --palace /path/to/palace
```

Check or remove the integration at any time:

```bash
mempalace kiro status
mempalace kiro uninstall      # leaves your palace data intact
```

## Install (manual)

If you prefer to edit the config yourself, copy the `mcpServers.mempalace`
block from [`mcp.json`](./mcp.json) into `~/.kiro/settings/mcp.json` and copy
[`steering/mempalace.md`](./steering/mempalace.md) into `~/.kiro/steering/`.

## Backfill & sync past conversations

Kiro has no live Stop/PreCompact hooks, so MemPalace captures history by
reading the session transcripts Kiro already writes to disk. Import them with:

```bash
mempalace kiro sync          # auto-detects Kiro's session directory
mempalace kiro sync --dry-run
```

`kiro sync` reads both Kiro data sources — the workspace-session transcript and
the per-execution exec store — and splices the real assistant output (reasoning
+ prose) onto the `"On it."` stubs Kiro writes to the transcript, so the agent's
actual answers are ingested, not just your prompts.

## Auto-sync & retention

`mempalace kiro install` configures two things in the MCP entry's `env`:

- **Auto-sync** (`MEMPALACE_KIRO_AUTOSYNC=1`) — the MCP server runs a debounced,
  detached `kiro sync` in the background each time Kiro starts it (Kiro has no
  Stop/PreCompact hooks, so MCP startup is the trigger). Debounce defaults to
  ~30 min (`MEMPALACE_KIRO_AUTOSYNC_INTERVAL_MIN`). Disable with
  `--no-autosync`.
- **Retention** (`MEMPALACE_KIRO_RETENTION_DAYS=30`) — a rolling window. Sessions
  whose transcript is older than the window (or whose transcript Kiro itself
  deleted) are skipped on ingest **and** pruned from the palace on each sync, so
  old data is removed automatically. Set `--retention-days 0` to keep everything
  forever, or any N for an N-day window.

```bash
mempalace kiro install --retention-days 90
mempalace kiro install --no-autosync
mempalace kiro sync --retention-days 7 --dry-run   # preview a prune
```

If Kiro is installed somewhere non-standard, set `MEMPALACE_KIRO_AGENT_DIR` to
its `globalStorage/kiro.kiroagent` path, or mine the directory directly:

```bash
mempalace mine <kiro-sessions-dir> --mode convos
```

## Available MCP tools

The server exposes MemPalace's full tool set (search, knowledge graph,
drawers, diary, taxonomy). All tools are auto-approved except the two
destructive deletes (`mempalace_delete_drawer`, `mempalace_delete_tunnel`),
which still require an explicit confirmation in Kiro. The complete list lives
in [`mcp.json`](./mcp.json).

## Where Kiro stores sessions

| Platform | Path |
|----------|------|
| macOS    | `~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/workspace-sessions/` |
| Linux    | `~/.config/Kiro/User/globalStorage/kiro.kiroagent/workspace-sessions/` |
| Windows  | `%APPDATA%/Kiro/User/globalStorage/kiro.kiroagent/workspace-sessions/` |

## Full documentation

See the main [README](../README.md) for architecture, search internals, and
advanced usage.
