# CLI Commands

All commands accept `--palace <path>` to override the default palace location.
The top-level command also accepts `--backend <name>` to select a storage
backend such as `sqlite_exact`, `milvus`, `qdrant`, or `pgvector`.

## `mempalace init`

Scan a project directory for people, projects, and rooms, and set up the palace.

```bash
mempalace init <dir>                 # <dir> is required
mempalace init <dir> --yes           # non-interactive mode
mempalace init ~/projects/myapp      # example
mempalace init .                     # initialize from the current directory
```

| Option  | Description                                                                  |
|---------|------------------------------------------------------------------------------|
| `<dir>` | **Required.** Project directory to scan. Pass `.` for the current directory. |
| `--yes` | Auto-accept all detected entities                                            |

What it does:

1. Scans `<dir>` for people and projects in file content
2. Detects rooms from `<dir>`'s folder structure
3. Saves detected entities to `<dir>/entities.json`
4. Ensures the global `~/.mempalace/` config directory exists

Running `mempalace init` with no argument will exit with
`error: the following arguments are required: dir`.

## `mempalace mine`

Mine files into the palace.

```bash
mempalace mine <dir>
mempalace mine <dir> --mode convos
mempalace mine <dir> --mode convos --extract general
mempalace mine <dir> --wing myapp
```

| Option | Default | Description |
|--------|---------|-------------|
| `<dir>` | — | Directory to mine |
| `--mode` | `projects` | `projects` for code/docs, `convos` for chat exports |
| `--wing` | directory name | Wing name override |
| `--agent` | `mempalace` | Agent name tag |
| `--limit` | `0` (all) | Max files to process |
| `--dry-run` | — | Preview without filing |
| `--extract` | `exchange` | `exchange` or `general` (for convos mode) |
| `--no-gitignore` | — | Don't respect .gitignore |
| `--include-ignored` | — | Always scan these paths even if ignored |

## `mempalace search`

Find anything by semantic search.

```bash
mempalace search "query"
mempalace search "query" --wing myapp
mempalace search "query" --wing myapp --room auth
mempalace search "query" --results 10
```

| Option | Default | Description |
|--------|---------|-------------|
| `"query"` | — | What to search for |
| `--wing` | all | Filter by wing |
| `--room` | all | Filter by room |
| `--results` | `5` | Number of results |

## `mempalace split`

Split concatenated transcript mega-files into per-session files.

```bash
mempalace split <dir>
mempalace split <dir> --dry-run
mempalace split <dir> --min-sessions 3
mempalace split <dir> --output-dir ~/split-output/
```

| Option | Default | Description |
|--------|---------|-------------|
| `<dir>` | — | Directory with transcript files |
| `--output-dir` | same dir | Write split files here |
| `--dry-run` | — | Preview without writing |
| `--min-sessions` | `2` | Only split files with N+ sessions |

## `mempalace wake-up`

Show L0 + L1 wake-up context (~600–900 tokens).

```bash
mempalace wake-up
mempalace wake-up --wing driftwood
```

| Option | Description |
|--------|-------------|
| `--wing` | Project-specific wake-up |

## `mempalace compress`

Compress drawers using AAAK Dialect.

```bash
mempalace compress --wing myapp
mempalace compress --wing myapp --dry-run
mempalace compress --config entities.json
```

| Option | Description |
|--------|-------------|
| `--wing` | Wing to compress (default: all) |
| `--dry-run` | Preview without storing |
| `--config` | Entity config JSON file |

## `mempalace status`

Show what's been filed — drawer count, wing/room breakdown.

```bash
mempalace status
```

## `mempalace repair`

Rebuild palace vector index from stored data. Fixes segfaults after database corruption.

```bash
mempalace repair
```

Creates a backup at `<palace_path>.backup` before rebuilding, replacing any backup already there.

| Flag | Description |
|------|-------------|
| `rebuild-index` | Positional alias for `--mode from-sqlite --archive-existing` |
| `--mode` | `legacy` (default), `max-seq-id`, or `from-sqlite` |
| `--dry-run` | Print what the repair would do and exit without modifying the palace |
| `--yes` | Skip confirmation for destructive changes |
| `--backup` | Back up SQLite before mutation (default: on) |
| `--source` | Source palace for `--mode from-sqlite` (defaults to `--palace`) |
| `--archive-existing` | Rename the existing palace to `<palace>.pre-rebuild-<timestamp>` first |
| `--segment` | Segment UUID filter for `--mode max-seq-id` |
| `--from-sidecar` | Pre-corruption `chroma.sqlite3` to copy clean `max_seq_id` values from |
| `--confirm-truncation-ok` | Override the truncation safety guard. Disables the abort that protects you when the collection layer returns fewer drawers than SQLite holds |

## `mempalace mcp`

Helper command that outputs setup syntax (like `claude mcp add...`) to connect MemPalace to your AI client, automatically handling paths.

```bash
mempalace mcp
mempalace mcp --palace ~/.custom-palace
```

## `mempalace hook`

Run hook logic for Claude Code / Codex integration.

```bash
mempalace hook run --hook stop --harness claude-code
mempalace hook run --hook precompact --harness claude-code
mempalace hook run --hook session-start --harness codex
```

| Option | Values | Description |
|--------|--------|-------------|
| `--hook` | `session-start`, `stop`, `precompact` | Hook name |
| `--harness` | `claude-code`, `codex` | Harness type |

## `mempalace instructions`

Output skill instructions to stdout.

```bash
mempalace instructions init
mempalace instructions search
mempalace instructions mine
mempalace instructions help
mempalace instructions status
```

## `mempalace logstream`

Agent coordination events — delegate work, wait for replies, acknowledge
outcomes (RFC 003). Operates on `logstream.sqlite3` in the palace directory;
safe to run alongside a live hub. See [Agent Logstream](/concepts/agent-logstream).

```bash
mempalace logstream append --type task.request --stream project/myapp \
  --room delegation --from-agent mac --to-agent windows \
  --correlation-id task_123 --body "Please fix the flaky test."

mempalace logstream list --stream project/myapp --room delegation --json
mempalace logstream wait --correlation-id task_123 --type patch.ready \
  --timeout-ms 300000 --json
mempalace logstream ack evt_... --from-agent mac --status applied
```

| Subcommand | Description |
|------------|-------------|
| `append` | Append an immutable event (`--type`, `--stream`, `--room`, `--from-agent` required; `--body`/`--body-file`, `--artifact-id` repeatable) |
| `list` | List events, oldest first (all routing fields as filters, `--since-event-id`, `--limit`) |
| `wait` | Long-poll until a match or timeout (`--timeout-ms`, max 300000; exits `2` on timeout) |
| `ack` | Append an `event.ack` for an event (`--from-agent` required, `--status`, `--body`) |
| `sync` | Pull missing events/artifacts from peer replicas (`--peer URL --token T`, or all peers in `peers.json`) |

All subcommands accept `--json` for scriptable output.

## `mempalace artifact`

Exact artifact exchange for agent handoffs — unified diffs, files, logs.
Content is stored verbatim with a SHA-256.

```bash
git diff | mempalace artifact put --kind patch --created-by windows --json
mempalace artifact get art_... | git apply --3way
mempalace artifact get art_... --out /tmp/handoff.patch
```

| Subcommand | Description |
|------------|-------------|
| `put` | Store content (`--kind patch\|file\|log\|json\|note`, `--created-by` required; `--content`, `--file`, or stdin) |
| `get` | Print exact content to stdout, or `--out FILE`; `--json` for metadata |
