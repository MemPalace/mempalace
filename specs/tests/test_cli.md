# Behavior Spec: `mempalace.cli` (derived from `tests/test_cli.py`)

This spec describes the observable behavior of the MemPalace CLI dispatcher and its
command handlers, as pinned by the test suite. Each command handler takes a parsed
argument namespace and produces side effects (delegated calls, stdout/stderr text,
process exit codes). The CLI exposes a top-level `main()` dispatcher and per-command
handlers: `cmd_status`, `cmd_search`, `cmd_instructions`, `cmd_hook`, `cmd_init`,
`cmd_mine`, `cmd_wakeup`, `cmd_split`, `cmd_repair`, `cmd_compress`, `cmd_sync`,
`cmd_daemon`, plus helpers `_maybe_run_mine_after_init`, `_run_pass_zero`, and
`_reconfigure_stdio_utf8_on_windows` (tests/test_cli.py:L15-L28).

## Process environment hygiene

`main()` must remove the `PYTHONPATH` variable from the process environment so any
subprocess the CLI spawns starts clean; after `main()` runs (even on a clean
`--version` exit) `PYTHONPATH` reads as absent (tests/test_cli.py:L37-L90). The
package import step (before `main` runs) must NOT strip the env var — `PYTHONPATH`
is preserved verbatim through import time (tests/test_cli.py:L84-L86). The package
import must also filter any leaked sentinel paths out of the module search path so
they are not present at import time (tests/test_cli.py:L66-L88). `main --version`
exits with code 0 or none (tests/test_cli.py:L67-L71).

## `cmd_status`

Resolves the palace path from configuration when no `--palace` is given, then
delegates a status report keyed by that path (tests/test_cli.py:L96-L103). When
`--palace` is supplied, the path is tilde-expanded before delegation
(tests/test_cli.py:L106-L115).

## `cmd_search`

Delegates a search with the parsed query, resolved palace path, optional wing and
room filters, and a result count, passing them through unchanged
(tests/test_cli.py:L121-L135). A search failure is not swallowed: the handler exits
the process with code 1 (tests/test_cli.py:L138-L147).

## `cmd_instructions`

Delegates to the instructions runner, forwarding the requested instruction name
(tests/test_cli.py:L153-L157).

## `cmd_hook`

Delegates to the hook runner, forwarding the hook name and harness identifier
(tests/test_cli.py:L163-L167).

## `cmd_init`

Initializes a palace for a project directory. With no detected entities it still
runs room detection (passing the project dir and the `--yes` flag) and performs
config initialization (tests/test_cli.py:L173-L183). When entities are detected it
runs the detect/confirm flow and writes an entities file (tests/test_cli.py:L186-L205).

Wing-name normalization is a contract: a hyphenated directory name like
`my-cool-app` must be normalized to the underscore slug `my_cool_app` before being
written into the known-entities registry, matching the slug the config file uses, so
later miner lookups do not miss (tests/test_cli.py:L208-L242).

`--palace` must be honored over the default location. The palace directory passed
into pass-zero corpus-origin detection equals the absolute form of the user-supplied
`--palace`, and the environment variable `MEMPALACE_PALACE_PATH` is set to the
absolute palace path so downstream reads resolve correctly
(tests/test_cli.py:L245-L297). Pass-zero corpus-origin detection runs unconditionally
within init unless explicitly stubbed (tests/test_cli.py:L233-L237).

When entities are detected but the combined total is zero, the handler prints
`No entities detected` and proceeds (tests/test_cli.py:L300-L314).

## `_maybe_run_mine_after_init` (post-init mine prompt)

Computes the palace path from the given config and the project files from a scan,
then decides whether to mine. The decision rules:

- Interactive accept: an empty answer, `y`, `yes`, or `Y` triggers an in-process
  mine with the project dir, palace path, and scanned file list
  (tests/test_cli.py:L340-L372).
- Interactive decline: an `n` answer skips mining and prints a resume hint plus the
  word `Skipped` (tests/test_cli.py:L375-L393).
- `--yes` alone is scoped to entity auto-accept only; it must STILL prompt for mine
  and must not auto-mine (tests/test_cli.py:L396-L414).
- `--auto-mine` runs the mine automatically and must NOT call the interactive prompt
  (tests/test_cli.py:L417-L434).
- `--yes --auto-mine` together is fully non-interactive: never prompt, always mine
  (tests/test_cli.py:L437-L449).

The resume hint shell-quotes the project directory so paths with spaces or shell
metacharacters produce a copy-paste-safe `mempalace mine <quoted-dir>` command; the
bare unquoted form must not appear (tests/test_cli.py:L452-L474). On POSIX-safe paths
the quoting is a no-op; paths with spaces or backslashes are wrapped in single quotes
(tests/test_cli.py:L389-L392).

A prompt EOF (piped / non-interactive stdin) is treated as a decline: no mine, and
`Skipped` is printed, without crashing (tests/test_cli.py:L477-L490).

