# Behavior Specification — `mempalace/cli.py`

The CLI dispatcher for the `mempalace` console entry point. Parses argv, applies
global backend selection, then dispatches to one of a fixed set of subcommands.
Each subcommand resolves a palace path, calls into a library module, and reports
results to stdout/stderr with conventional exit codes.

## Entry point and global behavior

`main()` is the console entry point (`mempalace/cli.py:L1464-L2035`). On startup it
unconditionally removes `PYTHONPATH` from the process environment so any subprocess
the CLI spawns inherits a clean env; callers invoking `main()` programmatically lose
`PYTHONPATH` from their own process too (`mempalace/cli.py:L1474-L1478`). It then
reconfigures stdio to UTF-8 on Windows, decoding stdout/stderr with `replace` error
handling (so verbatim drawer text containing surrogate halves cannot crash a print
mid-output) while stdin keeps `surrogateescape` (`mempalace/cli.py:L1448-L1461`,
`L1480`).

Global flags available on the top-level parser: `--version` prints `MemPalace <version>`
and exits (`mempalace/cli.py:L1482-L1493`); `--palace PATH` overrides palace location,
default resolved from config (`mempalace/cli.py:L1494-L1498`); `--backend NAME` (stored
as `global_backend`) selects a storage backend for the command
(`mempalace/cli.py:L1499-L1504`).

After parsing, `_apply_backend_arg(args)` runs: if a backend was selected on either the
subcommand `--backend` or the global `--backend`, the name is normalized to
lowercase/trimmed, validated by resolving its backend class (raising if unknown), and
exported into the environment as both `MEMPALACE_BACKEND_EXPLICIT` and `MEMPALACE_BACKEND`
(`mempalace/cli.py:L57-L71`, `L1982`). The subcommand value takes precedence over the
global value (`mempalace/cli.py:L57-L59`).

If no subcommand is given, top-level help is printed and the process returns normally
(`mempalace/cli.py:L1984-L1986`). Two-level subcommands (`hook`, `instructions`,
`palace`, `daemon`) print their own help and return when no action is given
(`mempalace/cli.py:L1988-L2017`). All single-level subcommands dispatch through a fixed
map (`mempalace/cli.py:L2019-L2035`).

Palace path resolution is uniform across most commands: `os.path.expanduser(args.palace)`
when `--palace` is given, otherwise `MempalaceConfig().palace_path`
(e.g. `mempalace/cli.py:L532`, `L913`, `L930`, `L992`).

## Command: `init <dir>`

`cmd_init` (`mempalace/cli.py:L269-L434`). Sets up a project directory: entity detection,
corpus-origin detection, room detection, gitignore protection, and an optional immediate
mine.

Flags: `dir` (positional, project directory), `--backend`, `--yes` (auto-accept detected
entities, non-interactive), `--auto-mine` (skip the post-init mine prompt and mine
automatically), `--lang` (comma-separated language codes), `--llm` (deprecated no-op,
LLM is on by default), `--no-llm` (heuristics-only), `--llm-provider`
(`ollama|openai-compat|anthropic`, default `ollama`), `--llm-model` (default `gemma4:e4b`),
`--llm-endpoint`, `--llm-api-key`, `--accept-external-llm`
(`mempalace/cli.py:L1509-L1593`).

If `--palace` is given, `MEMPALACE_PALACE_PATH` is set to its absolute expanded path so
all downstream reads route to that location (`mempalace/cli.py:L280-L281`). Languages:
`--lang` (split on commas, empty parts dropped, falling back to `["en"]`) overrides and
is persisted to config; otherwise config's entity languages are used
(`mempalace/cli.py:L285-L292`).

LLM acquisition: unless `--no-llm`, a provider is requested with the configured
provider/model/endpoint/api-key (`mempalace/cli.py:L299-L309`). If available, prints
`  LLM enabled: <provider>/<model>` (`mempalace/cli.py:L310-L313`). If the provider is an
external service, prints an EXTERNAL API warning that folder content will be sent
(`mempalace/cli.py:L319-L326`). When the provider's api key was sourced from the
environment (`api_key_source == "env"`) and `--accept-external-llm` was not passed, an
interactive consent prompt requires a `y` answer; any other answer (or EOF) declines and
falls back to heuristics-only (`mempalace/cli.py:L332-L353`). If no provider is reachable,
or provider init raises `LLMError`, init prints a one-line message and continues
heuristics-only — init never blocks on a missing LLM (`mempalace/cli.py:L354-L362`).

