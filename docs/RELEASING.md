# Releasing MemPalace

## Pre-release checklist

Run from the repo root before cutting a release tag.

### Verify `mempalace-mcp` entry point alignment

The plugin configs reference `mempalace-mcp` as the MCP server command, which
resolves to a console script declared under `[project.scripts]` in
`pyproject.toml`. If these disagree, `pip install mempalace` ships a plugin
config pointing at a binary that was never installed — exactly what broke
v3.3.2 ([#1093](https://github.com/MemPalace/mempalace/issues/1093)).

```bash
grep -r mempalace-mcp pyproject.toml .claude-plugin .codex-plugin
```

Expected on a healthy `develop` (post-[#340](https://github.com/MemPalace/mempalace/pull/340)) — one line per file:

```
pyproject.toml:mempalace-mcp = "mempalace.mcp_server:main"
.claude-plugin/plugin.json:      "command": "mempalace-mcp"
.codex-plugin/plugin.json:      "command": "mempalace-mcp"
.claude-plugin/.mcp.json:    "command": "mempalace-mcp"
```

If `pyproject.toml` has no match, **stop** — the entry point is missing and
any fresh `pip install` will ship a broken plugin config. Investigate whether
the release branch was cut before
[#340](https://github.com/MemPalace/mempalace/pull/340) landed on `develop`.