A mine failure is not swallowed: the handler exits the process with code 1 and the
error text appears on stderr (tests/test_cli.py:L493-L507).

Ordering guarantee: a scope estimate (file count, e.g. `4 files`, a size estimate in
`MB`, and the phrase `would be mined`) must be printed BEFORE the prompt fires, so the
user sees scope before confirming (tests/test_cli.py:L510-L539).

## `cmd_mine`

Dispatches on mode.

In `projects` mode it delegates project mining with: project dir, palace path,
wing override, agent, limit, dry-run flag, a `respect_gitignore` flag (true when
`--no-gitignore` is false), the include-ignored list, and a max-chunks-per-file value
(tests/test_cli.py:L545-L572).

In `convos` mode it delegates conversation mining with: convo dir, palace path, wing,
agent, limit, dry-run flag, and extract mode (tests/test_cli.py:L575-L600).

The `include_ignored` argument is comma-split and flattened: a list like
`["a.txt,b.txt", "c.txt"]` becomes `["a.txt", "b.txt", "c.txt"]`
(tests/test_cli.py:L603-L622).

Daemon background: with `--daemon --background`, mining is NOT run in-process;
instead a job is submitted (with the resolved palace path and `wait=False`), the job
payload carries the flattened include-ignored list, and the returned job id is printed
to stdout (tests/test_cli.py:L625-L657).

`--background` without `--daemon` is an error: exit code 2 with
`--background requires --daemon` on stderr (tests/test_cli.py:L660-L680).

Lock contention: when the underlying mine raises an "already running" error, the
handler exits with code 1 and prints the lock holder's identity (PID and process
name) to stderr (tests/test_cli.py:L683-L719).

## `cmd_wakeup`

Builds a memory stack for the resolved palace path, prints the wake-up context text,
and prints a token count (the output contains the context and the word `tokens`)
(tests/test_cli.py:L725-L735).

## `cmd_split`

Delegates to the split tool (tests/test_cli.py:L741-L745). After it runs, `sys.argv`
is restored — `sys.argv[0]` is not left set to `mempalace split`
(tests/test_cli.py:L748-L754).

## `main()` argparse dispatch

With no arguments, `main()` prints help that contains `MemPalace`
(tests/test_cli.py:L760-L764). Subcommands dispatch to their handlers:
`status`→`cmd_status`, `search`→`cmd_search`, `init`→`cmd_init`, `mine`→`cmd_mine`,
`wake-up`→`cmd_wakeup`, `split`→`cmd_split`, `repair`→`cmd_repair`,
`compress`→`cmd_compress`, `instructions <name>`→`cmd_instructions`,
`hook run`→`cmd_hook` (tests/test_cli.py:L767-L965, L922-L947).

The `--backend` flag sets the parsed `backend` attribute and exports
`MEMPALACE_BACKEND_EXPLICIT` to the chosen value; accepted values include
`sqlite_exact` and `qdrant` (tests/test_cli.py:L776-L807).

`hook` with no subcommand prints help mentioning `hook` or `run`
(tests/test_cli.py:L915-L919). `instructions` with no subcommand prints help
mentioning `instructions` or `init` (tests/test_cli.py:L934-L938).

### `mcp` command setup guidance

`mempalace mcp` prints quick-setup guidance to stdout (stderr empty) containing the
line `MemPalace MCP quick setup:`, the registration commands
`claude mcp add mempalace -- mempalace-mcp` and
`codex mcp add mempalace -- mempalace-mcp`, an `Optional custom palace:` section, and
the example `mempalace-mcp --palace /path/to/palace`; the literal placeholder
`[--palace /path/to/palace]` must NOT appear (tests/test_cli.py:L855-L867).

When a global `--palace` is provided, the guidance embeds the tilde-expanded palace
path into the registration commands (`mempalace-mcp --palace <expanded>`,
`claude mcp add ... --palace`, `codex mcp add ... --palace`), and the
`Optional custom palace:` section is omitted (tests/test_cli.py:L870-L884).

When `--backend` is provided, the guidance includes `mempalace-mcp --backend <name>`
for backends such as `sqlite_exact` and `qdrant`; stderr stays empty
(tests/test_cli.py:L887-L912).

## `cmd_repair`

When the palace directory does not exist, prints `No palace found`
(tests/test_cli.py:L981-L988). When the directory exists but has no palace database
file (`chroma.sqlite3`), prints `No palace database found`
(tests/test_cli.py:L991-L1000). When reading the collection raises, prints
`Error reading palace` (tests/test_cli.py:L1003-L1016). When the collection holds
zero drawers, prints `Nothing to repair` (tests/test_cli.py:L1019-L1033).

Successful repair (with `--yes`) rebuilds drawers into a temp collection then a live
collection, prints `Repair complete` and `N drawers rebuilt`, and performs a precise
sequence of collection deletes against the temp and live collection names
(tests/test_cli.py:L1036-L1069). The configured collection name is used for the
read collection and the temp collection is named
`<collection>__repair_tmp`; deletes target the temp name, the live name, then the
temp name again (tests/test_cli.py:L1072-L1108). The rebuild uses upsert (not add)
into both temp and new collections (tests/test_cli.py:L1067-L1069).