Pass 0 (corpus-origin detection) via `_run_pass_zero` (`mempalace/cli.py:L364-L372`,
`L145-L238`): gathers samples, runs Tier-1 heuristic detection always, and Tier-2 LLM
detection only when a provider is available, then writes the result and returns a wrapped
dict for the entity classifier. Pass 1 discovers entities, printing `  Scanning for
entities in: <dir>` and the languages line when not just `("en",)`
(`mempalace/cli.py:L374-L385`). Pass 2 detects rooms from folder structure
(`mempalace/cli.py:L419-L420`). `cfg.init()` runs, and if a backend was selected it is
persisted to the palace (`mempalace/cli.py:L421-L424`). Pass 3 ensures gitignore
protection (`mempalace/cli.py:L426-L427`). Pass 4 offers/runs the mine
(`mempalace/cli.py:L429-L434`).

Entity confirmation: total counts people+projects+topics+uncertain
(`mempalace/cli.py:L386-L391`). When total > 0, entities are confirmed (auto-accepted under
`--yes`) (`mempalace/cli.py:L392-L393`). If any confirmed people/projects/topics exist,
they are written to `<project>/entities.json` (indent 2, UTF-8, non-ASCII preserved) and
merged into the global registry keyed by the normalized wing name (derived from the
project directory name); paths are printed (`mempalace/cli.py:L399-L415`). When total is
0, prints `  No entities detected — proceeding with directory-based rooms.`
(`mempalace/cli.py:L416-L417`).

### Pass 0 detail — `origin.json` contract

`_gather_origin_samples` scans the project for up to `_PASS_ZERO_MAX_FILES` (30) files,
skips MemPalace's own per-project files (`mempalace.yaml`, `entities.json`) for
idempotency, reads each file's full content capped at `_PASS_ZERO_PER_FILE_CAP`
(100,000 chars) with a total ceiling `_PASS_ZERO_TOTAL_CAP` (5,000,000 chars), skipping
unreadable/empty files (`mempalace/cli.py:L44-L51`, `L95-L132`). When there are no
readable samples, Pass 0 prints `  Skipping corpus-origin detection — no readable
samples.` and returns nothing (`mempalace/cli.py:L161-L164`).

Tier 1 heuristic detection always runs (`mempalace/cli.py:L166-L167`). Tier 2 runs only
when a provider is available, after trimming samples to `_PASS_ZERO_LLM_PER_SAMPLE`
(2,000) chars each and at most `_PASS_ZERO_LLM_MAX_SAMPLES` (20) samples
(`mempalace/cli.py:L52-L53`, `L135-L142`, `L181-L183`). Merge policy: the heuristic owns
`likely_ai_dialogue` and `confidence` (never overridden by the LLM); the LLM contributes
`primary_platform`, `user_name`, and `agent_persona_names` only when non-empty
(`mempalace/cli.py:L184-L192`). Evidence lists are combined and prefixed `Tier-1
heuristic: ` and `Tier-2 LLM: ` (idempotently — already-prefixed entries are not
re-prefixed) (`mempalace/cli.py:L193-L207`). Any Tier-2 exception is caught and reported,
falling back to heuristic-only (`mempalace/cli.py:L208-L209`).

The persisted file is `<palace>/.mempalace/origin.json`, a JSON object with keys
`schema_version` (1), `detected_at` (UTC ISO-8601 timestamp), and `result` (the detection
result dict), written indent 2, UTF-8, non-ASCII preserved
(`mempalace/cli.py:L211-L221`). On a file-write `OSError`, the failure is printed to
stderr but the wrapped dict is still returned so the in-memory pipeline benefits
(`mempalace/cli.py:L217-L226`). A one-line banner reports either the detected platform
(`  Detected: <platform> (user: <user>, agents: <agents>)`) or `  Corpus origin: not
AI-dialogue (confidence: <conf>)` (`mempalace/cli.py:L228-L236`).

### Gitignore protection

`_ensure_mempalace_files_gitignored` runs only when `<project>/.git` exists. It appends a
block `# MemPalace per-project files (issue #185)` followed by each missing entry
(`mempalace.yaml`, `entities.json`) to `.gitignore`, with a leading newline only when the
existing file does not already end in a newline; returns whether the file was updated
(`mempalace/cli.py:L44`, `L241-L266`).

### Post-init mine

