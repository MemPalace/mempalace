# Changelog

All notable changes to [MemPalace](https://github.com/MemPalace/mempalace) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added

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

### Bug Fixes
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

### Bug Fixes

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

[Unreleased]: https://github.com/MemPalace/mempalace/compare/v3.10.0...HEAD
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
