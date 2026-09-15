# MemPalace DeepSeek Harness Plugin

The [MemPalace](https://github.com/MemPalace/mempalace) plugin for the
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (DSH). It is
a DSH bundle plugin with three rows:

| Row | What it does |
| --- | --- |
| `mempalace-recall` | Puts your stored memory (`mempalace wake-up`) in each session's system prompt, from the first model call |
| `mempalace-autosave` | Keeps an append-only transcript per session and runs MemPalace's own `stop`, `precompact` and `session-end` hooks against it |
| `mempalace-mcp` | Serves the palace through DSH's own MCP client with MemPalace's light server: three PQL tools, `mcp__mempalace__palace_query` and friends |

Sister plugins for other harnesses live alongside it: `.claude-plugin/`,
`.codex-plugin/`, `.cursor-plugin/`, `.antigravity-plugin/`.

Built against DSH `0.1.5` (its `dsh-agent`, `dsh-session`, `dsh-system-prompt`
and `dsh-subprocess` extension points).

## Requirements

- DSH `0.1.5` or later with a base-backed profile (`web`, `headless`, `acp`, `sdk`).
- MemPalace installed so that both `mempalace` and `mempalace-light-mcp` are on
  the `PATH` the harness sees, and a palace (`mempalace init`, then
  `mempalace mine`). An editable install made before `mempalace-light-mcp`
  existed will not have that script; reinstall (`pip install -e .`) to get it.
  On Windows, stop running MemPalace processes first (a hub, `logstream watch`):
  they hold the `mempalace.exe` launcher open and pip cannot replace it.
- Node `20.3` or later (DSH requires newer anyway).

## Install

```sh
dsh plugin --profile web add link:/absolute/path/to/mempalace/.dsh-plugin
```

Restart the profile (`dsh web`): bundle plugins mount at startup. Check that the
rows are there:

```sh
dsh web --dump-config | grep mempalace
```

To turn one row off without uninstalling, add a patch to the profile's own
`cordis.patch.yml`, e.g. `- id: mempalace-mcp` with `disabled: true`.

## What each row does

### `mempalace-recall`

When a top-level session starts, the row runs `mempalace wake-up --wing <wing>`
once and registers a `## MemPalace memory` section on that agent. The section
serves the cached text on every model step; there is no further I/O.

- **Wing.** The wing `mempalace mine` files the project under: `wing:` from the
  workspace's `mempalace.yaml` (or `mempal.yaml`), else the directory name,
  lowercased with spaces and hyphens turned into underscores. Set `wing` to pin one.
- **First model call.** DSH builds the prompt before the first step can wait on
  anything else, so the row holds that one assembly until the memory arrives,
  for at most `firstAssemblyBudgetMs` (default 10000 ms). A cold `wake-up`
  measured 7.4 s on a Windows development machine, mostly ChromaDB import, and
  about 1.7 s once warm, so the budget sits above the cold case. A slower palace means the first call goes without
  memory; every later call has it. Lower the budget if first-message latency
  matters more to you than first-message memory.
- **Verbatim.** The wake-up text is injected exactly as MemPalace printed it.
  Only the CLI banner and placeholder hints ("No identity configured", "No palace
  found") are left out.
- **Subagents** get no memory section unless `subagents: true`.

### `mempalace-autosave`

DSH stores sessions zstd-compressed and passes hooks an empty transcript path,
so this row keeps its own transcript:
`$DSH_HOME/mempalace/transcripts/<session>.jsonl` (default `~/.dsh/...`).

- It is **append-only**. Each line is one message as it entered the session. A
  later compaction changes what the model sees, not what this file already holds.
- It holds **only what you wrote and the assistant's text**. Harness context,
  plugin injections, tool results, reasoning and compaction summaries are left
  out, because none of those are anyone's words.
- On each turn boundary it runs `mempalace hook run --hook stop --harness dsh`.
  When compaction starts it runs `--hook precompact`, and when a session closes
  it runs `--hook session-end`. Hooks run in the background, one at a time per
  session. A turn never waits on one.

Everything else is MemPalace's own hook behaviour, the same as in Claude Code:
the save interval (every 15 messages), the diary checkpoint (agent `dsh`, wing
`wing_<project>`), mining the transcript into the `sessions` wing, and daemon
write routing. MemPalace's `hooks.auto_save: false` turns it off.

`hooks.silent_save: false` (asking the model to write the checkpoint itself) is
not supported. The plugin logs a warning instead of relaying the request.

### `mempalace-mcp`

A plain [`@deepseek-ai/dsh-mcp-client`](https://github.com/deepseek-ai/deepseek-harness)
row running MemPalace's **light** MCP server, `mempalace-light-mcp`, over
stdio. Its three tools (`palace_query`, `palace_exec`, `palace_coordinate`)
cover everything the 45-tool server does. They take a compact PQL query instead
of one tool per operation, e.g. `FIND "auth flow" IN my_project LIMIT 5`.
Results come back as JSON with each drawer's text unmodified.

Light is the default because DSH re-sends every tool schema on every model
step. Measured on MemPalace 3.9.0, that is about 2,100 tokens for the light
server against about 7,700 for the full one. The trade-off: the model has to
write PQL. The recall section gives it an exact example, but a small local
model may still form queries badly. The MCP row is one process per `dsh web`,
not one per session, so the light server's heavier start (about 1 s, 90 MB)
is paid once.

For the full server instead, override the row in the profile's `cordis.patch.yml`
and point `searchTool` at its search tool:

```yaml
- id: mempalace-mcp
  config:
    serverName: mempalace
    transport: stdio
    command: mempalace-mcp
- id: mempalace-recall
  config:
    searchTool: mcp__mempalace__mempalace_search
```

## Configuration

Recall and autosave take these keys in the profile's `cordis.patch.yml`. A
patch **replaces** a row's whole `config`, so restate every key you rely on:

```yaml
- id: mempalace-recall
  config:
    command: C:/Users/me/AppData/Roaming/Python/Python314/Scripts/mempalace.exe
    wing: my-project
    firstAssemblyBudgetMs: 6000
```

| Key | Row | Default | Meaning |
| --- | --- | --- | --- |
| `command` | both | `mempalace` | Executable, looked up on the harness `PATH` unless absolute |
| `commandArgs` | both | `[]` | Arguments before the subcommand, e.g. `command: python` + `commandArgs: ['-m', 'mempalace']` |
| `env` | both | `{}` | Extra environment for the CLI (e.g. `MEMPALACE_PALACE_PATH`) |
| `timeoutMs` | recall | `30000` | Ceiling on `mempalace wake-up` (autosave hooks get 120 s) |
| `subagents` | both | `false` | Also act on subagent sessions |
| `wing` | recall | project wing | Wake up a fixed wing instead |
| `firstAssemblyBudgetMs` | recall | `10000` | How long the first model call may wait for memory; `0` never waits |
| `searchTool` | recall | `mcp__mempalace__palace_query` | Tool the memory section points at. A `palace_query` tool gets a PQL example; any other gets plain wing guidance |
| `transcriptDir` | autosave | `$DSH_HOME/mempalace/transcripts` | Where session transcripts are kept |

The MCP row takes the standard `dsh-mcp-client` keys (`command`, `args`, `env`,
`cwd`, `toolCallTimeoutMs`, ...).

## Privacy

The plugin itself sends nothing anywhere. It runs the local `mempalace` CLI and
writes transcripts under the harness home. But the memory section is part of the
system prompt, and the harness sends that prompt to whichever model provider the
profile is configured to use. If that is a remote API, your wake-up memory goes
with every request. Search results the model asks for go the same way. Use a
local model, or leave `mempalace-recall` and `mempalace-mcp` disabled, if that is
not acceptable.

Uninstalling removes the rows. Transcripts and palace data stay where they are;
the plugin never deletes either.

## Development

```sh
cd .dsh-plugin && node --test test/*.test.mjs   # the plugin suite
uv run pytest tests/test_dsh_plugin.py  # the same suite, as CI runs it
uv run pytest tests/test_hooks_cli.py -k dsh
```

`lib/palace.js` holds settings, the single CLI spawn path and the wing rule.
`lib/transcript.js` is the transcript record filter and the append-only writer.
`lib/recall.js` and `lib/autosave.js` are the two rows. `test/host.mjs` is a fake
host shaped after the DSH typings. It resolves prompt text before the assemble
waterfall, as DSH does, which is the behaviour the first-call barrier exists for.
