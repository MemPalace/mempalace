# Changelog

All notable changes to [MemPalace](https://github.com/MemPalace/mempalace) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Fixed

- **Replaying an identical closed historical assertion through `add_triple` returns the
  original row instead of storing a duplicate.** The dedup check matched only open triples
  (`valid_to IS NULL`), so a historical insert with `valid_to` set was never recognized as a
  replay. An interrupted import that re-sent the same assertion stored a second logical
  assertion under a new id, and the as-of query returned the same fact twice. A replay must
  match subject, predicate, object, both bounds, confidence and all provenance columns; a
  recurring interval or an independently sourced assertion of the same interval stays two
  separate facts.
- **A writer with a different `HOME` no longer bypasses the palace's writer
  lock.** The lock that keeps one writer per palace lived only under
  `~/.mempalace/locks`, so a process started with another `HOME` on the same
  palace (a service account, a container, a profile manager) took its own lock
  and wrote beside the holder. Writers now also lock
  `.<palace-name>.mempalace.lock` next to the palace directory, which every
  process that can write the palace contends for, and a different-`HOME`
  writer is refused like any other second writer. The `~` lock is still taken
  too, so an older and a newer version that share a `HOME` keep excluding each
  other. When the palace's parent directory cannot hold the file, the file
  there cannot be opened, or the name there is a link or anything but a
  regular file, a warning is logged and only the `~` lock is used, as before.
- Registered Chinese, Japanese, and Korean entities are tagged when adjacent to
  other words or particles. Mixed-script names retain word boundaries on their
  non-CJK edges.
- Conversation mining compares transcript timestamps by instant rather than
  text order, so mixed UTC offsets and fractional seconds select the correct
  authored time. Invalid timestamps are ignored.
- Hybrid search breaks authored-time ties by instant across UTC offsets.

### Upgrade notes

- Existing conversation drawers keep their metadata when an unchanged source is
  skipped. For Chroma palaces with retained JSONL transcripts, run the
  [authored-time backfill](docs/authored-at.md#backfilling-existing-memory) from
  this checkout to correct previously selected timestamps without re-embedding.
  Preview with the default dry run before adding `--apply`.

---

## [3.12.0] — 2026-10-18

Mining keeps every byte of your sources, never leaves a file without its
drawers when a write fails part-way, and stores your messages as written.
The one-time re-read this release schedules reuses stored vectors, so it
mostly costs reading files. Timestamps are stored as UTC instants, every
tool picks a palace's knowledge graph and a project's wing the same way,
and a loopback hub needs no token while one reachable from another machine
does. Underneath, helpers that had been copied across backends, hooks, the
hub client and the LLM paths now live in one place each. MemPalace now
requires Python 3.10.

### Upgrade notes

- **MemPalace now requires Python 3.10 or newer.** Python 3.9 reached end
  of life in October 2025, and the optional extras (MarkItDown, the
  multimodal and EmbeddingGemma models, Milvus Lite, Pillow 12) already
  needed 3.10, so on 3.9 they installed as silent no-ops. Their
  `python_version` markers are gone. pip on Python 3.9 keeps resolving
  `mempalace` to the last release that supports it. The `mempalace-py`
  native wheel is built for the 3.10 stable ABI. CI tests 3.10, 3.11
  and 3.13.
- **Drawer `filed_at` and the other stored timestamps are now always UTC
  with an explicit offset** (`2026-10-17T13:00:00.123456+00:00`). Most
  writers used to store naive local time while the diary importer stored
  UTC, so on a machine not on UTC the `since`/`before` filters of
  `list_drawers`, `search` and `mempalace search` misplaced drawers by the
  local offset. Stored values are not rewritten: a timestamp without an
  offset is read as local time, as it was written, and the filters now
  compare instants. A bound without an offset (`2026-04-01`,
  `2026-04-01T09:30:00`) still means local time; a bound with `Z` or
  `+HH:MM` is now an exact instant instead of having its offset dropped.
  Newest-first listings (wake-up, `diary_read`, recent drawers on every
  backend) order by instant too, so a palace still receiving naive values
  from an older hub, hook or peer lists its newest drawer first. The
  response of `diary_write` now reports `timestamp` in UTC like the stored
  `filed_at`; its `date` stays the local day. Tunnel and hallway strength
  decay reads naive `created_at`/`last_activated` values as local time
  instead of UTC. Backup and set-aside file names keep their local-time
  stamps.

- **Every tool now picks a palace's knowledge graph the same way, and none
  switches graphs under you.** The MCP server, the CLI, the audit, the fact
  checker, mining and the Hermes provider share one rule. A palace uses its
  own `knowledge_graph.sqlite3` once that file holds facts. Until then it
  keeps reading and writing the graph it used before, if that one holds
  facts, so its facts never split across two files: for the default palace
  `~/.mempalace/palace`, and for any palace selected through
  `MEMPALACE_PALACE_PATH` or `config.json` rather than `--palace`, that is
  `~/.mempalace/knowledge_graph.sqlite3`; for the Hermes provider it is the
  graph beside its palace directory. Otherwise a new graph is created inside
  the palace. An empty graph file counts as no graph, so an empty palace-local
  file created by an older release no longer hides the facts. A palace named
  with `--palace` keeps its graph inside itself, as before. Reading the graph
  (MCP `kg_query`, `kg_timeline`, `kg_stats`, the fact checker) no longer
  creates the file. Graphs are never merged: when the palace's own graph and
  the one it used before both hold facts, the palace reads its own, and the
  MCP KG tools add a `notice` to their response (logged once) and
  `mempalace status` prints a note naming the other file. To bring those
  facts in, run `mempalace kg import-legacy` (it shows what it would copy;
  add `--yes` to copy, `--from <file>` for another graph). It skips facts the
  palace already records, matching on subject, predicate, object and
  validity window, and lists, rather than copies, a current fact whose
  subject and predicate already hold a different current value in the
  palace. The preview accounts for earlier accepted source facts, so its
  counts and conflicts match applying unchanged inputs. Reused fact IDs
  with different content or validity windows are reported separately and
  leave the target fact intact; they are never counted as copied.

- **A project now has one wing, whichever tool files into it.** Wing names
  come from one slug rule (lower case, spaces and hyphens become `_`), used by
  `mempalace init`, the miners, the diary, and the Claude Code, Codex, Cursor
  and Antigravity hooks. A session's diary checkpoints now land in the same
  wing as its transcript and as `mempalace mine` of the project, for example
  `my_app` instead of `wing_my_app`; a session started in the home directory
  files both into the machine's workstation wing. Cursor's hooks now turn
  hyphens into `_` like every other producer, the Cursor and Antigravity hooks
  ask that same Python function for a folder name with non-ASCII characters
  (`Café Notes` is `café_notes` everywhere), and an agent's default diary wing
  slugs the same way (`wing_cursor_ide`). Existing drawers are not renamed: a
  wing filter in search (the BM25 fallback included), `list_drawers`,
  `list_rooms`, `diary_read`, wake-up, `compress`, `sync` and closet
  regeneration also matches the older spellings: `wing_<slug>`, the
  hyphenated slug, the old Cursor rule (every byte outside `[a-z0-9_-]`
  written as `_`, so `café_notes` also reads `caf_notes`; skipped when it
  would drop a whole word, as `日本_api` → `api`), any stored wing whose name
  slugs to the same thing (`c_tool` reads `c++tool`; the palace's first
  10,000 wing names are checked), and, for the workstation wing, the
  `wing_<user>` wing home-directory diaries used. Two things to know:
  `list_wings`, `status` and the taxonomy still show one project under each
  raw spelling it was filed with, until those drawers are moved; and because
  the old Cursor rule wrote dots as `_`, a filter on `my.app` also returns a
  separate `my_app` project's drawers.

- **Every hook wrapper now picks its interpreter the same way.** The Claude
  Code plugin's Stop and PreCompact hooks now honour `MEMPAL_PYTHON`, and the
  Codex plugin hook now falls back to `python3` or `python` when `mempalace`
  is not on `PATH` (before, it tried only `mempalace`). The order everywhere is
  `MEMPAL_PYTHON`, `mempalace`, `python3`, `python`, taking the first that can
  import mempalace. Plugin hooks still exit 1 when none can; the legacy hooks
  under `hooks/` still reply `{}`.
- **A hook no longer imports a `mempalace` from the project directory.** Hooks
  run in your project directory, and Python puts that directory first on its
  import path, so a project holding `mempalace/` or `mempalace.py` (a mempalace
  checkout included) ran its own code instead of the installed package. The
  hook wrappers and the mines a hook starts now keep the working directory off
  the import path, on every supported Python version.

- **`python -m mempalace.dedup` is gone.** It deleted near-duplicate drawers
  and also any drawer shorter than 20 characters, so it could remove real
  memories. Nothing else in MemPalace called it. A re-mine already replaces a
  file's own drawers without duplicating them, and search folds identical text
  from different files at read time.
- **`python -m mempalace.onboarding` is gone.** `mempalace init` does project
  setup, and the one choice only onboarding offered, the embedding model, is
  now `mempalace init --embedding-model embeddinggemma` (or `minilm`,
  `embeddinggemma2`, `openai-compat`), which records it in `config.json`.
  `MEMPALACE_EMBEDDING_MODEL` and `embedding_model` in `config.json` work as
  before. Onboarding's `aaak_entities.md` and `critical_facts.md` bootstrap
  files are no longer written; nothing read them.
- **`mempalace init` records a backend chosen with `MEMPALACE_BACKEND`.** The
  backend is read from `config.json` before the environment, and a palace init
  has not created yet has nothing on disk to detect it from, so the first
  `mine` run without the variable used the default backend. When `config.json`
  names no backend, init now writes the one `MEMPALACE_BACKEND` selects. This is
  the one setting init takes from the environment; it never replaces a backend
  `config.json` already names, and an unknown name is reported and skipped.
- **The next mine re-reads every source once.** The drawer normalization
  version moves to 3, so each mined file reads as stale and is filed again
  through the write-then-prune path. Drawers whose text comes out the same
  keep their stored embedding, so the pass mostly costs reading files, not
  embedding them. A file whose text did not change also keeps its closets,
  LLM-generated ones included, and its AAAK summaries; in a changed file only
  the summaries of rewritten or removed drawers go. What changes: user turns
  are stored as written instead of spell-corrected, ChatGPT exports follow the
  branch the conversation ended on, lossy parsing steps are stamped in
  `transforms`, a conversation is filed under every file that holds it (files
  once skipped as duplicates get their own drawers), and non-UTF-8 sources
  keep their exact bytes.
- **Hallways move from `hallways.json` into `hallways.sqlite3`.** Both sit
  beside the palace directory. The first hallway write after the upgrade,
  usually the next mine, imports `hallways.json` in one transaction and then
  renames it to `hallways.json.imported-<stamp>-<random>`; nothing is
  deleted. Until that write, hallways are read from the JSON as before. On a
  palace with 830,000 hallways the import took 25 to 35 s, once. A
  `hallways.json` that does not parse is moved aside and the write is
  refused, as a save over it was before. A MemPalace older than 3.12 that
  mines into the same palace keeps writing `hallways.json`, which this
  version no longer reads, so upgrade every install that shares a palace.
  Hallways are derived from drawers: after a downgrade,
  `mempalace hallways --rebuild` writes them again.
- **A loopback HTTP MCP server needs no token; anything reachable from another
  machine does.** `mempalace serve` and `mempalace-mcp --transport http` on
  `127.0.0.1`, `::1` or `localhost` serve local clients with no header, as
  before. Without a token the hub refuses browser pages (any `Origin` other
  than its own exact address, any cross-site `Sec-Fetch-Site`) and DNS-rebound
  `Host` names, and it refuses requests carrying the usual proxy headers
  (`X-Forwarded-*`, `X-Real-IP`, `Forwarded`, `Via`, `Tailscale-*` and others).
  That proxy check is best effort: a forwarder that adds no headers, or a TCP
  forwarder such as `ssh -R` or `socat`, makes a tokenless hub public, so
  configure a token whenever anything forwards to it. `POST /mcp` now needs
  `Content-Type: application/json` on every hub, so a page cannot post to it
  without a CORS preflight. Pass `--token` or set `MEMPALACE_MCP_HTTP_TOKEN` to
  require a token on loopback. A network bind, or a loopback bind fronted by a
  proxy (`MEMPALACE_MCP_EXTRA_ALLOWED_HOSTS` set), reuses or creates the palace's
  token at `~/.mempalace/server/<key>/token` (`0600`), where local `mempalace`
  clients already look; remote clients send `Authorization: Bearer <token>`.
  `--no-token` does nothing on a plain loopback bind and is refused on any
  other; only `--allow-insecure` serves a reachable hub without a token. The
  token is never printed at startup; its path is.
- **MCP tool arguments are checked against the tool's schema before the tool
  runs.** Wrong types, out-of-range limits, unknown enum values and over-long
  strings return `-32602` with the parameter named, instead of reaching the tool
  (`"false"` used to arrive as a truthy string). Numeric and boolean strings
  (`"5"`, `"false"`) are still accepted and converted. An internal tool error
  reports its error class without the exception text.
- **`closet_llm` reads `MEMPALACE_LLM_ENDPOINT`, `MEMPALACE_LLM_MODEL` and
  `MEMPALACE_LLM_KEY` instead of the generic `LLM_ENDPOINT`, `LLM_MODEL` and
  `LLM_KEY`.** Generic names are often set for other tools, and reading them
  could send drawer content to an endpoint nobody chose for MemPalace. Rename the
  variables; when only the old names are set, the command says so. A non-local
  endpoint also needs `--accept-external-llm` or `MEMPALACE_ACCEPT_EXTERNAL_LLM=1`,
  the same consent `init`, `rooms propose` and `kg normalize` ask for, and every
  LLM provider now refuses a non-local endpoint until that consent is given.
- **The legacy shell hooks under `hooks/` no longer read `MEMPAL_VERBOSE`.**
  `mempal_save_hook.sh` and `mempal_precompact_hook.sh` now hand off to
  `mempalace hook run` like the plugin hooks, through a shared
  `mempal_hook_run.sh` that must stay next to them. To have the Stop hook block
  and ask the AI to save instead of saving silently, set `hooks.silent_save` to
  `false` in `~/.mempalace/config.json`. When no interpreter can import
  mempalace, the hooks print `{}` and exit 0 as before.
- **Conversation mining no longer spell-corrects your messages by default.**
  When the optional `autocorrect` package was installed, chat imports rewrote
  user turns before filing them. Turns are now stored as written. To keep the
  old behaviour, set `"convo": {"spellcheck": true}` in `config.json` or
  `MEMPALACE_CONVO_SPELLCHECK=1`; it corrects user messages only, and every
  drawer from a file whose text the speller changed carries
  `spellcheck_user` in its `transforms` metadata. Drawers filed before this
  release keep their corrected text until the source file is mined again.
- **`mempalace_search` no longer applies a default distance cutoff.** It used to
  drop every hit past cosine distance 1.5, which could hide a relevant but
  distant drawer. Pass `max_distance` to keep a cutoff; it is now read on the
  cosine scale (0 = identical, 2 = opposite) whatever metric the palace uses,
  and the response reports how many hits it dropped in
  `filtered_by_max_distance`. In `candidate_strategy: "union"`, a lexical-only
  hit whose vector distance can't be computed is kept with `distance: null` and
  `fallback_reason: "distance_unavailable"` instead of disappearing.

- **A conversation that appears in more than one file is filed under each of
  them.** Conversation mining used to drop a conversation it had already filed
  from another file in the same wing, so a re-exported bundle or a copied
  transcript had no drawers of its own and anything scoped to that file missed
  it. Each file now keeps its own drawers; re-mining a file still files nothing
  new. Files an older palace registered as duplicates are mined again once the
  conversation parsing version changes.

- **`mempalace init` writes only what you chose into a new `config.json`.**
  It used to copy the palace path, collection name, topic wings and hall
  keywords into the file, which froze those defaults against later releases
  and froze a `MEMPALACE_PALACE_PATH` from your shell into the config. A new
  file now holds only `--palace`, `--lang` and `--backend`; the defaults keep
  applying from the code and the default palace keeps following the config
  directory. `--palace` together with `--lang` now records both (`--lang` used
  to create the file first, so the palace path was lost). Existing
  `config.json` files are not touched. If you rely on `MEMPALACE_PALACE_PATH`
  for processes started without it, pass `--palace` to `init` or set
  `palace_path` in `config.json`.

- **Log records now carry their module's name.** Records that came from the
  flat `mempalace_mcp`, `mempalace_graph`, `mempalace_hallways`,
  `mempalace_llm`, `mempalace_logstream`, `mempalace_format_miner` and
  `mempalace_mcp_light` loggers now come from `mempalace.<module>` (for
  example `mempalace.miner`, `mempalace.mcp_server`), so one
  `logging.getLogger("mempalace")` setting covers all of them. Logging
  setup that names an old logger should name `mempalace` instead.
  Messages and levels are unchanged.

- **Every `MEMPALACE_*` switch reads true and false the same way.** Every
  boolean `MEMPALACE_*` variable accepts `1/true/yes/y/on` and
  `0/false/no/n/off`, case and surrounding spaces ignored, and a blank
  variable counts as unset. A value that does not parse logs one warning and
  falls through to `config.json` where the setting has one, then the
  default. Before, some switches took a smaller set of words, and an
  unrecognised value was read differently per variable:
  `MEMPALACE_HOOKS_AUTO_SAVE=garbage` counted as true, while
  `MEMPALACE_CONVO_SPELLCHECK` and `MEMPALACE_HOOKS_DAEMON` counted it as
  false. An unparseable hybrid search weight or `qdrant_timeout` now falls
  through to `config.json` instead of straight to the default, and a bad
  `MEMPALACE_DAEMON_RETENTION_DAYS` no longer makes `mempalace.daemon` fail
  to import.
- **Palace Query (`palace_query`, `palace_exec`) stores drawer and diary
  content as typed.** The parser used to turn `\n` and `\t` inside quoted
  content into real whitespace, collapse runs of spaces, and coerce values,
  so `3.10` was stored as `3.1`, `007` as `7` and `null` as `None`. Content,
  document, text, entry and diff values now stay the exact string: `"a\nb"`
  is stored as the four characters typed, and spacing between words is kept.
  Filter values in `FIND` and `LIMIT` still expand escapes and coerce numbers.
- **Knowledge graph entity ids keep punctuation, and re-adding an entity
  merges its properties.** `O'Brien` and `OBrien` used to fold into one
  entity, and re-adding an entity replaced all of its stored properties. New
  ids keep punctuation (`o'brien`, `obrien`), with `_` and `%` escaped as
  `%5F` and `%25` so no two names share an id. Existing entities keep their
  stored ids and are still found by the spellings that reached them before,
  unless the stored name keeps that same id under the new rule: an entity
  stored as `OBrien` is no longer found by `O'Brien`, which now gets its own
  entity.
  A re-add now merges properties key by key; a type other than `unknown`
  replaces the stored one. `KnowledgeGraph.report_entity_id_collisions()`
  lists existing entities whose old id may hold facts for more than one
  spelling.

### Added

- **`mempalace export <dir>` writes the palace out as markdown.** One folder
  per wing, one file per room, with an `index.md`; it was only reachable from
  Python before.
- **`mempalace_update_drawer` keeps the text it replaces.** Before an
  overwrite, the previous verbatim text and metadata are appended to
  `drawer_revisions.jsonl` in the palace directory, and the update is refused
  if that append fails. Partial OS writes are completed before the revision
  is acknowledged; a write that cannot finish or be synced leaves the
  original drawer intact. Closets are purged only after the new text is
  written, so a failed update leaves the drawer and its closets as they were.
  Deleting a drawer (`mempalace_delete_drawer`, `mempalace_delete_drawers`,
  `mempalace_delete_by_source`) removes its revisions too, after the delete
  commits; each delete result reports
  `revisions_deleted`. A re-mine and `sync` leave revisions alone.

### Security

- **A credential in an LLM or embedding endpoint URL no longer shows up in
  errors and logs.** An endpoint configured as
  `https://user:pass@host/v1?key=...` had its password and key repeated
  in the error a failed embedding request raised, in the `check_available`
  messages of the LLM providers, in the external-endpoint refusal and in
  the warnings that name the endpoint. Those messages now mask URL
  user info and `key`, `api_key`, `token`, `access_token`, `secret`,
  `password` and `auth` query values. An embedding server's reply quoted
  in an error is masked the same way and cut to 500 characters.
- **The light MCP server only calls the tools it lists.** `palace_query`,
  `palace_exec` and `palace_coordinate` used to turn an unknown target into
  `mempalace_<target>`, so a read-only query could reach `delete_drawer`,
  `delete_by_source` or `add_drawer`. Targets and actions now come from fixed
  allowlists, and anything else returns `-32602` without running. As a
  result, `palace_exec` with `delete_drawers` is refused. A query also no
  longer writes `config.json` when it is forwarded to a hub.
- **Daemon `mcp_tool` jobs pass the same checks as a direct MCP call.** They
  called tool handlers directly, skipping read-only mode, the integrity and
  stale-library gates, the peer-writer lease and argument validation. A
  read-only daemon now refuses a write job with `-32003`, and a job that
  meets a peer writer is deferred like one that meets a held lock.
- **LLM and embedding errors no longer carry keys.** Every model and
  embedding request goes through one client. The body of a 401 or 403 reply
  is dropped, and `sk-…` tokens, `Bearer` values, `api_key` fields and `key=`
  query parameters are masked in the error a failed model or embedding
  request raises.
- **State files are written through a fresh temporary file.** The writers of
  the room, tunnel, wing-split and `kg normalize` plans, the logstream watch
  cursor, `replica.json` and `mesh_state.json` used a fixed `.tmp` name (a
  pid-numbered one for `mesh_state.json`) opened without `O_EXCL`, so a file
  or link planted at that name was reused and a planted link was written
  through. `serverinfo.json` and
  the daemon's token, endpoint and pid files were written in place, where a
  reader could see half a record. All of them now create a new randomly named
  file and rename it into place.
- **On Windows, no other server can take over the HTTP hub's port.** The hub
  bound with `SO_REUSEADDR`, which on Windows lets a later socket bind a port
  that is already listening and receive some of its connections.
  `python -m http.server 8765`, a static preview server on the same port,
  answered requests meant for the hub: authenticated MCP calls failed with
  `RemoteDisconnected`, and the server that took them over received their
  `Authorization` header. On Windows the hub now binds with
  `SO_EXCLUSIVEADDRUSE`, so a later server on its port fails to start, and
  the hub itself refuses to start on a port another server holds. POSIX
  binds are unchanged.

### Performance

- **Writes to a large `sqlite_exact` palace no longer scan the full-text
  index.** Every add, upsert, update and delete removed the drawer's
  full-text row with a query that scanned the whole index, so each write
  cost time in proportion to the palace size. Full-text rows now share the
  drawer's row id and are removed by it. On a palace with 336,000 indexed
  rows, adding 50 drawers took 10 s and now takes 10 ms. The first writable
  open of an existing palace re-keys the rows that drifted, once (about 3 s
  for 3,000 rows).
- **A mine no longer reads and rewrites every hallway of the palace.** Each
  mine loaded the whole `hallways.json`, replaced its own wing's records and
  wrote the whole file back. Most of that time was spent decoding and
  encoding JSON while holding the GIL, so an HTTP hub serving the mine
  answered nothing else meanwhile. Hallways are now rows in
  `hallways.sqlite3`: a mine reads its own wing's rows, rewrites only the
  hallways whose count, rooms or entities changed, and leaves an unchanged
  record as stored, `created_at` included. Listing one wing's hallways, the
  `mempalace_list_hallways` page and `delete_hallway` read only what they
  return, and cross-wing entity tunnels read one strength per wing and
  entity instead of every record. On a copy of a 228,000-drawer palace with
  830,000 hallways (a 545 MB file), re-mining a wing went from about 20 s to
  1 to 4 s, listing one wing's hallways from 3.8 s to 27 to 56 ms, and the
  process peaked at about 150 MB instead of 3.8 GB. A wing holding 386,000
  of those hallways still takes about 30 s, now spent reading its drawers
  and pairing their entities rather than in the hallway file. The store is
  about 1.4 times the size of the JSON it replaces.
- **Newest-first reads on `sqlite_exact` are fast on large palaces.**
  `$in` and `$eq` filters on plain values, such as the wing filter wake-up
  builds from every spelling a wing was filed under
  (`{"wing": {"$in": [...]}}`), now run in SQL, and two indexes on `filed_at` (palace-wide and per wing) serve
  the newest-first reads. Existing palaces build them on their next writable
  open; a read-only open uses them when present and reads correctly without.
  On a 150k-drawer palace a 2,000-drawer recency read drops from 1.5 s to
  80 ms, and the wing-scoped one from 5 s to 60 ms.
- **The Stop hook's diary save runs in the background.** The hook imported
  the MCP server in the foreground, which alone took about half a second, the
  whole hook budget. Unless hooks route through the daemon, it now hands the
  checkpoint to a detached process and returns without loading the server,
  printing "checkpoint queued". That process files the checkpoint, then
  starts the transcript and project mines. When another writer holds the
  palace for more than about 30 s, the checkpoint is skipped and the next
  Stop retries.
- pgvector looks up drawers by id with its primary key instead of reading
  the table, so `get(ids=...)`, `add` and `update` no longer scan every row.
- Importing `mempalace.layers` no longer loads the search stack and
  chromadb (about 0.6 s down to under 0.1 s); the palace opens on the first
  read.

### Bug Fixes

- **Bookkeeping rows stay out of what you see.** The convo miner files a
  registry row (`ingest_mode="registry"`) for a transcript with no chunks, and
  the format miner a sentinel for a document that extracted to nothing. Search
  (vector, union and the BM25 fallback, MCP and CLI), `list_drawers`, wake-up
  and the L2/L3 layers, status (MCP and `mempalace status`), wing/room listings
  and the taxonomy, export and `compress` now leave them out, and the readers
  that return a page widen past them so the page still fills. The mining checks
  still read them. A real room a project names `_registry` is shown as usual.
- **Chat transcripts keep their own blank lines and Gemini messages join as
  written.** Claude Code messages used to have every run of three or more
  blank lines collapsed, even where no tool chrome was stripped; they now keep
  them, and where stripping chrome collapses a run the drawer carries
  `whitespace_collapse_internal`. Gemini messages split into several text parts
  were joined with spaces (JSON) or newlines (JSONL); the parts now join with
  nothing between them, the way Gemini's own clients read a message. Both
  apply on the re-read the version bump above already schedules.
- **Re-ingesting a diary no longer deletes the day before writing it.** A diary
  rebuild now goes through the same write-then-prune path as the miners, so a
  failed write keeps the previous entries and a failed prune is retried on the
  next ingest instead of leaving removed entries behind for good. Two diary
  files whose names start with the same date, in one wing, map to the same
  drawer ids; ingest now stops with a `CollisionError` naming both files
  instead of letting one silently overwrite the other. Rename one, or ingest
  them into separate wings. The collision check no longer treats a source
  re-filing its own drawer at a moved `chunk_index` as a collision.
- **A failed dimension probe no longer erases a recorded embedder dimension.**
  `mempalace palace set-embedder` and repair record the embedder in the palace
  sidecar. When the model's dimension probe failed, they wrote a dimension of 0
  over the one already recorded. A failed probe now keeps the recorded dimension
  for the same model, and records a new model as unknown with a warning.
- **`mempalace repair --mode max-seq-id` no longer runs alongside a writer.**
  The repair rewrote Chroma's `max_seq_id` rows without the palace lease, so a
  mine or an MCP write could land in the middle of it. It now takes the lease,
  works out the repair again once it holds it, and exits with code 2 without
  changing anything when another writer holds the palace. Its backup is taken
  through SQLite's backup API, so writes still in the WAL are part of it. The
  same applies to the max_seq_id check that runs before a repair rebuild.
- **Graph tools no longer report an unreadable palace as an empty one.**
  `traverse`, `find_tunnels` and explicit `create_tunnel` used to treat any
  failure to open the palace (a lock, a corrupt index) as "no rooms", and an
  explicit tunnel was let through when its room check failed. A palace that
  does not exist yet is still an empty graph, and any other failure, while
  opening the palace or reading its drawers, returns "palace could not be
  read" instead. Graph reads ask for a read-only open: on sqlite_exact that
  is a read-only connection; on Chroma it creates no files and skips the HNSW
  thread pin, but chromadb's own client still updates its bookkeeping when it
  opens a palace, so drawers and their metadata are left as they were rather
  than the file bytes.
- **Search says when it rewrote your query.** The `sanitizer` block on a
  sanitized `mempalace_search` result now carries `truncated` and
  `replaced_lone_surrogates`, and a query whose only change was a replaced
  lone surrogate is reported as sanitized. `original_length` counts the
  query as sent. `mempalace search` on the command line and the Hermes
  search tool now sanitize the same way: the CLI notes the rewrite on stderr,
  and Hermes returns the same `sanitizer` block.
- **Drawer bookkeeping fields are stored as numbers, flags and ISO text.**
  `chunk_index`, `chunk_total`, `line_start`, `line_end`, `entry_index` and
  the version stamps are stored as integers, `source_mtime` and
  `compression_ratio` as floats, `is_sentinel` as a boolean, and `filed_at`
  / `created_at` datetimes as ISO text, whatever form a writer passed. Range
  filters such as `{"chunk_index": {"$gt": 9}}` compare numbers instead of
  text, and the already-mined check reads legacy rows that hold `"1"` as 1
  instead of re-mining them. A value that does not parse, or an integer too
  big for 64 bits, is stored as given. Hallways, room proposals and sync no
  longer treat an old row whose `is_sentinel` is `"false"` as a sentinel.

- **Embedding a single string gives one vector.** The MiniLM and
  OpenAI-compatible embedding functions treated a bare string as a list of
  characters and returned one vector per character. Every embedding function
  now reads a bare string as one document.
- **A failed embedding dimension probe is retried.** When the embedder could
  not answer the first probe (a local server still starting, a model still
  loading), the dimension was cached as unknown for the rest of the process, so
  a later identity record, such as the one a repair writes, kept no dimension.
  Only a successful probe is cached now, and the first failure for a model is
  logged as a warning.
- MemPalace's own project files are handled under every name the miner
  reads. A project using `mempalace.yml` or the legacy `mempal.yaml` no longer
  has that file sampled by entity and corpus-origin detection on a re-run of
  `init`, and `init` adds the config file the project actually uses to
  `.gitignore` instead of always `mempalace.yaml`. The skip lists now come
  from one place. `init` also no longer overwrites an `entities.json` that
  holds AAAK entity codes for `mempalace compress`.
- **Mining no longer turns bytes that are not UTF-8 into U+FFFD.** Project,
  conversation, diary and sweep ingest decoded files as UTF-8 and replaced
  anything else with the replacement character, so a cp1252 file lost its
  smart quotes and accented letters for good. Now a UTF-8 file with a few
  stray bytes (its valid UTF-8 characters outnumber them) keeps its text
  exactly, with only the stray bytes held as lone surrogates, so `café` stays
  searchable. Any other file decodes as windows-1252, with every byte mapped.
  The palace stores every byte exactly. MCP responses show a stray byte as
  U+FFFD, because some clients' JSON parsers reject the escape that would
  carry it; `mempalace_get_drawer` with `include_source_bytes: true` returns
  the drawer's exact source bytes as base64. Exports write the original
  bytes, and MemPalace processes talking to each other (proxy and hub, the
  daemon, peer sync) pass them exactly. `mempalace_update_drawer` with new
  content drops `source_encoding` and `transforms`, since that text did not
  come from the source file. A drawer whose source was not plain UTF-8 carries
  `source_encoding` (`utf-8-sig`, `utf-8+surrogateescape`,
  `utf-8-sig+surrogateescape` or `windows-1252`), and
  `fsio.encode_source(text, source_encoding)` gives the source bytes back.
  The encoding-repair script (`scripts/mempalace_repair_encoding.py`) leaves
  those drawers alone. Drawers filed before this release keep their
  replacement characters until their file is mined again.
- **A re-mine no longer loses a file's drawers when it fails part-way.** The
  project and conversation miners deleted a changed file's drawers before
  writing the new ones, so a crash or backend error during the write left the
  file with nothing in the palace. All three miners now file through one
  write path: the new drawers are written first, marked unfinished, then the
  stale ones are removed and the marker is cleared. A failure at any point
  leaves the old or the new text readable, and the next mine finishes the file.
- **A mine stops when it cannot read which files are already filed.** The
  already-mined check used to answer "not mined" on any palace read error, so a
  re-mine re-filed files it could not see. It now raises `MinedSetUnavailable`
  and the project, conversation and format miners all abort the run on it. A
  pass that crashed between batches (fewer drawers than the largest
  `chunk_total` any of them names) reads as unfinished on every check.
- ChatGPT imports file the branch the conversation ended on (`current_node`),
  not the first draft of every regenerated or edited turn.
- A Slack message with `"user": null` is filed under `unknown` instead of
  failing the whole export.
- **Conversation drawers now say when their text is not the source as
  written.** Mining a chat transcript truncates long tool output, leaves out
  some tool results, cuts tool calls down to one line, strips UI chrome, drops
  thinking, image and system content, and adds its own markers, and none of
  that was recorded. Every drawer from a file whose parse did any of these now
  carries a `transforms` field listing the RFC 002 names, sorted and
  comma-joined (for example `strip_tool_chrome,tool_result_truncate`). The
  stamp covers the whole file, so in a bundle export it also lands on
  conversations the step did not touch. The text itself is unchanged. Drawers
  filed before this release carry no stamp until their file is mined again.
- `mempalace search` no longer hands a search to the running hub when the
  hub would ignore one of your settings. Hybrid-rank weights, the pgvector
  shared namespace and the EmbeddingGemma 2 dimension, modalities and
  revision now keep the search local when set in the environment, and a
  change to them in `config.json` no longer reuses a hub started with the
  old values.
- **A closet match keeps lifting its drawer in the final search order.** The
  hybrid re-rank scored every hit from its raw vector distance, so the rank
  boost a matching closet gave a drawer was dropped and a closet-backed drawer
  could fall behind a plain one. The boost is now part of the hybrid score. The
  boost's distance cap and clamp, and the light server's relevance confidence,
  now read distances on the cosine scale, so palaces using `l2` or `ip` rank and
  report the same way as cosine ones. Search responses name the raw distance's
  scale in `distance_metric`.
- **`mempalace compress` no longer writes into the closets that search reads.**
  Its lossy AAAK summaries were stored alongside closet pointers, so they
  boosted search results like real closets. They now go to their own
  `mempalace_aaak` collection, one row per drawer with `kind="aaak"`, and
  repair and migrate carry it over. Deleting or rewriting a drawer (by any
  delete tool, `sync`, or a re-mine) deletes its summary too. Search already
  ignores summaries an older `compress` left in the closets; the next
  `compress`, `repair --mode from-sqlite` or `migrate` moves them out.
- **`wake-up` no longer says "No memories yet" when it could not read the
  palace.** A recent-memories read that failed, for example while a mine held
  the database, was swallowed and the wake-up reported an empty palace; a scan
  that failed part-way showed whatever it had read as the whole window. Each
  read is now retried briefly, and one that still fails says the palace could
  not be read right now.
- A backend's `update` never creates a record for an id that does not exist.
  The built-in backends already skipped missing ids; the default that
  third-party backends inherit wrote them as empty records.
- **Qdrant and pgvector keep a drawer's metadata when one value is not
  JSON.** One value that is not JSON, such as a date, replaced the drawer's
  whole metadata with `{}`, losing its wing, room and source, and a NaN was
  sent as a token the database rejects. Such values are now stored as text,
  as Milvus already did, and the rest is kept. The remote
  backends' target markers are published atomically and rewritten only when
  the target changes, so `created_at` is kept.
- **NUL characters and lone surrogates in drawer text and metadata values are
  stored exactly.** Chroma, pgvector and Milvus used to strip or replace them
  before storage. A string holding one is now stored escaped, the record is
  flagged, and reads return the original text; filters by a value read back
  still match. Ids and metadata keys holding either character are refused.
- **`mempalace migrate` keeps the rest of the palace.** It rebuilt drawers
  from every SQLite collection and then swapped directories, dropping the
  knowledge graph, closets, assets, the embedder sidecar and every other
  palace file. It now rebuilds through the same path as
  `repair --mode from-sqlite`, copies everything that is not Chroma's own
  storage across (SQLite files through the backup API), refuses the swap
  unless every rebuilt collection matches the source's row count, and never
  deletes an existing `.old` directory. `migrate` takes the palace lock and
  exits 2 when another writer holds it.
- **`mempalace mine --mode extract` holds the palace lock and keeps the last
  good text.** A re-mine of a changed document deleted every drawer for that
  path first and swallowed a failed delete. It now writes the new drawers,
  then removes the stale ones, and a crash in between reads as unfinished so
  the next mine redoes the file. Only format-mode drawers are replaced. A
  failed extraction keeps the earlier drawers; only a file that extracted to
  nothing loses them. A per-file error ends the mine with a nonzero exit
  instead of 0.
- **A transcript appended to while it is mined is mined again.** The
  conversation miner took the file's mtime after reading it, so an append in
  that gap was stamped as already mined and skipped for good. It now stamps
  the mtime taken before the read. One export that fails no longer stops the
  rest of the run; the mine still ends with an error listing it.
- **`mempalace split` keeps everything.** It dropped the text before the
  first session and any session under 10 lines, and two sessions with the
  same generated name overwrote each other. The preamble is written as
  `part00`, short sessions are written, a taken name moves the session to
  the next `-2`, `-3` name (never through a link or a pipe), and a rerun
  writes no duplicate set. The original is renamed to the first free
  `.mega_backup` name, so an older backup is never replaced.
- **`mine --mode convos --extract general` keeps paragraphs that match no
  memory type.** They were dropped; they are now stored with
  `memory_type: general`. Files mined in this mode before are mined again on
  the next run.
- **Closet search returns each hit as its own drawer.** A closet hit's text
  was replaced with neighbouring chunks of the same file while keeping the
  original drawer id, so de-duplication could drop a different drawer whose
  rewritten text matched. `text` is now the matched drawer verbatim, the
  neighbours go in `context`, and only a repeat of the same stored chunk is
  removed. Several distinct chunks of one file can now appear in the top
  results.
- **A state file that does not parse is moved aside, not overwritten.** A
  corrupt `tunnels.json`, hallways file, entity registry or embedder sidecar
  used to be replaced on the next save with an empty or one-entry document.
  The save now renames it to `*.corrupt-<timestamp>-*` and raises
  `CorruptStateError`; reads still treat it as empty.
- **Windows: files another process holds no longer break saves and reads.**
  Config, the update-check cache and the other state files are published
  through one writer that retries a rename a reader briefly blocks, instead
  of failing or silently dropping the write (a background update check could
  lose a new release this way). Readers of files that are published over
  (the daemon and hub tokens, `serverinfo.json`, the update cache, the watch
  cursor) retry the brief access-denied window instead of reading the file as
  empty or corrupt. The cross-process mine lock rides out a lock file whose
  delete is still pending and waits past the ten tries Windows' own lock call
  gives up after, which used to crash a waiting `serve` start or mine.
- **The Hermes provider writes through the palace's configured backend.** It
  opened a Chroma backend whatever the palace used, and logged failed writes
  at debug level. It now opens the palace's own collection, counts and logs
  failures as warnings (without the text), and waits for a mine holding the
  lock instead of dropping the turn. The provider's own
  `mempalace_diary_write` tool now files diary drawers, its
  `mempalace_diary_read` tool lists entries still in an old `diary.jsonl`
  before them, and `import_legacy: true` files those entries as drawers once
  and renames the file to `diary.jsonl.imported`.
- `mempalace --palace PATH serve` serves that palace; the `serve`
  subcommand's own default used to replace the global flag with the default
  palace.
- A `sqlite_exact` reader no longer keeps serving the palace as it was
  before a small write. A cached read-only handle was retired only when the
  file's signature changed, which a small write after a checkpoint could
  leave the same. Every write now advances a generation the signature
  includes.
- The `mempalace-mcp` stdio proxy keeps `mempalace_search` and
  `mempalace_check_duplicate` local when its environment or `--backend` sets
  search settings the hub may not share, the rule `mempalace search` already
  applied. Every other tool still goes to the hub.
- `follow_tunnels` keeps recording crossings in a session that found another
  writer holding the palace; such a session used to stop recording them.
  Recording takes the tunnel file's own lock and never needed the writer
  lease.
- The daemon, hooks and server registry share one check of whether a process
  is alive. On Windows a process that exists but denies access now counts as
  alive, where some copies treated it as gone.
- Ten read tools that lacked it now advertise `readOnlyHint`: `traverse`,
  `find_tunnels`, `graph_stats`, `list_tunnels`, `list_hallways`,
  `follow_tunnels`, `mesh_peers`, `event_list`, `event_wait` and
  `artifact_get`.
- The hub's replication and mesh routes (`/sync/version_vector`,
  `mempalace_mesh_peers`) count drawers under the request lock when it is
  free and otherwise report the last count, so they never queue behind a
  long write, and peer-sync integrity fields are published
  together, so one answer never mixes two refreshes.
- `mempalace mine --source media` keeps `--wing` and `--related-drawer-id`
  when the mine runs through the daemon. The daemon job dropped both, so the
  assets were filed with no project and no related drawer.

### Pull requests merged since 3.11.0

- [#2744](https://github.com/MemPalace/mempalace/pull/2744) perf(sqlite-exact): synchronize docs_fts.rowid with documents.rowid for O(log N) writes
- [#2753](https://github.com/MemPalace/mempalace/pull/2753) fix(sqlite_exact): retire immutable readers after every commit
- [#2754](https://github.com/MemPalace/mempalace/pull/2754) ci: single-source the coverage threshold
- [#2758](https://github.com/MemPalace/mempalace/pull/2758) fix(cli): serve keeps the global --palace flag
- [#2759](https://github.com/MemPalace/mempalace/pull/2759) fix(hermes): use the palace write path and diary drawers
- [#2760](https://github.com/MemPalace/mempalace/pull/2760) perf(hooks): move the Stop diary save off the foreground
- [#2761](https://github.com/MemPalace/mempalace/pull/2761) fix(proc): share one pid_alive across daemon, hooks, and registry
- [#2762](https://github.com/MemPalace/mempalace/pull/2762) fix(hooks): legacy bash hooks delegate to hook run
- [#2763](https://github.com/MemPalace/mempalace/pull/2763) refactor(mcp): derive tool capability sets from one registry
- [#2764](https://github.com/MemPalace/mempalace/pull/2764) fix(service): route mcp_tool jobs through the dispatcher gates
- [#2765](https://github.com/MemPalace/mempalace/pull/2765) fix(mcp-light): allowlist palace targets and answer stdio errors
- [#2766](https://github.com/MemPalace/mempalace/pull/2766) fix(mcp-http): lock the node profile and publish one integrity verdict
- [#2767](https://github.com/MemPalace/mempalace/pull/2767) fix(mcp): keep a recoverable copy before update_drawer overwrites
- [#2768](https://github.com/MemPalace/mempalace/pull/2768) fix(mcp): record tunnel crossings without the writer lease
- [#2769](https://github.com/MemPalace/mempalace/pull/2769) perf(layers): wake up without importing the search stack
- [#2770](https://github.com/MemPalace/mempalace/pull/2770) fix(kg): keep punctuation in entity ids and merge properties on re-add
- [#2771](https://github.com/MemPalace/mempalace/pull/2771) fix(search): return closet hits as their own drawers
- [#2772](https://github.com/MemPalace/mempalace/pull/2772) fix(pql): keep stored content text opaque
- [#2773](https://github.com/MemPalace/mempalace/pull/2773) fix(llm): refuse silent calls to external endpoints
- [#2774](https://github.com/MemPalace/mempalace/pull/2774) fix(general_extractor): keep paragraphs that match no keyword
- [#2775](https://github.com/MemPalace/mempalace/pull/2775) fix(format_miner): hold the palace lock and keep failed rewrites recoverable
- [#2776](https://github.com/MemPalace/mempalace/pull/2776) fix(split): keep the preamble, short sessions, and colliding names
- [#2777](https://github.com/MemPalace/mempalace/pull/2777) fix(convo_miner): stamp the mtime from the read, not a later stat
- [#2778](https://github.com/MemPalace/mempalace/pull/2778) fix(fsio): publish config through one Windows-safe atomic writer
- [#2779](https://github.com/MemPalace/mempalace/pull/2779) fix(migrate): keep closets, the graph, and sidecars across a verified swap
- [#2780](https://github.com/MemPalace/mempalace/pull/2780) fix(storage): escape NUL and lone surrogates reversibly
- [#2781](https://github.com/MemPalace/mempalace/pull/2781) perf(pgvector): look up ids with the primary key
- [#2782](https://github.com/MemPalace/mempalace/pull/2782) fix: refuse to overwrite a state file that did not parse
- [#2783](https://github.com/MemPalace/mempalace/pull/2783) fix(backends): keep serializable metadata and publish markers atomically
- [#2788](https://github.com/MemPalace/mempalace/pull/2788) ci: run the test workflow on every pull request
- [#2789](https://github.com/MemPalace/mempalace/pull/2789) test(format_miner): compare the stored mtime with the miner's 1 ms tolerance
- [#2790](https://github.com/MemPalace/mempalace/pull/2790) fix(convo): keep user turns as written; ChatGPT active branch; Slack null user
- [#2791](https://github.com/MemPalace/mempalace/pull/2791) fix(mine): stop the mine when the mined set cannot be read
- [#2792](https://github.com/MemPalace/mempalace/pull/2792) fix(search): no default distance cutoff, one cosine scale, keep unscored BM25 hits
- [#2795](https://github.com/MemPalace/mempalace/pull/2795) fix(search): keep the closet boost through the hybrid rank and on the cosine scale
- [#2796](https://github.com/MemPalace/mempalace/pull/2796) fix(convo): stamp lossy parsing steps on every drawer of the file
- [#2797](https://github.com/MemPalace/mempalace/pull/2797) fix(hub): derive search forwarding and the hub fingerprint from one settings table
- [#2798](https://github.com/MemPalace/mempalace/pull/2798) refactor(mine): file every miner's drawers through one write-then-prune path
- [#2800](https://github.com/MemPalace/mempalace/pull/2800) fix(convo): file a conversation under every file that holds it
- [#2801](https://github.com/MemPalace/mempalace/pull/2801) fix(compress): store AAAK summaries in their own collection, not the closets
- [#2802](https://github.com/MemPalace/mempalace/pull/2802) fix(mine): decode source files without turning bytes into U+FFFD
- [#2803](https://github.com/MemPalace/mempalace/pull/2803) fix(embedding): embed a bare string as one document and retry a failed dimension probe
- [#2804](https://github.com/MemPalace/mempalace/pull/2804) fix(backends): update skips missing ids, and wake-up reads recent memories within its budget
- [#2805](https://github.com/MemPalace/mempalace/pull/2805) fix(init): record only explicit choices and name every project file once
- [#2806](https://github.com/MemPalace/mempalace/pull/2806) fix(mcp): validate tool arguments at dispatch and require an HTTP token by default
- [#2807](https://github.com/MemPalace/mempalace/pull/2807) fix(repair): run the max_seq_id repair under the palace lease and hoist id-set rebuilds
- [#2808](https://github.com/MemPalace/mempalace/pull/2808) fix: two order-dependent test failures, one of them a Windows cache-write bug
- [#2809](https://github.com/MemPalace/mempalace/pull/2809) fix: honest graph errors, a sanitizer that says what it changed, typed bookkeeping metadata
- [#2812](https://github.com/MemPalace/mempalace/pull/2812) test(stray-bytes): serve the loopback hub tokenless now that the token is on by default
- [#2813](https://github.com/MemPalace/mempalace/pull/2813) feat(mine): re-read every source once for the parsing fixes, reusing unchanged vectors
- [#2814](https://github.com/MemPalace/mempalace/pull/2814) fix(embedding): keep a recorded dimension when the probe fails for the same model
- [#2815](https://github.com/MemPalace/mempalace/pull/2815) fix(init): record a backend chosen with MEMPALACE_BACKEND
- [#2816](https://github.com/MemPalace/mempalace/pull/2816) fix(proxy): keep a search local when this session chose its own settings
- [#2817](https://github.com/MemPalace/mempalace/pull/2817) refactor(palace): purge what drawers leave behind through one drawers_removed helper
- [#2818](https://github.com/MemPalace/mempalace/pull/2818) refactor(diary): rebuild a diary through the shared write-then-prune path
- [#2820](https://github.com/MemPalace/mempalace/pull/2820) fix(lock): ride out Windows delete-pending opens and wait past LK_LOCK's ten tries
- [#2821](https://github.com/MemPalace/mempalace/pull/2821) fix(serve): keep a local-only loopback hub tokenless, and harden it against proxies and browsers
- [#2822](https://github.com/MemPalace/mempalace/pull/2822) fix(fsio): retry renames a Windows reader blocks, and fail fast on a lasting lock-file denial
- [#2823](https://github.com/MemPalace/mempalace/pull/2823) fix: keep bookkeeping rows out of user-facing readers, and keep chat whitespace and Gemini parts as written
- [#2824](https://github.com/MemPalace/mempalace/pull/2824) fix(mine): read stored vectors only for unchanged drawers, and re-embed if the read fails
- [#2825](https://github.com/MemPalace/mempalace/pull/2825) chore(lint): sort imports and drop unused noqa marks (ruff stage 0)
- [#2828](https://github.com/MemPalace/mempalace/pull/2828) ci: fail the Rust job on clippy warnings
- [#2829](https://github.com/MemPalace/mempalace/pull/2829) refactor: remove dedup.py and clean the backend and palace comments
- [#2830](https://github.com/MemPalace/mempalace/pull/2830) chore(search): drop dead closet helpers and PR-history comments
- [#2831](https://github.com/MemPalace/mempalace/pull/2831) chore(mcp): drop dead code and PR-history comments in the MCP server area
- [#2832](https://github.com/MemPalace/mempalace/pull/2832) chore(cli): wire mempalace export, drop onboarding.py, and clear dead code and PR-history notes
- [#2833](https://github.com/MemPalace/mempalace/pull/2833) chore: drop dead helpers and PR-history comments in the remaining modules
- [#2834](https://github.com/MemPalace/mempalace/pull/2834) chore(mine): drop dead helpers and PR-history comments in the miners
- [#2835](https://github.com/MemPalace/mempalace/pull/2835) test(layers): give the wake-up budget test headroom on slow CI runners
- [#2836](https://github.com/MemPalace/mempalace/pull/2836) chore: fail on PR and plan history in comments, docstrings and help text
- [#2837](https://github.com/MemPalace/mempalace/pull/2837) chore(lint): turn on bugbear, return and commented-out-code checks
- [#2838](https://github.com/MemPalace/mempalace/pull/2838) fix(update): retry a read that races a publish of the release cache
- [#2839](https://github.com/MemPalace/mempalace/pull/2839) refactor(hooks): one interpreter resolver for every hook wrapper
- [#2840](https://github.com/MemPalace/mempalace/pull/2840) refactor(lexical): one tokenizer and BM25 scorer for the searcher and every backend
- [#2841](https://github.com/MemPalace/mempalace/pull/2841) refactor: write every durable state file through fsio
- [#2842](https://github.com/MemPalace/mempalace/pull/2842) refactor(hub): forward every hub call through one HubClient
- [#2843](https://github.com/MemPalace/mempalace/pull/2843) refactor(backends): evaluate where filters through one shared _filters module
- [#2844](https://github.com/MemPalace/mempalace/pull/2844) refactor(llm): parse every LLM reply and send every model request through llm_client
- [#2845](https://github.com/MemPalace/mempalace/pull/2845) refactor: one wing slug for every producer, and diary and ingest share the project wing
- [#2846](https://github.com/MemPalace/mempalace/pull/2846) refactor(backends): validate every write batch through one helper
- [#2847](https://github.com/MemPalace/mempalace/pull/2847) refactor(env): parse environment variables through one shared helper
- [#2848](https://github.com/MemPalace/mempalace/pull/2848) refactor(chroma): one home for the raw-SQLite readers (backends/chroma_sqlite.py)
- [#2849](https://github.com/MemPalace/mempalace/pull/2849) refactor(paths): build every MemPalace state path through mempalace.paths
- [#2850](https://github.com/MemPalace/mempalace/pull/2850) refactor(kg): one knowledge graph path resolver; no silent graph switch, never merged
- [#2851](https://github.com/MemPalace/mempalace/pull/2851) refactor(timeutil): one timestamp helper; UTC writes, legacy-aware reads
- [#2852](https://github.com/MemPalace/mempalace/pull/2852) perf(sqlite_exact): push $in/$eq filters and index filed_at for get_recent
- [#2853](https://github.com/MemPalace/mempalace/pull/2853) refactor(logging): name module loggers by __name__ and share one warn-once helper
- [#2855](https://github.com/MemPalace/mempalace/pull/2855) chore: require Python 3.10 (D-20)
- [#2856](https://github.com/MemPalace/mempalace/pull/2856) chore: sync main into develop for 3.12.0
- [#2857](https://github.com/MemPalace/mempalace/pull/2857) docs(changelog): cover every user-visible change since 3.11.0
- [#2858](https://github.com/MemPalace/mempalace/pull/2858) fix(security): mask endpoint credentials in embedding and LLM errors
- [#2859](https://github.com/MemPalace/mempalace/pull/2859) chore(release): 3.12.0
- [#2861](https://github.com/MemPalace/mempalace/pull/2861) fix(mine): keep --palace and media options on daemon-routed adapter mines

---

## [3.11.0] — 2026-10-12

A palace can now be audited and repaired in guided steps: scored for organization, given closed room sets, split into per-project wings, and cleaned of noisy tunnels and one-off predicates. Conversation mining stops discarding text, and status, search, wake-up and hub mines stay responsive on large palaces. In-process SQLite reads keep Chroma's locks, and listing events no longer hides another agent's request because the caller passed its own identity.

### Upgrade notes

- **`EntityRegistry.research()` and `confirm_research()` are removed.** They were
  a Wikipedia lookup for unknown words, and the only call the package could make
  to a third-party service without the user configuring an endpoint. (The hub
  client, `llm_client`, `closet_llm`, the openai-compat embedder and the Qdrant
  backend also reach the network, but only to an address the user sets.) Nothing in the CLI, MCP server, miners or hooks called them, and the
  lookup was already opt-in and off by default, so no shipped code path reached
  the network. They are gone rather than merely gated, so local-first is a
  property of the code and not of a default argument. Existing `wiki_cache`
  entries in `entities.json` are still read by `lookup()`; nothing new is written
  to that cache. (GHSA-mrj5)
- **The first conversation mine after upgrading re-mines each conversation file
  it covers, once.** The exchange chunker no longer discards text (#2591), so
  conversation drawers filed before 3.11 count as stale and are re-chunked and
  re-embedded from transcripts still on disk; transcripts no longer on disk keep
  their drawers. It runs only when a conversation mine covers those files:
  `mempalace mine --mode convos`, the `mempalace_mine` tool, or the legacy shell
  hooks under `hooks/`, which mine the whole project transcript folder (the
  PreCompact one synchronously). The plugin hooks mine only the active
  transcript. Expect roughly 40 minutes to 3 hours per million drawers,
  depending on hardware. While it runs, a hub serves other requests only
  between files, and without a hub the mine and session MCP writes exclude each
  other. On Chroma the vector index keeps the replaced entries. On a large
  palace, back up first and run it in batches at a quiet time, for example
  `mempalace mine <dir> --mode convos --limit 200`; an interrupted run resumes
  where it stopped.
- **A palace now refuses to open with a different embedding model than the one
  that built it.** Search, mining and MCP writes stop with a message that names
  the fix: set `embedding_model` (config.json or `MEMPALACE_EMBEDDING_MODEL`)
  back to the model the palace was built with, or re-embed with
  `mempalace repair rebuild-index`. A palace with no recorded model opens with
  the configured one and a warning that names `mempalace palace set-embedder`.
  (#2694, #2717)

### Added

- **EmbeddingGemma 2 is available as an opt-in embedder, and a palace records
  which embedder built it.** `embedding_model: embeddinggemma2` (extra
  `mempalace[embeddinggemma2]`, PyTorch, CUDA when present) and native media
  retrieval are opt-in; the default MiniLM path is unchanged. An unknown
  `embedding_model` keeps the MiniLM fallback, and near misses of a known
  name are refused instead of silently falling back. Every write path
  (CLI, MCP, mine, `repair`) checks the palace's recorded embedder identity
  and refuses a mismatch with a message that names the fix. `repair
  rebuild-index` and `--mode from-sqlite` record it again after a verified
  rebuild (#2709), a new Qdrant or pgvector palace records it, MCP refuses
  rather than falling back to Chroma's default embedder, and a CUDA
  out-of-memory error halves the batch and retries. (#2694, #2717; fixes #2709)

- **Read-only MCP tools advertise `annotations.readOnlyHint`.** Fourteen
  inspection tools (`status`, `list_wings`, `list_rooms`, `get_taxonomy`,
  `get_aaak_spec`, `search`, `check_duplicate`, `get_drawer`, `get_drawers`,
  `list_drawers`, `diary_read`, `kg_query`, `kg_timeline`, `kg_stats`) set
  `read_only: true` in the catalog so plan modes and read-only subagents can
  admit memory search without a host-side allowlist. `memories_filed_away`
  is not among them: it unlinks the checkpoint ack file, so the catalog
  omits `readOnlyHint` (MCP default false). Server `--read-only` still hides
  and refuses that tool.
- **Hallway co-occurrence `min_count` is configurable.**
  `MempalaceConfig.hallway_min_count` follows `MEMPALACE_KG_HALLWAY_MIN_COUNT`,
  then the `hallway_min_count` config key, then the default of 2 (clamped to
  at least 1), the same shape as `topic_tunnel_min_count`.
  `compute_hallways_for_wing` uses that value when `min_count` is omitted; an
  explicit integer is still honoured, so an unconfigured palace still pairs
  at 2 and existing hallways are unchanged. (#2328, #2588)
- **`mempalace_search` description is scoped to past sessions** (under
  200 characters) so small models and schema-pruning harnesses do not
  call it for referents from the current conversation.

- **`hallways --rebuild` holds the palace writer lock,** so a mine cannot save
  a newer snapshot between the rebuild's scan and its save. It, `rooms apply`,
  `wings split` and `kg normalize --yes` now report a held palace on one line
  and exit 1, like `mempalace mine`, instead of raising a traceback. The prune
  and the audit group spelling variants by the wing's file clusters, so a
  record with an ambiguous bare name can no longer bridge two files and cost
  one of them its association.
- **Hallway records keep a file's qualified path, and the miner never writes
  what the prune would remove.** Records used the shortest spelling, so two
  wings' different `user.py` files met as bare `user.py` and the tunnel builder
  linked them. A file now carries its most qualified path; a symbol still reads
  as its shortest name. `git diff`'s `a/<path>` and `b/<path>` collapse to one file at any
  depth when both appear, while a lone `a/` is kept as a real directory; a bare name that could belong to several files is not used as an entity
  at all. The audit's duplicate check now matches the prune's, so it never
  recommends a cleanup that removes nothing. A rebuild that finds no pairs now
  replaces the wing's old records instead of leaving them. On a real wing the rebuild went
  from 79,135 to 66,927 records, and the prune then found no artifacts at all.
- **`rooms apply` and `wings split` stop, recovery marker kept, when the closet
  collection exists but cannot be opened.** Only a collection that was never
  created counts as "no closets"; any other failure used to skip the closet
  phase and clear the marker, so a retry could not repair it.
- **An interrupted `rooms apply` or `wings split` finishes every phase on
  retry.** Both write drawers first and their index layers after. Once every
  drawer had moved, a retry found nothing to do and never reached the closet
  re-key or the hallway drop. Each command now records that it started, with
  `rooms apply` also saving its closet decisions from the first, complete plan,
  and a retry replays the remaining phases. A completed run re-run is still a
  no-op.
- **Only the legacy default palace falls back to `~/.mempalace`'s knowledge
  graph.** A palace chosen with `--palace`, `MEMPALACE_PALACE_PATH` or
  `config.json` keeps its graph inside itself, so `audit` never reads, and
  `kg normalize --yes` never rewrites, an unrelated graph.
- **The external-LLM consent gate judges IP addresses, not how a hostname
  starts.** `10.example.com`, `fd.example.com` and other names that merely begin
  like a private range were classified as local, so `rooms propose` and
  `kg normalize` would send sampled drawer excerpts and facts to them without
  `--accept-external-llm`. IP literals are now parsed and checked as loopback,
  private, link-local or CGNAT (100.64.0.0/10); single-label and `.local` names are
  resolved and must land on such an address; any other dotted name is external.
- **Cross-wing entity tunnels no longer link two files that only share a
  basename,** and the per-wing cap now counts links rather than entities. An
  entity shared by five wings is four links, so a wing could exceed its 25-link
  budget several times over. `tunnels prune` and the audit match duplicates the
  same path-aware way, so two distinct files between the same wings are never
  collapsed into one.
- **Two files with the same name are no longer treated as one entity.**
  Keying a hallway's endpoints on the basename alone merged
  `src/models/user.py` with `tests/models/user.py` and let
  `hallways --prune-spellings` delete one of them. Spellings now merge only
  when one path is a suffix of the other, and a bare name that could belong to
  two files is left on its own.
- **`rooms apply` moves the closet layer with the drawers.** A closet is one
  record per wing, room and source file, and search passes the same room
  filter to closets, so a reclassified drawer lost its index boost. A closet
  follows its source only when every drawer of that source and room moved to
  one room; a source that split, or only partly moved, keeps its closet where
  it is and is reported, since one record cannot index two rooms.
- **`tunnels propose --yes` drops rows naming a wing that no longer exists, and
  rows whose link was created meanwhile under another spelling.**
  The plan is written for review, so a wing can be split or renamed in the
  meantime; entity tunnels skip endpoint validation, so those rows became
  tunnels the audit immediately counted as artifacts.
- **A normalized fact keeps its confidence and provenance.** `kg normalize`
  opened the successor with confidence 1.0 and no source, overstating
  certainty and cutting the fact off from the drawer it came from.
- **`mempalace audit` detects mixed wings on the ChromaDB backend too, and
  counts only the drawer collection.** The mixed-wing reader used to exist for
  the sqlite_exact layout alone and grouped every row of the shared
  `documents` table, closets included; both backends now expose one
  collection-scoped `(wing, source_file, n)` reader.
- **`kg normalize --yes` rewrites each fact in one transaction, addressed by
  the triple id the plan recorded.** A fact closed or replaced while the plan
  sat under review is left alone and reported as stale instead of being
  resurrected; the apply runs under the palace writer lock; and a palace chosen
  with `--palace` never falls back to the home knowledge graph.
- **An interrupted `wings split` or `rooms apply` finishes on re-run.** Each
  batch is one backend write and a row is only ever wholly in its old or its new
  place; both commands plan over the rows still to move, so the same command
  again moves exactly the remainder and a completed run is a no-op.
- **A single-label LLM hostname is local only when it resolves to a private
  address.** `http://gpu-box` on the LAN needs no consent; a bare label that a
  search domain expands to a public host, or that does not resolve, is
  external and needs `--accept-external-llm`.
- **Hallway writers hold the hallway-file lock from load to save.** A mine's
  recompute, `hallways --rebuild`, `--prune-spellings` and `delete_hallway`
  each loaded, edited and saved the whole file unlocked, so the later save
  dropped the earlier one's records. `tunnels prune --yes` takes the tunnel
  lock the same way.
- **Hook-ingested transcripts file under the project wing.** The Stop and
  PreCompact hooks mined every Claude Code transcript with `--wing sessions`,
  so a palace grew one flat `sessions/technical` pile next to its project
  wings. Ingest now derives the wing the way the diary already did: the
  session's `cwd` first (git worktrees collapse to their project), the encoded
  project folder second; a session started in the home directory goes to
  `mac_workstation` / `windows_workstation` / `linux_workstation`. Existing
  `sessions` wings move with `mempalace wings split --wing sessions`.
- **Generic source files and library references are no longer entities for
  tunnels.** `app.js`, `model.ts`, `mod.rs`, `pathlib.Path`, `page.evaluate`
  and the like exist in every repo of their language; `is_generic_entity`
  drops a bare file name whose stem is a generic word and a dotted name whose
  first segment is a runtime, standard-library or test-framework namespace.
- **Following a tunnel now records the traversal.** `mempalace_follow_tunnels`
  potentiates every tunnel it crosses (`access_count`, `strength`,
  `last_activated`) under the tunnel-file lock, best-effort, so a read never
  fails on a write. Until now `dynamics.potentiate()` had no caller: every
  tunnel in every palace reported `access_count: 0` forever. A `--read-only`
  server or a peer without the writer lock skips the write.
- **`mempalace audit` scores tunnels as quality × coverage.** Coverage is the
  share of linkable wings (wings that share a strong entity with another wing
  by the hallways) that a sound tunnel reaches; `tunnels.unlinked_wings` names
  the rest. Traversal is reported but no longer scored, since it could only be
  raised by use. `mempalace tunnels propose` skips links that already exist and
  gives every unlinked wing its strongest link before filling by strength, so
  one round raises coverage as far as the hallways allow.
- **`mempalace audit` scores how well organized a palace is.** `status` says
  what is filed; `audit` says whether an agent could find it by walking the
  palace. It scores five layers 0–100 and lists concrete findings: the share of
  drawers in generic rooms (`technical`, `architecture`, `planning`, `general`,
  `problems`), wings whose rooms are flat, wing and room names that are one
  name spelled two ways (`acme-app` / `acme_app`, `release-3.6.0` /
  `release_3_6_0`), stub wings, tunnels that were never traversed or link
  generic entity names, hallways that link an entity to its own file or path
  spelling (`main.zig` ↔ `src/main.zig`), and knowledge-graph predicates used
  only once. Every reader is read-only and never opens the vector index or the
  palace lock, so it runs against a palace an MCP server is serving. Findings are
  grouped by layer and wrapped to the terminal width; on a terminal each reader
  reports a timed progress line on stderr (`--quiet` silences it). `--json`
  emits the full report; `--fail-under SCORE` exits 2 below a threshold for
  cron or CI checks.
- **Guided repair session after an audit.** `mempalace instructions audit`
  returns a protocol the agent follows with the user: present the scorecard,
  then walk the findings one structured question at a time (recommended option
  first) — merging wings and rooms spelled two ways, folding stub wings,
  deleting tunnels on generic tokens and self-link hallways, agreeing a
  knowledge-graph predicate vocabulary, and deciding how the next mine should
  handle generic-room concentration — then re-run the audit and write a diary
  entry with the before/after scores and every decision. Moves over
  deletions, numbers before actions, content never touched. Wired into the
  `mempalace` skill and as `/mempalace:audit` (Claude), `$mempalace audit`
  (Codex), and `/mempalace-audit` (Cursor).

- **`mempalace rooms propose` / `apply` give a wing a closed room set.** The
  transcript miner files almost everything into five keyword rooms, so on a
  real palace the room layer carries no information. `propose --wing W`
  samples the wing (reproducible seed), sends the excerpts to the configured
  LLM — local by default (Ollama, vLLM, NInfer or any OpenAI-compatible
  endpoint); a non-local endpoint requires `--accept-external-llm` — and saves
  the proposed rooms (slug, one-line description, keywords) to
  `<palace>/rooms/<wing>.json` for the user to edit. A second, narrower
  call labels each sampled excerpt with a room, and those drawers become the
  room's exemplars. `apply --wing W` builds each room's prototype as the
  centroid of its exemplars' stored embeddings (falling back to the embedded
  description) and moves every drawer to its nearest room by cosine against
  the drawer's stored embedding, so no drawer is re-embedded and no per-drawer
  LLM call is made; below `--threshold` (default 0.30) a drawer keeps its
  room. Only drawers in the miner's generic rooms move by default (`--from`
  widens or narrows that; `--from all` reclassifies everything), so hand-filed
  rooms such as `decisions` and `diary` are never touched. Dry run by default,
  `--show N` prints example moves per room with their confidence, `--yes`
  writes `room` metadata only, under the palace lock. The decider is a
  one-method protocol so a calibrated structured-decision model can replace
  the embedding decider later.

- **`mempalace wings split` turns a machine-level transcript wing into one wing
  per project.** Mining a conversation export without `--wing` files every
  session under one wing (`claude_conversations_windows`); on the maintainers'
  palace that one wing held 27 projects and 124k drawers, so no room set
  could mean anything and a wing-scoped search returned everything.
  `wings split --wing W` groups the wing's drawers by the project their
  transcript path encodes (Claude Code project directories, including
  subagent transcripts and Claude/Codex worktrees; Codex rollouts via the
  `cwd` in the file when it is readable), resolves each to an existing wing
  whose name the key ends with (`p--acme-portal` → `portal`) or a
  derived name, and writes `<palace>/wings/split-<wing>.json` for review;
  `--yes` re-keys drawers and their closets under the palace lock and drops
  the split wing's hallway records. `mempalace audit` now flags a wing that
  mixes five or more projects, and `mempalace hallways --rebuild` recomputes
  hallways for one or every wing without a mine.
- **`mempalace tunnels propose` / `prune` and a real tunnel filter.** The miner
  dropped an entity tunnel for every entity with hallways in two wings, so a
  palace ended up with tunnels on `content`, `thinking`, `Server` and `WebFetch`
  and four copies of each real link under spelling variants. Entity tunnels
  now skip generic names (short lower-case words, harness tool names, generic
  nouns, files every repo has, bare paths, shouting constants), entities
  present in more than a quarter of all wings, and links weaker than three
  co-occurrences; spellings merge; each wing keeps its 25 strongest.
  `tunnels propose` writes the strongest cross-wing links to
  `<palace>/tunnels/proposal.json` for review and `--yes` creates them;
  `tunnels prune` removes generic, dangling and duplicate-spelling tunnels.
  `mempalace audit` scores the layer as quality × coverage (see the entry
  above), so it can be improved by tooling rather than only by use.
- **`mempalace kg normalize` maps one-off predicates onto a closed
  vocabulary.** Agents filing facts one at a time invent a predicate per fact
  (51 of 57 on the maintainers' palace), so `mempalace_kg_query` cannot find
  facts by relation. The LLM proposes, per off-vocabulary fact, a predicate
  from the vocabulary (default `works_on, owns, depends_on, uses, decided,
  status, located_in, measured`; `--vocabulary` overrides) and an object that
  keeps the detail; the plan is saved to `<palace>/kg/normalize.json` for
  review, and `--yes` closes each old fact and opens the rewrite at one shared
  instant, so an as-of query before it still returns the original wording.
- **`--llm-model auto` follows a local OpenAI-compatible server.** vLLM, NInfer
  and LM Studio serve one model at a time and its id changes whenever the
  operator swaps it, so a batch of `rooms propose` calls died with
  `model_not_found` halfway through. `auto` resolves to the served model at
  the reachability probe, and a wrong explicit id now names the served ones.
  Reasoning models are told not to think (`reasoning_effort: none`) when a
  caller asks for a plain classification, since Qwen3.8's default effort spent
  the whole completion budget reasoning and returned empty content; a server
  that rejects the field gets one retry without it. Single-label LAN hostnames
  (`http://gpu-box:8010`) count as local for the external-LLM consent gate.
- **`mempalace_list_hallways` pages, strongest first.** Returning every record
  closed the MCP connection on a palace with 148k hallways; the tool now takes
  `limit` (default 100, max 500) and `offset` and returns `{hallways, total,
  count, offset, limit}`.
- **Metadata-only updates on the sqlite backends are ~6000x faster.** A wing
  or room move (`update_drawer`, `rooms apply`, `wings split`) rewrote the
  document, embedding and full-text row of every drawer; a 240k-row split ran
  at about a thousand rows a minute. `update()` with only `metadatas` now
  merges the JSON in one statement per row and leaves the FTS row alone
  (110k rows/s measured).

### Security

- **ChromaDB telemetry is disabled explicitly, not just silenced.** MemPalace only
  raised the log level on `chromadb.telemetry.product.posthog`, leaving ChromaDB's
  own `anonymized_telemetry=True` default in place. Nothing is transmitted on the
  1.x line we support, where the posthog client is a no-op stub and posthog is not
  a dependency, so this was never exploitable — but the default was ChromaDB's to
  change. Every client the backend opens now passes
  `Settings(anonymized_telemetry=False)`, and importing `mempalace` sets
  `ANONYMIZED_TELEMETRY=False` (via `setdefault`, so an explicit operator export
  still wins) for any other chromadb client in the process. (GHSA-8h77)


### Performance

- **Mines on very large palaces do far less redundant work.** Projects-mode
  mining reads every file's stored mtime in one metadata pass instead of one
  query per file (#2689); a mine that wrote nothing skips the post-mine
  `quick_check` (#2690); hallways read the wing through a scoped sqlite
  query instead of paging with `OFFSET` (#2691); and the mine's tunnel passes
  load and save `tunnels.json` once per batch (#2688). (#2684, #2683)

- **Diary reads and single-drawer lookups no longer open Chroma when SQLite
  already has the rows.** `mempalace_diary_read` counts and hydrates the
  newest diary rows from `chroma.sqlite3` before opening the vector index,
  and a direct drawer (or every scalar chunk of one parent) is read the same
  way. Missing rows and a collection whose metadata segment has not consumed
  queued writes still fall back to Chroma, so a metadata read cannot skip a
  write that has not landed. (#2619, #2650)

### Bug Fixes

- **Drawer listings keep metadata and page previews in one recovered snapshot.**
  SQLite shortcuts fall back to Chroma when recovery is pending, fetch only the
  displayed page's text, and consistently exclude case-sensitive `chroma:` keys.
  Expected recovery fallback is logged at debug level instead of as a scan error.
- **Approved tunnel proposals retain their full spelling context.** Candidate
  limits and wing filtering no longer cause approved qualified links to be
  silently skipped as aliases. Audit and prune use the same ambiguity context.
- **Room recovery rejects damaged journals before applying changes.** Duplicate
  JSON members, duplicate closet targets and malformed closet rows are refused.
- **Metadata-aware updates and upserts guard the inputs used for embedding.**
  Concurrent document or metadata changes cause refusal before publication;
  removed fields are omitted from embedding metadata. Embedder construction runs
  inside the transport's embedding section, and guarded writes are counted once.
- **Malformed HTTP tool names return normal JSON-RPC validation errors.**
  Non-string names no longer fail during dispatch.

- **`mempalace mine --mode convos --include-subagents` keeps the flag through
  a running hub or the daemon.** Both routes dropped it, so the forwarded mine
  skipped `subagents/` folders as if the flag had not been passed. The
  `mempalace_mine` tool accepts `include_subagents` (convos mode only), and the
  daemon job carries it. A hub too old to advertise `mine_include_subagents` is
  refused with an upgrade message instead of mining locally beside the hub's
  writer lease. A plain `mempalace mine <dir>` with no `--mode` now forwards
  `mode: "projects"` to the hub instead of `null`, which the hub refused.

- **`mempalace_reconnect` no longer runs under a running mine, and failed files
  count as errors.** A reconnect during a mine now waits up to 10 s and then
  returns `MineInProgress`; a mine that failed to file some files raises
  `MineFileErrors` (`success: false`) and the CLI exits 1. (#2701)
- **`mempalace.yml` is accepted as a project config filename, and `init`
  updates an existing one** (with a backup) instead of shadowing it with a new
  `mempalace.yaml`. (#2699, #2704)
- **`.al` (Business Central) and `.ps1` (PowerShell) sources are mined,** and
  the entity detector samples them when prose is scarce. (#2702, #2706)
- **A `config.json` that is valid JSON but not an object** (`null`, `[]`,
  `42`) now warns once on stderr instead of being ignored silently. (#2700)
- **Splitting a wing pins its plan across interrupted retries.** An edited
  plan is refused until the original targets are restored. (#2669, #2697)
- **EmbeddingGemma's token table is looked up before it is dequantized,**
  fixing the memory blow-up on the q8 graph; any rewrite error falls back to
  the unmodified file. (#2681, #2693)
- **A failed MCP writer-lock setup releases the palace lock** instead of
  pinning `mine_palace_*.lock` until the process dies. (#2618, #2632)
- **Hallways and tunnels keep distinct files distinct.** Unequal code
  extensions (`parser.c` vs `parser.h`) and ambiguous bare names no longer
  merge or cost a file its tunnels. (#2655, #2656)
- **Room apply and replay are resumable and safe.** Closet apply resumes by
  stable id, approved spellings survive a room-set reload, and pending closet
  snapshots are validated before any write. (#2657, #2664, #2668)
- **WAL-mode palaces stay fresh and checkpoint.** The no-OFD SQLite helper
  releases its snapshot after each read (#2658), and overview caches
  invalidate when only the WAL changes (#2662).
- **KG rewrite no longer rewrites onto a future-only successor,** which
  emptied as-of queries until the future start. (#2661)
- **HTTP diary writes survive a concurrent `reconnect`.** (#2663)
- **Singular drawer reads omit reserved `chroma:*` metadata** (such as
  `chroma:uri`), matching `Collection.get`. (#2665)
- **Unfiltered lexical search ranks its candidate window before capping
  it,** so a drawer's best match is no longer missed when a query matches
  more than 500 drawers. (#2667, #2670)
- **DirectML embedding sessions run serially,** because ONNX Runtime does not
  support concurrent `Run()` on one DirectML session. (#2687)

- **The Stop hook no longer reports folded messages as memories filed.**
  `_save_diary_direct()` returned the count of user messages folded into one
  diary checkpoint, and the hook rendered that as "N memories woven into the
  palace". A successful checkpoint now reports `drawers_filed: 1` and
  `messages_folded: N`, and the line reads "1 checkpoint saved — N messages
  woven into the palace". Failure paths report zeros. (#2303, #2587)
- **The SQLite lock helper keeps a read transaction only in WAL mode.** The
  helper process that holds Chroma's locks used to leave a SHARED lock taken
  even on a rollback journal, and that foreign lock blocks every writer's
  COMMIT. The helper now reads, checks `journal_mode`, and keeps the
  transaction only for WAL. Otherwise it rolls back and closes its own
  connection (never the parent's) and stays idle, so a later switch to WAL
  can reopen. (#2652)
- **`mempalace mine --include-ignored` reaches a running hub.** Those paths
  used to be dropped when the CLI forwarded the mine, so an ignored file the
  user named was never scanned. The hub tool accepts `include_ignored`
  (projects mode only). A hub too old to advertise `mine_include_ignored` is
  refused with an upgrade message instead of mining locally beside the hub's
  writer lease. (#2584)
- **`from_agent` on `event_list` and `event_wait` no longer filters by
  writer.** Agents pass their identity as `from_agent` on every coordination
  call. On append and ack that is the writer, but on list and wait it was
  applied as a writer filter, so a recipient that asked for a task by
  correlation id saw only its own events and missed the request written to
  it. `from_agent` is now the caller's identity and does not narrow the
  result; an explicit `writer` filter (and the DSL `from:` on LIST and WAIT)
  does. A list or wait that passes `from_agent` without `writer` includes a
  one-line note. The storage layer and the CLI `--from-agent` flag are
  unchanged. (#2616)
- **HTTP tool dispatch no longer holds the request lock across embedding
  inference.** A slow `diary_write` or search embed stalled unrelated status
  and reads for the whole model call. Search, `check_duplicate`, and
  `diary_write` drop the read or write lease only around inference; compound
  writers stay serialized. Embedding stays outside the palace write lock, and
  `mempalace_reconnect` takes a lifecycle lock so it cannot close backends
  mid-embed. (#2604, #2633)
- **A reader that cannot take open-file-description locks no longer drops
  Chroma's locks when it closes.** A busy anchor prime used to close the
  connection it had just opened, and that close drops every POSIX lock in
  the process, Chroma's included. The connection stays open and the prime is
  retried. Where the platform has no OFD locks, a helper process holds the
  WAL read so a close in this process cannot drop it, and the in-process
  anchor is installed only after that helper is holding. (#2573)
- **`mempalace_update_drawer` applies a wing or room rename that changes only
  case.** The comparison used `str.lower()`, so a rename such as `ZZTest` to
  `zztest` was treated as unchanged, skipped the write, and still returned
  success with the old value. `list_drawers` treats those casings as distinct
  wings, so the caller's casing is now authoritative. Resubmitting the
  identical value is still a no-op. (#2426)
- **`kg_add`, `kg_invalidate`, and `kg_supersede` no longer take the
  peer-writer lease.** Those tools write `knowledge_graph.sqlite3` under the
  knowledge graph's own lock and do not touch Chroma. Gating them on the
  palace lease meant a second MCP server on the same palace could not record
  facts for the lifetime of the first. They are exempt the way the logstream
  tools already were. (#2297, #2423)
- **`mempalace sweep` and `mempalace sync` routed to the daemon no longer resolve
  a relative path against the daemon's working directory.** Both put the path into
  the job as typed, and the daemon keeps the directory it was started in: a
  `sweep` run from another directory swept the daemon directory's transcripts, and
  `sync --apply` removed drawers of the project there. The CLI now resolves the
  sweep target, the sync project dir and every `--root` before the job is built,
  following symlinks the way the direct route reads them. A direct `sweep` of a
  relative file now files its drawers under the absolute path too: `sync` counted
  a relative `source_file` as having no source and never pruned it. (#2582)
- **Mempalace's own SQLite reads no longer break Chroma's locks in the same
  process.** Status, taxonomy, `list_drawers`, BM25, the integrity check, and
  repair open `chroma.sqlite3` through Python's `sqlite3`, while Chroma opens it
  through its bundled SQLite. Closing a Python connection dropped every lock the
  process held on the file, Chroma's included, and a check racing a Chroma
  commit could read torn pages ("database disk image is malformed"). All Python
  access now goes through one reader/writer wrapper that holds a per-file
  process lock and keeps Chroma's locks in place. (#2302, #2506)
- **A one-file mine no longer scans the whole palace to find out whether that
  file is already mined.** Every hook-triggered mine checked the full
  collection. On a 660k-drawer palace that was 79% of a 23-minute mine. With 50
  or fewer candidate files the check now asks only for those files' rows.
  (#2561, #2567)

- **Conversation mining no longer discards text.** In exchange mode (the default
  for `mempalace mine --mode convos`) a line starting with `---` ended the AI
  response, and everything from it to the next user turn was never filed.
  Assistant replies use `---` as a markdown rule all the time, so any Claude Code
  session with one lost the rest of that reply. Text before the first user turn
  was skipped the same way, and an exchange or paragraph at or below the minimum
  chunk size (30 characters by default, e.g. `> ok`) was dropped as noise, as
  was a whole transcript that short. A `---` is now part of the response, text
  before the first turn is filed as its own drawer, and a unit that small joins
  the previous drawer (or becomes its own drawer when that one is full). Units
  larger than the chunk size are now split after the last whitespace in the back
  half of the window, not mid-word. Exchange drawers now carry
  `convo_chunker_version`, and drawers without the current one count as stale,
  so the next `mempalace mine --mode convos` re-mines existing conversation
  files once and recovers the lost text. Project files are not re-mined.

- **The MCP server no longer reloads the whole vector index on almost every
  call.** The session's Chroma client recorded `chroma.sqlite3`'s timestamp
  before its own client build, collection open, and writes moved it, so the
  next call took the server's own footprint for another process's write,
  rebuilt the client, and reloaded the HNSW index. Search (through
  `ChromaBackend`) and the other tools (through the session) also took each
  other's writes for external ones. Both now record the stat they leave the
  file at. A write from another process still rebuilds the client (#2002). On
  a 100k-drawer palace a tool call's collection open went from 70-130 ms and
  +15 MB of RSS to 0.3 ms.

- **`mempalace_status`, `list_wings`, taxonomy, and `graph_stats` no longer
  recount the whole palace on every call.** The 5 s count cache was stamped
  before its query ran, so any count slower than 5 s, as on a
  multi-million-drawer palace, was cached already expired. `graph_stats` had
  no cache at all. The Chroma caches now stay valid until `chroma.sqlite3`
  changes, and the TTL runs from when the count finished. Repeated status
  calls on a 360k-drawer palace went from 8-12 s each to about 1 ms.

- **Opening a palace with no recorded embedder no longer loads every vector.**
  To tell an empty collection from a populated one, the identity check called
  `count()`, which on a fresh Chroma client loads the whole HNSW segment while
  holding the GIL and stalls every thread in the process. It ran on every
  CLI search the hub forwarded. It now reads one row from `chroma.sqlite3`.

- **Mining no longer rescans the whole palace with quadratic paging.** Every
  conversation mine, a hook's one-file mine included, scanned every drawer for
  content hashes, and bulk mines scanned them again for mined files, both by
  paging `get(limit, offset)`. Chroma turns that `offset` into SQL `OFFSET`,
  which steps over every skipped row, so a scan grew with the square of the
  palace (154 s on a 360k-drawer palace), and each began with a `count()` that
  loads the whole vector index. Chroma now streams just the keys these checks
  read from `chroma.sqlite3` in one pass, and the content-hash scan reads only
  drawers that carry a hash: under 10 ms and 1.6 s on that palace.
  `get_all_metadata`, behind the status and graph fallbacks, uses the same pass.

- **A reset of Chroma's shared cache now reopens every client.** Only the MCP
  session closed its client before the shared System cache was reset. After a
  reset by the session, repair, or the diary tool, every backend kept its client
  on the discarded System, and kept returning it while the palace file looked
  unchanged. Backends are now drained before any reset, and reopen a client
  opened before one.

- **A mine stops instead of running against a partial list of what's already
  mined.** A metadata scan that failed partway returned what it had gathered,
  with only a warning. That reads as "not mined yet" and "no duplicate", so
  copies of a transcript could be filed again. A failed fast scan now falls back
  to a complete paged scan, and the mine stops if that fails too. The fast scan
  also reads older schemas without `bool_value`.

- **The BM25-only search fallback finds the drawers that actually contain a
  name.** With the vector index disabled, search picks BM25 candidates from
  `chroma.sqlite3`'s trigram full-text index, where `aven` also matches inside
  `haven't` and `Avenue`. It took the first 500 matches in storage order, the
  oldest substring hits, and returned them even when they scored 0, so a search
  for a name answered with `haven't`, `New Haven`, and `Avenue`. Candidates are
  now read in full-text rank order, whole-word matches first. A wing, room, or
  source filter matching few drawers reads just those drawers (6.4 s to under
  10 ms for a ten-drawer wing on a 360k-drawer palace), and stop words stay out
  of the full-text query. When the first 50,000 ranked matches hold too few
  whole-word ones, the rest are read newest first, within a budget of 500,000
  rows. If that budget runs out, the result says so with
  `candidates_truncated`.

- **Search no longer returns every copy of a passage as a separate result.**
  Backups, autosaves, and re-exports put the same text under several files, and
  each copy took a result slot. Identical text of at least 100 characters now
  shows once. The best-ranked occurrence lists the others under `also_in`, with
  each one's file and chunk position (the CLI prints `Also in: ...`). Shorter
  identical text, such as two unrelated "Yes." drawers, stays separate, and so
  does a file's own repeats. When the folding leaves a page short, the search
  widens its candidate pool (up to 500). If it's still short, it says so with
  `distinct_results_truncated`.

- **A mine on the HTTP hub no longer blocks every other request until it
  ends.** The hub ran a forwarded mine under its exclusive lock for the whole
  run, so status, search, and wake-up waited for all of it. The mine still runs
  exclusively, but between files it hands the lock to the requests queued
  behind it, then takes it back; a second mine still waits for the first.

- **`mempalace wake-up` reads its recent drawers from `chroma.sqlite3`.**
  Chroma's `get` loads the whole index even for a metadata read, so waking up
  a ten-drawer wing loaded every vector in the palace, and the window was the
  first page Chroma returned rather than the newest drawers. On a
  360k-drawer palace wake-up went from 0.8-8.8 s at up to 920 MB to about
  0.5 s at 238 MB. The read stays on the metadata segment, so a vector-segment
  row cannot come back as an empty drawer. Chroma advertises
  `supports_recency_order` for that exact window.

- **A write from another process reconnects every Chroma client in this one.**
  Search and the other tools share one in-memory index. The client that
  noticed the write rebuilt and recorded the new file stat; the other treated
  that stat as its own write and kept reading the index the rebuild had
  discarded. Both clients now drop together. `mempalace_status` also recounts
  as soon as the palace file changes, including inside its 5 second cache.

- **The legacy `mempalace repair` no longer runs without the palace lease, so a
  hook miner can no longer destroy a repair that is already half done.**
  `cmd_repair` extracted every drawer and copied the whole palace to
  `<palace>.backup` with no `mine_palace_lock` held, and only the per-batch
  acquires inside the rebuild contended. On a large palace that is minutes of
  work during which a `mempalace mine` started by a hook could take the lease,
  after which the rebuild failed with `MineAlreadyRunning` and the extraction
  and the backup were both discarded. The lease is now held across the whole
  pass and contention is reported with the holder's identity before anything is
  read or copied - what `rebuild_index` and `rebuild_from_sqlite` already did.
  (#2562, #2569)

- **The stdio MCP servers no longer exit on a line `json.loads` cannot load.** An
  integer past Python's digit limit raises `ValueError` and nesting too deep to
  parse raises `RecursionError`. Neither the full server's stdio loop nor the light
  server's caught them, so one such line ended the session. The full server
  (`mempalace-mcp` with no hub running) now answers it with `-32700`, as the hub's
  HTTP transport does, and `mempalace-light-mcp` skips it, as it skips invalid
  JSON. (#2556)

- **Hallways no longer pair an entity with its own spelling.** The structural
  extractor records a file as both its path and its basename, so conversation
  mining wrote `main.zig ↔ src/main.zig` as the strongest hallway in every code
  wing and four copies of each real association (`ChatStore ↔ RootView` under
  every spelling combination). The miner now keys pairs by spelling
  (basename, known code extension stripped, case-folded) and materializes the
  shortest spelling seen in the wing; dynamics fields survive the recompute
  under the same key. `mempalace hallways --prune-spellings` finds the records
  older mines already wrote (dry run) and `--yes` removes the self-links and
  merges the variants, keeping the highest-count record. Found by
  `mempalace audit` on the maintainers' own palace: 2,437 self-links and the
  top-100 hallways 36% self-linked.

- **A `known_entities.json` write no longer appears to hang on Windows when the
  directory refuses a temporary file.** `_publish_registry` falls back to writing
  in place when the directory takes no new name, and it learned that from the
  `EPERM` / `EACCES` / `EROFS` that `tempfile.mkstemp` raises. On Windows
  `mkstemp` does not raise it: it reads a `PermissionError` as a name collision
  and tries the next candidate, `tempfile.TMP_MAX` times. That is 20 on Python
  3.13 and later, but `os.TMP_MAX` — 2,147,483,647 — on 3.12 and earlier, about
  28 hours of retries at the measured rate, so the fallback never ran and the
  write looked frozen. The temporary name is now opened directly with
  `O_CREAT | O_EXCL`, retried only on a real collision and only a few times, so
  the permission error reaches the fallback on every interpreter. (#2530)

- **Importing `mempalace.mcp_server` no longer parses the importing program's
  command line.** `mempalace-light-mcp --help` printed the full server's options,
  and a host program exited 2 on its own `--port abc`. The entry points apply
  their flags now, so `mempalace-light-mcp --palace` also starts instead of
  raising `AttributeError`. A daemon started with
  `mempalace daemon start --foreground` and no `--palace` now writes `mcp_tool`
  knowledge-graph facts beside its palace, not to
  `~/.mempalace/knowledge_graph.sqlite3`. (#2528)

- **`mempalace-mcp` now answers the requests it could not serve.** When its hub
  was gone and its own server could not start, every request was dropped and the
  client waited on each one. That happens with a `--backend` that names no
  backend, or with a storage stack that fails to import; the failed import also
  sent later answers to stderr. These requests now get an error naming why. A line
  that does not parse gets `-32700`, and JSON that is not an object gets `-32600`.
  A hub answer that breaks off counts as a failed hub call in the proxy and the
  full server. A call that changes state, `mempalace_memories_filed_away`
  included, is then not replayed, and a read is served locally. `--palace` or
  `--backend` with no value is refused at startup. (#2554)

- **`sync --apply` no longer removes a drawer whose corroborating neighbour is in a different directory than the one its source was mined from.** #2322 made removal ask for a witness in the same directory, which three mount shapes defeat: a mount point whose lower layer holds a mined file of its own, a volume mounted over a directory the palace already knows a file in, and a bind mount of another directory over one it knows, which is what a container does with `-v /host/elsewhere:/project/sub`. Driven through real `mount` and `umount`, all three removed drawers of files that were on disk the whole time. Mining now records which directory each source was read from, as that directory's inode, and `sync` compares it against the inode answering when the verdict is formed. Nothing is written into the source tree for it, so the read-only mounts the README's container recipes use keep working; `st_dev` could not do it, since a bind mount puts both sides on one filesystem where it is the same number. Ten paths record the identity, and `update_drawer` carries it forward when it refiles a drawer as chunks, so every row `sync` can remove holds one. Four bounds: one volume swapped for another at the same path is not separated, a directory deleted and recreated may answer with a different inode and then keeps the drawers of files that really went, a filesystem reporting no inode of its own gains nothing and reproduces the bug in full, and the `gitignored` removal route is unchanged. An existing palace gains the field only as it is re-mined, which `file_already_mined` decides from the stored mtime. Closets are now purged per source a pass emptied rather than per source it removed a drawer from, so a source that kept one keeps the lines that index it. (#2367)

- **`sync --apply` no longer removes a drawer because a volume mounted over its
  directory carries a file of the same name and a `.gitignore` that names it.**
  The `gitignored` verdict was formed from one reading of the path, with nothing
  to say whose directory answered; #2367 guarded the `missing` verdict beside it
  with the inode recorded at mine time and listed this route as the bound it
  left open. The same reading now guards both routes: a drawer carrying an
  identity is removed as gitignored only when the directory answered with that
  inode before and after the rule was read, and the rules a pass caches are
  keyed by the identity they were read under, so a rule read from a volume does
  not decide sources read after the volume left. Anything else is kept and
  reported as unresolved, as an uncorroborated absence is. A drawer carrying no
  identity is decided by the rule alone, as before. (#2563)


### Documentation

- **MCP tool counts (44/45 → 47) and plugin command counts (5 → 6) are
  corrected** across the READMEs, manifests and website, with drift tests so
  they stay true; the docs also describe `MineInProgress` and
  `MineFileErrors`. (#2698, #2703, #2705)

- **Recall instructions tell agents to fetch only the context a question
  needs.** Wake-up, search, knowledge-graph, and diary guidance now starts
  with the tool that fits (KG for a known entity's relationships or
  time-bound facts, FIND/search for source text, DIARY for recent
  continuity) and stops when the result answers it. An empty KG or filtered
  search is not treated as proof the palace has nothing. (#825, #2651)
- **LoCoMo benchmark docs no longer treat rerank or top-50 as a recall
  gain.** `--llm-rerank` reorders a set already cut to top-k, so `Avg Recall`
  cannot rise for the same retrieved set, and it adds latency and provider
  cost. The guide now states that session retrieval at top-50 is
  structurally saturated because every session fits, records the 60.3%
  figure as session top-10, and notes that the CLI default is top-50 so a
  top-10 comparison must pass `--top-k 10`. The old 77.8% top-50 number is
  kept as a historical value whose granularity was not recorded, not as the
  expected result of the session top-50 command. (#2164, #2645)

### Internal

- **Release version guard checks every copy of the version, not only the
  seven Python manifests.** `scripts/check_versions.py` compares
  `mempalace/version.py` with `pyproject.toml`, the Claude, Codex and DSH
  manifests, the OpenClaw skill, the README badge, the MemPalace entry in
  `uv.lock`, `Cargo.toml`, and the three workspace packages in
  `Cargo.lock`. The Version Guard workflow runs it when those files change
  and on `v*` tags; stable tags must match, prerelease tags stay exempt.
  (#2644)
- Dependabot: `docker/build-push-action` 7.3.0 → 7.4.0,
  `docker/setup-buildx-action` 4.3.0 → 4.4.0, and
  `docker/setup-qemu-action` 4.3.0 → 4.4.0. (#2547, #2548, #2549)

### Dependencies

- **Pillow 12.3.0, chromadb 1.5.9 and routine lock bumps.** Pillow 12.3.0
  needs Python 3.10+, so Python 3.9 installs stay on Pillow 11.3.0, which is
  affected by the Pillow advisories fixed in 12.3.0. `SECURITY.md` now warns
  against exposing a Chroma HTTP server. (#2719)

### Pull requests merged since 3.10.0

- [#2368](https://github.com/MemPalace/mempalace/pull/2368) fix(sync): record which directory a source was mined from (#2367)
- [#2423](https://github.com/MemPalace/mempalace/pull/2423) fix(mcp): exempt kg_add/kg_invalidate/kg_supersede from peer-writer lease
- [#2426](https://github.com/MemPalace/mempalace/pull/2426) fix(mcp): apply case-only wing/room renames in tool_update_drawer
- [#2445](https://github.com/MemPalace/mempalace/pull/2445) fix(cli): honor MEMPALACE_PALACE env var and --palace after init (#2366)
- [#2459](https://github.com/MemPalace/mempalace/pull/2459) fix(wal): record write outcome in WAL to close #538 audit gap
- [#2506](https://github.com/MemPalace/mempalace/pull/2506) fix(storage): keep Python sqlite3 readers from breaking Chroma's SQLite in-process (#2302)
- [#2524](https://github.com/MemPalace/mempalace/pull/2524) fix(chroma): disable ChromaDB telemetry explicitly (GHSA-8h77)
- [#2526](https://github.com/MemPalace/mempalace/pull/2526) fix(entity-registry): remove the unused Wikipedia lookup (GHSA-mrj5)
- [#2527](https://github.com/MemPalace/mempalace/pull/2527) refactor(entity-registry): drop the tables the Wikipedia lookup left behind
- [#2531](https://github.com/MemPalace/mempalace/pull/2531) fix(miner): bound the temporary-name search so a refusal reaches the caller
- [#2534](https://github.com/MemPalace/mempalace/pull/2534) fix(mcp): parse the server's flags in main(), not when mcp_server is imported (#2528)
- [#2547](https://github.com/MemPalace/mempalace/pull/2547) chore(deps): bump docker/build-push-action from 7.3.0 to 7.4.0
- [#2548](https://github.com/MemPalace/mempalace/pull/2548) chore(deps): bump docker/setup-buildx-action from 4.3.0 to 4.4.0
- [#2549](https://github.com/MemPalace/mempalace/pull/2549) chore(deps): bump docker/setup-qemu-action from 4.3.0 to 4.4.0
- [#2555](https://github.com/MemPalace/mempalace/pull/2555) fix(mcp): answer the requests the stdio proxy cannot serve (#2554)
- [#2557](https://github.com/MemPalace/mempalace/pull/2557) fix(mcp): keep the stdio servers up on a line json.loads rejects (#2556)
- [#2564](https://github.com/MemPalace/mempalace/pull/2564) fix(sync): read the directory's identity around a gitignored verdict (#2563)
- [#2567](https://github.com/MemPalace/mempalace/pull/2567) fix(mine): scope prefetch_mined_set to candidate files below a threshold
- [#2569](https://github.com/MemPalace/mempalace/pull/2569) fix(cli): hold the palace lock across the legacy repair
- [#2572](https://github.com/MemPalace/mempalace/pull/2572) feat(mcp): add bulk get and delete drawer tools
- [#2573](https://github.com/MemPalace/mempalace/pull/2573) fix(storage): keep Chroma's locks when a reader cannot use OFD locks
- [#2576](https://github.com/MemPalace/mempalace/pull/2576) feat: palace audit and guided repair tooling (rooms, wings split, tunnels, kg normalize)
- [#2583](https://github.com/MemPalace/mempalace/pull/2583) fix(cli): resolve relative sweep and sync paths against the caller's cwd (#2582)
- [#2584](https://github.com/MemPalace/mempalace/pull/2584) fix: forward include-ignored mines through the running hub
- [#2587](https://github.com/MemPalace/mempalace/pull/2587) fix(hooks): report drawers_filed and messages_folded separately in stop-hook output (#2303)
- [#2588](https://github.com/MemPalace/mempalace/pull/2588) feat(config): make hallway co-occurrence min_count configurable — #2328
- [#2590](https://github.com/MemPalace/mempalace/pull/2590) docs(closets): describe search as drawer-first with a closet boost
- [#2591](https://github.com/MemPalace/mempalace/pull/2591) fix(convo): stop the exchange chunker from discarding text
- [#2593](https://github.com/MemPalace/mempalace/pull/2593) fix: keep status, search and wake-up responsive on large palaces
- [#2610](https://github.com/MemPalace/mempalace/pull/2610) feat(mcp): advertise readOnlyHint and scope search to past sessions
- [#2616](https://github.com/MemPalace/mempalace/pull/2616) fix(logstream): stop from_agent filtering event_list and event_wait
- [#2617](https://github.com/MemPalace/mempalace/pull/2617) chore(release): 3.11.0
- [#2632](https://github.com/MemPalace/mempalace/pull/2632) fix(mcp): release palace lock if writer setup fails after acquire (#2618)
- [#2633](https://github.com/MemPalace/mempalace/pull/2633) fix(mcp): release HTTP request lock around embedding inference (#2604)
- [#2640](https://github.com/MemPalace/mempalace/pull/2640) fix(mcp): drop readOnlyHint from memories_filed_away
- [#2644](https://github.com/MemPalace/mempalace/pull/2644) ci(version): validate release badges and workspace lock versions
- [#2645](https://github.com/MemPalace/mempalace/pull/2645) docs(benchmarks): clarify LoCoMo rerank recall limits
- [#2650](https://github.com/MemPalace/mempalace/pull/2650) perf(mcp): read diary and drawer lookups from SQLite
- [#2651](https://github.com/MemPalace/mempalace/pull/2651) docs: guide agents to retrieve only needed memory context
- [#2652](https://github.com/MemPalace/mempalace/pull/2652) fix(storage): retain helper read transactions only in WAL mode
- [#2655](https://github.com/MemPalace/mempalace/pull/2655) fix(hallways): keep unequal code extensions as distinct files
- [#2656](https://github.com/MemPalace/mempalace/pull/2656) fix(tunnels): do not prune distinct links via an ambiguous bare alias
- [#2657](https://github.com/MemPalace/mempalace/pull/2657) fix(rooms): resume closet apply by stable id, not current room
- [#2658](https://github.com/MemPalace/mempalace/pull/2658) fix(storage): release no-OFD WAL helper snapshot after each read
- [#2661](https://github.com/MemPalace/mempalace/pull/2661) fix(kg): do not rewrite onto a future-only successor
- [#2662](https://github.com/MemPalace/mempalace/pull/2662) fix(mcp): invalidate overview caches when only the WAL changes
- [#2663](https://github.com/MemPalace/mempalace/pull/2663) fix(http): protect diary handles across embedding/reconnect
- [#2664](https://github.com/MemPalace/mempalace/pull/2664) fix(rooms): preserve approved spellings on room-set reload
- [#2665](https://github.com/MemPalace/mempalace/pull/2665) fix(chroma): omit reserved chroma:* keys from SQLite hydrate
- [#2666](https://github.com/MemPalace/mempalace/pull/2666) chore: sync main history before v3.11 review
- [#2668](https://github.com/MemPalace/mempalace/pull/2668) fix(rooms): harden pending closet snapshot replay
- [#2669](https://github.com/MemPalace/mempalace/pull/2669) fix(wings): pin split plan targets across interrupted retries
- [#2670](https://github.com/MemPalace/mempalace/pull/2670) fix(search): rank the unfiltered lexical window before capping it
- [#2687](https://github.com/MemPalace/mempalace/pull/2687) fix(embedding): serialize ONNX runs on DirectML sessions
- [#2688](https://github.com/MemPalace/mempalace/pull/2688) perf(tunnels): batch the mine's tunnel passes into one load and one save
- [#2689](https://github.com/MemPalace/mempalace/pull/2689) perf(mining): skip already-filed project files from one metadata pass
- [#2690](https://github.com/MemPalace/mempalace/pull/2690) perf(mining): skip the post-mine quick_check when the mine wrote nothing
- [#2691](https://github.com/MemPalace/mempalace/pull/2691) perf(hallways): read the wing through a scoped sqlite pass on Chroma
- [#2693](https://github.com/MemPalace/mempalace/pull/2693) fix(embedding): look up EmbeddingGemma tokens before dequantizing the table
- [#2694](https://github.com/MemPalace/mempalace/pull/2694) feat: add opt-in EmbeddingGemma 2 and native media retrieval
- [#2697](https://github.com/MemPalace/mempalace/pull/2697) test(wings): pin metadata-merge contract for wing split (Copilot wipe report not reproducible)
- [#2698](https://github.com/MemPalace/mempalace/pull/2698) docs: correct stale MCP tool counts (44/45 → 47) + drift test
- [#2699](https://github.com/MemPalace/mempalace/pull/2699) fix(miner): accept mempalace.yml as a project config filename
- [#2700](https://github.com/MemPalace/mempalace/pull/2700) fix(config): warn when config.json is valid JSON but not an object
- [#2701](https://github.com/MemPalace/mempalace/pull/2701) fix(hub): keep mempalace_reconnect out of a running mine; count failed files as errors
- [#2702](https://github.com/MemPalace/mempalace/pull/2702) fix(miner): read Business Central .al and PowerShell .ps1 sources
- [#2703](https://github.com/MemPalace/mempalace/pull/2703) docs: correct plugin command counts (5 → 6) + drift test
- [#2704](https://github.com/MemPalace/mempalace/pull/2704) fix(init): update an existing mempalace.yml instead of shadowing it
- [#2705](https://github.com/MemPalace/mempalace/pull/2705) docs: MineInProgress on reconnect, MineFileErrors on mine (stacked on #2701)
- [#2706](https://github.com/MemPalace/mempalace/pull/2706) chore(entity_detector): sample .al and .ps1 when prose is scarce (post-3.11)
- [#2717](https://github.com/MemPalace/mempalace/pull/2717) fix(embedding): keep the MiniLM fallback for unknown embedding_model; refuse near misses; read legacy palaces; check and record the embedder identity everywhere; run EmbeddingGemma 2 on CUDA
- [#2719](https://github.com/MemPalace/mempalace/pull/2719) chore(deps): 3.11 dependency sweep (Pillow 12.3.0, chromadb 1.5.9, routine lock bumps)

---

## [3.10.0] — 2026-09-15

Agents get lighter ways in: a 3-tool MCP server with Palace Query Language, shared-brain rules for `host:harness:project` identities, and optional native exact-vector search. New installs follow XDG, and storage fixes close a silent write-loss path, a macOS integrity refusal and several hangs.

### Upgrade notes

- **`mempalace rules` no longer accepts `--agent`.** Pass `--host`, `--harness` and `--project`, then re-render any installed shared-brain block. (#2508)
- **`get_collection()` refuses names MemPalace never reads.** Any name other than the configured drawers collection or `mempalace_closets` raises `CollectionNameMismatchError`; maintenance scripts can pass `_skip_name_check=True`. (#2446)
- **The MCP event listing returns the newest events first when no cursor is given.** `mempalace_event_list` and `palace_coordinate` now default to newest-first unless `since_event_id` or `order` is passed; `mempalace logstream list` and `Logstream.list_events()` still default to oldest-first. (#2497)
- **New installs keep config and palace under `~/.config/mempalace`** (or `$XDG_CONFIG_HOME/mempalace`). Existing `~/.mempalace` installs are unchanged. (#148)

### Performance & Architecture (Dual-Track Rust Engine)

- **Optional native Rust exact-vector acceleration (`rust_exact`).**
  Reads the existing SQLite format into a contiguous float buffer, uses Rayon
  for large scans, and releases the Python GIL. Local and external writes
  invalidate the native cache. Loading is collection-scoped and decodes blobs
  safely; serial and parallel scans preserve deterministic tie ordering.
  Complex filters and absent native extensions fall back to Python.

- **Standalone native vector CLI and optional wheels.**
  GitHub releases include native wheels and platform executables. The CLI
  accepts JSON embedding vectors via `--vector` or stdin; it does not embed
  text or require Python. Platform runtime libraries may still be required.

- **NumPy top-k optimization.**
  Cache vector norms and partition distances before sorting the selected
  results, preserving row-order ties at the cutoff.

- **Read-only search operations stop contending with palace writer leases.**
  `searcher.py` and `_open_search_collection` now explicitly open collections with `read_only=True`. Read-only queries connect in SQLite WAL reader mode without attempting schema initialization or writer lease locks, allowing CLI and agent searches to run concurrently with an active background MCP server or writer process without raising `MineAlreadyRunning`.

### Refactors

- **MCP server is a package, not an 8 000-line module.** `mempalace.mcp_server` stays the public import path (`TOOLS`, `handle_request`, `main`, every `tool_*` handler, and the process-global names tests monkeypatch). Implementation lives in domain files under `mempalace/mcp_server/` so PRs can target drawers, KG, coordination, protocol, or HTTP without colliding on one god-file. Fragments exec into the package namespace — a physical split, not a behaviour change.
- **CLI is a package, not a 4 000-line module.** `mempalace.cli` stays the public import path (`main`, every `cmd_*` handler, and the names tests monkeypatch). Implementation lives in domain files under `mempalace/cli/` so PRs can target init, mine, search, repair, logstream, or argparse without colliding on one god-file. Fragments exec into the package namespace — a physical split, not a behaviour change.
- **MCP server tests are split by domain.** `tests/test_mcp_server.py` is now `tests/mcp/` (protocol, read, search, write, diary, KG, guards, stale-library) so community MCP PRs do not collide on one 8 600-line test file. Shared helpers live in `tests/_mcp_server_helpers.py`.
- **Searcher is a package, not a 2 200-line module.** `mempalace.searcher` stays the public import path (`search`, `search_memories`, `SearchError`, and the helpers tests monkeypatch). Implementation lives under `mempalace/searcher/` (ranking, filters, sqlite BM25, candidates, query, CLI, render) so PRs can target one seam. Fragments exec into the package namespace — a physical split, not a behaviour change.
- **Palace ops are a package, not a 1 800-line module.** `mempalace.palace` stays the public import path (`get_collection`, `mine_lock`, `mine_palace_lock`, closet helpers, and the names tests monkeypatch). Implementation lives under `mempalace/palace/` (collection, backend, closets, mine locks, mined-set) so PRs can target one seam. Fragments exec into the package namespace — a physical split, not a behaviour change.

### Features

- **Shared-brain rules use a `host:harness:project` identity and can name either MCP tool surface.** `mempalace rules` takes `--host`, `--harness` and `--project` (stable lowercase tokens) instead of `--agent`, plus `--mcp full|light`: `full` (default) names the 45-tool `mempalace-mcp` tools, and `light` names the `palace_query` / `palace_exec` / `palace_coordinate` triad with identical prose. The rendered block has agents compose the identity from their current workspace, sweep the inbox when they start collaborating instead of arming a watcher at session start, and arm `logstream watch` only when asked to listen, after claiming a task or after delegating. `logstream watch --agent` now defaults `--state-file` to `~/.mempalace/watch/<agent>.json` with `_` doubled and `:` sanitized to `_`, so Windows `host:harness:project` identities need no private path. (#2508)
- **`mempalace-light-mcp`: a 3-tool MCP server with Palace Query Language.** `palace_query`, `palace_exec` and `palace_coordinate` cover search, taxonomy, KG, graph, diary, drawers, mining, tunnels and RFC 003 coordination through a one-line PQL string (`FIND "oauth" IN backend/auth LIMIT 5`) or a structured dict, at about a quarter of the 45-tool schema (10.6 KB versus 41.6 KB). Writes go through the same sanitizing handlers, so drawer text stays verbatim; search ranking is unchanged, stdio is hub-first, and `--read-only` refuses `palace_exec` and `palace_coordinate`. Register it beside the full server rather than in place of it: `claude mcp add mempalace-light -- mempalace-light-mcp`. (#2412)
- **Config follows the XDG Base Directory Specification on new installs.** Resolution order is `$MEMPALACE_CONFIG_DIR`, then an existing `~/.mempalace` that holds `config.json`, `people_map.json` or `palace/chroma.sqlite3`, then `$XDG_CONFIG_HOME/mempalace` (absolute values only), then `~/.config/mempalace`. Existing installs keep `~/.mempalace`, and `palace_path` defaults to `<config_dir>/palace`. (#148)
- **CLI writes honour the daemon write-routing policy.** `mine`, `sweep`, `sync` and the post-setup mine in `init` follow `direct` / `prefer` / `require` from `MEMPALACE_CLI_WRITE_ROUTING` or `write_routing.cli` in `config.json`, with `--daemon` / `--direct` as per-command overrides. The default stays `direct`, so nothing changes unless you opt in. Once a job is submitted to the daemon it is never re-run directly on error, since the daemon may already have accepted it. `sweep` is now a daemon job. (#2033)
- **pgvector fleets can share one table set across machines.** The opt-in `pgvector_shared_namespace` (config key, `MEMPALACE_PGVECTOR_SHARED_NAMESPACE`, or the `shared_namespace` backend option) replaces the palace-path hash in the table prefix, so nodes with different local palace paths read and write the same tables. Unset, prefixes are byte-for-byte unchanged; the value is normalized strictly and recorded in the backend marker. (#2167)
- **Hybrid-rank weights are configurable.** `MEMPALACE_HYBRID_VECTOR_WEIGHT` / `MEMPALACE_HYBRID_BM25_WEIGHT`, or `hybrid_rank_vector_weight` / `hybrid_rank_bm25_weight` in `config.json`. Defaults stay 0.6 / 0.4, and malformed values fall back to them. (#2430)
- **Drawers track `last_modified`.** New drawers set it from `filed_at`; every `update_drawer` that changes content, wing or room stamps it afresh while `filed_at` keeps the creation time. Legacy drawers report `filed_at` as their `last_modified`. (#2147)
- **Search results say where their dates come from.** Vector, union and lexical-only hits return `filed_at`, `authored_at_source`, `content_date` and `content_date_source` (`filename`, `frontmatter`, `body`, `mtime`, or `unknown`). Provenance is recorded at mine time and never reconstructed for legacy drawers. (#2453)
- **`mempalace_kg_timeline` paginates.** `limit` / `offset` follow the `list_drawers` convention; the hardcoded first-100 cap is gone. (#2406)
- **Logstream listings default to the newest events when no cursor is given.** `mempalace_event_list` and `palace_coordinate` return the most recent events first when `order` is omitted and no `since_event_id` is passed, and keep ascending order when resuming from a cursor; an explicit `order` always wins and is case-insensitive. The light server's `palace_coordinate` schema now exposes `order`, `limit`, `preview` and `since_created_at`. (#2497)
- **`dynamics.effective_strength()` computes connection decay at read time.** It is pure, so reading a strength cannot change it and repeated maintenance passes cannot compound decay the way `apply_decay` does. The module docstring no longer claims hallways and tunnels already potentiate or decay. (#2416)
- **MemPalace plugin for the DeepSeek Harness (`.dsh-plugin/`).** A DSH bundle plugin with three rows, built against the DSH 0.1.5 extension points. `mempalace-recall` puts `mempalace wake-up` for the project's wing in each top-level session's system prompt from its first model call; DSH resolves prompt text before plugins can wait on anything else, so the row holds only the first assembly, for a bounded time, and never fails the step. `mempalace-autosave` keeps an append-only transcript per session under `$DSH_HOME/mempalace/transcripts/`, holding only what the user wrote and the assistant's text (never harness context, tool results, reasoning or compaction summaries), and runs MemPalace's own `stop`, `precompact` and `session-end` hooks against it through the new `dsh` harness. `mempalace-mcp` serves the palace through DSH's own MCP client with `mempalace-light-mcp`: its 3 tools cost about 2,100 schema tokens per model step against about 7,700 for the 45-tool server. A `node --test` suite covers the plugin and `tests/test_dsh_plugin.py` runs it under pytest.

### Improvements

- **`repair` says when it declines an in-place FTS5 heal, and names the SQLite that decided.** Declining was a silent `return` on two paths, so the operator saw the ABORT banner with no sign that a rebuild had been considered; `--dry-run` withheld the same explanation. All of them now print it, and the `--dry-run` copy is marked as a preview so it cannot be read as the run it is predicting. `sqlite3.sqlite_version` accompanies every verdict these paths state — the abort banner, both `--dry-run` branches, the heal's outcome line, the post-recovery check, all three shapes of the MCP integrity payload, the tool-refusal payload, the integrity log line and `MineValidationError`'s message — because a clean result from a build that cannot detect a given FTS5 fault reads exactly like one from a build that can. The post-mine validator reports at `warning` rather than `info`, the level that survives on the `mempalace mine` path, which configures no logging. (#2240, #2254)
- **A skipped startup integrity probe is no longer reported as a clean verdict.** Above `MEMPALACE_STARTUP_INTEGRITY_MAX_MB` no `quick_check` runs, yet the MCP payload answered `checked: true, ok: true`. It now returns the not-applicable shape with the skip as its `reason`, and that reason states sizes precisely enough to show the comparison it reports — a 1.4 MB database against a 1 MB limit used to read as "is 1 MB, over the 1 MB limit". Write tools gate on recorded errors rather than on `ok`, so nothing is newly refused, and `/statusz` no longer collapses a missing verdict into a failure — `ok` is false only when a check came back dirty, which also stops a non-chroma backend from reporting itself unhealthy (#1931). (#2240, #2254)
- **The FTS5 classifier is anchored to the start of the `quick_check` row.** `sqlite_integrity_errors` reports a failed probe as `PRAGMA quick_check failed: <error>`, and an unanchored match read such a row as an isolated FTS5 fault — authorizing a rebuild on a database whose state was never established. `fts5: checksum mismatch`, which SQLite 3.51.2 emits, stays unmatched deliberately: it does not name which side is damaged, and a rebuild reads from the content table. (#2240, #2254)
- **`list_drawers` and `graph_stats` walk a Qdrant collection in one scroll.** `BaseCollection.get_all_rows()` joins `get_all_metadata()` as the ids-carrying bulk read; its default still pages through `get(limit=, offset=)`, and Qdrant overrides it with a single scroll where the offset loop had cost 275 s and 292 s on a 126k-drawer palace. The embedding wrapper forwards the call so the override is reached. (#2452, #2474)
- **Hallway recompute reads only the mined wing.** `compute_hallways_for_wing` fetched every drawer in the palace on each mine; it now pages through `get(where={"wing": ...})` in bounded pages, keeping the #1619 variable-limit protection, so the cost follows the wing rather than the palace. On Qdrant, whose `get()` still scrolls `offset + limit` rows for each page, a wing larger than one page re-reads its earlier pages. (#2466, #2477)
- **CLI `status` reads backend metadata in one pass when the direct SQLite tally is unavailable.** Chroma palaces are still tallied straight from `chroma.sqlite3`. On the collection path, used by backends such as Qdrant, `status()` now calls `get_all_metadata()` when the collection exposes it instead of paging with `get()`: 1 m 46 s down to 3.4 s on a 160k-drawer Qdrant palace. (#2153, #2154)

### Bug Fixes

- **`init` accepts `--palace` in the natural invocation order (#2366).** `mempalace init <dir> --palace <path>` previously failed with `unrecognized arguments: --palace` because the flag was registered only on the global parser. `--palace` now mirrors the existing `serve` subcommand pattern (#1877), so both `--palace <path> init <dir>` and `init <dir> --palace <path>` work. The global form takes precedence on Python <3.12 where subparser defaults can otherwise clobber it.
- **`MEMPALACE_PALACE` environment variable is honored (#2366).** The short var name from the issue body now resolves through `MempalaceConfig.palace_path`, so `MEMPALACE_PALACE=<dir> mempalace status` (and every other command reading `palace_path`) targets the intended palace. Precedence: `MEMPALACE_PALACE_PATH` > `MEMPALACE_PALACE` > legacy `MEMPAL_PALACE_PATH`.
- **The transcript-path fallback no longer gives every git worktree its own wing.** `_wing_from_transcript_path`'s primary path (reading `cwd` from the JSONL) already collapsed a `<project>/.claude/worktrees/<wt>` segment before deriving the wing; the fallback path, used whenever `cwd` is absent, had no equivalent strip, so the flattened `--claude-worktrees-<wt>` segment survived into the wing name. Applied the same collapse there. (#2388, #2454)
- **`mempalace mine --daemon` no longer mines the daemon's own working directory instead of the caller's.** The daemon is a long-lived background process that keeps whatever cwd it happened to start with, so a relative source (`mempalace mine .`) submitted to it resolved against that stale cwd rather than the directory the CLI call actually ran from, silently mining the wrong project into the wrong wing on every subsequent hook-driven call. `cmd_mine` now resolves the source to an absolute path before it enters the daemon job payload, matching the resolution `_forward_mine_to_hub` already does for the hub-forwarding path. (#2441, #2467)
- **`mempalace init` no longer replaces a `known_entities.json` it could not read with the entities of that one run.** The merge reads the registry, falls back to an empty dict when the read does not conclude, and rewrites the file whole, so a registry holding four categories became one holding `people: 1`, measured, with `_load_known_entities` dropping from 10 names to 1 and `get_topics_by_wing` from two wings to none, while the command printed `Registry updated`. None of that needs an interrupted write: through the CLI, one `mempalace init` replaced a 28-byte JSON array and a 172-byte truncated object alike, and a registry re-saved in a legacy encoding ended the run in a traceback before any of it. The writer also truncates the file and serializes into it afterwards, so a killed run or a full disk leaves the same kinds of file. Reads now treat only `FileNotFoundError` as an absent registry. One that does not parse, whether from a truncated write, a byte that is not UTF-8, a hand-edit that lost a brace or a JSON array, is renamed aside and its new name printed before a fresh registry is written, and one that exists but cannot be read is left alone with a message rather than overwritten. The write itself goes through a temporary file and a rename, as `migrate._apply_topics_by_wing_renames` already did for this same file, so an interrupted run leaves the previous registry byte for byte. A byte that is not UTF-8 also stopped leaving `UnicodeDecodeError` out of the call, where `mempalace init` rendered it as a traceback. The call answers `None` when it wrote nothing, so `mempalace init` no longer prints `Registry updated` over a registry it left alone; a registry reached through a symlink is written through it rather than replaced; a directory that will not take a temporary file gets the merge in place with a message rather than a traceback; and a UTF-8 byte-order mark is no longer read as a parse failure. (#2358, #2359)
- **`repair-status` no longer reports a database it could not reach as one the palace never had.** `repair.status` drew that line with `os.path.isfile`, which follows symlinks and folds `OSError` and `ValueError` alike into False, so `Palace dir at … exists but has no chroma.sqlite3 yet` and `status: "uninitialized"` came back for a palace whose database sat intact behind a directory permission, for one whose name had become a broken symlink, and for a symlink loop — word for word what a palace that really has no database is told. What that sentence invites is a re-mine, which on the broken-symlink shape builds a second database beside the untouched first and leaves behind any drawer that has no source file to be re-derived from. Absence is now decided by `ENOENT` alone, the `_integrity_target_is_absent` predicate #2290 added, and an unreachable database no longer takes that early return: the capacity report below it answers for the palace instead, printing `sqlite count: (unreadable)` and `UNKNOWN`. A truly absent database still reports `uninitialized`. Getting past that gate puts one more state in front of the read: a named pipe, which `os.path.isfile` used to turn away and whose open parks in the kernel until a writer arrives — inside sqlite3, where the `O_NONBLOCK` answer `miner._read_text_no_follow` uses is not available. What keeps the read from parking is a type check next to the open itself, in `sqlite_drawer_count`; the gate above repeats it only so the operator is told which state this is instead of the `(unreadable)` a permission also produces, and a check at the gate alone would not do, because under a directory this process cannot enter its `stat` fails and answers False by design. No other type needs a case: a directory, a socket, a block device and a character device each answered with an error in well under a second, and landed in that same unreadable report. The printed answer and the returned dict are what change; the exit code was 0 for all of these before and still is. (#2293, #2393)
- **Layer 1 wake-up fetches the most recent drawers instead of an arbitrary scan window.** `BaseCollection.get_recent()` returns the newest N by `filed_at`; pgvector overrides it with an `ORDER BY ... DESC LIMIT n` pushdown (capability token `supports_recency_order`), and backends without pushdown keep the previous scan-and-sort behavior. On palaces larger than the 2,000-drawer scan cap, wake-up no longer leads with the oldest backfill. Ordering is on the stored `filed_at` text, the same comparison Layer 1 has used since #1630. (#1630, #2168)
- **Checkpoints summarize the conversation, not the harness.** Text the tooling injects with `role: "user"` (slash-command expansion, local-command blocks, task notifications, skill preambles, pasted-image placeholders, interruption records) no longer becomes the checkpoint's `recent:` line. The match is anchored to the opening of the message, so a genuine message that quotes one of those wrappers anywhere after the first character is still summarized. (#2170)
- **The backend header sniff no longer drops the WAL writer's POSIX locks.** `detect()` and `_database_signature()` opened the palace database with a plain `open()` on nearly every MCP call; on POSIX, closing that descriptor released every `fcntl` lock the process held on the file, including the WAL writer's SHARED lock, so an external connection's clean close could checkpoint and unlink the live `-wal` / `-shm` from under it. The sniff no longer disturbs the process's locks. (#2471, #2472)
- **macOS no longer refuses every tool on a healthy WAL palace.** Apple's SQLite fails a read-only connection's first statement when the `-wal` / `-shm` sidecars are absent, which is the normal state before any process opens the palace, so the integrity gate reported the palace unreadable. That one case now takes a normal open; everything else keeps `mode=ro`. (#2489, #2490)
- **A malformed JSON-RPC request that carries an `id` gets an answer instead of silence.** A non-string `method` or non-object `params` raised inside dispatch, so stdio wrote nothing and HTTP closed the socket, leaving the client waiting on that `id` forever. Malformed envelopes now return `-32601` / `-32602`, and a dispatch failure returns `-32603`. `notifications/*` methods are still never answered, as before. (#2231, #2232)
- **`get_collection` refuses a collection name MemPalace never reads.** Any name other than the configured drawers collection and `mempalace_closets` raises `CollectionNameMismatchError` (a `ValueError`) instead of silently creating an orphan store that search, MCP and repair cannot see. Maintenance scripts can pass `_skip_name_check=True`. (#2347, #2446)
- **A stranded HNSW index is no longer reported as flush-lag.** When `index_metadata.pickle` is absent, two states that never resolve on their own are now named. A `hnsw:sync_threshold` the collection can never reach is reported as `flush_unreachable`, with how to force a flush, and vector search stays enabled. A replacement stub whose payload cannot hold the SQLite count is reported as `diverged`, with `mempalace repair` as the fix. Both need at least 2,000 embeddings (the stub case, a shortfall of at least 2,000), so smaller collections still get the flush-lag report. (#2435)
- **Impossible counts in an HNSW `header.bin` are rejected.** Astronomical capacities and element counts above capacity now feed segment health, startup quarantine and the capacity report before Chroma's native reader opens the segment. (#2146)
- **Caller-vector Chroma collections are created without an embedding function.** Chroma attached `DefaultEmbeddingFunction` when none was passed and persisted it in the schema, so a document-only write on a 3-dimensional collection tried to embed at 384 dimensions. Collections are now created and reopened EF-free, and fail closed if one is still exposed. (#2141)
- **Chroma systems owned by the backend are stopped before the shared cache is cleared.** On an external inode or mtime change the backend cleared Chroma's process-global system cache before closing its own clients, so the displaced `System.stop()` never ran. Clients the backend does not own, such as a directly constructed `PersistentClient`, are outside this fix. (#2421)
- **pgvector reconnects once when Postgres drops the pooled connection.** A server restart used to cost each long-lived client one `terminating connection due to administrator command` error. SQLSTATE class 57 and a closed handle retry once on a fresh connection; statement-level errors still propagate on the first attempt. (#2084)
- **CLI `search --wing` / `--room` recovers from Chroma's `Error finding id`.** It now uses the same filtered-query fallback as MCP search. (#2373, #2380)
- **An unparsed Codex rollout is no longer imported as raw JSON.** A rollout with `session_meta` that the installed parser cannot normalize is skipped with a logged reason and left eligible for a retry after a parser update; existing drawers are untouched. (#2469)
- **`mempalace_diary_read` sees entries past the first 10,000.** It pages through the agent's diary and keeps the newest `last_n`, and `total` is no longer capped. (#563, #2464)
- **A backend that fails to initialize is no longer reported as a peer writer.** Setup failures are reported separately from lease contention, with recovery guidance, and the light MCP client forwards writes to a live hub instead of first trying to take the local writer lease. (#2465)
- **A busy daemon keeps its registration.** `start_daemon` deleted `endpoint.json` and the pid file after one failed health probe, stranding a live daemon that still owned the palace lock. (#2442, #2473)
- **Stripping a leaked `PYTHONPATH` keeps the running interpreter's own site-packages.** An embedding host that put the venv's own `purelib` / `platlib` on `PYTHONPATH` lost its dependencies at import. (#2484, #2496)
- **A named pipe at `chroma.sqlite3` no longer stops the MCP server from answering.** The integrity probe opened the pipe for reading, which waits until a writer arrives, and gated tool calls, `mempalace_status`, `mempalace_reconnect` and `/statusz` all run it or wait behind it. Over stdio the first such call left the session answering nothing, `ping` included, until the idle watchdog ended the process; over HTTP those calls hung while `ping` and `/healthz` kept answering. The probe now refuses the pipe by type before any open, as `repair-status` already does, and reports it as an integrity error: gated tool calls get the `-32002` refusal naming it, and `python -m mempalace.repair rebuild` stops at the abort banner instead of hanging. (#2498)
- **Hooks run on fresh installs that keep their config under `~/.config/mempalace`.** Since #148 a new install writes its config and palace to the XDG config directory and never creates `~/.mempalace`, but the hooks' kill-switch only looked for `~/.mempalace`, so the Stop, PreCompact and SessionEnd hooks returned `{}` without saving anything: a fresh `mempalace init` followed by a PreCompact hook filed 0 drawers, against 20 once `~/.mempalace` existed. The kill-switch in `hooks_cli` and in the Antigravity shell hooks now also accepts the resolved config directory, and the shell hooks read `hooks.auto_save` from it. Existing installs behave exactly as before.
- **`mempalace wake-up` no longer reports a healthy palace as missing while a hub is running.** The L1–L3 layers opened the palace writable, and on the `sqlite_exact` backend a writable open takes the palace mine lock to initialise its schema. With a hub, daemon or mine holding that lock the open failed, and the swallowed exception printed "No palace found. Run: mempalace mine <dir>", inviting a pointless re-mine. The layers now ask for a read-only open, which `sqlite_exact` serves without the lock; ChromaDB ignores the option, but its collection open never took the mine lock. A lock conflict that does surface gets its own message instead of the missing-palace one.
- **A writable `mempalace serve` waits for a busy writer lease instead of restart-looping.** Startup refused with status `2` the moment another process held the palace writer lease, and under the systemd template's `Restart=always` that became a loop that never started, never gave up and never reached `failed`. The server now retries with backoff for `MEMPALACE_MCP_WRITER_WAIT_SECONDS` (default `120`, `0` refuses at once) and logs once when the wait starts; a backend or lock-directory failure still refuses immediately. The unit sets `RestartPreventExitStatus=2`, so a wait that runs out lands in `failed` with the reason in the journal, and the idle watchdog runs the registered cleanup before exiting, so `serverinfo.json` no longer advertises a dead PID. (#2500, #2501)
- **`mempalace daemon start` and `stop` recover from a stale registration.** The #2442 guard refused to start while the registered pid was alive, and after a crash that pid can belong to an unrelated process, so start refused forever; the suggested `mempalace daemon stop` used the same failing health probe and did nothing. A registered pid that is dead, or whose process started after the registration was written, is now treated as stale: `start` replaces it and `stop` removes it. A live daemon that does not answer keeps its registration, a foreground start refuses beside it as a background one does, and both commands name the process to stop.

### Documentation

- **Codex plugin install instructions use the built-in marketplace.** (#2372)
---

## [3.9.0] — 2026-08-31

One hub now serves every agent: CLI search reuses the live palace index, concurrent reads stop blocking each other, and skill-first coordination plus opt-in release awareness make shared-brain fleets easier to operate safely.

### Features

- **Release awareness is opt-in and agent-visible.** `mempalace update` can configure or explicitly run stable-release checks and prepare an exact upgrade plan, while `mempalace_status` exposes only cached state so agents can ask for authorization without blocking or installing anything automatically.

- **Skill-first setup is now the recommended agent onboarding path.** `npx skills add MemPalace/mempalace` discovers a guided `mempalace` setup skill that can bootstrap the Python package and MCP connection, distinguish a private local palace from a shared-brain hub or client, assign a stable fleet identity, install the canonical shared-brain rules, and verify logstream readiness. The new `mempalace-task` skill covers active coordination without mixing it into the recall protocol.
- **High-level task creation turns raw logstream primitives into a complete handoff.** `mempalace task create` and the equivalent remote-safe `mempalace_task_create` MCP tool accept the goal, worker identities, project, branch, immutable hexadecimal base commit id, and definition of done; they reject mutable branch/tag base references, create the canonical immutable `task.request`, generate its correlation id, and return one ready-to-paste line that wakes the destination agent while the exact specification stays in the logstream. `task launch` is an explicit controlled-mode adapter for Codex and Claude: it validates the complete stored request, proves the worker checkout is clean and on the stored branch/base commit, validates the worker identity, releases the logstream handle, and invokes the runner without a shell or permission-bypass flags.

### Bug Fixes

- **`mempalace search` reuses a live palace Hub instead of loading another full HNSW index.** Agent workers commonly invoke the CLI even when `mempalace serve` already owns the same palace; each direct Chroma client then cold-loads its own private copy of the vector index. On a 4.99-million-vector palace with a 7.8 GB HNSW file, two concurrent searches held about 18.6 GB of private RSS and pushed WSL into sustained swap and I/O wait. A forwardable CLI search now discovers the per-palace Hub and asks its existing `mempalace_search` MCP tool for CLI-compatible candidate selection, ranking, and output while reusing the Hub's open collection. Explicit backend selections, queries or filter names the MCP sanitizers would rewrite or reject, result counts outside its 1–100 range, search-affecting per-invocation environment overrides, Hub startup configuration that no longer matches the current effective search settings, and machines without a healthy Hub keep the direct path. Once a Hub accepts the request, a transport failure is reported without silently falling back to another full local index load; `MEMPALACE_HUB_FORWARD=0` remains the explicit escape hatch.
- **EmbeddingGemma's internal sub-batch size is now overridable.** `_EMBEDDINGGEMMA_BATCH_SIZE` bounds documents per `session.run()` by count, not by allocation, and attention buffers scale with `batch * padded_len ** 2`, so a palace whose drawers approach the 2048-token cap can still exceed available memory at the default of 32 with no supported knob to lower it, killing `mempalace repair rebuild-index` outright. `MEMPALACE_EMBEDDINGGEMMA_BATCH_SIZE` (or `embeddinggemma_batch_size` in `config.json`) now reaches `EmbeddinggemmaONNX`, following the same env-then-file-then-default precedence as `embedding_threads`. (#2330)
- **The Qdrant backend no longer scans an entire collection to answer a small, bounded `get()`.** `QdrantCollection.get()`/`_rows()` always materialized every row `_scroll_all()` could reach before slicing to `limit`/`offset` in Python, so a caller asking for one page (`list_drawers` on the non-Chroma path is the reported case) paid the full-collection cost regardless of how small a page it wanted. `_scroll_all()` now accepts an optional row bound and stops paging once it is met, and `get()` passes `offset + limit` through whenever that is provably safe: no `ids` lookup (which needs every matching point regardless of scroll order) and no `where`/`where_document` shape that requires the local Python re-filter (which could otherwise drop rows the bound already discarded, under-returning). `get_all_metadata()` and any local-filter-requiring `where` keep the original unbounded walk. (#2363)
- **A hub `mempalace_search` no longer stalls every other MCP client.** HTTP dispatch used one exclusive lock for protocol calls, palace reads, and palace writes. Protocol methods and tools backed by independent stores now bypass the palace lock; palace reads share a writer-preferring lock; palace writes remain exclusive. Concurrent searches no longer queue behind each other at the HTTP layer, while state-changing calls still wait for in-flight reads.
- **Search now reports raw vector similarity and indexes closet source lookups.** Closet boost still affects ranking through `effective_distance`, while the public `similarity` remains the underlying vector score. sqlite_exact adds an expression index for `source_file`, avoiding collection scans during closet enrichment.

- **A `config.json` this process could not read is no longer written over.** `MempalaceConfig` reads the file once and falls back to an empty dict on any failure, and every setter then serializes that dict back over the whole file. One unreadable read was therefore enough to lose every setting the file held: a config hand-edited into something that does not parse, one trailing comma or a byte-order mark an editor added, went from 219 bytes to `{"backend": "chroma"}` at the next `mempalace init --backend`, with `palace_path` back to the default and `people_map` gone. Such files also come from the setters themselves, since one `set_hook_setting` on a 3.6 MB config is a window during which the file on disk is not the config, and ten of ten runs killed inside it left a truncated file behind. The file is now read as bytes, and only `FileNotFoundError` counts as "there is no config here". A file that does not parse is renamed aside, with its new name printed, before a fresh one is written; a file that exists and cannot be read at all is not touched and the setter says so instead of returning as though it had saved. Writes go through a temporary file named after this process and are renamed into place, so an interrupted write leaves the previous config exactly where it was, and a setter that cannot write reports it rather than swallowing the error. `people_map.json` is written the same way. A config reached through a symlink is written through it rather than replaced, so a dotfiles checkout keeps receiving the settings; a directory that will not take a temporary file gets the write in place with a message rather than losing the setting; a UTF-8 byte-order mark is no longer read as a parse failure; and a setter that has to create `~/.mempalace` restricts it to the owner. (#2356)
- **A palace with no database is no longer reported as one that passed its integrity check.** `sqlite_integrity_errors` answers `[]` when `chroma.sqlite3` is absent, and the MCP gate published that as `checked: true, ok: true`. Absence is now decided by `ENOENT` alone, which proves that nothing resolves under the path, and reported as the not-applicable shape #1931 introduced, `checked: false`/`ok: null` plus a reason. Every state that is not proven absent reaches the probe, and a probe that cannot open the file reports `PRAGMA quick_check failed`, which trips the existing `-32002` refusal: a dangling symlink, a database under an unreadable directory, a symlink loop, a name the filesystem rejects, an embedded NUL in the path, and, on POSIX, a palace path whose parent is a file. A palace directory named with a byte that is not valid UTF-8 reached the probe and, up to Python 3.12, raised out of it, which `mempalace mine` and `mempalace repair` never guarded against; it is now reported like every other unreadable path. `/statusz` reads an absent verdict as healthy, so the new `ok: null` does not turn a fresh install red, and non-chroma backends stop reporting themselves unhealthy, which they had done since the #1931 fix. The size-limited startup skip still publishes a clean verdict; the only change there is that it no longer inherits the previous probe's absence reason. (#2290)
- **`update_drawer` and `delete_drawer` no longer strand stale closets at the mutated drawer's source.** Closets are the AAAK search-index layer, built once from a source file's raw content at mine time, and neither per-drawer mutation tool ever touched them: correcting a drawer in place left its closet quoting the pre-correction text indefinitely, and deleting a drawer left its closet's `->drawer_id` pointer dangling. `search_memories` boosts ranking by `source_file` and previews the closet document verbatim, so a retracted instruction could keep out-ranking its own corrected drawer. Both tools now purge the matching source's closets through the existing `_purge_source_closets` helper (already used by `delete_by_source`, #1722) rather than rebuild them, since closets are LLM-derived from file content this call path does not have: `delete_drawer` purges unconditionally, `update_drawer` only when `content` actually changes (a wing/room move alone leaves the quoted text correct). Rebuilding stale closets from the stored drawers, rather than only purging them, is a separate, larger change. (#2325)
- **`mempalace_mine` accepts a single conversation file again, so hook transcript ingest survives a running hub.** `cli.py` has always documented the mine source as "Directory to mine, or one conversation file with `--mode convos`", and `hooks_cli._ingest_transcript` submits exactly one `.jsonl`. The MCP tool validated `os.path.isdir` regardless of mode, and `cmd_mine` forwards to the hub whenever one is registered and healthy, so the documented single-file form was unreachable in the configuration most users run: every Stop and PreCompact transcript ingest failed with `source directory not found`. Nothing surfaced it, because `hook_precompact` returns the same empty object on the success path, leaving a compaction that captured nothing indistinguishable from one that captured everything. `convos` now accepts a file or a directory; the tree-walking modes still require a directory. (#2281)

---

## [3.8.0] — 2026-08-20

Large palaces get fast and stay small: both storage backends lost their palace-wide read paths, a proxied agent session no longer loads a storage stack it never uses, and agents gained a background watcher so coordination stops stalling on nobody listening.

### Features

- **`mempalace logstream watch` — a background watcher an agent can be woken by.** `logstream wait` is a primitive, not a watcher: it caps at five minutes and reports a timeout, so every caller ended up writing the same re-arm loop and each one had to remember to carry its cursor forward. Most did not, and coordinated tasks stalled on nobody listening rather than on the work. `watch` owns the loop and the cursor, and exits on a match so any harness that can background a process and react to its exit gets woken — `0` when it printed a match, `2` on `--idle-exit-ms`, `130` when interrupted; only `0` means "you have mail". Two filters a watcher needs are things `list_events` cannot express, because its SQL is single-valued and positive-only: repeating a flag means "or" (`--type task.request --type task.reply --type patch.ready` wakes for any of them and stays quiet for everything else), and `--agent <id>` expands to `--to-agent <id> --exclude-from-agent <id>`. That exclusion is not cosmetic — `to_agent=<you>` deliberately also matches `*` broadcasts, and your own broadcasts are broadcasts, so a watcher without it wakes itself every time it posts a status. `--state-file` persists the cursor so a restart resumes exactly, advancing past events that were examined and rejected rather than only matches; a cursorless first run starts at the tip like the SSE live-tail rather than replaying weeks of fleet history it cannot tell is stale, and says so on stderr. `--follow` keeps the process alive past the first match and emits NDJSON, since repeated indented documents on one stream are not parseable JSON. The invariant throughout is that a restart may cost a duplicate, never a missed delegation. (#2315)
- **The monitoring protocol is documented, including the cursor rule that costs real events.** Events are ordered by append order (`ORDER BY rowid`), not wall clock, so a peer's event is appended whenever it syncs and can already be older than a timestamp high-water mark — resuming with `since_created_at` therefore drops late-arriving cross-replica events permanently. Measured on a live fleet: a windows-origin event created `09:10:48Z` was ingested *after* a mac-origin event created `09:13:21Z`, in a single fifty-event window. `list_events`' docstring already said `since_event_id` was "the precise cursor … regardless of timestamp ties"; it just never reached an agent. That rule now appears in the `mempalace_event_list` and `mempalace_event_wait` tool descriptions at the point of use, both `since_created_at` parameters are marked "NOT a resume cursor", and `coordination-protocol.md` gained a monitoring section covering the modes, the announce-your-watch convention, and declaring when you are *not* watching — a false watcher is worse than a declared-absent one, because the requester stops looking for a human to nudge. The system-prompt snippet every agent copies was updated with it. (#2315)

### Bug Fixes

- **`sync --apply` no longer deletes drawers whose source file it could not reach.** `_classify_drawer` separated keep from remove with one `Path.exists()`, and `missing` feeds `removable_ids`, so every state that answered no became a deletion. Of twelve source-file states driven through `sync_palace`, eight were deleted and four of those had really gone: two were a file on a volume that is not mounted, at an empty mount point or at one holding a committed `.gitkeep`, and two were a path that could not be walked at all. Three further states ended the whole run instead, dry run included, and two of those moved with the interpreter as `pathlib` stopped raising. End to end, a project mined from a mounted volume lost every drawer to one `sync --apply` while the volume was away, and the search came back empty once it returned. Removal now asks for corroboration rather than a probe: a file not at its path is removable only while the palace still sees a source of its own, a regular file, in that same directory. A deletion leaves the file's neighbours where they were; an unmounted volume takes all of them at once, and no errno separates the two. Both halves of the verdict are read again at the moment it is formed, because a volume can leave inside one pass and come back inside one. Everything uncorroborated lands in a new bucket, `unresolved`, never added to `removable_ids` and reported the way removals are: counted beside the other buckets, and its source files named in `unresolved_by_source` and printed, with the remainder stated when that list is cut at five. The price: a directory with no surviving known source corroborates nothing, so a file deleted alone from a one-file directory, or a directory's files deleted together, is kept and reported rather than pruned. (#2320)
- **`list_drawers` and the tunnel tools stop cold-loading the vector index on a chroma palace.** Both paged `col.get()`, which opens the collection and loads HNSW before answering a question that is pure metadata: on a 1.7 GB / 165k-drawer palace `list_drawers` with no filter and `limit=20` took **36.6 s**, and `find_tunnels` died at 29.7 s with an internal tool error. `chroma.sqlite3` already holds everything those calls need, so they now read it directly and never open the collection. Two traps had to be avoided to make that a win rather than a trade. `chroma:document` lives in `embedding_metadata` alongside the loci, so an unqualified join drags the palace's entire verbatim text — 162 MB across 331k rows here — into memory to render one page; documents are excluded from the scan and the displayed page is hydrated separately. And `embedding_id` is only indexed as the second column of `UNIQUE (segment_id, embedding_id)`, so a join filtering on it alone degenerates into a full scan of `embedding_metadata`: 5.7 s for a single twenty-row page, against 0.003 s once the segment is sought first and `embedding_metadata`'s `(id, key)` primary key second. The wing/room filter is pushed into SQL rather than applied afterwards. Measured on the same palace, `list_drawers(wing=…, limit=20)`: **2.31 s and 1148 MB peak RSS → 0.01 s and 86 MB**. `find_tunnels`' `recent` field survives the move via a `MAX(date)` on the grouped read, and a missing or unreadable palace still reports a diagnostic rather than looking like a palace with no tunnels. (#2314)
- **`sqlite_exact` search and status stop scanning the whole palace.** `query()` was exact cosine over every row — selecting `id`, `document`, `metadata_json` and `embedding`, JSON-parsing metadata, then dotting in a Python loop — which `mempalace_search` does twice, once for drawers and once for closets. `status` paged every metadata row because the backend had no `facet_counts` and the taxonomy read was chroma-only. Ranking now runs vectorized over the embedding column with only the top-k documents hydrated, the embedding matrix and the `wing` / `room` / `source_file` columns are cached on the long-lived backend handle and invalidated on write, and `facet_counts` plus a grouped `json_extract` read serve status, `list_wings` and `list_rooms`. Exact cosine ranking is unchanged. Measured on a live 167k-drawer / 166k-closet palace: `mempalace_status` **6997 ms → 1045 ms**, warm `mempalace_search` **6364 ms → 210–1600 ms**, wing-scoped search 3910 ms → 1453 ms. (#2308)
- **The remaining palace-wide reads on `sqlite_exact` are gone.** `get(ids=…)` scanned every row and then did a dict lookup, `include=["metadatas"]` still selected documents and embeddings, and equality filters with `LIMIT`/`OFFSET` were applied in Python — so `list_drawers` at `limit=20` took 2752 ms because it loaded every document in the palace to show twenty previews. Those now go to SQL, pagination walks metadata only to collapse logical drawers and compute the total, and the page's previews are hydrated by id. `status`, `list_wings`, `list_rooms` and `get_taxonomy` share one grouped result behind a 5 s cache dropped on writes, and `graph_stats` gained the same sqlite reconstruction the chroma path uses. (#2311)
- **`wing`, `room` and `hall` are indexed places rather than JSON scanned palace-wide.** `sqlite_exact` stored them only inside `metadata_json`, so every structural question — taxonomy, `graph_stats`, `list_drawers where=wing`, facet counts — meant `json_extract` across the corpus. They are now VIRTUAL generated columns over the same JSON with a composite `(collection_id, wing, room, hall)` index, which existing palaces pick up on their next writable open. VIRTUAL is the point: the 1.6 GB embedding blob table is not rewritten, so the migration walks metadata once rather than copying a table. Read-only opens against a pre-migration palace fall back to `json_extract` unchanged. (#2313)
- **`embeddinggemma` no longer produces silently unusable vectors on Apple Silicon.** With the default `embedding_device=auto`, `_AUTO_ORDER` puts CoreML ahead of CPU, and CoreML returns NaN or all-zero vectors for this model without raising — ONNX Runtime only warns on stderr about how few nodes it could take. Same model and input: CoreML gave 9216 NaN in the hidden state and a 768-zero pooled vector with norm 0.0, against a healthy norm on CPU, reproduced under two onnxruntime versions and two graph partitionings. Nobody opted into this, and a `repair rebuild-index` under those conditions rewrites the whole palace with vectors that are indistinguishable from healthy ones once stored. Two layers now: a per-model provider denylist keeps `auto` from selecting CoreML for embeddinggemma at all, and the vectors are checked for a **finite and non-zero** norm before use — an all-zero vector passes any `isnan()` test and is exactly as unusable, so a NaN-only check would have looked like a fix while missing the observed case. Anyone auditing an existing palace should look for zero-norm vectors, not NaN. CUDA and DirectML are untouched, and other models were never affected. (#2283)
- **A proxied MCP session no longer loads the storage stack it never uses.** `mempalace-mcp` is spawned once per agent session, and whenever a hub is running every one of those processes is a pure proxy — `_dispatch_stdio_request` forwards each JSON-RPC request over HTTP and local storage is never touched. Importing `mempalace.mcp_server` to do that cost ~77 MB regardless, because chromadb (~61 MB by itself), numpy, pydantic, grpc and opentelemetry are all imported at module scope: a fleet of 50 agents spent ~3.9 GB holding proxies that did no work. The `mempalace-mcp` entry point is now `mempalace.mcp_proxy`, which imports only the standard library plus `config` and `server_registry`; a proxied session runs at ~17 MB measured end to end (~24 MB import weight against ~80 MB), and the full server is imported lazily the first time this process must answer a request itself. Anything that is not a plain stdio session — another transport, a server-side flag, an unrecognised argument — goes straight to the full server unchanged. The local fallback is preserved exactly, including the rule that a mutating call which failed mid-flight is never replayed locally, and it now says so: the first `tools/call` served locally carries a notice in `result.content` telling the driving agent its memory backend lost the hub and this process is holding the index. Restoring stdout on that lazy import is required, not cosmetic — importing the server performs `os.dup2(2, 1)`, so responses would otherwise be written to stderr and the session would hang. (#2312)
- **A long-running MCP server no longer grows by ~440 MB per collection open.** `ChromaBackend._client` keys its client cache on `chroma.sqlite3`'s inode and mtime to detect writes by another process, but constructing a `chromadb.PersistentClient` writes to that file itself, and so do the collection opens that follow — so the stamp taken at construction time was already stale by the time the next open compared against it. Every search opens `mempalace_drawers` and then `mempalace_closets`, and the first open moved the mtime the second one checked, so the cache missed on essentially every request: each search rebuilt the client and reloaded both HNSW segments, and the displaced client was dropped from the dict without being closed, so its native index memory was never returned. On a palace of ~165k vectors that was ~650 MB of resident growth per search; a server left running for an afternoon reached 3.9 GB. The freshness stat is now re-baselined once the backend's own operation finishes — including writes through `ChromaCollection`, so the file-a-drawer-then-search cycle stops reloading the index too — which makes the recorded value mean "`chroma.sqlite3` as this backend last left it", and a genuine external change still rebuilds. The rebuild path also closes the client it replaces. Measured over eight searches on the same palace: 951 MB → 3522 MB before, 940 MB → 965 MB after. An external write landing *while* one of our own operations is in flight is absorbed into the new stamp and picked up on the next change; mtime cannot distinguish writers, and `PRAGMA data_version` does not help because chromadb's own connection reads as foreign to a probe connection. (#2307)
- **`mempalace_mesh_peers` answers with the real estate from every transport, not just the hub.** The peer sync loop is started only by `_serve_http`, and the estate it builds (`_PEER_SYNC_STATE`, `_KNOWN_PROFILES`) lives in process memory — but the tool that reports it ships in every transport, including the stdio servers agents actually connect through. Those processes never run a sync round, so they answered from a permanently empty estate: each peer reduced to a bare `name` and `url` with no `reachable`, `last_success_at`, `remote_version_vector` or `profile`, `origin_profiles` holding only this node, and configured peers misreported as `unnamed_origins` because their `replica_id` was never learned — all while the hub next door had the complete picture. The sync loop now publishes the estate to `mesh_state.json` in the per-palace server state directory (0600, write-and-rename) after every round, and `_mesh_peers_payload` merges it underneath any in-process state, so the syncing process still trusts its own fresher reading. A new `estate_source` field says whether the status was observed in this process or published by the hub, when it was published, and whether that hub is still alive — a crashed hub leaves a last-known-good estate that is shown but never read as live. peers.json tokens never reach the file. (#2309)
- **`sweep` books a failed `stat` as a failure again.** The non-regular-file gate added in 3.7.1 reads `stat.S_ISREG(f.stat().st_mode)` inside a `try`, and its `except OSError` printed `SKIP` and moved on. A dangling symlink, a symlink loop and a file unlinked between `rglob` and the gate all raise there, and before the gate existed every one of them reached `sweep()` and was booked in `failures` — so `sweep` went from reporting a transcript it could not read to reporting success. A failed probe is now an error, not a benign file type: it is logged, printed as `WARNING`, and appended to `failures`, while a probe that succeeds and says "not regular" still skips silently. (#2221)
- **`mempalace init` no longer tracebacks on a directory it cannot enter.** `_parse_gradle`'s `is_file()` gate sat in front of the `try` that the parser's own `except OSError` provides, so a manifest under a directory with `r` but no `x` raised `PermissionError` out of a call that used to answer "no manifest name". The gate moved inside that `try`, and `_collect_manifest_names` stats through `os.path.isfile`, which reports rather than raises. (#2221)
- **`split` no longer blocks on a FIFO at its own output name, nor writes through a broken link.** The type gate in `main` covers the files the glob listed; `split_file` builds its output names itself, so a pre-existing named pipe at one of them wedged `write_text` in the kernel waiting for a reader. The check asks about the link itself rather than its target, because a dangling symlink at an output name reads as "nothing there" and `write_text` would create the target — landing a chunk outside the output directory. Output names that are anything but a regular file are now skipped with a `SKIP` line. (#2221)
- **The FTS5 auto-heal checks the content table before it rebuilds from it.** `PRAGMA quick_check`'s isolated `malformed inverted index for FTS5 table` says the inverted index and `embedding_fulltext_search_content` disagree, not which of them is wrong, and damaging the content table produces that same wording on SQLite 3.45.1, 3.47.1 and 3.51.2 alike. The heal rebuilt from that table regardless and reported "rebuilt from intact content", so a damaged content table cost the palace its lexical reach — 12 of 30 drawers stopped answering `lexical_search` for a word `embedding_metadata` still held — permanently on the `mine` path, where nothing re-files afterwards. Chroma writes every document twice, into `embedding_metadata` under `chroma:document` and into the FTS5 table at `rowid = embeddings.id`, so the shadow copy has an authority: the heal now checks it against that table, restores the rows that disagree and rebuilds, all in one transaction under the mine lock. Rows the authority cannot speak for keep their content and are named in the output, and a check that cannot conclude declines the rebuild instead of guessing. (#2278)

---

## [3.7.1] — 2026-08-12

Post-3.7.0 integrity patch: ingest no longer hangs on non-regular files, incomplete mines can be retried instead of permanently skipped, chromadb reconnect no longer rewinds the HNSW index, and MCP releases the writer lease on SIGTERM/SIGHUP.

### Bug Fixes

- **Ingest commands no longer hang on a named pipe.** `os.walk` and `glob` list a FIFO as an ordinary filename and MemPalace decides what to read from the suffix, so a pipe called `notes.md` in a mined directory wedged `mine` in the kernel forever: opening a FIFO for reading waits for a writer that never arrives, and the `S_ISREG` refusal written on the next line could never run. `mine --mode convos`, `sweep`, `init`, `compress` and `split` blocked the same way through their own readers. The four affected opens now pass `O_NONBLOCK`, which makes the existing type check reachable — a pipe is refused on its mode, with or without a live writer — and the discovery walks drop non-regular entries with a `SKIP: <name> (not a regular file)` line, so the readers that use a plain `open()` never see one. Regular files read back byte-identical; the one case where the flag is not inert, a reader breaking a write lease, re-checks the file type and retries without it rather than dropping the file. `mine --mode extract` was already immune through its zero-size gate. (#2221)
- **Project re-mine no longer silently skips a partial or interrupted file.** Four related gaps in `process_file`: (1) multi-batch upserts now stamp every drawer with `chunk_total` so `file_already_mined` can tell "N of N committed" from "crashed after batch 1"; (2) `source_mtime` comes from the same `fstat` as the content read, so an append between read and a later re-stat cannot permanently hide the new tail; (3) a failed stale-drawer purge aborts the file instead of half-overwriting; (4) closets are purged even when the re-mine ends with zero drawers. A mid-file upsert failure also deletes the partial drawers and closets for that source before re-raising, so the next mine retries instead of treating the incomplete set as complete. (#2088, #2122, #2151)
- **Conversation mine completeness matches the project path.** Convo drawers now stamp `chunk_total`; a mid-batch upsert failure deletes that source's partial drawers before re-raising; `prefetch_mined_set` omits incomplete groups so the bulk "already filed" skip cannot permanently strand missing exchanges from an interrupted transcript mine. (#2183)
- **Stale chromadb System cache is cleared on palace reconnect.** After a peer or rebuild changes `chroma.sqlite3` on disk, both `mcp_server._get_client` and `ChromaBackend._client` drop chromadb's path-keyed `SharedSystemClient` cache before reopening — otherwise the stale in-memory HNSW segment is reused and can persist an outdated index over the peer's writes (index count going backwards). (#2002, #2028, #2026, #2032)
- **MCP releases the palace writer lease on SIGTERM/SIGHUP.** The lease was only released via `atexit`, which CPython skips on those signals' default disposition. SSH disconnect (SIGHUP) and container/systemd stop (SIGTERM) therefore left `mine_palace_*.lock` naming a dead PID until a contender's liveness check reclaimed it. `main()` now installs handlers that exit through `sys.exit`, so the existing `atexit` release path runs. (#2205)

---

## [3.7.0] — 2026-08-11

### Features

- **Agent logstream coordination (RFC 003).** Append-only event layer for multi-agent work: durable task packets, wait/ack handoffs, patch and file artifacts, and live tailing over the MCP HTTP hub (`GET /logstream/stream` SSE). MCP tools include `mempalace_event_append` / `list` / `wait` / `ack`, artifact put/get, and patch submit; CLI `mempalace logstream` mirrors the core verbs. Events and artifacts stay local, verbatim, and separate from the drawer palace (`logstream.sqlite3`). Shared-brain / multi-machine agent fleets no longer need a human to relay status between hosts. (#2162, phases 1–5)

- **Logstream multi-master sync foundation (RFC 004 step 0).** Estate-level logstream replication hooks so coordinated agents can share the coordination layer across palace replicas — the storage step for a replicated shared brain. (#2162, logsync)

- **OpenAI-compatible embeddings.** Opt-in `embedding_model: "openai-compat"` talks to any `/v1/embeddings` endpoint (LM Studio, llama.cpp, vLLM, Ollama's OpenAI shim, or a self-hosted server) over stdlib `urllib` — no new dependency. Config and env vars set URL, model, and key; the model id is part of the embedder name so a model change forces a reindex. Default MiniLM/ONNX path is unchanged. (#1671, #1559)

- **Search date window.** `mempalace_search` and `mempalace search` accept `since` / `before` (`[since, before)` on `filed_at`), shared with `list_drawers` via `mempalace.date_window`. Undated drawers are excluded while a bound is active; the vector candidate pool widens under a filter and reports truncation when the pool is full. (#2000, #463)

- **Hermes memory provider (core).** In-package Hermes `MemoryProvider` files live turns through `file_conversation_exchange()` so metadata matches convo mining, with a background worker so the agent loop never blocks. Install/backfill/docs remain a stacked follow-up. (#1915, #2215)

- **RFC 002 source adapters on mine.** `mempalace mine <source> --source <adapter>` resolves registered adapters, holds the palace writer lease for the full ingest, and keeps dry-runs inert. Legacy `--mode` paths are unchanged. (#2068, #2062)

- **Hook write-routing through the daemon.** Background hook saves and mines can honor the shared write-routing policy so multi-session setups serialize mutations through one local owner. (#2030, #1963)

### Performance

- **EmbeddingGemma groups documents by size before sub-batching**, cutting padded-token work on long sweeps without changing vector meaning. Applies only to `embedding_model: embeddinggemma`. (#2104)

- **HNSW capacity probes are cached** and invalidated by palace file signature, so repeated status/taxonomy paths no longer re-scan native segments every call. (#2051, #1471)

- **`chunk_text` line numbering is O(N)**, fixing multi-second hangs on large sources. (#2054, #2055)

### Bug Fixes

- **Mining works again on the default Chroma backend.** Explicit-embedding writes no longer hand Chroma `np.float32` scalars that `normalize_embeddings` rejects. (#2187)

- **ChatGPT data exports are parsed as conversations**, not stored as raw JSON arrays. (#2160)

- **Local backends enforce process-lifetime single-writer ownership.** File-backed palaces require one writer owner for the process lifetime; read-only MCP may coexist; remote backends remain multi-process. (#2079, #2045)

- **MCP refuses writes when the served library drifts** (mempalace, and chromadb when that backend is active) after an upgrade without restart. Opt out with `MEMPALACE_MCP_ALLOW_STALE_LIBRARY`. (#2081, #899)

- **Chroma HNSW write defaults match chromadb** (`batch_size=100` / `sync_threshold=1000`), retiring the old 2/2 bloat guard. (#2107, #2106)

- **Repair and recovery are safer under contention.** Mine-lock before archive, preserve temp collections on failed swap, fail loud on truncated pagination; dry-run is a true preview in both legacy and from-sqlite modes; backups skip sockets/pipes/device nodes so a live palace socket no longer aborts repair/migrate. (#2109, #2086, #2087, #2133, #2144, #2207, #2212)

- **`rebuild_index` holds the palace writer lease** for the full snapshot→rebuild/swap cycle. (#2195)

- **Orphaned per-source mine locks are reaped** (age + nonblocking flock), throttled to once per 15 minutes; palace-level locks are untouched. (#2200)

- **HNSW divergence is preflighted** before remaining `count()` crash sites across mine, dedup, migrate, repair, and palace helpers. (#2093)

- **Re-mine and conversation ingest no longer lose or duplicate drawers.** Content-hash dedup, sweeper purge scope, round-trippable `drawer_id` on search; Claude Code `subagents/` skipped by default (`--include-subagents` to opt in). (#2050, #2125, #2090, #1330, #1217)

- **MCP and daemon lifecycle harden multi-agent use.** Read-only refuses config/checkpoint-ack host mutations; stdio exits on EOF; daemon defers lock-refused jobs; Windows stdout capture falls back or fails closed if the protocol stream cannot be restored. (#2126, #2072, #2029, #2211, #2210)

- **Encoding and Windows locale hardening.** Pin UTF-8 on config/dialect opens; mojibake repair no longer destroys clean Portuguese/Vietnamese/Turkish prose; repair-encoding CLI reconfigures stdio; non-ASCII CLI symbols replaced for GBK consoles. (#2098, #2208, #2194, #1104, #1034, #2193)

- **Small correctness fixes.** Entity-candidate ReDoS guard; reject pathological `chunk_overlap`; worktree cwd maps to the project wing; markdown emphasis is not emotion; service entrypoints restore `MEMPALACE_PALACE_PATH`; systemd `Restart=always` with idle watchdog docs; valid `docker-compose.yml` environment key. (#2127, #2056, #2206, #2199, #2192, #2203, #2204, #2188)

### Documentation

- Agent logstream concept page, coordination protocol, shared-brain fleet guide, and RFC 003/004. (#2162)
- Operator write-routing / single-writer recovery notes. (#2079)
- Remote-server idle watchdog and read-only semantics. (#2126, #2204)
- README Docker section leads with the published image and real mount/permission pitfalls. (#2196)
- MX3 public-shim example and CONTRIBUTING Discussions cleanup. (#597, #555)

### Internal

- Docker publish is gated on a real smoke script (Compose parse, mine, MCP handshake). (#2189)
- Embedding empty-batch / plain-sequence regression tests; HNSW defaults assertions. (#2191, #2159)

## [3.6.0] — 2026-07-14

### Features

- **Turnkey secure remote / team server.** `mempalace serve` exposes the full MCP surface over HTTP with secure defaults: non-loopback binds require a bearer token, generated tokens are stored with restrictive permissions and passed through the child environment rather than argv, native TLS 1.2+ is supported, and `--read-only` both hides and refuses mutating tools. Docker Compose, systemd, and environment templates are included for deployment. (#1877, #1897, #1900)

- **Milvus storage backend.** The new opt-in `milvus` backend supports embedded Milvus Lite, self-hosted Milvus, and Zilliz Cloud through `pymilvus`, with COSINE vector search, native BM25 lexical search for new collections, namespace isolation, target-mismatch protection, and an optional `milvus` dependency extra. (#1899)

- **Atomic knowledge-graph fact replacement.** `supersede()` / `mempalace_kg_supersede` closes an open fact and opens its successor at one shared instant, so model, employer, address, and other single-valued facts can change without a hand-written invalidate/add race. Temporal queries now use half-open intervals at timestamp precision, returning only the successor at the transition boundary while preserving whole-day semantics for date-only facts. (#1913)

- **Conversation chronology is preserved.** Conversation drawers now retain transcript time as `authored_at` alongside ingest time, search results expose it, and exact hybrid-score ties prefer the more recently authored drawer. An idempotent, dry-run-first backfill script adds the metadata to existing palaces without re-embedding. `mempalace_list_drawers` also accepts inclusive `since` and exclusive `before` filing-time bounds. (#1890, #1891)

- **Mined sessions populate the associative graph.** A precision-biased, no-LLM structural extractor records code symbols, URLs, paths, and qualified identifiers as drawer entities; conversation mining then derives hallways from them. The new `mempalace hallways` CLI command exposes the graph. (#1894, #1895)

- **Per-project mining exclusions.** `exclude_patterns` in `mempalace.yaml` filters pre-scanned project files without changing the corrected `--limit` semantics. (#1213, #1953)

- **LaTeX project coverage.** `.tex` and `.bib` files are now treated as readable prose sources. (#1901)

### Performance

- **MCP initialization no longer waits for palace-wide integrity work.** stdio answers the JSON-RPC `initialize` request immediately while startup preflight runs on a background thread; a tool arriving mid-probe joins the same verdict instead of launching duplicate work. The startup SQLite probe is also skipped above a configurable size limit (512 MiB by default), while repair preflights remain strict. (#1911, #1987)

- **Qdrant metadata counts use server-side facets.** Taxonomy, overview, and status paths can count metadata remotely instead of fetching and tallying every record; the embedding wrapper now forwards the facet and bulk-metadata capabilities correctly. (#1868, #1898)

- **pgvector metadata-only fetches omit document payloads**, reducing transfer and decoding work on enumeration paths. (#1892)

### Bug Fixes

- **Chroma recovery distinguishes derived-index damage from data loss.** Valid all-layer-0 HNSW segments with an empty `link_lists.bin` are no longer quarantined repeatedly; isolated FTS5 inverted-index corruption is rebuilt from intact content during repair and after mining; embedded NUL bytes are sanitized before reaching ChromaDB. (#1716, #1872, #1878, #1927, #1928)

- **SQLite recovery is bounded, atomic, and honest.** Integrity checks wait up to 15 seconds for transient writer contention instead of reporting a healthy palace as corrupt. In-place recovery archives use atomic `os.rename` rather than `shutil.move`'s destructive copy/delete fallback. `repair --mode from-sqlite` now rebuilds FTS5, vacuums, and requires a clean final `PRAGMA quick_check`; cleanup failures return non-zero with recovery details instead of printing a false success banner. (#1945, #2015, #2017)

- **CLI search checks HNSW divergence before opening ChromaDB.** A diverged palace routes directly to the existing SQLite BM25 fallback, avoiding embedder or collection initialization against damaged native index state while leaving healthy Chroma and non-Chroma backends on their normal vector path. (#2016)

- **Writer leases recover instead of stranding servers read-only.** A server refused while a peer owns the palace now retries on each mutating call and promotes itself after the peer exits. Status remains read-only and cannot acquire the writer lease, the in-process re-entrant holder set is released even across asynchronous interruption, and `checkpoint` / `delete_by_source` are correctly classified as mutations for both read-only serving and peer-writer protection. (#1923, #1930, #1934, #1960, #1970, #1971)

- **Status reports only checks that actually ran.** Non-Chroma backends now mark the Chroma SQLite integrity check as not applicable rather than claiming an unperformed success. (#1931, #1946)

- **Conversation transcripts are treated as mutable.** Appended or rewritten sessions are purged and re-filed based on modification time, preventing later turns from being silently skipped. Claude Code `tool-results/` sidecars are excluded from conversation scans so raw machine dumps cannot flood the embedding space. (#1957, #2010)

- **Explicit palace selection now scopes derived graph state.** Project, conversation, and format mining write hallways and tunnels beside the selected `--palace` instead of leaking them into the ambient default palace. (#2018)

- **Remote and local server hardening.** Non-loopback HTTP now requires a token unless an explicit insecure override is supplied; optional-provider probes cannot reuse an ambient external key before the user selects that provider; hook transcript paths and file-copy/repair paths receive stricter local validation and no-follow handling. Importing the MCP server no longer clobbers the host application's root logger, HTTP writes can re-enter the process-wide palace lock safely, and routine client disconnects no longer emit server tracebacks. (#1859, #1860, #1864, #1885, #2003, #2004)

- **Mining and configuration correctness.** Oversized files produce a visible stderr warning, `~` in configured palace paths expands consistently, wing slugs handle special characters, and Windows background daemon / synchronous hook mines use `CREATE_NO_WINDOW`. (#923, #1852, #1857, #1863, #1865)

- **`mempalace init` handles non-ASCII `.gitignore` files on Windows.** The project-file ignore guard now reads and appends UTF-8 explicitly instead of relying on locale defaults such as GBK. (#1648)

- **L1 wake-up surfaces the latest moments.** Drawers with equal importance are now ordered by `filed_at` recency rather than insertion order, so startup context prefers recent memories instead of the oldest ones. (#1630)

- **Backend detection requires a SQLite magic header** before classifying a target as Chroma or `sqlite_exact`, preventing unrelated files from being mistaken for a palace. (#1893, #1896)

### Documentation

- Added a storage-backend configuration reference, a remote/team-server deployment guide, `authored_at` migration guidance, and refreshed the OpenClaw integration for the full 36-tool MCP surface. (#1719, #1877, #1890, #1904, #1905)

### Internal

- Expanded WAL crash-safety, idempotence, and redaction-path coverage (#1869); updated GitHub Actions and Ruff; repaired `uv.lock` drift so it again matches the Ruff 0.15.20 pin and includes Python 3.9 dependency markers introduced by the Milvus resolution.

---

## [3.5.0] — 2026-06-22

### Features

- **Opt-in local daemon for queued writes.** A new `mempalace daemon` queues MemPalace writes through a single local process so background mines, diary saves, and hook-driven ingests serialize against one palace handle instead of racing for it. Opt-in and local-only — nothing binds to a public interface. (#1826)

- **Opt-in HTTP transport for the MCP server.** `mempalace-mcp --transport http` serves JSON-RPC at `POST /mcp` (with a `GET /healthz` liveness probe) for operators running MemPalace behind a long-lived HTTP MCP client/proxy, avoiding the long-lived-stdio framing failures of #1801. stdio remains the default and is unchanged. The transport reuses the exact stdio request dispatcher (no separate write/search path), binds `127.0.0.1` by default, and is hardened against the two ways a local HTTP server leaks to the network: it pins the `Host` header to loopback on a loopback bind and rejects any non-loopback browser `Origin` (DNS-rebinding/SSRF guard), and supports an optional bearer token via `MEMPALACE_MCP_HTTP_TOKEN` (required on `/mcp`, never on `/healthz`). A 16 MiB request cap and a loud warning when bound to a non-loopback host round it out. (#1801, #1806)

- **`mempalace_checkpoint` batch-save MCP tool.** Collapses multiple `add_drawer` calls plus an optional diary entry into a single MCP round-trip for agents that want to file a whole session at once. Stores content verbatim and reuses the existing idempotent add/dedup path. (#1851)

- **`mempalace_delete_by_source` bulk-cleanup MCP tool.** Exact-match, dry-run-by-default deletion of every drawer (and its matching closet/AAAK index entries) for a given `source_file` — the recourse for benchmark/eval files mined into the same wing as real data and drowning out search. The dry run reports the drawer and closet blast radius before anything is removed, and the commit writes a WAL audit entry. (#1722, #1729)

- **Optional `source_file` filter for `mempalace_search`.** Scope a search to an exact stored source path. The filter is threaded through every search path (vector, BM25/SQLite fallback, lexical union, and index-mismatch fallback) so it never silently drops a matching drawer, and results now expose the full `source_path` as a round-trippable key. (#1815, #1817)

- **New transcript parsers / importers.** Continue.dev session parser (#731), Gemini CLI / AI Studio JSON session import (#204), and a Pi agent JSONL session normalizer (#169).

- **Wider miner language coverage.** C# / .NET, PHP (#1819), Swift / Kotlin (#1368), and Java project detection including rootless subprojects (#1720).

- **Final mine on Claude plugin `SessionEnd`.** The Claude Code plugin now runs a closing mine when a session ends so the last exchanges are captured without waiting for the next save nudge. (#1814, #1820)

### Performance

- **Overview/status MCP tools answer from the SQLite aggregate.** Large palaces no longer time out building wing/room/status overviews — the counts come from a single SQLite aggregate instead of a client-side fetch-and-tally. (#1748, #1379)

- **`graph_stats` SQLite fast path.** Knowledge-graph stats are computed in SQLite rather than walking the collection, fixing large-palace timeouts. (#1379)

- **Embedder caps ONNX-runtime intra-op threads** so a background mine no longer pins every core. (#1068)

- **Backend pagination pushed into the query.** `sqlite_exact` (#1841, #1842) and `pgvector` (#1830, #1840) now apply `get(limit, offset)` in SQL, and Qdrant fetches bulk metadata in a single scroll with a larger page size (#1796, #1832).

### Bug Fixes

- **pgvector tolerates hostile transcript bytes.** A lone Unicode surrogate (#1833) or a NUL byte (#1829) in a transcript no longer aborts the whole mine — both are sanitized before the row is written.

- **SQLite read-only URIs are percent-encoded** so palace paths with spaces or special characters open correctly, and `_sqlite_graph_stats` is routed through the same `sqlite_read_uri` helper.

- **Stale ChromaDB HNSW divergence routes to the SQLite fallback** instead of failing the read outright. (#1816, #1822)

- **Diverged-index recovery now points at `repair --mode from-sqlite`, not a re-mine.** A failed ChromaDB HNSW compaction leaves the index out of sync while the rows stay intact in `chroma.sqlite3`; the old "re-mine from source" advice silently dropped MCP-added drawers and diary entries (which have no source file). Both the legacy `repair`/`rebuild_index` error messages and the `repair-status` recommendation, plus the recall skill docs, now guide users to rebuild from SQLite. (#1843, #1847, #1849)

- **The MCP server refuses a second writer for the same palace** rather than letting two processes race the same HNSW handle. (#1818, #1823)

- **Windows hook miner spawns with `CREATE_NO_WINDOW`** so background mines no longer flash a console window. (#1783, #1848)

- **`fact_checker` `__main__` no longer emits a runpy warning** under the test runner. (#1798)

### Internal

- Live-substrate conformance test module for pgvector (#1769); dependabot bumps for `docker/login-action` (3→4), `docker/build-push-action` (6→7), and `docker/metadata-action` (5→6) (#1788, #1787, #1786); ruff dev dependency bumped to 0.15.18.

---

## [3.4.1] — 2026-06-14

### Features

- **Cursor IDE plugin (`.cursor-plugin/`).** Drops into `~/.cursor/plugins/local/mempalace` (or installs from the Cursor marketplace once published) and auto-registers the `mempalace-mcp` server, five slash commands (`/mempalace-help`, `/mempalace-init`, `/mempalace-mine`, `/mempalace-search`, `/mempalace-status`), and the model-invocable [`mempalace` skill](skills/mempalace/SKILL.md) — no manual `~/.cursor/mcp.json` edit required. The plugin manifest deliberately omits a hardcoded `version` field — `mempalace/version.py` is the single source of truth, so there is nothing to drift on the next release (a contract test enforces the field stays absent). The canonical plugin components (`commands/`, `skills/`, `mcp.json`) are real files at the plugin root; no symlinks are committed (committed symlinks materialise as broken text files on Windows clones with `core.symlinks=false`). Mirrors the surface of [`.claude-plugin/`](.claude-plugin/) and [`.codex-plugin/`](.codex-plugin/) without duplicating their hook scripts: the Cursor hook scripts under [`hooks/cursor/`](hooks/cursor/) (shipped in the same release) remain the canonical install path for `stop` / `preCompact` / `sessionStart`, wired separately by [`hooks/cursor/install.sh`](hooks/cursor/install.sh). Contract tests in [`tests/test_cursor_plugin_manifest.py`](tests/test_cursor_plugin_manifest.py) cover manifest JSON validity, kebab-case naming, `..`-free relative paths, on-disk path resolution, marketplace alignment, MCP config shape (`mcpServers` wrapper required by Cursor, unlike Claude's flat `.mcp.json`), the version-field-absent guard, the no-symlink guard, and every skill/command frontmatter — all pure file inspection so they run on any CI platform without Cursor itself.

- **Cursor IDE hook support (`stop` / `preCompact` / `sessionStart`).** Three new bash hooks live under [`hooks/cursor/`](hooks/cursor/) and share a `lib/common.sh` helpers module. The save hook counts `stop` invocations per Cursor `conversation_id` and emits a `followup_message` every `MEMPAL_SAVE_INTERVAL` (default 15) so the agent files the session into MemPalace and writes a diary entry. Unlike the silent-by-default Claude Code hook, the Cursor followup fires **on by default**: Cursor's transcript format is undocumented and `normalize.py` has no Cursor parser yet, so the background `mempalace mine --mode convos` is best-effort only and the `followup_message` is the load-bearing verbatim-capture path. Users who want the Claude-style "zero tokens in the chat window" behaviour can suppress it with `MEMPAL_CURSOR_SILENT=1` (or `MEMPAL_VERBOSE=false`); the default flips to silent once a Cursor transcript parser lands. The precompact hook synchronously mines the transcript before Cursor's compaction summarises it and drops a marker so the next `stop` forces a save nudge (Cursor's `preCompact` is observational-only — it cannot block or emit a `followup_message`, unlike Claude Code's `PreCompact`); the synchronous mine is bounded by Cursor's per-hook timeout, and because `mempalace mine` is incremental/append-only a killed mine resumes cleanly on the next run rather than corrupting the palace. The wake hook is Cursor-only: `sessionStart` returns `additional_context` telling the agent to recall scoped to the wing inferred from the workspace root. Honours the same `MEMPALACE_HOOKS_AUTO_SAVE=false` kill switch as the Claude Code hooks, plus a new `MEMPAL_DISABLE_HOOK=1` alias and a `MEMPAL_STATE_DIR` env override. Per-conversation state files are garbage-collected by a daily-throttled, Cursor-namespaced TTL sweep (`MEMPAL_STATE_TTL_DAYS`, default 30) so `cursor_*.count` / `cursor_*.pending` cannot grow unbounded — shared logs and other editors' state are never touched. Includes an opt-in installer at [`hooks/cursor/install.sh`](hooks/cursor/install.sh) with `--scope user|project`, `--variant full|minimal`, `--dry-run`, and `--uninstall` (idempotent, preserves unrelated hooks via `python3`-based JSON merge — no `jq` dependency). Example wirings live at [`examples/cursor/hooks.json`](examples/cursor/hooks.json) and [`examples/cursor/hooks.minimal.json`](examples/cursor/hooks.minimal.json); they are intentionally not placed at the repo root because Cursor auto-loads project hooks from any trusted workspace and we do not arm hooks on contributor checkout. Per-event stdin/stdout schema documented at [`hooks/cursor/STDIN_SHAPE.md`](hooks/cursor/STDIN_SHAPE.md). Walkthrough at [`website/guide/cursor-hooks.md`](website/guide/cursor-hooks.md). Coverage added in [`tests/test_cursor_hooks_shell.py`](tests/test_cursor_hooks_shell.py) and [`tests/test_cursor_hooks_install.py`](tests/test_cursor_hooks_install.py).

- **First-class Antigravity IDE support.** New `.antigravity-plugin/` package + idempotent installer at `hooks/antigravity/install.sh` that registers MemPalace as a Google Antigravity plugin (MCP server, skill, two lifecycle hooks) at `~/.gemini/config/plugins/mempalace/`. The Stop hook background-mines the active conversation transcript every Nth fire (default 15, configurable via `MEMPAL_SAVE_INTERVAL`); the PreInvocation hook injects verbatim memory on the first model call only via Antigravity's `injectSteps[].ephemeralMessage` output, gated by `invocationNum == 1`. Both hooks are bash 3.2.57 compatible (macOS default), use the same `~/.mempalace/hook_state/` directory as the Claude Code / Codex / Cursor hooks (`antigravity_*`-namespaced state files), and respect every existing kill switch (`MEMPAL_DISABLE_HOOK`, `MEMPALACE_HOOKS_AUTO_SAVE`, `~/.mempalace/config.json` `hooks.auto_save`). Installer is `cmp`-gated (re-run produces a byte-identical install), uninstall is basename-guarded (refuses to wipe a directory whose basename isn't `mempalace`), and `--dry-run` is side-effect free. Full audit of which Antigravity surfaces we ship and which we deliberately don't is in [`hooks/antigravity/INVESTIGATION.md`](hooks/antigravity/INVESTIGATION.md). User-facing guide: [`website/guide/antigravity.md`](website/guide/antigravity.md). Standalone examples in [`examples/antigravity/`](examples/antigravity/).
  - **Zero-config interpreter resolution.** `mempal_resolve_python` now derives the Python interpreter from the `mempalace-mcp` / `mempalace` console-script shebang on `$PATH` before falling back to `python3`. The common `uv tool install mempalace` / `pipx install` layout installs the console scripts into an isolated environment whose interpreter is **not** system `python3`, so the previous `command -v python3` resolution landed on a Python that couldn't import `mempalace`, the `-m mempalace` probe failed, and mining silently never fired. Resolution is pure shebang parsing + `stat` (no Python subprocess at source time, preserving the hook performance budget). `MEMPAL_PYTHON` remains the explicit override. Documented under *How the hooks find your `mempalace` install* in the guide.

### Bug Fixes

- **`embeddinggemma` no longer OOM-kills bulk re-embeds.** `EmbeddinggemmaONNX.__call__` ran a single `session.run` over its entire input, so a repair-scale batch (5000 docs) allocated attention buffers far beyond available RAM and the kernel killed the process silently: `mempalace repair --yes` on an `embedding_model: embeddinggemma` palace died right after `Building temporary collection:` with no traceback and no crash report (#1770). Embedding now runs in sub-batches of 32 docs (constructor-tunable `batch_size`), matching the internal batching of ChromaDB's bundled MiniLM embedder. Per-document vectors are unchanged: the model's pooled output is attention-masked, so sub-batch padding does not affect values. The embedder is also hardened for shared use: the process-wide EF cache and the lazy model load are thread-safe (concurrent first calls build exactly one ONNX session), and `__call__` handles a bare string, `None`, and empty input without triggering the model download.
- **Backup retention to prevent unbounded disk usage.** `mempalace migrate` (full-palace `<palace>.pre-migrate.<timestamp>` copies) and `mempalace repair max-seq-id` (`chroma.sqlite3.max-seq-id-backup-<timestamp>` copies) each wrote a fresh, full-size, timestamped backup every run and never deleted the old ones. On a machine that mines or repairs on a schedule, those copies could silently accumulate until they filled the disk — one palace was found with hundreds of GB of stale backups beside a few hundred MB of live data, hidden from a normal `du` of the home directory. A new `max_backups` setting (default `10`, env `MEMPALACE_MAX_BACKUPS`, or `config.json`) now prunes the oldest backups after each new one is written. Set it to `0` to keep every backup. Pruning is keyed by filesystem mtime, scoped strictly to each backup's own naming pattern (live data is never touched), and best-effort so a deletion failure can never abort a migration or repair that already succeeded.

---

## [3.3.6] — 2026-05-24

### Features

- **Office-document mining via `--mode extract`.** New `mempalace mine <dir> --mode extract` ingests PDFs, Word (`.docx`), PowerPoint (`.pptx`), Excel (`.xlsx`), RTF, and EPUB books in addition to the existing source-code/text path. Install with `pip install mempalace[extract]` — pulls `striprtf` for RTF and MarkItDown (with `[docx,pdf,pptx,xlsx]` sub-extras) for the binary formats. Python 3.9 users get RTF coverage only because MarkItDown requires 3.10+. Drawers from the extract path carry `extract_mode` metadata so the convo miner's "already mined?" check and drawer-id generation stay isolated per mode (#1528). (#1555)

- **Virtual line numbers + surgical closet pointers.** Stored drawers now carry virtual line numbers so the read CLI verb and closet pointers can cite exact line ranges. Closet pointers (Tier 6a) include date+line-range information derived from filename and content-body date parsing (`python-dateutil` is now a core dep), so MemPalace can point you to exact lines on exact dates rather than just "somewhere in this drawer." (#1555, #1584)

- **Within-wing hallways.** When two entities (people, projects, topics) co-occur in the same drawer, the miner now records a "hallway" — a graph edge connecting them inside that wing. Computed automatically as part of the post-mine step in `compute_hallways_for_wing` so the graph grows incrementally with new content, no separate command. Foundation for cross-room entity navigation inside one palace. (#1558, #1560)

- **Cross-wing tunnels promoted from hallways.** When the same entity appears in hallways across multiple wings, MemPalace now automatically promotes that into a tunnel — letting queries hop from one person's wing to a project wing they appear in, without anyone calling `create_tunnel` manually. Topic tunnels from the existing `compute_topic_tunnels` path remain unchanged. (#1565)

- **Living-memory dynamics (Hebbian potentiation + Ebbinghaus decay).** Hallways and tunnels get stronger every time the same connection is reinforced by new content ("what fires together wires together") and fade gradually if a connection stops appearing in incoming drawers. Navigation weights track real palace usage instead of being static, so retrieval ranking improves over time as the palace is actually used. (#1578)

- **API-tool transcripts auto-route to `wing_api`.** Conversation transcripts from API-style AI tools (Claude Code, Claude.ai, ChatGPT, Slack-bot exports, generic OpenAI-shape JSON, etc.) now route into a dedicated `wing_api` instead of mixing into the human-conversation wings. Keeps tool-call traffic from polluting personal/project wings and improves search precision when you're looking for "what did *I* say" vs. "what did the agent say." (#1236)

- **Multilingual embedding by default for new installs: `embeddinggemma-300m` ONNX (q8, MRL→384-dim).** MemPalace's previous embedder (`all-MiniLM-L6-v2`) is trained English-only — cross-lingual cosine similarity on parallel-translated text averages 0.35 across DE/FR/HI/IT/KO/RU (RU at 0.17, near-orthogonal). A Russian-speaking user effectively cannot find their own memories, which breaks the "100% recall" design promise from CLAUDE.md. New `EmbeddinggemmaONNX` class in [`mempalace/embedding.py`](mempalace/embedding.py) brings this to 0.88 average (validated lossless vs the Ollama gguf via direct ONNX-runtime test). Lazy-downloads `onnx-community/embeddinggemma-300m-ONNX` (~300 MB) on first use via `huggingface_hub`. Output is truncated to 384 dims via Matryoshka Representation Learning so the model is a drop-in for ChromaDB's 384-dim collections — no schema change. Sim prefix (`"task: sentence similarity | query: "`) is applied automatically.

  Onboarding (`python -m mempalace.onboarding`) now offers the multilingual model as the default — choosing it writes `embedding_model: embeddinggemma` to `config.json` so subsequent runs pick it up without re-prompting. Existing installs that never set the env var or ran onboarding stay on `minilm` (back-compat). `MEMPALACE_EMBEDDING_MODEL=minilm|embeddinggemma` overrides both. Switching models on an existing palace requires re-embedding — run `mempalace repair rebuild-index` after the change. (#1483)

- **Multilingual deps moved to core.** `huggingface_hub`, `tokenizers`, and `numpy` are now required deps so the multilingual path works out of the box after `pip install mempalace`. The `[multilingual]` extra is kept as a no-op alias for back-compat with install scripts. The 300 MB ONNX model itself is still lazy-downloaded on first use, not at install time.

- **Friendlier ChromaDB EF-name-mismatch error.** Switching `MEMPALACE_EMBEDDING_MODEL` on an existing palace without running `rebuild-index` previously surfaced ChromaDB's bare `Embedding function conflict: new: X vs persisted: Y` `ValueError` — accurate but didn't tell users how to recover. `ChromaBackend.get_collection()` now wraps that error and points at both options: revert the env var, or run `mempalace repair rebuild-index --palace <path>`. (#1483)

- **`hooks.auto_save` toggle for silent-mode sessions.** New config knob (and `--silent` CLI flag wiring through the save hook) lets users opt out of automatic diary saves on `Stop` / `PreCompact`. Useful for "silent mode" sessions where you don't want every conversation captured. Default behavior is unchanged — auto-save still runs unless explicitly disabled. (#711)

- **Filter common English content words from entity detection.** High-frequency English content words ("system", "user", "memory", "project", "context", etc.) were getting tagged as entities by the per-drawer detector and polluting the entity registry as "people." A shipped COCA wordlist (top-N content words) is now consulted during entity classification so these get filtered before they reach the registry. Hardened against malformed JSON in the bundled wordlist. (#1605)

- **Case-insensitive entity matching at mine time.** The initial palace build (`mempalace init`) matched entity names case-insensitively, but the per-drawer tagger used during incremental mining did not — so the same person was tagged differently between init and ingest ("Aya" vs. "aya" became distinct entities). The incremental tagger now mirrors the case-insensitive matcher, restoring entity-tag consistency across the palace lifecycle. (#1557)

### Bug Fixes

- **Silent data loss in three upsert paths.** Three upsert sites (file ingest, conversation ingest, and one repair branch) were calling the embedder on unchunked content, silently truncating at the embedder's max-token limit. Long drawers landed with only their leading section indexed, breaking the "100% recall" promise on long-form content. All three now route through the chunker first so the full document is embedded and stored. (#1540, follow-up to #1539)

- **Paragraph chunker emitted oversized chunks for long paragraphs.** The paragraph splitter assumed paragraphs were always shorter than `CHUNK_SIZE` and emitted them whole; long paragraphs (legal text, dense technical writeups) silently exceeded the embedder's context window. The splitter now hard-caps each emitted chunk to honor `CHUNK_SIZE`. (#1538, fixes #1534)

- **Per-file chunk cap was hardcoded and too low for large transcripts.** A safety limit capped chunks per file at a value tuned for source code; mining very large conversation transcripts silently dropped the tail past that cap. Now configurable, with the default raised to cover realistic transcript sizes. (#1554, fixes #1455)

- **Hook subprocess / ChromaDB deadlock on Windows.** Stop/PreCompact hooks could deadlock against an already-open ChromaDB client on Windows, leaving the host (Claude Code) stuck waiting on the hook. Three-part fix: stale-PID timeout on mine-lock reclamation, idle-exit path in the MCP server, and structured errors when the deadlock pattern is detected so the host can recover. (#1562, fixes #1552)

- **`create_tunnel` corrupted hyphenated wing names.** The endpoint parser split on `-`, so wings whose name contained a hyphen (`mem-palace`, `my-app`) were truncated mid-name and the tunnel pointed at a non-existent endpoint. Endpoint parsing now preserves the full slug. (#1529, fixes #1504)

- **MCP knowledge-graph cache produced duplicate graphs for symlinked / differently-cased palace paths.** Cache key was the raw path string, so `/Users/me/.mempalace/palace` and `/Users/me/.mempalace/Palace` (case-folded on macOS) or a symlinked alias produced two separate cached `KnowledgeGraph` instances pointing at the same SQLite file, with stale-read risk. Cache now normalizes via `realpath` + `normcase` so they collapse onto a single canonical key. (#1383, fixes #1372)

- **Save-hook truncated hyphenated project folder names.** Wing-name parser was splitting on `-` and keeping only the first segment, so `mem-palace` became `mem`. Fix preserves the full project-folder slug so hyphenated palaces stay coherent across hook invocations. (#1424, fixes #1410)

- **Miner silently skipped symlinks.** Users were confused about missing data after mining; the miner was skipping symlinks without surfacing it. Now logs each skipped symlink with the reason so the gap is visible. (#1466, fixes #1462)

- **Host-leaked `PYTHONPATH` could shadow MemPalace's own modules at import.** Package `__init__` now strips leaked entries on import so the imported MemPalace is always the installed one. (#1439, fixes #1423)

- **macOS stock-bash hook scripts.** Hook scripts used `mapfile` (bash 4+), breaking macOS's stock `/bin/bash` 3.2. Switched to a sed pipeline so hooks work out of the box on every Mac. (#1441, fixes #1440)

- **Plugin Stop/PreCompact hooks could hang indefinitely on a stuck child.** Bounded timeout ensures the host can always make forward progress even if MemPalace's child process is unhealthy. (#1470, fixes #1465)

- **MCP handlers now return structured JSON-RPC errors for malformed input.** Unknown parameter names returned `-32602 Invalid params` instead of an unstructured Python `TypeError`; parameters of the wrong shape return a structured error instead of a raw traceback. (#1500, #1513)

- **CLI distinguished "palace doesn't exist" from "palace exists but is empty".** Two states that look the same to a new user are now reported separately with actionable next-step messages for each. (#1532, fixes #1498)

- **`mempalace repair` post-pass: VACUUM + FTS5 rebuild.** After a palace repair, the SQLite knowledge-graph file kept fragmented pages and a stale FTS5 index; running `VACUUM` and rebuilding FTS5 at the end reclaims disk and restores search performance. (#1523)

- **Convo miner mode isolation.** The "already mined?" check and drawer-id generation ignored `extract_mode`, so switching modes either re-mined the same content (data dup) or collided drawer IDs across modes. Scoping by mode keeps each mode's drawers isolated and dedup-correct. (#1528, fixes #1505)

- **FTS5 validation at end of mine.** Mining now validates the FTS5 index integrity as a post-step so corruption is caught at write time, not at read time. (#1548, fixes #1537)

- **`hooks_cli` crashed on shallow install paths.** Code indexed `Path.parents[3]` assuming a deep install tree, raising `IndexError` when MemPalace ran from a shallow path (e.g. `/opt/mp`). Adds a guard so shallow installs no longer crash on hook commands. (#1585)

- **HNSW segment quarantine: zero-byte vs missing-dim.** Earlier quarantine heuristic flagged any HNSW segment missing the `dim` metadata field as corrupt, but most were recoverable; the check now distinguishes recoverable-missing-dim from actually-corrupt, preserving working index segments. Zero-byte link-list files (partially-written segments) are now rejected outright. (#1452, #1461, fixes #1457)

- **Mine-lock holder file written as UTF-8 instead of cp1252.** Non-ASCII Windows usernames and paths no longer corrupt the lock file and break stale-lock detection. (#1438)

- **Miner slot claim now writes a placeholder PID immediately.** Crash between claim and PID-write no longer leaves a phantom lock. (#1543, fixes #1443)

- **`mine_convos` now runs inside `mine_palace_lock`.** Two concurrent `convos mine` invocations can no longer corrupt the index. (#1477)

- **Migration tool cleanup.** ChromaDB-version migration tool now closes its SQLite connection and removes the temp palace directory if an exception fires mid-migration; failed migrations stop leaking file handles and disk. Entity-registry atomic-write now deletes its `.tmp` sidecar if the write or rename fails. (#1216, #1408, fixes #1373)

- **Repair tool tolerated empty/None metadata cells.** ChromaDB occasionally returns cells with empty metadata dicts or `None` during rebuild; both are now coerced to sensible defaults so the rebuild completes and otherwise-stuck palaces recover. (#1459, #1445, fixes #1426)

- **`create_tunnel` MCP handler now propagates errors.** Bad endpoint or direction was being swallowed and returned as misleading success; now propagates as a structured MCP error. (#1546, fixes #1473)

- **Explicit tunnels were stored at a hardcoded ``~/.mempalace/tunnels.json`` path that ignored ``MempalaceConfig.palace_path``.** Drawers, KG triples, the people map, and every other piece of palace state honour the configured ``palace_path`` (and the ``MEMPALACE_PALACE_PATH`` env var), but ``palace_graph._TUNNEL_FILE`` was a module-level constant initialised once from ``os.path.expanduser("~") + "/.mempalace/tunnels.json"``. Under any setup where ``$HOME`` is isolated from the configured palace — subagent profiles with their own ``$HOME``, sandboxes, multi-tenant hosts, container mounts that move the palace to ``/srv/`` — drawers landed in the configured palace while tunnels silently landed in a different file that no other process touching the same palace could see. Worst case is the agentic one: an isolated worker calls ``create_tunnel`` then ``list_tunnels`` and gets back its own write from the bubble, so the worker self-confirms a tunnel that doesn't exist in the shared palace and reports completion to the orchestrator. ``palace_graph._TUNNEL_FILE`` is replaced by ``_get_tunnel_file()`` which derives the path from a new ``MempalaceConfig.tunnel_file`` property (sibling of ``palace_path``). The default single-user install is unchanged because the default ``palace_path`` is still ``~/.mempalace/palace`` and its sibling ``tunnels.json`` is the legacy path. Backwards-compatibility: if the configured tunnel file does not exist but a file is present at the pre-3.3.6 hardcoded ``~/.mempalace/tunnels.json`` path AND the two paths differ, ``_load_tunnels`` logs a one-line ``WARNING`` naming both paths and returns an empty list — we intentionally do NOT auto-migrate because silently merging tunnel state across two locations risks clobbering newer data; the user moves or copies the file themselves. (#1467)

- **``create_tunnel`` did not validate that the source and target rooms actually exist in the chroma index.** ``_require_name`` only checked that wing/room names were non-empty strings; nothing queried the collection to confirm at least one drawer carried matching ``{wing, room}`` metadata. Pointing an explicit tunnel at a phantom room — common when an agent fabricates a room name it expects to exist, or types a slug wrong — silently succeeded. Combined with the read-bubble described in the previous fix, an agent could ``create_tunnel`` → ``list_tunnels`` and have both calls return its own bogus write. ``create_tunnel`` now calls ``_check_room_exists(wing, room, col)`` for both endpoints before persisting an explicit tunnel; if either endpoint has zero matching drawers the call raises ``ValueError`` naming the offending wing/room pair. Three deliberate carve-outs: (1) ``kind != "explicit"`` skips validation because topic tunnels generated by ``compute_topic_tunnels`` use synthetic ``topic:<name>`` room identifiers that don't correspond to real chroma rooms; (2) ``_get_collection`` returning ``None`` (palace not yet created, transient backend failure, tests without a real chroma backend) skips validation rather than fail-closed — matches the tolerance pattern used everywhere else in ``palace_graph``; (3) exceptions raised by the underlying ``col.get(where=..., limit=1, include=[])`` query are logged and treated as "can't verify, allow" so a temporary index fault never blocks legitimate writes. **Behaviour change:** existing callers that previously created tunnels pointing at empty rooms (e.g. as scaffolding before mining them) will now raise ``ValueError``. File the drawer first, then create the tunnel — this is the order the documentation has always recommended. (#1468)

### Performance

- **Convo miner pre-fetches mined-set once.** Was issuing one `WHERE` query per file to check "already mined?"; now pre-fetches the full mined set once, slashing wall time on large transcript corpuses. (#1474)

- **`rebuild_index` progress callback.** Multi-hour rebuilds now report progress with default ETA printer; users no longer have to guess whether the process is making progress. (#1487)

- **MCP cold-start diagnostics + opt-in warmup.** Adds visibility into which embedder is loading and how long it takes, plus an opt-in warmup path so users can see and address slow first-query latency. (#1530, fixes #1495)

### Internal

- ``palace_graph._TUNNEL_FILE`` (module-level constant) replaced by ``_get_tunnel_file(config=None)`` and ``_legacy_tunnel_file()``. Tests previously monkeypatching the constant must now monkeypatch the resolver functions. The ``tests/test_palace_graph_tunnels.py::_use_tmp_tunnel_file`` helper, ``tests/test_closets.py::TestTunnels`` setup/teardown, and three tests in ``tests/test_miner.py`` were updated accordingly. Topic-tunnel tests in ``test_miner`` continue to work without stubbing ``_get_collection`` because ``kind="topic"`` short-circuits the new validation path.

---

## [3.3.5] — 2026-05-09

### Bug Fixes

- **MCP `tool_search` now retries once on transient `Error finding id` from chromadb's HNSW flush window.** After a bulk CLI mine, ChromaDB's HNSW segment metadata can be unflushed for ~30-60s; wing-scoped MCP search hits `Internal error: Error finding id` during that window. `tool_search` now detects this transient via response-shape sniffing, drops both the MCP-local client cache and `_DEFAULT_BACKEND._clients` / `_freshness` for the palace, sleeps 2s, and retries once. Successful retries are tagged with `index_recovered: true` so callers can observe when it fired; non-transient errors bypass the retry path entirely. Partial fix for the broader #1315 cluster — `tool_check_duplicate` and other index-touching tools still need the same wrapper. (#1396, refs #1082, #1315)
- **`mempalace_diary_read` silently dropped entries on agent-name case mismatch.** `tool_diary_write` stored the `agent` metadata verbatim after `sanitize_name`, which preserves case, while `tool_diary_read` filtered by exact match. Writing as `"Claude"` and reading as `"claude"` (or vice-versa) returned zero rows. Both endpoints now lowercase `agent_name` immediately after sanitization, so reads are case-insensitive and the default per-agent wing slug is stable across casings. **Behavior change:** entries written prior to this fix under mixed-case agent names will not match the new lowercase filter; run `mempalace repair` if you need to migrate legacy diary metadata. (#1243)
- **Knowledge-graph triples with `valid_to < valid_from` were silently invisible.** `KnowledgeGraph.query_entity()` filters with `valid_from <= as_of AND valid_to >= as_of`, so an inverted interval matches no `as_of` and the row is durably stored but unreachable — a P0 data-integrity foot-gun any caller that mixes up the two date params can hit. `add_triple()` now rejects inverted intervals at write time with a clear `ValueError` naming both bounds. Open intervals (one bound only) and point-in-time facts (`valid_from == valid_to`) remain accepted unchanged. (#1214)
- **`ChromaBackend.close_palace()` / `close()` did not release the SQLite file lock.** Evicted clients sat in `_clients` without `close()`, and chromadb 1.5.x retains the rust-side SQLite lock until GC. Reopening the same palace path after `shutil.rmtree` + recreate within one process failed with `SQLITE_READONLY_DBMOVED` (code 1032). New `_close_client()` helper now calls `PersistentClient.close()` (with a try/except fallback for older chromadb) on `close_palace()`, on whole-backend `close()`, and on the `_client()` invalidation path that detects a missing `chroma.sqlite3`. The mtime/inode auto-invalidation branch is intentionally left alone — callers there may still hold a live `ChromaCollection`. (#1067, #1105)
- **`EntityRegistry.save()` could leave a corrupt or empty `entity_registry.json` on crash.** `Path.write_text()` is not atomic — kernel sees `open('w')` (truncate), `write`, `close`, and any failure between truncate and full-flush (power loss, OOM, FS-full, kill -9) wipes the months-of-mining people/projects map silently (the registry's `load()` swallows `JSONDecodeError`). Save now writes to a sibling `.tmp` in the same directory, `fsync`s, `chmod 0o600`s, then `os.replace()`s into place — atomic on POSIX and Windows. The previous registry stays intact on any crash before the rename returns. (#1215)
- **`miner.detect_room` bidirectional substring matching caused systemic misrouting.** The priority-1 (path parts) and priority-2 (filename) checks used `c in part or part in c` against room names + keywords, so any token that was an unbounded substring of a room name (or vice versa) matched. Priority-1 iterates left-to-right and returns on first match, so `views/billing-page/src/Foo.test.tsx` routed to an `interviews` room because `"views" in "interviews"` matched before reaching `billing-page`. Both call sites now use a `_name_matches` helper that compares names as equal or as separator-bounded tokens of each other (split on `-`, `_`, `.`, `/`). (#1004, closes #1002)
- **`mempalace compress` crashed on large palaces.** `regenerate_closets` fetched all closet_llm drawers in a single `col.get()`, which trips `SQLITE_MAX_VARIABLE_NUMBER` on palaces above ~32k drawers. Mirrors the #851 fix in `miner.py`: drawer fetch is now paginated at `batch_size=5000`. Per-source aggregation works across batches, so the LLM regeneration call still groups chunks correctly. (#1073, #1107)
- **CLI and `fact_checker --stdin` mojibaked non-ASCII content on Windows.** Python defaults `sys.stdin`/`stdout`/`stderr` to the system ANSI codepage (cp1252/cp1251/cp950), so `mempalace search > out.txt` and piped fact_checker invocations corrupted Cyrillic / CJK drawer text at the process boundary. New `mempalace/_stdio.py` helper reconfigures all three streams to UTF-8 on `sys.platform == "win32"`, with per-stream `errors` policy: `surrogateescape` on stdin (preserves bad bytes from redirected files for the consumer's parser), `replace` on stdout/stderr (substitutes U+FFFD instead of `UnicodeEncodeError`-ing mid-print). With this, all three user-facing console_scripts (`mcp_server`, `hooks_cli`, `cli`/`fact_checker`) now reconfigure identically on Windows. (#1282)
- **MCP knowledge-graph tools forwarded malformed date strings to SQLite.** `tool_kg_query` (`as_of`), `tool_kg_add` (`valid_from`), and `tool_kg_invalidate` (`ended`) accepted any string and produced empty result sets on natural-language inputs like `"March 2026"` or `"yesterday"` — callers (especially LLM agents) could not distinguish "no fact at this time" from "your date format was unrecognized." New `sanitize_iso_temporal()` validator in `config.py` (with `sanitize_iso_date()` retained as a backwards-compat wrapper) accepts `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`, and `YYYY-MM-DDTHH:MM:SS+00:00` (normalized to the `Z` form), and passes `None`/`""` through unchanged; all three KG tools call it before values reach the storage layer. Partial dates (`YYYY`, `YYYY-MM`), naive datetimes, and non-UTC timezone offsets are rejected because KG queries compare TEXT temporal values where mixed formats silently return wrong results. **Behavior change:** previously-silent date typos now raise a clear `ValueError` naming the offending field; partial-date inputs that worked in 3.3.4 (`"2026"`, `"2026-05"`) no longer parse — pass a full `YYYY-MM-DD` or a canonical UTC datetime instead. (#1164, #1167, #1374, #1417)
- **MCP server's `_kg` was a module-level singleton.** Multi-tenant hosts that rotate `MEMPALACE_PALACE_PATH` between tool calls hit the wrong sqlite file, because the KG was constructed once at import time while the ChromaDB side was already per-call via `_get_client()`. The KG is now resolved per-call through a lazy per-path cache (`_kg_by_path` keyed by `os.path.abspath`, with a double-checked-locking init under `_kg_cache_lock`). `tool_reconnect` drains and `close()`s cached KGs alongside the existing chroma reconnect. A `_call_kg` retry guard catches `sqlite3.ProgrammingError` once after a reconnect race. (#1136, #1160)
- **`mempalace repair` can now recover palaces whose HNSW segment writer is stuck on `apply_logs`.** Both the existing `--mode legacy` rebuild and the inline `cli.cmd_repair` path call `Collection.count()` as their first read — exactly the call that raises `chromadb.errors.InternalError: Failed to apply logs to the hnsw segment writer` on the corruption class introduced upstream and reported in #1308. Repair would print `Cannot recover — palace may need to be re-mined from source files` even though the underlying SQLite tables were fully intact (the corruption lives in the on-disk index files, not the data layer). New `--mode from-sqlite` reads `(id, document, metadata)` rows directly from `chroma.sqlite3` via a `segments` → `embeddings` → `embedding_metadata` join, never opens a chromadb client against the corrupt palace, and re-upserts everything into a fresh palace at `--palace`. `--source PATH` extracts from a corrupt palace already moved aside; `--archive-existing` handles the in-place case by renaming the existing palace to `<palace>.pre-rebuild-<timestamp>` before reading from it. Documents are re-embedded under the user's configured embedding function (the original HNSW vectors live in the corrupt `data_level0.bin` and cannot be recovered, but the embedding model is deterministic so search results remain semantically equivalent). Verified end-to-end on a 52,300-row real-world corrupt palace. (#1308)

### Documentation

- **`CONTRIBUTING.md` git-identity guidance.** New section asks contributors to verify `git config user.name` and `git config user.email` before pushing, with an explicit warning for agentic coding tools that may not inherit the user's normal Git config. Avoids placeholder/template author values in commit history. (#1385, closes #1317)

### Internal

- **Test reliability: `multiprocessing` start method.** `tests/test_palace_locks.py` and `tests/test_chroma_collection_lock.py` switched from `fork` to `spawn` for child processes. Under Python 3.13 the pytest parent is multi-threaded by the time these tests run (chromadb + onnxruntime each spawn background threads on import); `fork` snapshotting that state into the child without the threads themselves deadlocked Linux 3.13 and macOS CI jobs indefinitely while Linux 3.9 / 3.11 / Windows finished normally. macOS additionally forbids fork-without-exec via CoreFoundation. `spawn` re-imports modules in the child (~0.5s per Process — bounded by the 10 subprocesses these tests fork) but is safe under threading. (#1431)
- **Test cleanup: SQLite connection lifecycle.** Wrapped naked `conn = sqlite3.connect(...)` blocks in `tests/test_backends.py`, `tests/test_sources.py`, and `tests/test_repair.py` with `contextlib.closing(...)`. The flat `conn.close()` pattern at the end of each test leaked the connection on any exception or assertion failure between connect and close, producing `ResourceWarning: unclosed database` noise in CI logs and creating a secondary risk of advisory-lock starvation on Python 3.13 / macOS. Mirrors the `try/finally` pattern already used in production code. (#1430)

---

## [3.3.4] — 2026-04-30

### Added

- **`mempalace init` now prompts to mine the same directory.** After entity confirmation, room detection, and gitignore guard, `init` shows a one-line scope estimate (e.g. `~423 files (~12 MB) would be mined into this palace.`) computed from its existing corpus walk, then asks `Mine this directory now? [Y/n]` (default yes) and runs `mine()` in-process if accepted. The estimate fires before the prompt so users on a real corpus aren't surprised by a minutes-long ChromaDB write. Declining prints the exact `mempalace mine <dir>` command for later. (#1181)
- **New `--auto-mine` flag on `mempalace init`** for the non-interactive path (`mempalace init --auto-mine <dir>` skips the mine prompt and runs mine directly). `--yes` retains its existing scope of entity auto-accept only and still prompts for the mine step, so existing scripted callers see no behaviour change; combining `--yes --auto-mine` gives a fully non-interactive setup. (#1181)
- **Cross-wing topic tunnels.** When two wings have confirmed `TOPIC` labels in common (the LLM-refine bucket from `mempalace init --llm`), the miner now drops a symmetric tunnel between them at mine time so the palace graph reflects shared themes (frameworks, vendors, recurring concepts). Tunnels are routed through the existing `create_tunnel` storage so they share dedup and persistence with explicit tunnels. Topic tunnels are stored under a synthetic `topic:<name>` room and tagged with `kind: "topic"` on the stored dict — this keeps them distinct from literal folder-derived rooms of the same name (a wing with both an `Angular` folder room and an `Angular` topic tunnel no longer collides at `follow_tunnels` read time) and gives LLMs scanning `list_tunnels` a visible discriminator. Threshold is configurable via `MEMPALACE_TOPIC_TUNNEL_MIN_COUNT` env var or `topic_tunnel_min_count` in `~/.mempalace/config.json` (default `1`). Manifest-dependency overlap and per-topic allow/deny lists remain out of scope. (#1180)
- **Context-aware corpus detection at `mempalace init`.** A new Pass 0 runs at the start of `init` — before entity detection — and answers one question: *is this corpus an AI-dialogue record, and if so, which platform and what persona names has the user assigned to the agents?* Tier 1 is a free regex heuristic (well-known AI brand terms + turn-marker patterns, with a co-occurrence rule that suppresses ambiguous terms like `Claude`/`Gemini`/`Haiku` when no unambiguous AI signal is present, so French novels and astrology forums don't false-positive). Tier 2 is an LLM call (~$0.01 with Anthropic Haiku, free with local Ollama/LM Studio/llama.cpp/vLLM) that extracts `user_name` and `agent_persona_names` from dialogue structure. Result is persisted to `<palace>/.mempalace/origin.json` with a `schema_version: 1` envelope so downstream tools can read it. Entity classification then routes names matching `agent_persona_names` (case-insensitive) into a new `agent_personas` bucket instead of `people`, so a Claude Code transcript no longer misclassifies the user's `Echo`/`Sparrow`/`Cipher` agents as biological people. `llm_refine` receives the same context as a system-prompt preamble so it can disambiguate other ambiguous candidates with corpus-level knowledge too. Backwards compatible: callers that don't pass `corpus_origin` see the v3.3.3 return shape unchanged. (#TBD)
- **`mempalace init` runs LLM-assisted refinement by default.** v3.3.3 made `--llm` opt-in; the LLM-assisted path is qualitatively better (extracts persona names, refines ambiguous classifications) so it now runs by default. Provider precedence is unchanged — Ollama at `http://localhost:11434` first, then openai-compat, then anthropic with API key. **Never blocks init on a missing LLM**: if no provider is reachable (Ollama not running, no API key set), init prints a one-line message pointing at `--no-llm` and falls through to the heuristic-only path. `--no-llm` is the new explicit opt-out. The legacy `--llm` flag is preserved as a deprecated alias of the default so scripted callers see no behaviour change. Cost story: zero for users with a local LLM (the majority on this repo), ~$0.01 per init for users with `ANTHROPIC_API_KEY` set who explicitly choose `--llm-provider anthropic`, zero for users with no LLM (graceful fallback). (#TBD)
- **`mempalace mine --redetect-origin` flag.** Re-runs corpus-origin detection on the current corpus state and overwrites `<palace>/.mempalace/origin.json`. Useful when the corpus has grown since `mempalace init` and the stored origin may be stale. Heuristic-only by design (the flag is meant to be cheap); re-run `mempalace init` for full Tier 2 LLM refinement. Default `mempalace mine` does not touch `origin.json` — the flag is opt-in. (#TBD)

### Bug Fixes

- **MCP server `tool_diary_write` SIGSEGV when default EF provider differs.** `mcp_server._get_collection` bypassed `ChromaBackend.get_collection` and called `client.get_collection` / `client.create_collection` without `embedding_function=`. ChromaDB 1.x persists the EF *identity* (its `name()`) with the collection but not the EF *instance/configuration*, so the MCP server's reopen silently bound chromadb's built-in `DefaultEmbeddingFunction` — its `name()` matches `mempalace.embedding`'s spoofed `"default"` so the identity check passes, but its provider list is chromadb's default rather than the user's resolved device. The miner / Stop hook ingest path routes through the backend helper and binds the configured EF instead. On bleeding-edge interpreters (python 3.14 + chromadb 1.5.x on Apple Silicon) the default provider selection could SIGSEGV the host process on first `col.add()`, killing the MCP stdio server and leaving every subsequent tool call returning `Connection closed` until Claude Code was relaunched. `_get_collection` now reuses `ChromaBackend._resolve_embedding_function()` on the reopen branches that actually open a collection (warm-cache reads stay zero-cost), matching the miner/backend path. (#1299, follow-up to #1262 / #1289)
- **Hooks no longer recreate `~/.mempalace/` after the user removes it.** When `~/.mempalace/` is deleted (a strong "do not auto-capture" signal), the next `Stop`, `PreCompact`, or `SessionStart` hook would silently rebuild the dir hierarchy and ingest existing transcripts: `_log()` called `STATE_DIR.mkdir(parents=True, exist_ok=True)` unconditionally, so the very act of writing `[HH:MM] SESSION START …` recreated `~/.mempalace/hook_state/`; subsequent calls in the save path then materialized `palace/`, `wal/`, `knowledge_graph.sqlite3`, and N drawers from `~/.claude/projects/*.jsonl`. All four entry points (`hook_stop`, `hook_precompact`, `hook_session_start`, and `_log` itself) now check a new module-level `PALACE_ROOT = Path.home() / ".mempalace"` constant first and short-circuit (returning `{}` on stdout, never logging) when the directory is absent. The user-removable directory becomes a kill-switch — `rm -rf ~/.mempalace` is now a stable state. Net: 23 lines added in `mempalace/hooks_cli.py`, 5 unit tests in `tests/test_hooks_cli.py`. (#1305)
- **Cross-wing topic tunnels for hyphenated dir names.** `mempalace init` recorded the `topics_by_wing` registry key under the raw directory name (e.g. `mempalace-public`), while `mempalace.yaml`'s `wing` field used the lower-cased + separator-collapsed slug (`mempalace_public`). At mine time the miner read the slug from the yaml and missed the registry, so `_compute_topic_tunnels_for_wing` returned `0` silently. Real-world: any project whose folder contained a hyphen or space lost every topic tunnel. Now both call sites route through a shared `normalize_wing_name()` in `config.py`. (#1194, follow-up to #1180)
- **CLI `mempalace search` retrieval quality.** The CLI was using pure ChromaDB cosine distance with no BM25 rerank, so drawers containing every query term but embedding as noise (directory listings, diff output, shell logs) scored `Match: 0.0` alongside genuinely irrelevant results with no way to tell them apart. Wired the CLI through the same `_hybrid_rank` the `mempalace_search` MCP tool already used, and surfaced both `cosine=` and `bm25=` scores in the output so users see which component of the match is firing. MCP search was unaffected; this fixes the human-facing CLI parity gap.
- **Legacy-palace distance-metric warning.** CLI search now detects palaces created before `hnsw:space=cosine` was consistently set and prints a one-line notice pointing at `mempalace repair`. Without the warning such palaces silently used L2 distance, under which the similarity display floored every result to `Match: 0.0`. New palaces mined today already set cosine correctly and now have invariant tests pinning that behavior so future refactors can't silently regress it. (#1179)
- **Graceful Ctrl-C during `mempalace mine`.** Interrupting a long mine no longer dumps a multi-frame `KeyboardInterrupt` traceback. The main file-processing loop now catches the signal, prints `files_processed: N/M`, `drawers_filed: K`, and `last_file:` so the user knows what landed, then exits with code 130 (standard SIGINT). Already-filed drawers are upserted idempotently on re-mine via deterministic IDs, so resuming is safe. The hooks PID lock at `~/.mempalace/hook_state/mine.pid` is now also actively cleaned up in a `finally` when its entry points at us — clean exit, error, or interrupt — preventing the next hook fire from briefly waiting on a stale PID. (#1182)
- **`mempalace init` is now idempotent across re-runs.** Running `init` twice on the same project produced different `origin.json` results because the first run wrote `entities.json` into the project directory, and the second run's corpus-origin sampling included that file as corpus content — shifting Tier 1's character-density math. Sampling now skips the per-project artifacts (`entities.json`, `mempalace.yaml`), so re-running `init` produces the same classification it did the first time. Pinned by an integration test in `tests/test_corpus_origin_integration.py`. (#TBD)

---

## [3.3.3] — 2026-04-23

### Bug Fixes

- **Install regression** — `mempalace-mcp` console script is now declared in `pyproject.toml` alongside `.claude-plugin/plugin.json`'s reference to it. In v3.3.2 the two drifted apart (plugin.json shipped the new `"command": "mempalace-mcp"` form before the matching entry point landed), so every fresh `pip install mempalace==3.3.2` produced a Claude Code plugin config pointing at a binary that wasn't installed. (#1093, #340)
- Restore silent-save visibility after the Claude Code 2.1.114 client regression — production transcript saves were failing silently until this PR. (#1021)
- Paginate `status`-path metadata fetches so large palaces don't trip SQLite variable limits. (#851)
- Resolve the Claude plugin hook runner across platform / plugin-dir variations; previously broke on Windows and some macOS layouts. (#942)
- Real `python3` resolution for `.sh` hooks with a `MEMPAL_PYTHON` override path. (#833)
- Add optional `wing` parameter to `tool_diary_write` / `tool_diary_read` and derive per-project wing from the Claude Code transcript path when writing from the stop hook — diary entries from different projects no longer collapse into a shared default wing. (#659)
- Treat empty string as "no filter" in `mempalace_search` `wing`/`room`; LLM agents that default to filling every optional parameter with `""` no longer get bounced with `must be a non-empty string`. (#1097, #1084)
- Broaden `_wing_from_transcript_path` to handle Claude Code project folders without a `-Projects-` segment (e.g. `~/dev/<parent>/<project>`, `~/code/<project>`). The project name is now derived from the final dash-separated token of the encoded folder, so Linux users with code outside `~/Projects/` get per-project diary scoping instead of falling through to `wing_sessions`. (#1145, follow-up to #659)
- `mempalace_diary_read(wing="")` now returns diary entries from every wing this agent has written to, matching the #1097 "empty-string as no filter" pattern. Previously defaulted to `wing_<agent>`, siloing entries that hooks wrote to project-derived wings. (#1145)
- `mempalace mine` now skips the generated `entities.json` file so its contents aren't re-ingested as project content. (#1175)

### Improvements

- **Deterministic hook saves.** Save hook now uses a silent Python API path, so successive hook invocations produce reproducible results and zero data loss on the hot path. (#673)
- **Graph cache with write-invalidation** inside `build_graph()` — warm-path calls no longer rebuild the palace-graph per request. (#661)
- **`mempalace init` entity detection overhaul.** Canonical project names now come from package manifests (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`) and real people come from git commit authors, rather than being inferred from prose. Includes union-find dedup across name/email aliases, bot filtering that keeps `@users.noreply.github.com` humans, and automatic "mine" flagging by contribution share. (#1148)
- **Regex detector accuracy.** CamelCase extraction so `MemPalace`, `ChromaDB`, `OpenAI` aren't fragmented; tighter versioned/hyphenated pattern kills `context-manager` / `multi-word` false positives; dialogue `^NAME:\s` requires ≥2 hits so `Created: <date>` metadata stops classifying field names as people; expanded stopwords for common English participles and descriptors; high-pronoun signal classifies as person rather than dumping to uncertain. (#1148)
- **Init → miner wire-up.** Confirmed entities merge into `~/.mempalace/known_entities.json` on init, which the miner reads to tag drawer metadata for entity-filtered search. Previously init's output was not consumed by the miner; the per-project `entities.json` is kept as an audit trail. (#1157)
- **Case-insensitive project dedup** across manifest, git, and convo sources so casing variants of the same project name collapse into one review entry. (#1175)

### Added

- i18n: Belarusian translation. (#1051)
- i18n: entity detection for German, Spanish, and French locales. (#1001)
- i18n: Traditional + Simplified Chinese entity detection. (#945)
- **`mempalace init --llm`**: optional LLM-assisted entity classification. Defaults to local Ollama (zero-API); also supports any OpenAI-compatible endpoint (LM Studio, llama.cpp server, vLLM, OpenRouter, etc.) and the Anthropic Messages API. Runs interactively with a progress indicator; Ctrl-C cancels cleanly and returns partial results. Useful for prose-heavy folders where the regex detector struggles (diaries, transcripts, research notes). Opt-in only — default init path remains zero-API. (#1150)
- **Claude Code conversation scanner.** `~/.claude/projects/<slug>/` directories now contribute project entities using each session's authoritative `cwd` metadata, avoiding slug-decoding ambiguity. (#1150)

### Known — deferred to v3.3.4

- HNSW parallel-insert SIGSEGV when `hnsw:num_threads` is unset on collection creation (#974) — fix in-flight as #976, awaiting rebase against develop.

---

## [3.3.2] — 2026-04-19

### Bug Fixes

- Fix silent drop of `.jsonl` files in project miner; raise `MAX_FILE_SIZE` cap from 10 MB to 500 MB so large transcripts no longer fall through unnoticed. Adds a tandem **sweeper** — a message-level, timestamp-coordinated, idempotent safety net that catches anything the primary miner missed. (#998)
- `mempalace sweep <target>` CLI to run the sweeper on demand against a transcript file or a directory. (#998)
- Guard `Layer3.search_raw` against `None` doc/meta rows returned by ChromaDB — prevents `AttributeError` crashes on mixed-schema palaces. (#1011, #1013)
- Guard searcher API path, closet loop, and miner status histogram against `None` metadata; matching guards added to `tool_status` / `list_wings` / `list_rooms` / `get_taxonomy` in the MCP server. (#999)
- Upgrade `chromadb` floor to `>=1.5.4` for Python 3.13 / 3.14 compatibility and pin upper bound to `<2` so future breaking majors don't silently install. (#1010)
- Fix Unicode checkmark rendering on Windows terminals that can't encode the `✓` glyph — avoids `UnicodeEncodeError` crashes on first-run output. (#681)
- **`quarantine_stale_hnsw`** — on open, detect HNSW segment directories whose `data_level0.bin` is significantly older than `chroma.sqlite3` and rename them out of the way. Recovers cleanly from HNSW/sqlite drift that otherwise causes SIGSEGV on `count()` / `query(...)` (the chroma-core/chroma#2594 failure mode). Rebuilds the index lazily on next use. (#1000)
- **PID file guard** — `mine` writes a per-source-directory PID file and refuses to start if an existing mine is still running, preventing process stacking that bloats HNSW and wedges concurrent writes. Includes cross-platform PID liveness check (`os.kill(pid, 0)` terminates on Windows, so the guard falls back to a platform-aware probe). (#1023)

### Improvements

- **RFC 001 §10 — typed backend contracts.** `BaseBackend` now returns typed `QueryResult` / `GetResult` dataclasses and `PalaceRef` for palace identity; registry-based backend discovery. Internal refactor; no user-facing API change. (#995)
- **RFC 002 §9 — source adapter scaffolding.** Introduces `BaseSourceAdapter`, adapter registry, and `PalaceContext` — the plumbing that future pluggable ingest sources will target. Internal refactor; no user-facing API change yet. (#1014)

### Documentation

- **RFC 002** — full specification for the source adapter plugin system (future pluggable ingest). (#990)
- First-run help text and `README` now reference the real `~/.claude/projects/<project>/` path shape instead of the placeholder `/path/to/transcripts`. (#996, #1012)

### Internal

- Harden sweeper for production: verbatim tool blocks, full `session_id`, logged failures.
- Address Copilot review on #995: cursor tie-break, honest metrics, accurate comments.
- Test hygiene: avoid ONNX network download in update-length validation tests; dedup update-length-validation tests; fix Windows file-lock in cache-invalidation test.

---

## [3.3.1] — 2026-04-16

### New Features

**Multi-language entity detection** — lexical patterns (person verbs, pronouns, dialogue markers, project verbs, stopwords, candidate character classes) now live in the optional `entity` section of each locale JSON under `mempalace/i18n/<lang>.json`. Every public function in `entity_detector` accepts a `languages=` tuple and unions patterns across enabled locales. Default stays `("en",)` so existing English-only callers are unchanged. (#911)

- **Five new fully-supported locales** with CLI strings, AAAK compression instructions, and entity-detection patterns:
  - Brazilian Portuguese `pt-br` (#156)
  - Russian `ru` (#760)
  - Italian `it` (#907)
  - Hindi `hi` (#773)
  - Indonesian `id` (#778)
- **`MempalaceConfig.entity_languages`** — persistent palace-level language selection; `MEMPALACE_ENTITY_LANGUAGES` env override; `mempalace init --lang en,pt-br` flag that saves to `~/.mempalace/config.json` (#911)
- **Per-language `candidate_pattern`** — non-Latin scripts register their own character class, so names like `João`, `Инна`, `राज` are no longer silently dropped by the ASCII-only default (#911)
- **VSCode devcontainer** matching the CI environment (#881)
- `MEMPAL_VERBOSE` env toggle — developers see diaries surfaced in chat while the default remains silent (#871)
- `created_at` timestamps included in search results (#846)

### Bug Fixes

**i18n / Unicode**

- Script-aware word boundaries for combining-mark scripts — Python's `\b` fails on Devanagari vowel signs (`ा ी ु`), Arabic, Hebrew, Thai, Tamil, Khmer etc., truncating names like `अनीता` → `अनीत` and making person-verb patterns never fire. Locales now declare an optional `boundary_chars` field and the i18n loader expands `\b` into a script-aware lookaround boundary (#932)
- Case-insensitive BCP 47 language code resolution — `--lang PT-BR`, `zh-cn`, `Pt-Br` previously fell through to English silently; now resolve to the canonical locale file via lowercase matching, with the entity-pattern cache keyed on the canonical form so casing variations share one cache entry (#928)
- Wire i18n candidate patterns into `miner._extract_entities_for_metadata()`, `palace.build_closet_lines()`, and `entity_registry.extract_unknown_candidates()` — three code paths that still hardcoded ASCII-only `[A-Z][a-z]{2,}` and silently missed Cyrillic, accented Latin, and non-Latin entity metadata tags (#931)
- Explicit `encoding="utf-8"` on `Path.read_text()` calls across entity_registry, instructions_cli, split_mega_files, and onboarding tests — prevents Windows GBK (and other non-UTF-8) locales from corrupting UTF-8 files (#946, #776)
- `ko.json` `status_drawers` used `{drawers}` instead of `{count}`, showing the raw template string instead of the number (#758)
- Move `test_i18n.py` from inside the installed package into `tests/` so pytest actually collects it; remove the `sys.path.insert` hack (#758)
- `Dialect.from_config()` defaulted to `current_lang()` (module-global) when config had no `lang` key — replaced with explicit `"en"` fallback for determinism (#758)

**Other**

- Guard `KnowledgeGraph.close()` and `query_relationship`/`timeline`/`stats` methods with the instance lock to prevent concurrent-access corruption (#887, #884)
- Replace invalid `{"decision": "allow"}` with `{}` in hook responses — the string wasn't a valid decision value and triggered schema warnings (#885)
- `entity_registry.research()` defaults to local-only — previously made outbound Wikipedia HTTPS requests without explicit user opt-in; callers now must pass `allow_network=True` (#811)
- Precompact hook no longer blocks compaction when it fails or takes too long (#856, #858, #863)
- Redirect stdout to stderr during MCP server import so library logging can't corrupt the JSON-RPC channel (#225, #864)
- `mempalace init` auto-adds per-project files to `.gitignore` in git repositories so users don't accidentally commit `mempalace.yaml` / `entities.json` (#185, #866)
- Searcher guards against empty ChromaDB query results that previously raised on edge-case corpora (#195, #865)
- Return empty status instead of an error on a cold-start palace with no drawers yet (#830, #831)
- Restrict file permissions on sensitive palace data (#814)
- Slack transcript importer writes a provenance header and preserves speaker IDs (#815)
- Allow `mempalace mine` to run in directories without a local `mempalace.yaml` and surface the missing-yaml warning on stderr (#604)
- Security hook injection fix (#812)
- Save hook auto-mines transcripts even when `MEMPAL_DIR` is unset (#840)
- Pin the Pages custom domain via a shipped `CNAME` in the deploy artifact (#877)
- Version drift safeguard — sync pyproject + `version.py` + README badge in one place (#876)
- Deploy docs workflow now runs on `develop` only, preventing accidental main-branch deploys (#845)

### Improvements

- Regex compilation optimization for entity extraction — pre-compile per-entity pattern sets once and cache by `(name, languages)` tuple, so multi-language callers don't thrash the cache (#880)
- Knowledge-graph value sanitization now preserves natural punctuation (commas, colons, parentheses) that commonly appears in KG subject/object values (#873)

### Documentation

- Clarify that `mempalace init` requires a `<dir>` argument in CLI help text (#210, #862)
- Domain name and specific impostor sites called out in the scam-alert section (#869)
- Tightened `SECURITY.md` with a real version-support policy and the GHPVR-only reporting channel (#810)
- Fixed stale `pyproject.toml` URLs (#853)
- v4 planning prep (#852)

### Internal

- `palace_graph` tunnel helper test coverage (#908)

---

## [3.3.0] — 2026-04-13

### New Features
- Closet layer — a compact searchable index of pointers to verbatim drawers, enabling fast topical lookup without reading all content (#788)
- BM25 hybrid search — closets boost ranking, drawers remain the source of truth (#795, #829)
- Entity metadata on every drawer for filterable search (#829)
- Diary ingest — day-based rooms for conversation transcripts (#829)
- Cross-wing tunnels — explicit links between rooms in different wings for multi-project agents (#829)
- Drawer-grep — returns the best-matching chunk plus adjacent context drawers (#829)
- Offline fact checker against the entity registry and knowledge graph (#829)
- LLM-based closet regeneration — optional, bring-your-own endpoint, no mandatory API key (#793)
- Hall detection — routes drawer content to `emotions` / `technical` / `family` / `memory` / `identity` / `consciousness` / `creative` halls, enabling hall-based graph connectivity within wings (#835)

### Bug Fixes
- Repair `max_seq_id` corruption caused by `_fix_blob_seq_ids` misinterpreting chromadb 1.5.x's sysdb-10 BLOB format (`b'\x11\x11'` + ASCII digits) as legacy 0.6.x big-endian BLOBs. The shim now skips the `max_seq_id` table entirely and guards the `embeddings` branch with a prefix check. New subcommand `mempalace repair --mode max-seq-id [--from-sidecar <path>]` restores affected palaces. Fixes silent drawer-write drops that began after chromadb 1.5.x upgrades on palaces that still had BLOB-typed `max_seq_id` rows at migration time.
- Set `hnsw:space=cosine` metadata on all collection creation sites — fixes broken similarity scoring under ChromaDB's default L2 distance (#807, #218)
- File-level locking prevents duplicate drawers when agents mine the same file concurrently (#784, #826)
- Hybrid closet+drawer retrieval — closets boost ranking, never gate results (#795)
- Stop hooks from making agents write in chat — saves tokens on every turn (#786)
- Strip system tags, hook output, and Claude UI chrome from drawers before filing (#785)
- Verbatim-safe `strip_noise` scoped to Claude Code JSONL only (#785)
- Prevent diary entry ID collisions via microsecond timestamp and full content hash (#819)
- Auto-rebuild stale drawers via `NORMALIZE_VERSION` schema gate
- Enforce atomic topics in closets and extract richer pointers
- Sync `version.py` to match `pyproject.toml` (#820)
- Remove unused `main` import from `mempalace/__init__.py` (#827)
- README audit — fix 7 stale claims (tool count, version badge, wake-up token cost, `dialect.py` lossless disclaimer, `pyproject.toml` version) with 42 regression-guard tests (#835)

### Improvements
- Optimize entity detection with regex caching and pre-compilation (#828)
- Extract locked filing block into helper to keep `mine_convos` under C901 complexity

### Documentation
- Add `docs/CLOSETS.md` — closet layer overview
- Fix stale `milla-jovovich/*` org URLs in website and plugin manifests (#787)
- Fix remaining stale org URLs in contributor docs (#808)
- Rewrite `README.md` and `mempalaceofficial.com` benchmark pages to remove category-error cross-system comparisons (R@5 retrieval recall had been listed next to competitor QA accuracy under one column), remove the retracted "+34% palace boost" claim from the surfaces where it had remained, replace the `100%` Haiku-rerank headline with the honest held-out `98.4%` R@5, drop the LoCoMo `100%` top-50 row (retrieval-bypass artefact), and fix the broken `aya-thekeeper/mempal` reproduction URL (#875)
- Add `docs/HISTORY.md` as the canonical home for corrections, retractions, and public notices; move the 2026-04-07 "Note from Milla & Ben" and the 2026-04-11 impostor-domain notice out of `README.md`
- Add v3.3.0 reproduction result JSONLs and the deterministic `seed=42` 50/450 LongMemEval split under `benchmarks/` — every BENCHMARKS.md claim reproduces exactly

### Internal
- Add test coverage for `mine_lock`, closets, entity metadata, BM25, and diary
- Verify `mine_lock` via disjoint critical-section intervals
- Serialize `mine_lock` concurrency test with multiprocessing
- Make diary state path assertion platform-neutral
- Add `TestTunnels` coverage for cross-wing tunnel operations
- Ruff format with CI-pinned version (0.4.x); format `mempalace/palace.py`

---

## [3.2.0] — 2026-04-12

### Packaging
- Remove `chromadb<0.7` upper bound — unblocks installs against chromadb 1.x palaces (#690)
- Bump version to 3.2.0 across `pyproject.toml`, `mempalace/version.py`, README badge, and OpenClaw SKILL (#761)

### Security
- Harden palace deletion, WAL redaction, and MCP search input handling (#739)
- Consistent input validation, argument whitelisting, concurrency safety, and WAL fixes (#647)
- Remove hardcoded credential paths from benchmark runners (#177)
- Remove global SSL verification bypass in convomem_bench (#176)

### Bug Fixes
- Parse Claude.ai privacy export with `messages` key and sender field (#685, #677)
- Detect mtime changes in `_get_client` to prevent stale HNSW index (#757)
- Hash full content in `tool_add_drawer` drawer ID — stable re-mines (#716)
- Remove 10k drawer cap from status display (#707, #603)
- Correct typo in entity_detector interactive classification prompt (#755)
- Prevent convo_miner from re-processing 0-chunk files on every run (#732, #654)
- Remove silent 8-line AI response truncation in convo_miner (#708, #692)
- Store full AI response in convo_miner exchange chunking (#695)
- Fix `mine --dry-run` TypeError on files with room=None (#687, #586)
- Skip arg whitelist for handlers accepting `**kwargs` (#684, #572)
- Allow Unicode in `sanitize_name()` — Latvian, CJK, Cyrillic (#683, #637)
- Auto-repair BLOB seq_ids from chromadb 0.6→1.5 migration (#664)
- Remove no-op `ORT_DISABLE_COREML` env var (#653, #397)
- Disambiguate hook block reasons to name MemPalace explicitly (#666)
- Use epsilon comparison for mtime to prevent unnecessary re-mining (#610)
- Correct token count estimate in compress summary (#609)
- Implement MCP ping health checks (#600)
- Align `cmd_compress` dict keys with `compression_stats()` return values (#569)
- Skip unreachable reparse points in `detect_rooms_from_folders` on Windows (#558)
- Prevent HNSW index bloat from duplicate `add()` calls (#544, #525)
- Purge stale drawers before re-mine to avoid hnswlib segfault (#544)
- Mitigate system prompt contamination in search queries (#385, #333)
- Count Codex `user_message` turns in `_count_human_messages` (#373, #347)
- Paginate large collection reads and surface errors in MCP tools (#371, #339, #338)
- Expand `~` in split command directory argument (#361)
- Ignore `wait_for_previous` argument to support Gemini MCP clients (#322)
- Close KnowledgeGraph SQLite connections in test fixtures (#450)
- Remove duplicate cache variable declarations in mcp_server.py (#449)
- Add `--yes` flag to init instructions for non-interactive use (#682, #534)
- Add `mcp` command with setup guidance (#315)

### New Features
- i18n support — 8 languages (en, es, fr, de, ja, ko, zh-CN, zh-TW) (#718)
- New MCP tools: get/list/update drawer, hook settings, export (#667, #635)
- `mempalace migrate` — recover palaces from different ChromaDB versions (#502)
- Add OpenClaw/ClawHub skill (#491)
- Backend seam for pluggable storage backends (#413)

### Improvements
- Disable broken auto-bump workflow (#414)
- Improve agent readiness — AGENTS.md, dependabot, CODEOWNERS, labels (#497)

### Documentation
- Add CLAUDE.md and mission/principles to AGENTS.md (#720)
- Add VitePress documentation site (#439)
- Add warning about fake MemPalace websites (#598)
- Fix stale org URLs and PR branch target in contributor docs (#679)
- Fix misaligned architecture diagram (#734, #733)
- Add ROADMAP.md — v3.1.1 stability patch and v4.0.0-alpha plan

### Internal
- ruff format convo_miner.py (#741)
- ruff format all Python files (#675)
- CI: trigger tests on develop branch PRs and pushes (#674)
- CI: fix GitHub Pages publishing (#691)

---

## [3.1.0] — 2026-04-09

### Security
- Harden inputs, fix shell injection, optimize DB access (#387)
- Sanitize SESSION_ID in save hook to prevent path traversal (#141)
- Sanitize error responses and remove `sys.exit` from library code (#139)
- Shell injection fix in hooks, Claude Code mining, chromadb pin (#114)

### Bug Fixes
- MCP null args hang, repair infinite recursion, OOM on large files (#399)
- Release ChromaDB handles before rmtree on Windows (#392)
- Use `os.utime` in mtime test for Windows compatibility (#392)
- Negotiate MCP protocol version instead of hardcoding (#324)
- Use upsert and deterministic IDs to prevent data stagnation (#140)
- Make `drawer_id` deterministic for idempotent writes (#387)
- Honest AAAK stats — word-based token estimator, lossy labels (#147)
- Room detection checks keywords against folder paths (#145)
- Use actual detected room in mine summary stats (#165)
- Honour `--palace` flag in mcp_server (#264)
- Preserve default KG path when `--palace` not passed (#270)
- `--yes` flag skips all interactive prompts in init (#123)
- Repair command, split args, Claude export, room keywords (#119)
- Replace Unicode separator in convo_miner.py for Windows compatibility (#129)
- Coerce MCP integer arguments to native Python int (#84)
- Batch ChromaDB reads to avoid SQLite variable limit (#66)
- Respect nested .gitignore rules during mining (#78)
- Narrow bare `except Exception` to specific types where safe (#54)
- Mark MD5 as non-security in miner drawer ID generation (#53)
- Remove dead code and duplicate set items in entity_registry.py (#42)
- Silence ChromaDB telemetry warnings and CoreML segfault on Apple Silicon (#236)
- Unify package and MCP version reporting (#16)
- Fix broken AAAK Dialect link in README (#238)
- Update input prompt for entity confirmation (#83)
- Preserve CLI exit codes, log tracebacks, sanitize search errors (#139)
- Enable SQLite WAL mode and add consistent LIMIT to KG timeline (#136)
- Add limit=10000 safety cap to all unbounded ChromaDB `.get()` calls (#137)
- Re-mine modified files, idempotent `add_drawer`, cleanup ChromaDB handles (#140)
- Resolve formatting, regression logic, and pytest defaults (#270)
- Use `parse_known_args` to allow importing mcp_server during pytest (#270)

### New Features
- Package MemPalace as standard Claude and Codex plugins (#270)
- Add OpenAI Codex CLI JSONL normalizer (#61)
- Add Codex plugin support with hooks, commands, and documentation (#270)
- Add command documentation for help, init, mine, search, and status (#270)

### Improvements
- Cache ChromaDB `PersistentClient` instead of re-instantiating per call (#135)
- Tighten chromadb version range and add `py.typed` marker (#142)
- Consolidate split known-names config loading (#22)
- CI: add separate jobs for Windows and macOS testing
- CI: Upgrade GitHub Actions for Node 24 compatibility (#55)

### Documentation
- Add Gemini CLI setup guide and integration section (#106)
- Add beginner-friendly hooks tutorial (#103)
- Align MCP setup examples with shipped server (#21)
- Honest README update — own the mistakes, fix the claims

### Internal
- Expand test coverage from 20 to 92 tests, migrate to uv (#131)
- Add scale benchmark suite — 106 tests (#223)
- Increase test coverage from 30% to 85%, fix Windows encoding bugs (#281)
- Add WAL mode and entity timeline limit assertions
- Add coverage for `file_already_mined` mtime check

---

## [3.0.0] — 2026-04-06

Initial public release.

- Palace architecture with day-based rooms, drawers (verbatim), and closets (searchable index)
- AAAK compression dialect for memory folding
- Knowledge graph with entity detection and timeline queries
- MCP server for Claude, Codex, and Gemini integration
- CLI: `init`, `mine`, `search`, `status`, `compress`, `repair`, `split`
- Benchmark suite with recall and scale tests
- README with MCP flow, local model flow, and specialist agent documentation

---

[Unreleased]: https://github.com/MemPalace/mempalace/compare/v3.12.0...HEAD
[3.12.0]: https://github.com/MemPalace/mempalace/compare/v3.11.0...v3.12.0
[3.11.0]: https://github.com/MemPalace/mempalace/compare/v3.10.0...v3.11.0
[3.10.0]: https://github.com/MemPalace/mempalace/compare/v3.9.0...v3.10.0
[3.9.0]: https://github.com/MemPalace/mempalace/compare/v3.8.0...v3.9.0
[3.8.0]: https://github.com/MemPalace/mempalace/compare/v3.7.1...v3.8.0
[3.7.1]: https://github.com/MemPalace/mempalace/compare/v3.7.0...v3.7.1
[3.7.0]: https://github.com/MemPalace/mempalace/compare/v3.6.0...v3.7.0
[3.6.0]: https://github.com/MemPalace/mempalace/compare/v3.5.0...v3.6.0
[3.5.0]: https://github.com/MemPalace/mempalace/compare/v3.4.1...v3.5.0
[3.4.1]: https://github.com/MemPalace/mempalace/compare/v3.4.0...v3.4.1
[3.4.0]: https://github.com/MemPalace/mempalace/compare/v3.3.6...v3.4.0
[3.3.6]: https://github.com/MemPalace/mempalace/compare/v3.3.5...v3.3.6
[3.3.5]: https://github.com/MemPalace/mempalace/compare/v3.3.4...v3.3.5
[3.3.4]: https://github.com/MemPalace/mempalace/compare/v3.3.3...v3.3.4
[3.3.3]: https://github.com/MemPalace/mempalace/compare/v3.3.2...v3.3.3
[3.3.2]: https://github.com/MemPalace/mempalace/compare/v3.3.1...v3.3.2
[3.3.1]: https://github.com/MemPalace/mempalace/compare/v3.3.0...v3.3.1
[3.3.0]: https://github.com/MemPalace/mempalace/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/MemPalace/mempalace/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/MemPalace/mempalace/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/MemPalace/mempalace/releases/tag/v3.0.0