Failure during the live rebuild restores from backup: the handler closes the palace
handle, prints `Repair failed` and `restoring from backup`, performs the delete
sequence (temp, live, temp), and exits with code 1 (tests/test_cli.py:L1111-L1142).

Post-repair cleanup ordering: the handler must close Chroma handles BEFORE running
the FTS5 vacuum/rebuild — the observable order is "close" then "vacuum", and the
vacuum is invoked with the palace directory as its first argument
(tests/test_cli.py:L1172-L1205). A clean legacy repair prints `FTS5 index rebuilt.`
and `SQLite VACUUM complete.`, does not print `post-repair cleanup failed`, and
leaves the database passing an integrity quick-check (`ok`)
(tests/test_cli.py:L1208-L1240). When the rebuild fails, the FTS5 vacuum/rebuild must
NOT fire, `Repair failed` is printed, and exit code is 1
(tests/test_cli.py:L1243-L1267).

Without confirmation (no `--yes`, interactive answer `n`), the handler prints
`Aborted.` and creates no new collection (tests/test_cli.py:L1270-L1288).

Trailing-slash handling: the palace path is right-stripped of the path separator
before the backup path (`<palace>.backup`) is computed, so the backup directory is
placed outside (not recursively inside) the palace directory
(tests/test_cli.py:L1595-L1605).

### `cmd_repair` from-sqlite mode exit codes

In `from-sqlite` mode, a returned empty result (`{}`) signals a validation refusal
(missing source DB, in-place without archive, or refusing to overwrite an existing
destination) and the handler exits with code 1
(tests/test_cli.py:L1666-L1692). A successful rebuild that returns a populated counts
mapping — even with all zero values (an empty but valid source) — must NOT exit;
only the empty mapping is reserved for refusal (tests/test_cli.py:L1695-L1721).

## `cmd_compress`

On a missing palace directory the handler exits non-zero and prints `No palace found`
(tests/test_cli.py:L1410-L1421). When the collection has no drawers, prints
`No drawers found` (tests/test_cli.py:L1424-L1437).

Dry-run mode (`--dry-run`) prints a banner containing `dry run`, `Compressing`, and a
`Total:` summary line without writing (tests/test_cli.py:L1449-L1485). When a config
file is provided, prints `Loaded entity config` (tests/test_cli.py:L1488-L1508).

Non-dry-run compress stores results into the `mempalace_closets` collection: it
upserts compressed documents, prints `Stored`, prints `Total:`, and mentions
`mempalace_closets` (tests/test_cli.py:L1511-L1552).

On-disk contract: compressed drawers written by `cmd_compress` must be readable via
the same closets-collection read path the rest of the system uses. A compressed drawer
keeps its original id, has a non-empty document, retains its `wing` metadata, and
carries a `compression_ratio` metadata key (tests/test_cli.py:L1555-L1592).

## `cmd_sync`

On a missing palace directory, prints `No palace found` (to stdout or stderr)
(tests/test_cli.py:L1294-L1304). On a palace directory that exists but lacks
`chroma.sqlite3`, prints `has no chroma.sqlite3 yet` and is side-effect free — the
backend is not invoked and no files are created in the directory
(tests/test_cli.py:L1307-L1319).

Daemon background sync (`--daemon --background`) submits a job named `sync` whose
payload is exactly `{dir, root, wing, dry_run}` from the parsed args, submitted with
`wait=False`, and prints the returned job id (tests/test_cli.py:L1322-L1346).

## `cmd_daemon`

When the daemon is not running, `cmd_daemon` reads the durable on-disk job queue
directly rather than failing. The `jobs` action lists queued jobs from the durable
queue: output contains the job id, the state `queued`, and the job type (`mine`)
(tests/test_cli.py:L1349-L1375). The `wait` action reads a finished job's result from
the durable queue and prints its stdout payload (e.g. `done`)
(tests/test_cli.py:L1378-L1407). The daemon state root is overridable via the
`MEMPALACE_DAEMON_STATE_ROOT` environment variable (tests/test_cli.py:L1358-L1387).

## Windows stdio reconfiguration

`_reconfigure_stdio_utf8_on_windows()` only acts on Windows (`platform == "win32"`).
On Windows it reconfigures the three standard streams to UTF-8 with per-stream error
policies: stdin uses `surrogateescape` (a redirected non-UTF-8 file must not crash the
read), while stdout and stderr use `replace` (a round-tripped surrogate half must not
crash mid-print) (tests/test_cli.py:L1619-L1646). Off Windows (e.g. Linux/macOS) the
helper is a no-op and touches no stream (tests/test_cli.py:L1649-L1660).

<promise>SPEC_WRITTEN path=specs/tests/test_cli.md citations=64</promise>
