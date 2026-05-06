# Fork Changelog (jphein/mempalace)

Fork-ahead changes that aren't yet in upstream `MemPalace/mempalace`.
Upstream's release history lives in [`CHANGELOG.md`](CHANGELOG.md);
this file is the supplement.

> **This file is generated.** Edit `docs/fork-changes.yaml` and run
> `scripts/render-docs.py` to regenerate. Hand-edits will be
> overwritten on the next render.

Date-based sections, not semver — the fork tracks `upstream/develop` and
doesn't cut its own release tags. When a fork-ahead row lands upstream,
move the entry to the **Merged into upstream** section at the bottom
(kept ~30 days, then trimmed).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---


## [2026-05-05]


### Added


- **`hook_verbatim_mode` config flag preserves system tags + full tool I/O during transcript ingest** ([`ef98961`](https://github.com/jphein/mempalace/commit/ef98961))
  `normalize()` defaults match upstream — system tags, hook chrome,
  Read/Edit/Write tool results, long Bash output, and large
  Grep/Glob match lists are stripped or truncated so chunk
  embeddings don't drift on chrome tokens. That's the right
  default for a search-quality optimization but it also drops
  content a verbatim-archive consumer wants to keep.

  Adds a `hooks.verbatim_mode` opt-in in `config.json`
  (`MempalaceConfig.hook_verbatim_mode`, default `False`).
  `mempalace.convo_miner.mine_convos` reads the flag and passes
  `verbatim=...` through `normalize()` →
  `_try_normalize_json()` → `_try_claude_code_jsonl()` →
  `_extract_content()` → `_format_tool_use()` /
  `_format_tool_result()` / `strip_noise()`. When `verbatim` is
  true: `strip_noise` is a passthrough; Bash commands and
  unknown-tool JSON inputs aren't 200-char truncated; Bash output
  isn't head/tail-collapsed; Grep/Glob match lists aren't capped;
  Read/Edit/Write results are included rather than omitted;
  unknown-tool output isn't byte-capped.

  Other transcript schemas (Codex, Gemini, claude.ai, ChatGPT,
  Slack) didn't truncate to begin with, so they're already
  verbatim — the flag is a no-op for them.

  Daemon path picks up the toggle transparently because the
  daemon spawns `mempalace mine ...` as a subprocess that goes
  through `convo_miner.mine_convos`.

  Backs JP's 2026-05-05 question — "we're not missing any tool
  calls or anything, right?" — without altering the upstream
  default for installs that benefit from chrome-stripped
  embeddings.

  *Tests:* 9 new tests in `tests/test_normalize.py::TestVerbatimMode` —
covers strip_noise passthrough, Bash and unknown-tool input
no-truncation, Read/Edit/Write result inclusion, Bash
head/tail no-collapse, Grep/Glob match no-cap, unknown-tool
byte no-cap, full JSONL round-trip, default-off contract, and
config-file readback. Suite total 1562 passed.

  *Files:* `mempalace/config.py`, `mempalace/convo_miner.py`, `mempalace/normalize.py`, `tests/test_normalize.py`


## [2026-05-03]


### Fixed


- **`cfg.init()` no longer materializes chunking defaults into `config.json`** ([`6ce37c0`](https://github.com/jphein/mempalace/commit/6ce37c0))
  `cfg.init()` was unconditionally writing ``chunk_size: 800``,
  ``chunk_overlap: 100``, and ``min_chunk_size: 50`` into
  ``config.json`` on first run. The values match ``miner.py``'s
  module-level constants but conflict with ``convo_miner.py``'s
  stricter ``MIN_CHUNK_SIZE = 30`` floor — and ``convo_miner.py``
  lines 427-431 explicitly distinguishes "user has tuned this"
  from "user is on defaults" by checking
  ``_file_config.get("min_chunk_size") is None``. Materializing
  the value as a default broke that detection: any user who ran
  ``mempalace init`` then mined conversations would silently lose
  exchanges shorter than 50 characters, even though the convo
  miner's intended floor is 30.

  Surfaced by a pytest fixture leak. ``tests/conftest.py:21-27``
  redirects ``HOME`` to a session-tmp directory so tests don't
  trash the real ``~/.mempalace``. The first test that calls
  ``cmd_init`` writes the bloated default config into the
  session-tmp ``~/.mempalace``, and downstream
  ``test_convo_miner`` runs (in-process, same session) then read
  ``min_chunk_size: 50`` and skip the test fixture's ~30-char
  exchanges entirely. Both tests pass in isolation; the second
  fails when chained.

  Fix: drop the three chunking keys from ``cfg.init()``'s
  default-config-write. The
  ``MempalaceConfig.chunk_size``/``.chunk_overlap``/``.min_chunk_size``
  properties already provide the right fallbacks via
  ``_file_config.get(key, default)`` when the key is absent.
  Users who want to tune chunking still set the keys explicitly;
  the contract ``convo_miner.py`` relies on (``is None`` ⇔
  "untuned") is restored.

  Same fix pushed to the open #1024 PR branch as commit
  ``df9187c`` so the bug doesn't get reintroduced when #1024
  merges. Amends fork-ahead row 17.

  *Tests:* 1548/1548 (was 1546/1548 with 2 isolation failures in test_convo_miner)
  *Upstream:* [PR #1024](https://github.com/MemPalace/mempalace/pull/1024) (OPEN)
  *Files:* `mempalace/config.py`


## [2026-04-27]


### Changed


- **Retire the `kind=` filter — structural split made it inert** ([`7ba28dc`](https://github.com/jphein/mempalace/commit/7ba28dc))
  Phases A–E of the checkpoint collection split (2026-04-25 → 2026-04-26)
  moved every Stop-hook auto-save checkpoint drawer to the dedicated
  ``mempalace_session_recovery`` collection. Empirical check on the
  canonical 151K palace: ``mempalace_drawers`` has zero
  ``topic=checkpoint`` and zero ``topic=auto-save`` drawers; recovery
  collection holds 763. The ``kind=`` post-filter was filtering nothing.

  Deleted: ``_CHECKPOINT_TOPICS`` (moved to ``palace.py`` for write-side
  routing), ``_is_checkpoint_drawer``, ``_apply_kind_text_filter``, the
  ``max(n*20, 100)`` over-fetch hack (back to standard ``n_results * 3``),
  the ``kind=`` parameter on ``search_memories`` / ``build_where_filter`` /
  CLI ``search`` / ``mempalace_search`` MCP tool input_schema, and
  ``TestCheckpointFilter`` (9 tests). Companion fix in
  [palace-daemon](https://github.com/jphein/palace-daemon/commit/4a318d3)
  (v1.7.1) drops ``kind=`` from ``/search`` and ``/context`` HTTP routes.

  *Tests:* −9 (TestCheckpointFilter deleted; suite at 1500)
  *Files:* `mempalace/searcher.py`, `mempalace/mcp_server.py`, `mempalace/palace.py`, `mempalace/migrate.py`, `mempalace/layers.py`, `tests/test_searcher.py`


- **Hoist CLOSET_RANK_BOOSTS to module level + record VecRecall ablation finding** ([`3cb03f3`](https://github.com/jphein/mempalace/commit/3cb03f3))
  Two-step refactor + measurement. First (commit ``f558d3c``):
  hoist ``CLOSET_RANK_BOOSTS = [0.40, 0.25, 0.15, 0.08, 0.04]`` and
  ``CLOSET_DISTANCE_CAP`` from inside ``search_memories`` to module
  scope so they can be tuned from the outside (env var, config flag,
  or in-process patch for A/B benchmarking) without touching the
  function. No behavior change; pure ablation enablement.

  Then (commit ``3cb03f3``): A/B ablation against the 151K canonical
  palace (12-probe set covering recent fork-side decisions + mined-file
  content). Closet boost fires on ~20% of result rows, concentrated
  in queries whose answer lives in mined files; closets are sparse on
  chat-transcript queries (most fork-side decisions). When the boost
  fired, it re-ordered chunks within a single source file rather than
  displacing right answers with wrong ones — i.e. VecRecall's critique
  ([discussions/1129](https://github.com/MemPalace/mempalace/discussions/1129),
  "org-layer in retrieval path drops R@5") did not reproduce here.
  Hybrid degrades to effectively pure-vector for transcript queries
  and re-ranks within-file chunks for mined-file queries; neither
  shape matches the failure mode VecRecall is fixing. Findings noted
  in the comment block above the constants so future-us doesn't have
  to re-run the experiment.

  *Files:* `mempalace/searcher.py`


### Fixed


- **Strip embedded API key from .claude-plugin/ manifests; rely on env inheritance** ([`9f91e18`](https://github.com/jphein/mempalace/commit/9f91e18))
  ``.claude-plugin/.mcp.json`` and ``.claude-plugin/hooks/hooks.json``
  shipped with a real (rotated) API key embedded as a literal in the
  manifest's ``env`` block, plus my homelab daemon URL. Both are
  committed plugin templates that get pulled into every plugin install.

  Fix in two commits: ``8119149`` reverted both manifests to the
  upstream-shape (no env block, in-process MCP), then ``9f91e18``
  restored daemon-routing on ``.mcp.json`` (URL + path) but **without**
  the embedded credential — ``PALACE_API_KEY`` now inherits at runtime
  from ``~/.claude/settings.local.json``'s ``env`` block (which
  Claude Code passes to spawned MCP servers and hooks).

  Net: my fork-main carries the daemon-routed config matching production
  deployment; the literal credential lives one place only (gitignored
  ``settings.local.json``); future plugin installs inherit env rather
  than carrying a stale embedded key. Companion to palace-daemon
  [PR #12](https://github.com/rboarescu/palace-daemon/pull/12) which
  fixes the same class of embedded-default in ``clients/palace-mode``.

  *Files:* `.claude-plugin/.mcp.json`, `.claude-plugin/hooks/hooks.json`


## [2026-04-26]


### Added


- **Canonical YAML manifest + renderer for fork-ahead docs** ([`5a01aec`](https://github.com/jphein/mempalace/commit/5a01aec))
  The fork-ahead narrative previously lived (and drifted) across four
  hand-edited files: README's fork-change-queue table, CLAUDE.md's row
  inventory, FORK_CHANGELOG.md, and the promises tracker. New
  ``docs/fork-changes.yaml`` is now the canonical source; running
  ``scripts/render-docs.py`` regenerates FORK_CHANGELOG.md.
  ``scripts/check-docs.sh`` extended with a render-parity check that
  detects YAML→FORK_CHANGELOG drift, plus the existing test-count /
  commit-hash / upstream-PR-state checks. Researched towncrier, scriv,
  git-cliff, antsibull-changelog — none do single-source →
  multi-target render in this shape. README/CLAUDE/promises
  rendering planned for follow-on commits with marker-based
  insertion.

  *Files:* `docs/fork-changes.yaml`, `scripts/render-docs.py`, `scripts/check-docs.sh`, `FORK_CHANGELOG.md`, `CLAUDE.md`


- **Phase D migration + PreCompact recovery write** ([`42817d7`](https://github.com/jphein/mempalace/commit/42817d7))
  ``migrate_checkpoints_to_recovery(palace_path, batch_size=1000)`` walks
  the main collection in pages, filters drawers with topic in
  ``_CHECKPOINT_TOPICS`` in Python (avoids the chromadb 1.5.x ``$in``/``$nin``
  filter-planner bug), copies them to the recovery collection
  (preserving IDs + metadata), then deletes from main. Idempotent —
  re-running on a fully-reorganized palace returns 0. Add-then-delete
  order: a crash mid-migration leaves a duplicate, not a loss.
  Wired into ``mempalace repair --mode reorganize`` for explicit operator
  runs. PreCompact incorporated — ``hook_precompact`` now writes a
  session-recovery marker mirroring Stop, so context-compaction events
  leave a queryable timestamp in the recovery collection rather than
  nothing. Failures are non-fatal (logged; mining + compaction still
  proceed).

  *Tests:* 6 in TestMigrateCheckpointsToRecovery + 1 in test_hooks_cli
  *Files:* `mempalace/migrate.py`, `mempalace/cli.py`, `mempalace/hooks_cli.py`, `tests/test_migrate.py`


- **Surface drawer_id in search/diary/recovery payloads** ([`9a8bb77`](https://github.com/jphein/mempalace/commit/9a8bb77))
  ChromaDB's primary key was always returned by ``query()`` and ``get()``
  but never plumbed into result-building loops; consumers (e.g.
  familiar.realm.watch's citation-popover loop) couldn't link a hit
  back to the underlying drawer. Three call sites updated for parity:
  ``searcher.search_memories`` (vector path + sqlite BM25 fallback),
  ``mcp_server.tool_session_recovery_read``, ``mcp_server.tool_diary_read``.
  Defensive zip with id-pad: production chromadb always returns ids,
  but several test mocks omit them — pad with ``None`` when absent so
  existing fixtures keep working without touching N tests.

  *Tests:* 1 integration + 1 inline assertion
  *Files:* `mempalace/searcher.py`, `mempalace/mcp_server.py`, `website/reference/mcp-tools.md`


- **scripts/deploy.sh — one-command Syncthing-aware redeploy** ([`8252025`](https://github.com/jphein/mempalace/commit/8252025))
  Single command does the right shape: push fork main → wait for
  Syncthing to reach ``/mnt/raid/projects/memorypalace`` on the deploy
  host → ``systemctl --user restart palace-daemon`` → poll ``/health`` →
  ssh-import-check that today's fork-ahead surface is loaded.
  Replaces a three-step manual ritual that was easy to get wrong
  (e.g. ``pip install --upgrade`` was a no-op on the editable install).

  *Files:* `scripts/deploy.sh`


### Changed


- **Cherry-pick #1094 — coerce None metadatas at chromadb boundary** ([`43d728d`](https://github.com/jphein/mempalace/commit/43d728d))
  Fork main was carrying the per-site ``meta = meta or {}`` guards
  from #999 in eight read paths but didn't have the boundary
  coercion that closes the issue once for all callers. The typed
  ``QueryResult``/``GetResult`` contract declares
  ``metadatas: list[dict]``, never ``list[Optional[dict]]`` — so
  every call site that forgot the per-site guard was a latent
  ``AttributeError``. #1094 (open upstream, jp-authored) coerces
  at ``ChromaCollection.query()`` / ``.get()`` so downstream
  callers always receive ``list[dict]``. Per-site guards retained
  as belt-and-suspenders for paths that might bypass the typed
  wrappers. Three same-family fork-ahead PRs (#1198, #1201, #1083
  review) all pointed at gaps that would have been impossible if
  this pattern had been in place.

  *Tests:* 6 in test_backends.py (mixed/all-None inner lists, padding regression, get-without-metadatas)
  *Upstream:* [PR #1094](https://github.com/MemPalace/mempalace/pull/1094) (OPEN)
  *Files:* `mempalace/backends/chroma.py`, `tests/test_backends.py`


- **Cherry-pick #1087 rewrite — collection.delete(where=) instead of nuke-and-rebuild** ([`366a9ad`](https://github.com/jphein/mempalace/commit/366a9ad))
  Fork main had been carrying ``cmd_purge``'s nuke-and-rebuild
  shape (extract survivors, ``shutil.rmtree``, recreate, re-insert).
  Cherry-picked the post-review rewrite from PR #1087's branch:
  ``ChromaBackend.get_collection`` + ``col.delete(where=...)``.
  The race in #521 is on the upsert path
  (``updatePoint`` / ``repairConnectionsForUpdate``) — filter-delete
  doesn't reach it. Five fixes from @igorls's review now apply to
  our own purge: embedding function preserved, no rmtree window,
  routes through the backend, ``confirm_destructive_action`` reused,
  end-to-end test covers the embedding-fn-survival path.

  *Tests:* 5 in test_cli.py (TestCmdPurge + e2e)
  *Upstream:* [PR #1087](https://github.com/MemPalace/mempalace/pull/1087) (OPEN)
  *Files:* `mempalace/cli.py`, `tests/test_cli.py`


### Fixed


- **Integrity gate prevents quarantine_stale_hnsw from destroying healthy indexes** ([`645ba20`](https://github.com/jphein/mempalace/commit/645ba20))
  Previous behavior fired whenever ``sqlite_mtime - hnsw_mtime`` exceeded
  the (lowered, in #1173) 300s threshold. ChromaDB 1.5.x flushes HNSW
  asynchronously and a clean shutdown does not force-flush, so the
  on-disk HNSW is always meaningfully older than ``chroma.sqlite3`` —
  that's the steady state, not corruption. Quarantine renamed valid
  HNSW segments on every cold-start; chromadb created empty replacements;
  vector recall went to 0/N until rebuild. Confirmed in production on
  the disks daemon journal 2026-04-26 06:56:45: three of three healthy
  253MB segments quarantined on cold-start with 538-557s gaps. Fix:
  stage 2 integrity gate sniffs the chromadb segment metadata file
  for its protocol/terminator bytes (PROTO ``\x80`` head, STOP ``\x2e``
  tail) and a non-trivial size, **without deserializing**. Healthy
  segment with mtime drift → keep in place; truncated/zero-filled →
  quarantine.

  *Tests:* 4 in test_backends.py (renames-corrupt, leaves-healthy-with-drift, leaves-no-metadata, renames-truncated)
  *Upstream:* [PR #1173](https://github.com/MemPalace/mempalace/pull/1173) (MERGED)
  *Files:* `mempalace/backends/chroma.py`, `tests/test_backends.py`


### Performance


- **Cherry-pick #1085 — batch ChromaDB inserts in miner (10–30× faster)** ([`6be6fff`](https://github.com/jphein/mempalace/commit/6be6fff))
  Cherry-picked from upstream PR
  [#1085](https://github.com/MemPalace/mempalace/pull/1085) (@midweste,
  OPEN as of 2026-04-26). New ``_build_drawer()`` helper + ``add_drawers()``
  batch-insert path; ``process_file`` hands the full chunk list to
  ``add_drawers`` instead of looping per-chunk. Hoists ``datetime.now()``
  and ``os.path.getmtime()`` to file-level (2 syscalls per file instead
  of 2N). Reported 10–30× mining speedup upstream. Fork-side resolution
  preserved fork's existing ``DRAWER_UPSERT_BATCH_SIZE=1000``; aliased
  upstream's ``CHROMA_BATCH_LIMIT`` to it. Becomes a no-op when #1085
  merges to develop and we next sync.

  *Upstream:* [PR #1085](https://github.com/MemPalace/mempalace/pull/1085) (OPEN)
  *Files:* `mempalace/miner.py`


## [2026-04-25]


### Added


- **Phases A–C of the checkpoint collection split** ([`e266365`](https://github.com/jphein/mempalace/commit/e266365))
  New ``mempalace_session_recovery`` collection adapter
  (``_SESSION_RECOVERY_COLLECTION`` + ``get_session_recovery_collection``
  in ``palace.py``); ``tool_diary_write`` routes ``topic in _CHECKPOINT_TOPICS``
  to it. New ``mempalace_session_recovery_read`` MCP tool reads recovery
  collection only with optional filters (session_id, agent, since,
  until, wing, limit). Promoted from "future work" to "necessary" by
  the same-day Cat 9 A/B (``kind=all`` 632 tokens/Q vs ``kind=content``
  3 tokens/Q on the canonical 151K-drawer palace). Design doc at
  ``docs/superpowers/specs/2026-04-25-checkpoint-collection-split.md``.

  *Tests:* 12 across test_session_recovery.py + TestCheckpointRouting + TestSessionRecoveryRead
  *Files:* `mempalace/palace.py`, `mempalace/mcp_server.py`, `tests/test_session_recovery.py`, `tests/test_mcp_server.py`, `website/reference/mcp-tools.md`


### Fixed


- **Gate quarantine_stale_hnsw to once-per-palace-per-process** ([`70c4bc6`](https://github.com/jphein/mempalace/commit/70c4bc6))
  ``make_client()`` previously invoked ``quarantine_stale_hnsw`` on every
  reconnect; under steady write load the proactive check kept firing,
  racking up ``.drift-*`` directories every 10–30 minutes. New
  ``ChromaBackend._quarantined_paths: set[str]`` caps it to one fire on
  first open per palace per process. Real cold-start drift still caught
  (replicated/restored palace); real runtime errors still caught via
  palace-daemon's ``_auto_repair``, which calls ``quarantine_stale_hnsw``
  directly and bypasses this gate.

  *Tests:* 2 in test_backends.py (single-fire-per-palace, per-palace independence)
  *Upstream:* [PR #1173](https://github.com/MemPalace/mempalace/pull/1173) (MERGED)
  *Files:* `mempalace/backends/chroma.py`, `tests/test_backends.py`, `tests/conftest.py`


- **palace_graph.build_graph skips None metadata** ([`5fd15db`](https://github.com/jphein/mempalace/commit/5fd15db))
  ``palace_graph.py:95`` was calling ``meta.get("room", "")`` unconditionally;
  ChromaDB returns ``None`` for legacy/partial-write drawers, taking out
  every consumer of ``build_graph`` (graph_stats, find_tunnels, traverse,
  the daemon's ``/stats``). Caught by palace-daemon's ``verify-routes.sh``
  smoke test. Same family as upstream's #999 None-metadata audit, in a
  read path the audit didn't reach.

  *Upstream:* [PR #1201](https://github.com/MemPalace/mempalace/pull/1201) (MERGED)
  *Files:* `mempalace/palace_graph.py`


- **kind= filter on search_memories excludes Stop-hook checkpoints (transitional)** ([`f9f5cc4`](https://github.com/jphein/mempalace/commit/f9f5cc4))
  Three values: ``"content"`` (default, excludes), ``"checkpoint"``
  (recovery/audit only), ``"all"`` (no filter). Two same-day architecture
  corrections: (a) the where-clause filter (``topic $nin [...]``) tripped
  a chromadb 1.5.x filter-planner bug; the exclusion moved to post-filter
  only ([398f42f](https://github.com/jphein/mempalace/commit/398f42f));
  (b) vector top-N is dominated by checkpoints on this palace, so
  post-filter alone empties the result set without aggressive over-fetch
  — pull size raised to ``max(n*20, 100)`` for ``kind != "all"`` (this commit).
  Safety net during the transition; once Phase D ships and existing
  checkpoints migrate, the post-filter and over-fetch hack become
  deletable.

  *Tests:* 9 in TestCheckpointFilter
  *Files:* `mempalace/searcher.py`, `mempalace/mcp_server.py`, `tests/test_searcher.py`


---

## Merged into upstream (recent)


*Trim entries from this list once they're more than ~30 days old.*


*See CHANGELOG.md (upstream) for the full released history.*


- [PR #1173](https://github.com/MemPalace/mempalace/pull/1173) — quarantine_stale_hnsw on make_client + cold-start gate + integrity sniff — 2026-04-26
- [PR #1177](https://github.com/MemPalace/mempalace/pull/1177) — `.blob_seq_ids_migrated` marker guard (closes #1090) — 2026-04-26
- [PR #1198](https://github.com/MemPalace/mempalace/pull/1198) — _tokenize None-document guard in BM25 reranker — 2026-04-26
- [PR #1201](https://github.com/MemPalace/mempalace/pull/1201) — palace_graph.build_graph skips None metadata — 2026-04-26
- [PR #659](https://github.com/MemPalace/mempalace/pull/659) — diary `wing` parameter — 2026-04-23
- [PR #661](https://github.com/MemPalace/mempalace/pull/661) — graph cache with write-invalidation — 2026-04-22
- [PR #673](https://github.com/MemPalace/mempalace/pull/673) — deterministic hook saves — 2026-04-22
- [PR #1021](https://github.com/MemPalace/mempalace/pull/1021) — Claude Code 2.1.114 stdout/silent_save fixes — 2026-04-22
- [PR #999](https://github.com/MemPalace/mempalace/pull/999) — None-metadata guards across read paths — 2026-04-18
- [PR #1000](https://github.com/MemPalace/mempalace/pull/1000) — quarantine_stale_hnsw shipped — v3.3.2
- [PR #1023](https://github.com/MemPalace/mempalace/pull/1023) — PID file guard prevents stacking mine processes — v3.3.2
- [PR #681](https://github.com/MemPalace/mempalace/pull/681) — Unicode checkmark → ASCII — v3.3.2
