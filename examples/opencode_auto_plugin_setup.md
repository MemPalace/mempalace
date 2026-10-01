# OpenCode Auto-Plugin Integration Guide

This guide explains how to set up the **MemPalace Auto-Plugin** for [OpenCode](https://github.com/sst/opencode) — a zero-config integration that automatically initializes MemPalace in every project and injects the Memory Protocol into every agent session.

**What it does:**
1. On first event for a project in an OpenCode process, detects the current git repo root.
2. Runs `mempalace init --yes <root>` in the background (idempotent — safe to re-run).
3. After init succeeds, runs `mempalace mine --limit 200 <root>` in the background.
4. Injects the **MemPalace Memory Protocol** section into the system prompt of every chat — so every agent automatically knows when and how to save memories.

**What it does NOT do:**
- No `AGENTS.md` file patching — the protocol lives in the system prompt at runtime.
- No opencode.json changes — local plugins are auto-loaded.
- No hooks to install — pure OpenCode plugin API.

## Prerequisites

- Python 3.9+ with MemPalace installed (`pip install mempalace` or editable install)
- `mempalace` CLI on `$PATH` (verify with `mempalace --help`)
- `git` on `$PATH` (the plugin uses `git rev-parse --show-toplevel` to detect repo roots)
- [OpenCode](https://github.com/sst/opencode) installed and working
- MemPalace MCP server already configured in OpenCode (see [mcp_setup.md](mcp_setup.md))

## 1. Install the Plugin

Drop the plugin file into your global OpenCode plugins directory:

```bash
mkdir -p ~/.config/opencode/plugins
curl -fsSL https://raw.githubusercontent.com/milla-jovovich/mempalace/main/examples/opencode_auto_plugin.js \
  -o ~/.config/opencode/plugins/mempalace-auto.js
```

Or copy from a local clone:

```bash
cp examples/opencode_auto_plugin.js ~/.config/opencode/plugins/mempalace-auto.js
```

## 2. Enable ES Modules (if not already)

OpenCode plugins use ES module syntax (`import`/`export`). Make sure your `~/.config/opencode/package.json` declares `"type": "module"`:

```bash
cat ~/.config/opencode/package.json 2>/dev/null || echo "{}" > ~/.config/opencode/package.json
```

Edit to ensure it contains:

```json
{
  "type": "module"
}
```

## 3. Restart OpenCode

Close and reopen OpenCode. The plugin loads automatically — no `opencode.json` changes needed.

## 4. Verify

Open OpenCode in any git-tracked project, then check the log:

```bash
tail -f /tmp/mempalace-auto.log
```

You should see lines like:

```
[2026-04-08T14:32:01.234Z] plugin loaded, project=/Users/you/coding/my-project
[2026-04-08T14:32:01.456Z] init: /Users/you/coding/my-project
[2026-04-08T14:32:02.789Z] init ok → mine: /Users/you/coding/my-project
```

Inside OpenCode, ask the agent:

> What does the MemPalace Memory Protocol tell you to do?

The agent should describe the protocol — proof that the system-prompt injection worked.

Also verify that MemPalace now has content for the project:

```bash
mempalace status
```

You should see the palace summary update after init/mine completes.

## How It Works

The plugin uses two OpenCode plugin hooks:

### `event` hook
Fires on every OpenCode event. The first time a given project emits an event in an OpenCode process, the plugin:
1. Walks up from `ctx.directory` to find the nearest `.git` directory.
2. If a git root is found AND this process hasn't initialized it yet, spawns `mempalace init --yes <root>` detached with `unref()`.
3. On init success, spawns `mempalace mine --limit 200 <root>` detached.
4. While init/mine is in flight, subsequent events are a no-op (dedup via in-memory `Set`s).
5. The project is marked initialized only after `mempalace mine` succeeds; failed mine runs are retried on later events.

**Why detached + unref?** So OpenCode never waits for or blocks on these commands. They run fully in the background.

### `experimental.chat.system.transform` hook
Fires before every chat message. Pushes the MemPalace Memory Protocol section onto `output.system` — but only once per message (dedup check prevents duplicates if multiple plugins inject similar content).

## Customization

The plugin has three knobs you may want to tune (edit the top of `mempalace-auto.js`):

| Knob | Default | What to change |
|------|---------|---------------|
| `MEMPALACE_PROTOCOL` | Standard protocol text | Add project-specific memory guidelines |
| `LOG_FILE` | `/tmp/mempalace-auto.log` | Change to `~/Library/Logs/...` on macOS if you prefer |
| `mine --limit 200` | 200 files | Increase for large repos, decrease for fast init |

## Troubleshooting

**Nothing happens in the log:**
- Verify `~/.config/opencode/package.json` has `"type": "module"`.
- Check OpenCode startup logs for plugin load errors.
- Run `node --check ~/.config/opencode/plugins/mempalace-auto.js` to check syntax.

**`mempalace: command not found` in the log:**
- Add the mempalace binary path to OpenCode's environment.
- Or edit `spawnDetached('mempalace', ...)` to use an absolute path.

**Protocol not injected into the system prompt:**
- Verify the plugin loaded: check `/tmp/mempalace-auto.log` for `"plugin loaded"`.
- Ensure the project directory is inside a git repo — the plugin skips non-git dirs by design.
- Ask the agent directly: "Do you see the MemPalace Memory Protocol in your system prompt?"

**Mine runs too long / too much:**
- Lower the `--limit` value in the `spawnDetached('mempalace', ['mine', '--limit', '200', ...])` call.

## Security Notes

- The plugin executes `git rev-parse --show-toplevel` to detect repo roots, then spawns `mempalace` for init/mine.
- It only activates inside git repos, preventing accidental initialization of `~`, `/tmp`, or unrelated directories.
- Log file is at `/tmp/mempalace-auto.log` and captures stdout/stderr from `mempalace init` and `mempalace mine`; treat it as local diagnostic output, not as sanitized audit logs.

## Comparison with Other Integration Methods

| Method | Pros | Cons |
|--------|------|------|
| **This plugin** | Zero per-project config, automatic, opencode-native | Requires OpenCode plugin support (built-in) |
| Shell wrapper (`opencode` → `init-and-launch.sh`) | Works with any tool | Extra script to maintain, fragile |
| `direnv` / `chpwd` hook | Outside AI context | Fires on every `cd`, not session-bound |
| Manual `mempalace init` per project | Explicit control | Easy to forget, defeats automation |

---

**Questions?** File an issue at [milla-jovovich/mempalace](https://github.com/milla-jovovich/mempalace/issues).
