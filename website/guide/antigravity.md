# Antigravity Plugin

MemPalace ships first-class support for Google's
[Antigravity IDE](https://antigravity.google/) as an installable
plugin. The plugin registers MemPalace's MCP server, ships the
`mempalace` skill, and wires two lifecycle hooks (Stop and
PreInvocation) for background mining and startup memory injection.

## What gets registered

| Surface         | Antigravity component                                      |
|-----------------|------------------------------------------------------------|
| MCP server      | `mempalace` (stdio, runs `mempalace-mcp`)                  |
| Skill           | `mempalace` (in-plugin `skills/mempalace/SKILL.md`)        |
| Stop hook       | `mempalace-save` — background-mines the conversation        |
| PreInvocation   | `mempalace-wake` — injects memory on the first model call   |

The full audit of which Antigravity surfaces we use, why, and what we
deliberately do not ship is in [`hooks/antigravity/INVESTIGATION.md`](https://github.com/MemPalace/mempalace/blob/main/hooks/antigravity/INVESTIGATION.md).

## Prerequisites

- Python 3.9+
- [`mempalace`](https://github.com/MemPalace/mempalace) installed and
  on `$PATH` (`mempalace --version` to verify)
- [Antigravity IDE](https://antigravity.google/) installed (`~/.gemini/`
  exists)

## Install

From the cloned `mempalace` repo:

```bash
bash hooks/antigravity/install.sh
```

This installs to `~/.gemini/config/plugins/mempalace/`. Restart
Antigravity and the plugin loads automatically — you'll see
`mempalace` in the MCP store and the skill list.

### Dry run first

```bash
bash hooks/antigravity/install.sh --dry-run
```

### Custom install dir (workspace-scoped)

```bash
bash hooks/antigravity/install.sh \
  --install-dir <workspace>/.agents/plugins/mempalace
```

The installer absolutizes any relative path before baking it into the
rendered `hooks.json`, so the resulting plugin is portable to any
working directory.

### Idempotency

Re-running the installer produces a byte-identical install —
`cmp`-gated copies skip files whose contents already match. Safe to
run from CI.

### Uninstall

```bash
bash hooks/antigravity/install.sh --uninstall
```

The uninstaller has two safety guards:

1. The basename of `--install-dir` must be exactly `mempalace`.
2. The directory must contain a `plugin.json` whose `name` is
   `"mempalace"`.

This prevents an accidental wipe of an unrelated directory if the
install dir is ever misconfigured.

## How the hooks behave

### Stop hook (`mempalace-save`)

Fires every time the agent's execution loop terminates. Counts each
fire per-conversation; on every Nth fire (default 15, configurable
via `MEMPAL_SAVE_INTERVAL`), it spawns
`mempalace mine <transcript-dir> --mode convos` in the background.

Defers when:

- `fullyIdle == false` — background commands are still running, the
  transcript is in motion. Try again on the next Stop fire.
- `terminationReason == "error"` — the transcript may be corrupt.
- A previous save for this conversation is still running.
- Any kill switch (see below) is set.

The hook **always** returns `{}` to stdout — never
`{"decision": "continue"}`, which would force the agent into an
infinite re-execution loop.

### PreInvocation hook (`mempalace-wake`)

Fires before every model call, but is gated to `invocationNum == 1`
so memory only gets injected once per conversation (mimicking
Cursor's `sessionStart` semantics).

When the gate passes, runs `mempalace wake-up --wing <inferred>` with
a 500ms hard timeout and emits the verbatim output as an
`ephemeralMessage`. The injection lives for one turn only and never
persists into the transcript.

The wing is inferred from `workspacePaths[0]` (the first absolute
workspace path). If you have a multi-workspace conversation, the
first workspace wins.

## Kill switches

Any one of these silently disables both hooks:

| Knob                                | Value                                |
|-------------------------------------|--------------------------------------|
| `MEMPAL_DISABLE_HOOK`               | `1`, `true`, `yes`                   |
| `MEMPALACE_HOOKS_AUTO_SAVE`         | `false`, `0`, `no`                   |
| `~/.mempalace/config.json`          | `{ "hooks": { "auto_save": false }}` |
| (remove `~/.mempalace/` entirely)   | palace nuke = no-op hooks            |

Each kill switch results in `{}` on stdout and exit 0 — the hook
becomes a no-op without removing the plugin.

## Performance budget

- The hook scripts are designed to return in under 100ms when the
  kill switch trips or any gate fails.
- The Stop hook spawns mining in a detached background subprocess
  (`nohup ... &`) so the hook itself returns immediately while the
  mining proceeds.
- The PreInvocation hook enforces a 500ms hard cap on
  `mempalace wake-up`. If the call doesn't return in time, the hook
  emits `{}` and the conversation starts without injection rather
  than blocking the user.

## How the hooks find your `mempalace` install

The hooks run `mempalace` as `python -m mempalace`, so they need a
Python interpreter that can actually import the package. In almost
every case this is resolved **automatically** — you should not need to
configure anything. The resolution order is:

1. **`MEMPAL_PYTHON`** — an explicit interpreter path you export
   (escape hatch; see below).
2. **The `mempalace-mcp` / `mempalace` console-script shebang.** When
   you install via `uv tool install mempalace` or `pipx install`, the
   package lives in an *isolated* environment whose interpreter is
   **not** your system `python3`. The hooks read the shebang line of
   the console script already on your `PATH` (the same one the MCP
   server launches) to find that exact interpreter. This is what makes
   the common install paths work with zero configuration.
3. **`python3` on `PATH`** — covers an activated virtualenv or an
   editable (`pip install -e .`) dev checkout.
4. A bare `python3` fallback.

### When you might need `MEMPAL_PYTHON`

You only need to set it if the hooks can't otherwise reach a Python
with `mempalace` importable — for example, an unusual install layout,
or a wrapper interpreter the shebang heuristic can't follow. Point it
at the interpreter that owns the package:

```bash
# uv tool install: the interpreter lives under `uv tool dir`
export MEMPAL_PYTHON="$(uv tool dir)/mempalace/bin/python"

# or a project virtualenv
export MEMPAL_PYTHON=/path/to/.venv/bin/python
```

Add the line to your `~/.zshrc` / `~/.bashrc` so a GUI-launched
Antigravity (which may not inherit your interactive shell `PATH`)
picks it up. Verify with:

```bash
"$MEMPAL_PYTHON" -m mempalace --version
```

## Verifying installation

```bash
ls ~/.gemini/config/plugins/mempalace/
# expect: README.md hooks/ hooks.json mcp_config.json plugin.json skills/

cat ~/.gemini/config/plugins/mempalace/hooks.json
# absolute paths to the two hook scripts

mempalace-mcp --version
# binary on PATH

bash -n ~/.gemini/config/plugins/mempalace/hooks/*.sh
# no syntax errors
```

After restarting Antigravity:

1. The MCP store should list `mempalace` as a registered server.
2. Starting a fresh conversation should fire the wake hook — check
   `~/.mempalace/hook_state/antigravity_hook.log` for an
   `[event=preInvocation]` line.
3. Ending a turn should fire the save hook — same log.

## Troubleshooting

### "MCP server `mempalace` not found"

The plugin file is in place but the binary isn't on `$PATH`:

```bash
mempalace-mcp --version
# command not found?
```

Install via uv (recommended) or pip:

```bash
uv tool install mempalace
# or
pip install mempalace
```

### Hooks aren't firing

Check the antigravity hook log:

```bash
tail -50 ~/.mempalace/hook_state/antigravity_hook.log
```

Each fire writes a line. No lines = the hook is not being invoked.
Verify `~/.gemini/config/plugins/mempalace/hooks.json` exists and the
`command` paths point to executable files.

### Save fires but no mining happens

Two common causes:

1. **The interval hasn't elapsed.** Mining only triggers when
   `count % MEMPAL_SAVE_INTERVAL == 0`. The log shows the running
   counter and interval per fire — wait for the next save tick or set
   `MEMPAL_SAVE_INTERVAL=1` for testing.
2. **The resolved Python can't import `mempalace`.** Look for this
   line in `~/.mempalace/hook_state/antigravity_hook.log`:

   ```
   ERROR: mempalace is not runnable via <python> -m mempalace; install mempalace or set MEMPAL_PYTHON
   ```

   If you see it, the interpreter resolution (see *How the hooks find
   your `mempalace` install* above) landed on a Python without the
   package. Set `MEMPAL_PYTHON` to the correct interpreter and restart
   Antigravity.

### Wake injection isn't appearing

The wake hook is gated to `invocationNum == 1` AND only injects once
per conversation (atomic `mkdir` marker). Check
`~/.mempalace/hook_state/antigravity_woke_<conversationId>` exists
after a successful injection.

For a manual re-test:

```bash
rm -rf ~/.mempalace/hook_state/antigravity_woke_*
```

## See also

- [`hooks/antigravity/INVESTIGATION.md`](https://github.com/MemPalace/mempalace/blob/main/hooks/antigravity/INVESTIGATION.md)
  — every Antigravity surface investigated, with verbatim quotes from
  the official docs.
- [`hooks/antigravity/STDIN_SHAPE.md`](https://github.com/MemPalace/mempalace/blob/main/hooks/antigravity/STDIN_SHAPE.md)
  — exact wire format for both events.
- [`examples/antigravity/`](https://github.com/MemPalace/mempalace/tree/main/examples/antigravity)
  — standalone `hooks.json` + `mcp_config.json` for users who don't
  want the full plugin install.
- [Auto-Save Hooks](./hooks.md) — Claude Code equivalent.
- [Gemini CLI](./gemini-cli.md) — Gemini CLI integration (separate from Antigravity).