`_maybe_run_mine_after_init` performs a single corpus scan via `scan_project`, reusing the
result both for the size estimate and as the mine input so the tree is not walked twice
(`mempalace/cli.py:L451-L520`). It prints an estimate line `  ~<N> files (~<size>) would be
mined into this palace.` when the scan succeeds, where size is rendered by
`_format_size_mb` (`<1 MB` for anything under 1 MB or non-positive, otherwise integer
megabytes) (`mempalace/cli.py:L437-L448`, `L494-L501`). Unless `--auto-mine`, it prompts
`  Mine this directory now? [Y/n] `; an empty answer or `y`/`yes` proceeds, any other
answer or EOF declines with a `Skipped. Run mempalace mine <dir> when ready.` message
(`mempalace/cli.py:L503-L512`). `--yes` does NOT imply mining — it only auto-accepts
entities (`mempalace/cli.py:L429-L433`, `L456-L462`). A mine `KeyboardInterrupt` is
re-raised; any other mine exception prints `  ERROR: mine failed: <e>` to stderr and exits
with code 1 (`mempalace/cli.py:L521-L528`).

## Command: `mine <dir>`

`cmd_mine` (`mempalace/cli.py:L531-L632`). Flags: `dir`, `--backend`, `--mode`
(`projects|convos|extract`, default `projects`), `--wing`, `--no-gitignore`,
`--include-ignored` (repeatable; comma-separated values are split and trimmed), `--agent`
(default `mempalace`), `--limit` (int, default 0 = all), `--redetect-origin`, `--dry-run`,
`--daemon`, `--background`, `--extract` (`exchange|general`, default `exchange`),
`--max-chunks-per-file` (int, default from miner module / env, 0 disables)
(`mempalace/cli.py:L533-L535`, `L1596-L1674`).

`--background` requires `--daemon`; otherwise prints `mempalace: --background requires
--daemon` to stderr and exits code 2 (`mempalace/cli.py:L537-L539`). With `--daemon`, a
payload of the mine parameters is submitted to the daemon queue (see Daemon submission)
(`mempalace/cli.py:L541-L556`).

`--redetect-origin` re-runs Pass 0 with no LLM provider (heuristic-only), overwriting
`origin.json` before mining (`mempalace/cli.py:L558-L566`). Dispatch by mode: `convos`
calls `mine_convos`, `extract` calls `mine_formats`, default calls `mine`
(`mempalace/cli.py:L570-L607`). `MineAlreadyRunning` prints `mempalace: <exc>` to stderr
and exits code 1 (`mempalace/cli.py:L608-L614`). `MineValidationError` (SQLite integrity
check failed after the mine) prints the recovery banner plus a multi-line FTS5-rebuild
message to stderr and exits code 1 (`mempalace/cli.py:L615-L632`).

## Command: `sweep <target>`

`cmd_sweep` (`mempalace/cli.py:L635-L674`). Flags: `target` (a `.jsonl` file or a directory)
(`mempalace/cli.py:L1677-L1685`). If `target` is a file, sweeps it and prints `  Swept
<target>: +<added> new, <present> already present, <skipped> skipped (< cursor).`
(`mempalace/cli.py:L650-L656`). If a directory, sweeps recursively and prints the
files-succeeded/attempted summary; if any files failed, prints a warning to stderr and
exits code 2 (`mempalace/cli.py:L657-L671`). If `target` is neither file nor directory,
prints `  ERROR: Not a file or directory: <target>` to stderr and exits code 1
(`mempalace/cli.py:L672-L674`). The sweeper deduplicates against its own prior writes via
deterministic drawer IDs and a timestamp cursor and does not coordinate with the
file-level miners (`mempalace/cli.py:L636-L644`).

## Command: `sync [dir]`

`cmd_sync` (`mempalace/cli.py:L677-L780`). Prunes drawers whose source files are gitignored,
deleted, or moved. Flags: `dir` (optional positional), `--wing`, `--root` (repeatable),
`--dry-run` (default True), `--apply` (sets dry_run False; overrides `--dry-run`, requires
`--wing` or a project root), `--daemon`, `--background`
(`mempalace/cli.py:L1688-L1727`).

`--background` requires `--daemon` (else stderr message + exit 2)
(`mempalace/cli.py:L681-L683`). With `--daemon`, the sync payload is submitted to the queue
(`mempalace/cli.py:L685-L693`). If the palace directory does not exist, prints `  No palace
found at <path>` and returns (`mempalace/cli.py:L701-L703`). If the backend cannot be
resolved, prints to stderr and returns (`mempalace/cli.py:L704-L708`). If the palace dir
exists but has no backend artifact, prints a "no … yet" message plus `  Run: mempalace
mine <dir>` and returns (`mempalace/cli.py:L709-L715`).

