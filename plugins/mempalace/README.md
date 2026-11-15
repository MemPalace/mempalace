# MemPalace

**Local-first, verbatim AI memory palace for Google Antigravity** — mine projects and conversation histories into a searchable palace with hybrid semantic + FTS5 recall, a temporal knowledge graph, an interactive **Aux Pane UI Extension**, automatic background save & startup wake-up hooks, and multi-agent logstream coordination. Zero external API keys required.

## Prerequisites

Install the `mempalace` Python package (Python 3.10+):

```bash
uv tool install mempalace
# or: pip install mempalace
```

Initialize your palace once (if you haven't already):

```bash
mempalace init
```

## Installation

### From the Antigravity Marketplace or CLI

Install directly with the Antigravity CLI (or click **Install** on the MemPalace card in the Antigravity Marketplace):

```bash
# From a cloned MemPalace checkout (or standalone plugin bundle):
agy plugin install ./plugins/mempalace
```

Verify installation:

```bash
agy plugin list
```

*(POSIX alternative: `bash hooks/antigravity/install.sh` from the main repository also installs into `~/.gemini/config/plugins/mempalace/`.)*

## What's Included

| Component | Path | Purpose |
| :--- | :--- | :--- |
| **Manifest** | `plugin.json` | Official `v1` Antigravity plugin manifest, starter prompts, and branding (`assets/logo.svg`). |
| **Aux Pane UI Extension** | `sidecars/palace/` | Interactive 4-view panel (**Extensions > MemPalace**) with **Recall** (`~7ms` FTS5 lexical + hybrid search), **Palace** Wing/Room/Tunnel browser (scales to 400k+ drawers), **Graph** SVG entity explorer, and live **Logstream** + Diaries viewer. |
| **MCP Server** | `mcp_config.json` | Registers `mempalace-mcp` (`stdio`) exposing semantic search, verbatim drawer storage, temporal knowledge graph queries (`valid_from` / `valid_to`), palace health audits, and logstream coordination tools. |
| **Lifecycle Hooks** | `hooks.json` + `scripts/` | Cross-platform Node.js handlers (`save-hook.mjs` on `Stop` for background conversation mining every `N` turns; `wake-hook.mjs` on first-turn `PreInvocation` for `<500ms` verbatim wake-up context injection) working natively on Windows, macOS, and Linux. |
| **Skills** | `skills/mempalace/` & `skills/mempalace-recall/` | Guided setup, mining, palace health audit/repair (`/mempalace`), and the search-before-answer verbatim recall protocol (`/mempalace-recall`). |
| **Rule** | `rules/mempalace-recall.md` | Dynamic `model_decision` rule that prompts verbatim palace lookup on memory-relevant questions without adding overhead to greenfield coding turns. |

## Opening the Visual Palace Explorer

Open the Aux Pane `+` menu in Antigravity and choose **Extensions > MemPalace** (or ask the agent to open MemPalace to receive a one-click `[MemPalace](sidecar://mempalace/palace/)` pill in chat).

Key capabilities inside the Aux Pane extension:
- **Recall**: Instant lexical (`FTS5`), hybrid, or vector search scoped to your active workspace wing or across all wings, with one-click **Cite in Chat** and **Open File**.
- **Palace**: Browse Wings, Rooms, and cross-wing Tunnels with sub-75ms indexed pagination even on 400,000+ drawer `sqlite_exact` palaces (`j`/`k` to navigate cards, `Enter` to inspect full verbatim drawers, `c` to copy).
- **Graph**: Explore temporal entity relationships (`active` vs. `historical` facts) in an interactive SVG graph.
- **Logstream**: Monitor multi-agent `task.request`, `patch.ready`, and `event.ack` coordination streams alongside agent diaries.

## Hook Configuration & Kill Switches

Both lifecycle hooks fail open (`exit 0` with `{}`) and share `~/.mempalace/hook_state/` across harnesses:

| Variable / Setting | Default | Effect |
| :--- | :--- | :--- |
| `MEMPAL_SAVE_INTERVAL` | `15` | Background-mine the active conversation transcript every `N` idle `Stop` events. |
| `MEMPAL_DISABLE_HOOK` | unset | Set to `1` or `true` to disable both `Stop` and `PreInvocation` hooks. |
| `MEMPALACE_HOOKS_AUTO_SAVE` | `true` | Set to `false` (or `{"hooks": {"auto_save": false}}` in `~/.mempalace/config.json`) to disable hooks. |
| `MEMPAL_PYTHON` | auto-detected | Override the Python interpreter path (auto-detected from `mempalace-mcp` / `uv tool` / `pipx` / `PATH`). |

## Links & Documentation

- **Repository**: <https://github.com/MemPalace/mempalace>
- **Antigravity Integration Guide**: <https://github.com/MemPalace/mempalace/blob/main/website/guide/antigravity.md>
- **Canonical Recall Protocol**: <https://github.com/MemPalace/mempalace/blob/main/integrations/shared/recall-protocol.md>
- **Surface & Hook Architecture Audit**: <https://github.com/MemPalace/mempalace/blob/main/hooks/antigravity/INVESTIGATION.md>
- **License**: MIT
