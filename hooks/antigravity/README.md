# MemPalace — Antigravity hook scripts

Lifecycle hooks for the [Antigravity IDE](https://antigravity.google/).

This is the third sibling of the Claude Code and Codex integrations
(see `hooks/mempal_save_hook.sh` and `.codex-plugin/hooks/`). The
overall shape is the same — a Stop event triggers a background save,
a startup-time event injects memory into the agent — but the wire
format and STDOUT contract are Antigravity-specific (see
[STDIN_SHAPE.md](STDIN_SHAPE.md)).

## Quick start

From the repo root:

```bash
bash hooks/antigravity/install.sh
```

This installs the plugin to `~/.gemini/config/plugins/mempalace/`.
Restart Antigravity and the MCP server, skill, and hooks all register
automatically.

To dry-run first:

```bash
bash hooks/antigravity/install.sh --dry-run
```

To uninstall:

```bash
bash hooks/antigravity/install.sh --uninstall
```

## What gets installed

```
~/.gemini/config/plugins/mempalace/
├── plugin.json                              # marker manifest
├── mcp_config.json                          # registers mempalace-mcp
├── hooks.json                               # rendered from hooks.json.tmpl
├── README.md
├── skills/
│   └── mempalace/
│       └── SKILL.md
└── hooks/
    ├── lib/
    │   └── common.sh
    ├── mempal_save_hook_antigravity.sh      # Stop event handler
    └── mempal_wake_hook_antigravity.sh      # PreInvocation handler
```

`hooks.json` carries absolute paths to the two hook scripts (resolved
from `__PLUGIN_DIR__` at install time).

## What the hooks do

### `mempal_save_hook_antigravity.sh` (Stop event)

Fires every time the agent's execution loop terminates. Increments a
per-conversation counter; every `MEMPAL_SAVE_INTERVAL` fires (default
15), spawns `mempalace mine <transcript-dir> --mode convos --wing
<inferred>` in the background. The hook itself returns `{}` to stdout
in under a few milliseconds — the actual mining runs detached and
does not block the user.

Defers when:

- `fullyIdle == false` (background tasks still running)
- `terminationReason == "error"` (transcript may be corrupt)
- A previous save for this conversation is still running
- Any kill switch is set

### `mempal_wake_hook_antigravity.sh` (PreInvocation event, gated)

Fires before every model invocation. Gated to `invocationNum == 1`
(first invocation of the conversation only) — beyond that we'd be
re-injecting on every turn. Calls `mempalace wake-up --wing <inferred>`
with a 500ms hard timeout and emits the verbatim output as an
`ephemeralMessage` so the agent sees relevant memory on its first
response without polluting the persistent transcript.

Skips when:

- `invocationNum != 1`
- Already woke this conversation (atomic `mkdir` loop guard)
- `mempalace wake-up` exits non-zero, times out, or produces empty output
- Any kill switch is set

## Kill switches

Any one of these disables both hooks (silent passthrough, exit 0):

| Knob                                     | Value                          |
|------------------------------------------|--------------------------------|
| `MEMPAL_DISABLE_HOOK`                    | `1`, `true`, `yes`             |
| `MEMPALACE_HOOKS_AUTO_SAVE`              | `false`, `0`, `no`             |
| `~/.mempalace/config.json`               | `{"hooks": {"auto_save": false}}` |
| Removing `~/.mempalace/` entirely        | (palace nuke)                  |

## Workspace-scoped install (advanced)

If you want MemPalace to load only inside a specific workspace,
manually copy the rendered plugin into your workspace's `.agents/plugins/`:

```bash
bash hooks/antigravity/install.sh --install-dir /tmp/render-stage
mkdir -p <workspace>/.agents/plugins/
cp -r /tmp/render-stage <workspace>/.agents/plugins/mempalace
rm -rf /tmp/render-stage
```

The global install at `~/.gemini/config/plugins/mempalace/` is the
canonical UX and what we recommend.

## Troubleshooting

### Hooks aren't firing

1. Confirm Antigravity sees the plugin: open the IDE, navigate to the
   Customizations page; `mempalace` should appear in the global plugins
   list.
2. Check `~/.mempalace/hook_state/antigravity_hook.log` — every fire
   logs a line. No log lines = the hook is not being invoked.
3. Verify `mempalace-mcp` is on `$PATH`: `mempalace-mcp --version`.
4. Inspect the rendered `hooks.json` paths point at executable files:
   `bash -n ~/.gemini/config/plugins/mempalace/hooks/*.sh`.

### Save fires but no mining happens

1. Look for the most recent `[event=stop]` lines in
   `antigravity_hook.log` — `count` and `interval` should both be
   visible. Mining only triggers when `count % interval == 0`.
2. Ensure a Python that can import `mempalace` is reachable. The hook
   runs `"$MEMPAL_PYTHON_BIN" -m mempalace`, where `MEMPAL_PYTHON_BIN`
   is resolved (in order) from `$MEMPAL_PYTHON`, the
   `mempalace-mcp` / `mempalace` console-script shebang on `$PATH`,
   then `python3`. A failed probe logs:

   ```
   ERROR: mempalace is not runnable via <python> -m mempalace; install mempalace or set MEMPAL_PYTHON
   ```

   On a GUI-launched Antigravity the harness `PATH` may differ from
   your shell `PATH`; if the shebang heuristic can't find the right
   interpreter, export `MEMPAL_PYTHON=/abs/path/python` (e.g.
   `"$(uv tool dir)/mempalace/bin/python"`) and restart.

### Wake injection isn't appearing

1. The wake hook only injects on `invocationNum == 1`. Subsequent
   invocations are gated.
2. The atomic `mkdir` marker
   `~/.mempalace/hook_state/antigravity_woke_<conversationId>` exists
   after a successful injection. Remove it to re-inject (rare).
3. `mempalace wake-up --wing <inferred>` may be returning empty output
   if the wing doesn't exist yet. Run `mempalace status` to verify
   wing presence.

## See also

- [INVESTIGATION.md](INVESTIGATION.md) — every Antigravity surface we
  investigated, with verbatim quotes and source URLs.
- [STDIN_SHAPE.md](STDIN_SHAPE.md) — the exact wire format
  Antigravity uses, with worked examples.
- [../mempal_save_hook.sh](../mempal_save_hook.sh) — Claude Code
  equivalent.
- [../../.codex-plugin/hooks/](../../.codex-plugin/hooks/) — Codex
  equivalent.
- [../../website/guide/antigravity.md](../../website/guide/antigravity.md)
  — full user-facing guide.