Project dirs are assembled from `dir` plus all `--root` values (expanded)
(`mempalace/cli.py:L717-L721`). A banner reports palace, optional wing, project dirs, and
mode (`DRY RUN` vs `APPLY`) (`mempalace/cli.py:L723-L736`). `MineAlreadyRunning` → stderr +
exit 1; `ValueError` → stderr + exit 2; any other exception → `mempalace: sync failed:
<exc>` + exit 1 (`mempalace/cli.py:L746-L754`). On success, prints a report:
`Scanned/Kept/Gitignored/Missing/No source/Out of scope` counts with `(would remove)` or
`(removed)` suffixes, an optional top-5 sources list, and either a re-run hint (dry run) or
the removed drawers/closets totals (apply) (`mempalace/cli.py:L756-L780`).

## Daemon job submission — `_submit_daemon_cli_job`

`_submit_daemon_cli_job(kind, payload, args, *, background)` (`mempalace/cli.py:L783-L817`).
Submits a job to the daemon with `auto_start=True` and `wait=not background`. On
`DaemonError` prints `mempalace: daemon submission failed: <exc>` to stderr and exits code 1
(`mempalace/cli.py:L788-L799`). When `background`, prints `Submitted daemon job <id>
(<kind>)` and returns (`mempalace/cli.py:L801-L803`). Otherwise prints the job result and
computes an exit code; if the job did not succeed but the printed exit code was 0, prints
the error message to stderr and forces exit code 1; a non-zero exit code triggers
`sys.exit` (`mempalace/cli.py:L805-L817`).

## Command: `daemon <action>`

`cmd_daemon` (`mempalace/cli.py:L820-L907`). Subactions: `start` (with `--foreground` and
`--backend`), `stop`, `status`, `jobs` (with `--limit`, default 20), `wait` (positional
`job_id`) (`mempalace/cli.py:L1891-L1909`).

- `start`: foreground runs the daemon in-process and returns; otherwise starts detached and
  prints the running host/port, palace path, and PID (`mempalace/cli.py:L836-L845`).
- `stop`: prints `MemPalace daemon stopping` if a daemon was stopped, else `MemPalace
  daemon is not running` (`mempalace/cli.py:L847-L852`).
- `status`: if not running prints the not-running message and exits code 1; else prints
  running status with palace, PID, active job id, and job counts
  (`mempalace/cli.py:L854-L865`).
- `jobs`: lists jobs from the live client when running, otherwise reads the on-disk queue
  store (empty list when no queue file). Each line is `<id>  <state>  <kind>  <created_at>`
  (`mempalace/cli.py:L867-L882`).
- `wait`: waits on a job via the live client, or reads the queue store and raises
  `DaemonError` if the daemon is not running and the job is not in a terminal state; prints
  the job result and computes exit code as in submission
  (`mempalace/cli.py:L884-L904`).
- Any `DaemonError` prints `mempalace: daemon error: <exc>` to stderr and exits code 1
  (`mempalace/cli.py:L905-L907`).

## Command: `search <query>`

`cmd_search` (`mempalace/cli.py:L910-L923`). Flags: `query`, `--backend`, `--wing`, `--room`,
`--results` (int, default 5) (`mempalace/cli.py:L1730-L1739`). Calls `search` with the
query, palace, wing, room, and result count. On `SearchError`, exits code 1
(`mempalace/cli.py:L914-L923`).

## Command: `wake-up`

`cmd_wakeup` (`mempalace/cli.py:L926-L937`). Flag: `--wing` (`mempalace/cli.py:L1754-L1755`).
Builds an L0+L1 wake-up text for the optional wing and prints `Wake-up text (~<tokens>
tokens):` (token estimate = `len(text) // 4`), a 50-character `=` rule, then the text
(`mempalace/cli.py:L931-L937`).

## Command: `split <dir>`

`cmd_split` (`mempalace/cli.py:L940-L960`). Flags: `dir`, `--output-dir`, `--dry-run`,
`--min-sessions` (int, default 2) (`mempalace/cli.py:L1758-L1778`). Translates its args into
a `--source/--output-dir/--dry-run/--min-sessions` argv (source resolved to absolute
expanded path; `--min-sessions` only included when not the default 2) and invokes the
`split_mega_files` entry point, restoring `sys.argv` afterward
(`mempalace/cli.py:L945-L960`).

