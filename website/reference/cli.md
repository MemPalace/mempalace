# CLI Commands

All commands accept `--palace <path>` to override the default palace location.

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

Creates a backup at `<palace_path>.backup` before rebuilding.

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

## `mempalace replica`

Memory replication across your machines (RFC 004; see
[The Replicated Palace](/concepts/replicated-palace)). Bootstraps a new
replica from its peers and moves precomputed vectors between machines.

```bash
# Bootstrap: fold every peer's authored content into this palace.
# STOP the local hub first — this writes the palace directly.
mempalace replica pull --with-vectors

# One specific origin instead of peers.json:
mempalace replica pull --peer https://desktop.example.com --token "$TOKEN"

# Precompute vectors into the portable cache (safe alongside a live hub):
mempalace replica embed-cache --batch 512 --json
```

| Subcommand | Description |
|------------|-------------|
| `pull` | Fold drawers + knowledge graph from origins (`--peer`/`--token` or `peers.json`; `--with-vectors` uses origin-precomputed vectors, `--no-kg`, `--no-reconcile`) |
| `embed-cache` | Bulk-embed local content into `vector_cache.sqlite3` so peers can pull `--with-vectors` (`--model`, `--batch`, `--all`) |

`pull` requires the local hub to be stopped (single-writer rule) and the
origins to be quiescent (no active mines). Pulls are insert-only and
resumable — re-running heals any gap. Raise
`MEMPALACE_SYNC_HTTP_TIMEOUT` (seconds, default 30) for large bootstraps.

## `mempalace oplog`

The canonical memory op-log (RFC 004 step 2a — currently in dual-write
shadow). Every drawer and knowledge-graph write also lands as a
provenance-stamped op in `oplog.sqlite3`; ops travel between replicas and
fold into their stores.

```bash
mempalace oplog status --json    # counts, kind histogram, version vector
mempalace oplog sync             # pull missing ops from peers
mempalace oplog fold             # apply pulled ops to the local store (hub stopped)
mempalace oplog promote          # one-time: pre-oplog drawers become drawer.add ops
mempalace oplog verify           # replay ops vs the live store — the cutover gate
```

| Subcommand | Description |
|------------|-------------|
| `status` | Op counts, per-kind histogram, and this replica's version vector |
| `sync` | Anti-entropy pull of missing memory ops (`--peer URL --token T`, or all peers) |
| `fold` | Apply pulled remote ops to the local store — stop the hub first; a running hub folds on its own sync cadence |
| `promote` | Emit `drawer.add` ops for locally-authored drawers that predate the op-log (mined sets, pre-shadow captures). Idempotent and resumable (`--dry-run`, `--limit N`); safe alongside a live hub — reads the store, writes only the op-log |
| `verify` | Replay the op-log against the live store; exits `1` on divergence |

Promotion is how an **existing palace's history** becomes op-carried: after
one clean `promote`, the op-log covers everything the replica ever authored,
and future replicas receive that history as ops instead of snapshot pulls.
Remote-stamped copies are never promoted — each origin promotes its own.

A running hub does all of this automatically every `MEMPALACE_SYNC_INTERVAL`
seconds (default 15): logstream sync, memory-op sync, then the fold. The CLI
verbs exist for bootstraps, offline machines, and inspection.

## `mempalace migrate-ids`

The v4 content-pure id migration (RFC 004 id purity). Rewrites drawer ids from
the location-addressed forms (`drawer_<wing>_<room>_<hash>`) to content-pure
`drawer_<hash(content)>`, so organization (wing/room) becomes plain metadata and
the same content anywhere in the mesh is the same drawer — content-addressed
cross-machine dedup.

```bash
mempalace migrate-ids                              # dry-run plan (writes nothing)
mempalace migrate-ids --json                       # machine-readable plan
mempalace migrate-ids --apply --target ~/palace-v4 # materialize a v4 palace (copy-first)
```

| Option | Description |
|--------|-------------|
| *(none)* | Dry-run plan: drawers that change, content-collision groups that will MERGE, and KG/tunnel refs to remap. Writes nothing. |
| `--apply` | Materialize the migration. Requires `--target`. |
| `--target <path>` | Fresh palace path to write the migrated v4 palace into (must differ from the source — the source is never mutated). |
| `--json` | Machine-readable plan output |

The migration is **copy-first**: `--apply` reads the source and writes a new v4
palace into `--target`, copying vectors (never re-deriving them) and merging
content-identical drawers into one. The source palace is left untouched.

Content collisions **merge**: when several drawers hold identical content they
collapse to one v4 drawer (placement chosen by latest `filed_at`), and every
merged-away id is repointed so the knowledge graph's `source_drawer_id`
provenance and any tunnels still resolve.

Because a v4 id is a pure function of content, every replica migrates
independently and converges on identical ids — no migration is ever synced.

## `mempalace reconcile-ids`

Drain legacy v3-keyed "ghost" drawers that remain after the write-flip. New
writes already mint content-pure v4 ids; this command rewrites older store rows
to their content-hash ids, or drops a ghost when the same content already exists
under its v4 id.

```bash
mempalace reconcile-ids          # dry-run plan (writes nothing)
mempalace reconcile-ids --json   # machine-readable plan/output
mempalace reconcile-ids --apply  # write the drain; stop the hub first
```

| Option | Description |
|--------|-------------|
| *(none)* | Dry-run plan: legacy ghosts, rows to rewrite, and duplicates to drop. Writes nothing. |
| `--apply` | Rewrite/drop the ghosts in place. Stop the hub first; this command writes the palace directly. |
| `--force-live-hub` | Allow `--apply` even when a live write-capable hub is registered. Manual recovery only. |
| `--json` | Machine-readable output |

The write-flip and reconcile drain are separate. During the gap, ordinary
search may show duplicate logical content under a legacy id and its v4
content-hash id. Treat the migration as stable only after `reconcile-ids
--apply` reports zero remaining legacy ghosts.

Full runbook (run against the target, validate, then swap it in):

```bash
mempalace migrate-ids --apply --target ~/palace-v4
mempalace --palace ~/palace-v4 compress        # rebuild the closet index at v4 ids
mempalace --palace ~/palace-v4 oplog promote   # build the v4 op-log
mempalace --palace ~/palace-v4 oplog verify    # must report CLEAN
mempalace --palace ~/palace-v4 reconcile-ids   # confirm no legacy ghosts
mempalace --palace ~/palace-v4 search "..."    # confirm recall
```
