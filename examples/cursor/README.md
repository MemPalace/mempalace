# Cursor IDE Hooks — Example `hooks.json` Files

Sample configurations for wiring the MemPalace Cursor hooks into the
Cursor IDE. These are **examples only** — they are intentionally not
placed at the repo root (`/.cursor/hooks.json`) because Cursor
auto-loads project-level hooks from any trusted workspace, and the
repo is regularly opened by contributors. We do not auto-arm hooks on
contributor checkout.

## Variants

### `hooks.json` — full (recommended)

Three hooks wired:

- **`sessionStart`** — calls `mempal_wake_hook_cursor.sh`, which returns
  `additional_context` telling the agent to recall scoped to the wing
  inferred from the workspace root. Cursor-only — Claude Code has no
  equivalent.
- **`stop`** — calls `mempal_save_hook_cursor.sh`. Counts stop
  invocations per conversation and emits a `followup_message` every
  `MEMPAL_SAVE_INTERVAL` (default 15) telling the agent to file the
  session into the palace and write a diary entry. `loop_limit: 1` is
  defense-in-depth on top of our own loop-count check.
- **`preCompact`** — calls `mempal_precompact_hook_cursor.sh`. Runs
  `mempalace mine` synchronously on the transcript before compaction
  summarises it, then drops a marker so the next `stop` forces a save
  followup.

### `hooks.minimal.json` — `stop` only

Lightest install. Wires just the save hook. Use this if you don't
want the sessionStart recall context or the preCompact transcript
snapshot.

## How to use

The `$HOME` placeholder is **not** expanded by Cursor — you must
substitute the absolute path before saving the file. Pick one:

### Option A — let `install.sh` do it

Project scope — writes `<repo>/.cursor/hooks.json`:

```bash
hooks/cursor/install.sh --scope project --target /path/to/your/repo
```

User scope — writes `~/.cursor/hooks.json`, applies to every Cursor workspace:

```bash
hooks/cursor/install.sh --scope user
```

The installer copies the hook scripts to `~/.mempalace/hooks/cursor/`,
substitutes the absolute paths, and merges the entries into your
existing `hooks.json` without clobbering unrelated hooks. See
`install.sh --help` for `--dry-run`, `--uninstall`, and `--variant`.

### Option B — copy + edit manually

1. Copy the chosen example to the target location:
   - User scope: `~/.cursor/hooks.json`
   - Project scope: `<your-repo>/.cursor/hooks.json`
2. Replace every `$HOME` with the absolute path to your home
   directory (e.g., `/Users/you` or `/home/you`).
3. Make sure each hook script is executable
   (`chmod +x ~/.mempalace/hooks/cursor/mempal_*_hook_cursor.sh`).
4. Restart Cursor, or wait for it to auto-reload the file.

## Why aren't these files at the repo root?

Cursor automatically loads `.cursor/hooks.json` from any trusted
workspace. Placing a real `hooks.json` at the repo root would arm
MemPalace's hooks on every contributor's machine the moment they open
the repo in Cursor — which would modify their conversation behaviour
without consent and write to `~/.mempalace/hook_state/` without
asking. Editor configuration is sacred; opt-in only.

If you actually want MemPalace's hooks armed when working on the
MemPalace repo itself, run:

```bash
hooks/cursor/install.sh --scope project --target .
```

That will write `./.cursor/hooks.json` for the repo workspace
specifically — but it is your decision, not ours, and the file is
listed in `.gitignore` paths Cursor users typically already exclude.

## Related: the Cursor plugin

The hooks here are **only one half** of MemPalace's Cursor integration. The other half is the [`.cursor-plugin/`](../../.cursor-plugin/) folder at the repo root, which packages MemPalace's MCP server, five slash commands, and the model-invocable `mempalace` skill as a regular Cursor plugin you can drop into `~/.cursor/plugins/local/mempalace`.

The two install paths are orthogonal — install whichever you want, in any order:

| You want                                                                  | Install                                                  |
|---------------------------------------------------------------------------|----------------------------------------------------------|
| MCP tools (`mempalace_search`, `mempalace_add_drawer`, …) + slash commands | The plugin — see [`.cursor-plugin/README.md`](../../.cursor-plugin/README.md) |
| Auto-save every N turns + sessionStart memory recall                       | The hooks here — see Option A above                      |
| Both                                                                       | Install the plugin AND run `hooks/cursor/install.sh`     |

Hooks are deliberately **not** bundled into the plugin because Cursor's hooks system is configured per-user/per-project (in `~/.cursor/hooks.json` or `.cursor/hooks.json`), not per-plugin — so the installer here owns that file with idempotent merge semantics, while the plugin owns the MCP+commands+skill side.

## See also

- [`hooks/cursor/README.md`](../../hooks/cursor/README.md) — full reference for hooks
- [`hooks/cursor/STDIN_SHAPE.md`](../../hooks/cursor/STDIN_SHAPE.md) — per-event schema with citations
- [`website/guide/cursor-hooks.md`](../../website/guide/cursor-hooks.md) — rendered docs
- [`.cursor-plugin/README.md`](../../.cursor-plugin/README.md) — Cursor plugin (MCP + commands + skill)