## Command: `compress`

`cmd_compress` (`mempalace/cli.py:L1319-L1445`). Flags: `--wing` (default all wings),
`--dry-run`, `--config` (entity config JSON path) (`mempalace/cli.py:L1742-L1751`). Loads a
Dialect, optionally from `--config` or a discovered `entities.json` (project-relative then
palace-relative); prints `  Loaded entity config: <path>` when found
(`mempalace/cli.py:L1326-L1338`). Opens the `mempalace_drawers` collection via the
state-aware helper; if it cannot open, exits code 1 (`mempalace/cli.py:L1340-L1347`).

Drawers are read in batches of 500 (offset-paginated), optionally filtered by
`{"wing": <wing>}`; a read error with no accumulated docs prints the error and exits code 1,
otherwise the loop breaks (`mempalace/cli.py:L1349-L1377`). When no drawers found, prints
`  No drawers found[ in wing '<wing>'].` and returns (`mempalace/cli.py:L1379-L1382`). Each
drawer is compressed; in dry-run mode each entry's wing/room/source and token/ratio stats
are printed (`mempalace/cli.py:L1395-L1413`).

Unless dry-run, compressed entries are upserted into the `mempalace_closets` collection,
each carrying added metadata `compression_ratio`, `original_tokens`; prints `  Stored <N>
compressed drawers in 'mempalace_closets' collection.`; a storage error prints and exits
code 1 (`mempalace/cli.py:L1416-L1436`). A final summary prints estimated original→compressed
tokens (estimated at ~3.8 chars/token) and the overall ratio, with a `(dry run -- nothing
stored)` note in dry-run mode (`mempalace/cli.py:L1438-L1445`).

## Command: `mcp`

`cmd_mcp` (`mempalace/cli.py:L1293-L1316`). Flag: `--backend`
(`mempalace/cli.py:L1916-L1920`). Builds a `mempalace-mcp` server command, appending
`--palace <quoted-path>` when `--palace` is set and `--backend <quoted-name>` when a backend
is selected (`mempalace/cli.py:L1295-L1304`). Prints `claude mcp add` and `codex mcp add`
setup lines plus a direct-run line, and (when no `--palace` was given) optional custom-palace
variants (`mempalace/cli.py:L1306-L1316`).

## Maintenance commands (Chroma-gated)

`_maintenance_requires_chroma(palace_path, command_name)` resolves the palace's selected
backend; returns True only when it is `chroma`. On a resolution failure prints `  <command>
cannot resolve the palace backend: <exc>` to stderr and returns False; on a non-chroma
backend prints `  <command> is Chroma-only in this release (selected backend: <name>).` and
returns False (`mempalace/cli.py:L80-L92`).

### `migrate`

`cmd_migrate` (`mempalace/cli.py:L963-L974`). Flags: `--dry-run`, `--yes`
(`mempalace/cli.py:L1928-L1935`). Requires the chroma backend; raises `SystemExit(2)` when
not (`mempalace/cli.py:L966-L967`). Calls `migrate` with dry-run/confirm.

### `migrate-wings`

`cmd_migrate_wings` (`mempalace/cli.py:L977-L986`). Flags: `--dry-run`, `--yes`
(`mempalace/cli.py:L1942-L1947`). Normalizes legacy wing names; not Chroma-gated
(`mempalace/cli.py:L980-L986`).

### `status`

`cmd_status` (`mempalace/cli.py:L989-L993`). Flag: `--backend` (`mempalace/cli.py:L1949-L1954`).
Prints filing status for the palace.

### `repair-status`

`cmd_repair_status` (`mempalace/cli.py:L1043-L1050`). Read-only HNSW capacity check; requires
chroma (`SystemExit(2)` otherwise) (`mempalace/cli.py:L1046-L1047`).

### `repair`

`cmd_repair` (`mempalace/cli.py:L1053-L1276`). Flags: `--yes`, `--confirm-truncation-ok`,
`--mode` (`legacy|max-seq-id|from-sqlite`, default `legacy`), `--source`,
`--archive-existing`, `--segment`, `--from-sidecar`, `--backup` (boolean, default on),
`--dry-run` (`mempalace/cli.py:L1810-L1882`). Palace path is absolutized. Requires chroma
backend (`SystemExit(2)` otherwise) (`mempalace/cli.py:L1060-L1066`).

