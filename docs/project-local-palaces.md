# Project-local palaces

MemPalace normally uses the shared palace selected by its existing configuration. Use a named local palace to keep a project's memory separate from that shared palace or from another corpus in the same project. A name is a label, not a built-in role: `documents`, `decisions`, `visuals`, and `working-notes` are all valid choices.

## Create stores and index selected sources

Run these commands at your workspace root. A source must already exist and be inside the workspace. Use `--workspace /absolute/path/to/workspace` with `create`, `list`, `run`, or `mcp` to select the workspace from elsewhere. Commands run relative to that root, so source paths do not depend on where a client was started.

```sh
mempalace local create documents --source projects=docs --source extract=reports
mempalace local create decisions --source convos=approved-exports
mempalace local list
mempalace local run documents -- mine docs --dry-run
mempalace local run documents -- mine docs
mempalace local run documents -- mine reports --mode extract
mempalace local run decisions -- mine approved-exports --mode convos
mempalace local run documents -- search "design rationale"
mempalace local run decisions -- search "why we changed the design"
mempalace search "a memory from the shared palace"
```

Repeat `--source MODE=PATH` for several roots or modes in one palace. `projects` uses the normal project-file scanner (including Markdown and text); `extract` uses the office/document miner and its existing `mempalace[extract]` requirements; `convos` accepts a transcript file or directory and uses the existing conversation parser; `media` uses `mine <path> --source media` and the existing media-index/model requirements. Use `media` for supported image/audio/video formats or `projects` for explicitly prepared text representations. This feature does not add a parser, OCR, or new media model.

Create does not scan, embed, start a server, import global data, or capture conversations. The declared scope is checked whenever a source is mined, including MCP `mempalace_mine` calls. Explicitly select the right ingest mode; a path allowed for `convos` is not automatically allowed for `projects`. Point conversation scopes at only the exports you intend to include. Scope filters **file ingestion**, not manually supplied content such as drawers, diary entries, or facts. It is not a filesystem sandbox or a session-level consent manager.

CLI and MCP still use the regular MemPalace search implementation. Results come only from the selected palace. Searching two palaces is two separate calls; there is no fallback or cross-palace ranking. If the name is wrong or its scope cannot be loaded, the command fails.

## Connect an agent over MCP

Set the server command to `mempalace` and the argument array to:

```json
["local", "mcp", "documents", "--workspace", "/absolute/path/to/workspace"]
```

Register a second connection with the `decisions` name only if the agent needs access to that palace. Add `--read-only` if the connection is for recall only. `--light` uses the lightweight MCP tool surface. MCP supports the built-in file mining modes; use the CLI to invoke `--source media`. There is no automatic connection to the shared palace; keep or register it separately if wanted. A local connection does not install or reroute harness capture hooks.

## Storage and compatibility

Each palace lives under `.mempalace/palaces/<name>/`, with its own `scope.json`, `palace/`, `config/`, and `state/`. The managed launcher uses Chroma and its normal local embedding defaults, not inherited MemPalace environment overrides. It does not change your home directory or the global configuration. Back up the whole named directory while no writer is active. Existing stores made with `--palace` or with custom workspace layouts are not moved or adopted automatically. To try managed mode without risking those stores, create a new name and mine a subset of sources.

The managed area has a Git ignore file on creation. Verify the ignore rules before committing, particularly for transcript exports or generated representations outside that area. Treat the project and its local palace configuration as trusted inputs. Editing a scope file does not remove previously indexed content; create a new palace if you need a fresh, narrower scope.
