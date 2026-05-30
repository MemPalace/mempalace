# Codex CLI Parity Test Results

Date: 2026-05-26
Branch: `feat/codex-cli-support-enablement`

## Goal

Validate full Codex CLI plugin parity with the Claude plugin and the live MemPalace MCP runtime surface.

## Environment

- Repository: `MemPalace/mempalace`
- Default upstream branch target: `develop`
- Codex plugin install under test:
  - Marketplace: `mempalace-local-test`
  - Plugin: `mempalace@mempalace-local-test`
  - Version: `3.3.5`

## Live Plugin Verification

Commands executed:

```bash
codex plugin marketplace list
codex plugin list
```

Observed:
- Marketplace registered: `mempalace-local-test`
- Plugin state: `installed, enabled`

## Live MCP Tool Sweep (All 30 Tools)

Isolated test namespaces used:
- `codex_plugin_live_test_a_20260526`
- `codex_plugin_live_test_b_20260526`

Coverage exercised:
- Palace read: `status`, `list_wings`, `list_rooms`, `get_taxonomy`, `search`,
  `check_duplicate`, `get_aaak_spec`, `get_drawer`, `list_drawers`
- Palace write: `add_drawer`, `update_drawer`, `delete_drawer`, `sync`
- Knowledge graph: `kg_add`, `kg_query`, `kg_timeline`, `kg_stats`,
  `kg_invalidate`
- Navigation: `traverse`, `find_tunnels`, `graph_stats`, `create_tunnel`,
  `list_tunnels`, `follow_tunnels`, `delete_tunnel`
- Diary: `diary_write`, `diary_read`
- Runtime/system: `hook_settings`, `memories_filed_away`, `reconnect`

Result:
- All tool paths executed successfully with expected outputs.
- Test artifacts (drawers/tunnels/diary test entries) were cleaned up.

## Hook Runtime Verification

Direct wrapper execution validated for:
- `session-start`
- `stop`
- `precompact`

All exited successfully (`exit=0`) after setting executable mode on:
- `.codex-plugin/hooks/mempal-hook.sh`

## Automated Tests

### Command A

```bash
uv run pytest -v \
  tests/test_plugin_feature_parity.py \
  tests/test_codex_plugin_hook_config.py \
  tests/test_codex_plugin_hook_wrappers.py \
  tests/test_hooks_shell.py
```

Result: **27 passed**

### Command B

```bash
uv run pytest -v \
  tests/test_plugin_feature_parity.py \
  tests/test_codex_plugin_hook_config.py \
  tests/test_codex_plugin_hook_wrappers.py \
  tests/test_readme_claims.py::TestToolCount::test_readme_tool_count_matches_code
```

Result: **25 passed**

## Documentation/Manifest Parity Outcome

- Codex plugin docs/manifests updated from `19` to `30` MCP tools.
- Claude plugin docs/manifests aligned to the same value for cross-plugin parity.
- `mempalace instructions help` now advertises the full 30-tool surface.