- `max-seq-id` mode: calls `repair_max_seq_id` with segment/sidecar/backup/dry-run/assume-yes
  and returns (`mempalace/cli.py:L1084-L1095`).
- `from-sqlite` mode: source defaults to the palace path; gated behind a destructive-action
  confirmation when the operation touches an existing/dest palace. `RebuildPartialError`
  prints a partial-failure message and exits code 1; an empty counts result (validation
  refusal) exits code 1 (`mempalace/cli.py:L1097-L1146`).
- `legacy` mode (default): aborts cleanly with messages when the palace dir or database is
  absent (`mempalace/cli.py:L1148-L1155`). Runs a SQLite integrity preflight; on errors prints
  the abort banner and exits code 1 (`mempalace/cli.py:L1157-L1167`). Repairs poisoned
  max_seq_id rows if needed (`mempalace/cli.py:L1169-L1176`). Reads existing drawer count
  (errors print a recovery message and return); returns if count is 0
  (`mempalace/cli.py:L1183-L1197`). Confirms the destructive action, extracts drawers in
  batches of 5000, cross-checks the extraction count against SQLite ground truth (catching
  `TruncationDetected` → print + return), creates a validated `<palace>.backup` copy, rebuilds
  the collection via a temp collection, and on `RebuildCollectionError` attempts an automatic
  restore from backup (with manual-recovery instructions on restore failure) and exits code 1
  (`mempalace/cli.py:L1199-L1268`). On success it cleans up the FTS5 index and prints `  Repair
  complete. <N> drawers rebuilt.` plus the backup location
  (`mempalace/cli.py:L1270-L1276`).

## `palace set-embedder`

`cmd_palace_set_embedder` (`mempalace/cli.py:L996-L1040`). Subcommand `palace set-embedder`
with flags `--model`, `--force`, `--backend` (`mempalace/cli.py:L1958-L1979`). Records (or
force-overrides) the palace's embedder identity. On `EmbedderIdentityMismatchError` prints
`  ✗ <exc>` and raises `SystemExit(2)` (`mempalace/cli.py:L1013-L1022`). Prints whether the
identity was recorded, unchanged, or changed (`mempalace/cli.py:L1023-L1031`). It records
identity on the palace only — it does not change the configured model; when the recorded
model differs from the configured `MEMPALACE_EMBEDDING_MODEL`, it prints how to align them
(`mempalace/cli.py:L1032-L1040`).

## Command: `hook run`

`cmd_hook` (`mempalace/cli.py:L1279-L1283`). Subcommand `hook run` with required `--hook`
(`session-start|stop|precompact`) and required `--harness` (`claude-code|codex`)
(`mempalace/cli.py:L1786-L1798`). Reads JSON from stdin and writes JSON to stdout via the
hook runner (`mempalace/cli.py:L1280-L1283`).

## Command: `instructions <name>`

`cmd_instructions` (`mempalace/cli.py:L1286-L1290`). Subcommands `init`, `search`, `mine`,
`help`, `status` (`mempalace/cli.py:L1806-L1807`). Outputs the named skill instructions to
stdout (`mempalace/cli.py:L1287-L1290`).

## Observable contracts summary

- Exit codes: `0` success/normal return; `1` for operational failures (search error, mine
  failure, sync/daemon/compress/repair errors, validation refusals); `2` for usage errors
  (`--background` without `--daemon`, sync `ValueError`, sweep-directory failures) and for
  Chroma-gate / embedder-mismatch `SystemExit(2)`; `130` for mine SIGINT handled inside the
  miner (`mempalace/cli.py:L539`, `L614`, `L632`, `L671`, `L683`, `L751`, `L799`, `L923`,
  `L967`, `L1022`, `L1047`, `L1066`, `L1136`, `L1145`, `L1167`, `L1268`, `L1347`, `L1436`,
  `L521-L528`).
- On-disk: `<palace>/.mempalace/origin.json` (schema_version 1, detected_at UTC ISO-8601,
  result dict) (`mempalace/cli.py:L211-L221`); `<project>/entities.json` (confirmed entities,
  indent 2, UTF-8) (`mempalace/cli.py:L400-L404`); `.gitignore` append block for MemPalace
  per-project files (`mempalace/cli.py:L261-L265`).
- Environment side effects: pops `PYTHONPATH`; may set `MEMPALACE_PALACE_PATH`,
  `MEMPALACE_BACKEND_EXPLICIT`, `MEMPALACE_BACKEND` (`mempalace/cli.py:L1478`, `L281`,
  `L70-L71`).
