# CLAUDE.md — memorypalace

## What This Is

JP's fork of [milla-jovovich/mempalace](https://github.com/milla-jovovich/mempalace) — a local AI memory system using ChromaDB for verbatim storage and semantic search.

- **Fork**: `jphein/mempalace` (origin) / `milla-jovovich/mempalace` (upstream)
- **Version**: upstream shipped v3.3.2 on 2026-04-21 (includes our #681/#1000/#1023) and v3.3.3 on 2026-04-24 (includes our #659/#1021). Main merged upstream/develop through 2026-05-03 (commit `1888b67`) so fork runs post-v3.3.3 code; upstream's `chore/release-3.3.4-prep` is in flight.
- **Python**: venv at `./venv/`, editable install with dev deps
- **Palace data**: `~/.mempalace/palace` (ChromaDB) + `~/.mempalace/config.json`

## Key Files

- `~/Projects/mempalace.yaml` — **do not delete**. Mining config with wing/room definitions. Regenerate with `mempalace init ~/Projects --yes` if lost.
- `~/.mempalace/config.json` — topic wings and hall keywords, customized for JP's domains (infrastructure, development, tools, creative, projects, system).
- `~/.mempalace/palace/` — ChromaDB vector store. The actual data.
- `~/.mempalace/hook_state/` — stop hook session tracking.

## Development

```bash
source venv/bin/activate
python -m pytest tests/ -q              # ~1096 tests (benchmarks deselected)
mempalace status                         # check palace state
mempalace search "query"                 # test search
python -m mempalace.mcp_server           # run MCP server standalone
```

Ruff for linting (`ruff check`), line length 100, target Python 3.9.

## Fork Changes (still ahead of upstream after v3.3.2 merge)

1. **feat: bulk_check_mined()** — paginated pre-fetch of all source_file/mtime pairs for concurrent mining (fork-only; independent of the mtime comparison fix, which has since been upstreamed)
2. **feat: similarity threshold** — `max_distance` parameter in search, default 1.5 cosine distance in MCP
3. ~~**feat: hooks_cli silent save**~~ — **merged upstream via #673 on 2026-04-22.** No longer fork-ahead.
4. **feat: `mempal_save_hook.sh` Python auto-detection** — checks `MEMPAL_PYTHON` env var → repo venv → system `python3`; no hardcoded path required
5. **fix: convo_miner wing assignment** — `_wing_from_transcript_path()` extracts project name from Claude Code transcript path
6. ~~**perf: graph cache**~~ — **merged upstream via #661 on 2026-04-22.** No longer fork-ahead.
7. **perf: L1 importance pre-filter** — `_fetch_drawers()` tries `importance >= 3` first, falls back to full scan only if < 15 results
8. **fix: MCP stale HNSW index** — `_get_client()` detects external writes via mtime (not just inode), `mempalace_reconnect` MCP tool
9. ~~**fix: diary wing assignment**~~ — **merged upstream via #659 on 2026-04-23.** No longer fork-ahead.
10. ~~**fix: `.blob_seq_ids_migrated` marker**~~ — **merged upstream via #1177 on 2026-04-26.** No longer fork-ahead.
11. ~~**feat: `quarantine_stale_hnsw()`**~~ — **merged upstream via #1000 in v3.3.2.** No longer fork-ahead.
12. **feat: search warnings + sqlite BM25 top-up** — `search_memories()` returns `warnings: [...]` and `available_in_scope: N` whenever the vector path underdelivers (sparse HNSW after repair, `#951` filter-planner failure, drift). Fallback promotes BM25-ranked sqlite candidates tagged `matched_via: "sqlite_bm25_fallback"`. Closes the "silent 0-hit when data is in sqlite" failure mode. CLI `search()` delegates to `search_memories()` so both paths share the fallback.
13. ~~**fix: stop_hook_active guard**~~ — **merged upstream via #1021 on 2026-04-22.** No longer fork-ahead.
14. ~~**fix: `_output()` stdout routing**~~ — **merged upstream via #1021 on 2026-04-22.** No longer fork-ahead.
15. ~~**fix: `_get_client()` get-then-create guard**~~ — **merged upstream via #1262 (Legion345) on 2026-05-01 + the MCP-server-side companion #1289 (igorls) on 2026-05-01 + the `embedding_function=` plumbing in #1303 (igorls) on 2026-05-01**. All three landed in the develop sync on 2026-05-03. The fork-only `_get_session_recovery_collection` (introduced in row 23) still uses the older `get_or_create_collection` pattern; theoretical SIGSEGV exposure on legacy recovery collections only — tracked as a follow-up. No longer fork-ahead in the canonical paths.
16. **perf: `miner.status()` paginated `col.get()`** — upstream's single `col.get(limit=total)` hits SQLite's max-variable limit on palaces with many thousands of drawers; fork paginates in 10 K-drawer batches.
17. **feat: configurable chunking parameters** — `chunk_size`, `chunk_overlap`, `min_chunk_size` exposed via `MempalaceConfig` properties (defaults 800 / 100 / 50). **Update 2026-05-03 (commit `6ce37c0`):** the three keys are intentionally NOT written to `config.json` by `cfg.init()`. Earlier wording above was wrong — writing the miner.py defaults as materialized values into config.json broke `convo_miner.py:427-431`'s "user has tuned this" detection (`_file_config.get("min_chunk_size") is None` ⇔ "untuned"), which silently overrode convo_miner's stricter 30-char floor and dropped legitimate short conversation exchanges on any user who'd ever run `mempalace init`. JP's pre-row-17 config.json never had these keys so he never saw it; surfaced by a pytest fixture leak (`tests/conftest.py:21-27` HOME redirect → polluting test writes default config in session-tmp `~/.mempalace/` → next test reads `min_chunk_size: 50` from there). Fix is module-default-only; properties at `config.py:204-216` already supply the right fallback values via `.get(key, default)`. Same fix pushed to #1024's PR branch as `df9187c`.
18. ~~**fix: PID file guard prevents stacking mine processes**~~ — **merged upstream via #1023 in v3.3.2.** Includes the Windows `os.kill` → `OpenProcess` cross-platform fix. No longer fork-ahead.
19. **fix: `.claude-plugin/` venv-aware Python resolution** — hooks (`mempal-stop-hook.sh`, `mempal-precompact-hook.sh`) and `.mcp.json` resolve Python in this order: `MEMPALACE_PYTHON` env → `$PLUGIN_ROOT/venv/bin/python3` → system `python3`. Upstream's `5fe0c1c` + `be9214a` (fatkobra) and `9f5b8f5` (Pim) regressed to PATH-only lookups and bare `"mempalace-mcp"` command, which break editable dev installs where `mempalace`/`mempalace-mcp` only live in the repo venv. Documented here so future `upstream/develop` merges surface the conflict rather than silently re-regress. Attempted via #1115 on 2026-04-22; withdrew 2026-04-23 as premature pending #1069 arbitration — CI correctly caught the #942 PATH-only contract violation. Re-submit after bensig's direction on #1069.
20. ~~**fix: `_tokenize` None-document guard**~~ — **merged upstream via #1198 on 2026-04-26.** No longer fork-ahead.
21. ~~**feat: `kind` filter on `search_memories` excludes Stop-hook checkpoints by default**~~ — **deleted 2026-04-27 as transitional/inert.** The structural split (Phases A–E, see row 23) moved all checkpoints to `mempalace_session_recovery`; production has 0 checkpoints in `mempalace_drawers`, so the filter was filtering nothing. Removed `_CHECKPOINT_TOPICS` from `searcher.py`, `_is_checkpoint_drawer`, `_apply_kind_text_filter`, the `max(n*20, 100)` over-fetch hack (back to `n_results * 3`), and the `kind=` parameter on `search_memories` / `mempalace_search` / palace-daemon `/search` & `/context`. Write-side `_CHECKPOINT_TOPICS` (topic→collection routing in `tool_diary_write`) lives in `palace.py` now alongside `_SESSION_RECOVERY_COLLECTION`. `TestCheckpointFilter` (9 tests) deleted.
22. ~~**fix: `palace_graph.build_graph` skips None metadata**~~ — **merged upstream via #1201 on 2026-04-26.** No longer fork-ahead.

23. **feat: checkpoint collection split — phases A–C** (commit `e266365`, 2026-04-25) — Promoted from "future work" to "necessary" by 2026-04-25 Cat 9 A/B (`kind=all` 632 tokens/Q vs `kind=content` 3 tokens/Q on the canonical 151K palace; over-fetch=100 inadequate, structural fix non-optional). **Phase A:** new `_SESSION_RECOVERY_COLLECTION` constant + `get_session_recovery_collection()` in `palace.py` (mirrors `get_collection`'s shape — cosine, num_threads=1). **Phase B:** `tool_diary_write` routes `topic in _CHECKPOINT_TOPICS` to the dedicated `mempalace_session_recovery` collection, everything else stays in `mempalace_drawers`; new `_get_session_recovery_collection()` in `mcp_server.py` with parallel cache. **Phase C:** new `tool_session_recovery_read` MCP handler reads recovery collection only with optional filters `session_id`, `agent`, `since`, `until`, `wing`, `limit`; `session_id` added as optional metadata field on `tool_diary_write` so the new tool can filter by Claude Code session. Registered in `TOOLS` dict, documented in `website/reference/mcp-tools.md`. 12 new tests across `tests/test_session_recovery.py` + `TestCheckpointRouting` + `TestSessionRecoveryRead`. Design + plan at `docs/superpowers/specs/2026-04-25-checkpoint-collection-split.md` and `docs/superpowers/plans/2026-04-25-checkpoint-collection-split-impl.md`. **Phases D (data migration of ~640 existing checkpoints out of main collection) and E (palace-daemon `lifespan` auto-migrate + `mempalace repair --mode reorganize`) deferred** — multi-day work, gated on a separate go-ahead. Once D lands and the canonical-palace re-run shows the predicted `kind=all` ≈ `kind=content` token convergence, the `kind=` post-filter and over-fetch hack become deletable. **Update 2026-04-26:** phase D shipped — `migrate_checkpoints_to_recovery()` in `mempalace/migrate.py`, idempotent walk that moves topic in `_CHECKPOINT_TOPICS` drawers from main → recovery while preserving IDs and metadata. Wired into `mempalace repair --mode reorganize` (CLI dispatch in `cli.py` chooses between `rebuild` (HNSW from sqlite) and `reorganize` (this new path)). PreCompact hook also incorporated — `hook_precompact` now writes a recovery marker via `_save_diary_direct` mirroring Stop, so a context-compaction event leaves a queryable timestamp in the recovery collection. 6 new migration tests in `test_migrate.py::TestMigrateCheckpointsToRecovery`. **Phase E shipped** in palace-daemon commit [`034023c`](https://github.com/jphein/palace-daemon/commit/034023c) on 2026-04-26 — `lifespan` calls `migrate_checkpoints_to_recovery()` in an executor on startup, gated behind `PALACE_AUTO_MIGRATE_CHECKPOINTS=1` (default on), with `ImportError` fallthrough so upstream-shaped installs without `mempalace.migrate` still start cleanly. Canonical 151K palace migrated 667 checkpoints on 2026-04-26 10:24:09 PDT. **Cleanup phase shipped 2026-04-27** — empirical check on production showed 0 checkpoints in `mempalace_drawers` (763 in `mempalace_session_recovery`), so the kind= filter was provably inert. Deleted in row 21 above.

27. **perf: batch ChromaDB inserts in miner (cherry-pick of upstream #1085)** (commit `6be6fff`, 2026-04-26) — Cherry-picked @midweste's [#1085](https://github.com/MemPalace/mempalace/pull/1085) "batch ChromaDB inserts in miner — 10-30x faster mining". Upstream PR #1085 is still **OPEN** as of 2026-04-26 (created 2026-04-21, base=develop, not yet merged) — verified via `gh pr view 1085 --repo MemPalace/mempalace`. We cherry-picked the commit ahead of merge so the fork can use it now; this row clears when #1085 merges into develop and we next sync. We don't file a competing fork-side PR — the proposal is @midweste's. New `_build_drawer()` helper builds id+document+metadata in one shot; new `add_drawers()` batch-insert function takes the full chunk list and sub-batches at `DRAWER_UPSERT_BATCH_SIZE` (one chromadb upsert + one ONNX embedding forward-pass per sub-batch instead of per-chunk). `process_file` now calls `add_drawers` directly. Hoists `datetime.now()` and `os.path.getmtime()` to file-level (2 syscalls per file instead of 2N). **Conflict resolution:** fork already had a fork-only `_build_drawer_metadata` + an outer batch loop in `process_file`; upstream's clean structure supersedes both. Kept fork's `DRAWER_UPSERT_BATCH_SIZE=1000` (more conservative than upstream's 5000 for embedding-pass memory headroom); aliased upstream's `CHROMA_BATCH_LIMIT` to point at it so any code/test referencing either name sees the same value. 74/74 miner+convo_miner tests pass; full suite 1366/1366. Becomes a no-op when #1085 merges into upstream develop and we next sync develop→main.

26. ~~**fix: integrity gate in `quarantine_stale_hnsw`**~~ — **merged upstream via #1173 on 2026-04-26** (alongside the cold-start gate). No longer fork-ahead.

25. **feat: surface `drawer_id` in search + diary + recovery payloads** (commit `9a8bb77`, 2026-04-26) — ChromaDB's primary key was always returned by `query()` and `get()` but never plumbed into result-building loops; consumers (e.g. `familiar.realm.watch`'s citation-popover loop) couldn't link a hit back to the underlying drawer. Three call sites updated for parity: `searcher.search_memories` (vector path + sqlite BM25 fallback), `mcp_server.tool_session_recovery_read`, `mcp_server.tool_diary_read`. Defensive zip with id-pad: production chromadb always returns ids, but several test mocks in `test_searcher.py` omit them — pad with `None` when absent so existing fixtures keep working without touching N tests. New integration test `test_results_include_drawer_id` (seeded-collection, asserts non-empty `drawer_id` on every hit and the `drawer_*` prefix shape from conftest); session-recovery test extended to assert `drawer_id` is present and starts with `diary_`. `website/reference/mcp-tools.md` Return-shape docs updated for `mempalace_search`, `mempalace_diary_read`, `mempalace_session_recovery_read`. **Searcher slice deferred to upstream [#1219](https://github.com/MemPalace/mempalace/pull/1219) (@pepo72)** — once that merges into develop and we next sync, our `searcher.search_memories` diff vanishes. Diary + session-recovery slices remain fork-only; will file as a small follow-up PR after #1219 lands so first-filer credit stays with pepo72.

24. ~~**fix: gate `quarantine_stale_hnsw` to cold-start, not every reconnect**~~ — **merged upstream via #1173 on 2026-04-26** (with cold-start gate + integrity sniff packaged together). No longer fork-ahead.

28. **feat: canonical YAML manifest + renderer for fork-ahead docs** (commit `5a01aec`, 2026-04-26) — `docs/fork-changes.yaml` is now the canonical source for the fork-ahead narrative. `scripts/render-docs.py` regenerates `FORK_CHANGELOG.md` from it; the README fork-change-queue table, this file's row inventory (rows 1–28), and `scratch/promises.md` are still hand-maintained but planned for marker-based render insertion in a follow-on commit. `scripts/check-docs.sh` extended with a render-parity check (calls `render-docs.py --check`) plus the existing test-count / commit-hash / upstream-PR-state checks. Researched towncrier, scriv, git-cliff, antsibull-changelog before going custom — none do single-source → multi-target render in this shape (keep-a-changelog#230 has been asking for this since 2018). Documentation workflow now lives in the **Documentation maintenance** section above.

29. **perf: hoist `CLOSET_RANK_BOOSTS` to module level + record VecRecall ablation finding** (commits `f558d3c` → `3cb03f3`, 2026-04-27) — Two-step refactor: first hoist the closet-boost ranking constants from inside `search_memories` to module scope so they can be tuned externally (env var, config flag, or in-process patch for A/B benchmarking) without touching the function. Then run a 12-probe A/B against the canonical 151K palace, with default-vs-zeroed boosts. Result: closet boost fires on ~20% of result rows, concentrated in queries whose answer lives in mined files; closets are sparse on chat-transcript queries (most fork-side decisions). When the boost fired, it re-ordered chunks within a single source file rather than displacing right answers with wrong ones — VecRecall's critique ([discussions/1129](https://github.com/MemPalace/mempalace/discussions/1129), "org-layer in retrieval path drops R@5") did not reproduce on this corpus. Findings live in the comment block above the constants in `searcher.py` so future-us doesn't have to re-run the experiment. The hoist itself is benign and could be a small upstream PR; the empirical comment is fork-specific narrative.

30. **fix: scrub embedded API key from `.claude-plugin/` plugin manifests** (commits `8119149` → `9f91e18`, 2026-04-27) — Two of our committed plugin manifests (`.claude-plugin/.mcp.json`, `.claude-plugin/hooks/hooks.json`) shipped with my real (rotated) API key embedded as a literal in the manifest's `env` block, plus my homelab daemon URL. First commit reverted both to upstream-shape (no env block, in-process MCP); second commit restored daemon-routing on `.mcp.json` (URL + path) but **without** the embedded credential — `PALACE_API_KEY` now inherits at runtime from `~/.claude/settings.local.json`'s `env` block (which Claude Code passes to spawned MCP servers and hooks). Net: my fork-main carries the daemon-routed config matching production deployment; the literal credential lives one place only (gitignored `settings.local.json`). The literal is still in commit history at `c09582c` and earlier — destructive-history-rewrite is not worth the cost on an already-rotated key. Companion to palace-daemon [PR #12](https://github.com/rboarescu/palace-daemon/pull/12) which fixes the same class of embedded-default in `clients/palace-mode`.

31. **palace-daemon upstream PR push** (2026-04-27) — Filed seven small/medium PRs against `rboarescu/palace-daemon` covering most of the fork-ahead daemon work: [#7](https://github.com/rboarescu/palace-daemon/pull/7) `limit=` honored fix, [#8](https://github.com/rboarescu/palace-daemon/pull/8) `_canonical_topic` synonym rewrite, [#9](https://github.com/rboarescu/palace-daemon/pull/9) `verify-routes.sh` smoke test, [#10](https://github.com/rboarescu/palace-daemon/pull/10) portable-path fix in dispatcher (real bug — broke for everyone except me), [#11](https://github.com/rboarescu/palace-daemon/pull/11) `event-log-frame.md` architectural reference doc, [#12](https://github.com/rboarescu/palace-daemon/pull/12) strip embedded API-key/URL defaults from `palace-mode` (security hygiene), [#13](https://github.com/rboarescu/palace-daemon/pull/13) `GET /graph` endpoint with design doc. Pending follow-ups: `GET /viz` (depends on #13), `clients/mempal-fast.py` refactor; needs-generalization items: `palace-mode install` subcommand, `auto-repair-if-empty.sh`, `deploy.sh`. Daemon README has the full open-PR + needs-generalization breakdown.

32. **feat: `hook_verbatim_mode` config flag for verbatim transcript ingest** (commit `ef98961`, 2026-05-05) — Backs JP's 2026-05-05 question "we're not missing any tool calls or anything, right?" without disturbing the upstream default. `MempalaceConfig.hook_verbatim_mode` (reads `hooks.verbatim_mode` from `config.json`, defaults `False`) toggles a `verbatim` parameter plumbed through `normalize() → _try_normalize_json → _try_claude_code_jsonl → _extract_content → _format_tool_use / _format_tool_result / strip_noise`. When `True`: system tags + hook chrome are kept (`strip_noise` becomes a passthrough); Bash commands and unknown-tool JSON inputs aren't 200-char truncated; Bash output isn't head/tail-collapsed; Grep/Glob match lists aren't capped; Read/Edit/Write results are included rather than omitted; unknown-tool output isn't byte-capped. Daemon picks it up transparently — daemon's `/mine` spawns `mempalace mine` subprocess which reads the flag from `config.json` directly. Other transcript schemas (Codex, Gemini, claude.ai, ChatGPT, Slack) don't truncate so the flag is a no-op for them. 9 new tests in `tests/test_normalize.py::TestVerbatimMode` — strip_noise passthrough, Bash + unknown-tool input no-truncation, Read/Edit/Write result inclusion, Bash head/tail no-collapse, Grep/Glob match no-cap, unknown-tool byte no-cap, full JSONL round-trip, default-off contract, config readback. Suite 1562 passed.

### Closed by jphein-with-triage (this fork's maintainer-granted perms)

- **#622** (auto-memory conflict) closed 2026-04-26 — architectural concern fully resolved by #673 (silent saves, default since v3.3.0); the LLM is no longer in the save path so there's nothing to compete with auto-memory.

### Merged into upstream (post-v3.3.1)

- epsilon mtime comparison (upstream PR #610, merged 2026-04-12 by Arnold Wender — their threshold is 0.001, ours was 0.01, semantically equivalent)
- `None`-metadata guards across 8 read-path loops — searcher.py, miner.status, 4 mcp_server handlers (#999, merged 2026-04-18)
- Unicode checkmark → ASCII for Windows encoding (#681, shipped in v3.3.2)
- `quarantine_stale_hnsw()` for HNSW/sqlite drift (#1000, shipped in v3.3.2)
- PID file guard prevents stacking mine processes, with Windows cross-platform `os.kill` fix (#1023, shipped in v3.3.2)
- Graph cache with write-invalidation — `build_graph()` module-level cache with 60s TTL, `threading.Lock`, `invalidate_graph_cache()` on writes (#661, merged 2026-04-22)
- Deterministic hook saves — silent mode via direct Python API call to `tool_diary_write()`, plain-text save, marker advances only after confirmed write, `systemMessage` terminal notification (#673, merged 2026-04-22). Replaces the block-mode "ask AI to save" pattern that could silently drop entries.
- Hook `silent_save` guard + `_output()` stdout routing — silent-mode skips `stop_hook_active` guard so Claude Code 2.1.114 plugin dispatch keeps firing; `_output()` reuses already-loaded `mcp_server`'s `_REAL_STDOUT_FD` or writes directly to fd 1 to avoid cold-import side effects (#1021, merged 2026-04-22)
- Diary wing routing — `tool_diary_write` / `tool_diary_read` accept an optional `wing` parameter; stop hook derives project wing from Claude Code transcript path via `_wing_from_transcript_path()` (#659, merged 2026-04-23)
- `quarantine_stale_hnsw()` called proactively in `make_client()` with cold-start gate + integrity-sniff (kept healthy 253MB segments in place during async-flush drift) — threshold 3600→300s (#1173, merged 2026-04-26)
- `.blob_seq_ids_migrated` marker — skip `sqlite3.connect()` after first successful 0.6→1.5 migration so subsequent `PersistentClient` opens don't segfault (#1177, merged 2026-04-26, closes #1090)
- `_tokenize` None-document guard in BM25 reranker — closes the gap upstream's #999 None-metadata audit left in `_hybrid_rank → _bm25_scores → _tokenize` (#1198, merged 2026-04-26)
- `palace_graph.build_graph` skips None metadata — same family as #999 / #1094 in a read path the audit didn't reach; daemon `/stats` was 500-ing on a single legacy drawer (#1201, merged 2026-04-26)

### Merged into upstream v3.3.0

- BLOB seq_id migration repair (#664), --yes flag (#682), Unicode sanitize_name (#683), VAR_KEYWORD kwargs (#684), MCP tools/export (via #667)

### Pulled in from upstream v3.3.1 (merged 2026-04-18)

- Multi-language entity detection (Portuguese, Russian, Italian, Hindi, Indonesian, Chinese); BCP-47 case-insensitive locales; script-aware word boundaries for Devanagari/Arabic/Hebrew/Thai
- UTF-8 encoding on `Path.read_text()` (#946) — fixes Windows GBK/non-UTF-8 locales
- Non-blocking precompact hook (#863) — replaces our fork's blocking precompact
- Basic `silent_save` honoring in stop hook (#966) — narrower than our fork's deterministic-save architecture, so we keep #673's version

### Pulled in from upstream/develop (merged 2026-04-19)

- RFC 002 §9 scaffolding: `BaseSourceAdapter`, `PalaceContext`, registry, transforms (`mempalace/sources/`) — #1014
- `chromadb >=1.5.4,<2` — Python 3.13/3.14 compat, version cap guards future major breakage — #1010
- `Layer3.search_raw` None guard — #1013
- Sweeper + tandem transcript safety net — prevents silent drop of `.jsonl` files — #998
- `_validate_where()` operator validator (RFC 001 §1.4) — unknown operators raise `UnsupportedFilterError` instead of silently dropping — #995
- RFC 002 spec docs (`docs/rfcs/002-source-adapter-plugin-spec.md`) — #990
- Landing page redesign — #984
- `sweep` CLI command added alongside existing `export`
- `.jsonl` added to `READABLE_EXTENSIONS` — same SHA (560fdbd), upstream-authored, not a fork contribution. Related: upstream also raised `MAX_FILE_SIZE` 10MB → 500MB in d137d12.

### Superseded by upstream

- Hybrid keyword fallback (#662) — upstream shipped Okapi-BM25
- Batch ChromaDB writes (#629 partial) — upstream has file-level locking
- Inline transcript mining in hooks — upstream uses `mempalace mine` in background

## Upstream PRs

As of 2026-05-06: 20 merged ours (the +1 vs 2026-05-03 is the #1286→#1377 outcome described below), 6 open ours, 11 closed. PRs target `develop`. Fork `main` tracks `upstream/develop` (synced 2026-05-03 to commit `1888b67`; brought in #1262/#1289/#1303 — get-then-create + EF plumbing on `_get_collection`, clearing row 15; #1287 — HNSW divergence floor scales with sync_threshold; #1288 — repair max-seq-id BLOB heuristic; #1306 — `candidate_strategy="union"` BM25∪vector rerank pool; #1322 — quarantine_stale_hnsw wired into `_client()`; #1323 — case-insensitive agent name in diary (#1243); #1325 — security: omit absolute paths from MCP responses; plus #1313 honor --palace, #1314 kg_add temporal forwarding, #1244 cmd_compress→closets routing, #1234 Gemini CLI normalize, #1233 privacy consent gate). #1286 was closed 2026-05-06 by @igorls because the remote branch had drifted to carry fork-only files + version-bump-without-plugin-manifest skew + convo_miner.py regressions; he extracted the surgical retry-once into [#1377](https://github.com/MemPalace/mempalace/pull/1377) (~25 LOC + 2 tests, MERGED for v3.3.5) with `Co-authored-by: Jeffrey Hein <jp@jphein.com>`. Lessons re-confirmed: PR branches must stay surgical (memory `feedback_main_first_dont_drift_to_pr_branch.md`, `feedback_fork_readme_handling.md`).

| PR | Status | Description |
|----|--------|-------------|
| #660 | open (`MERGEABLE`, waiting review) | L1 importance pre-filter |
| #1005 | open (CI green all platforms, Dialectician-acked, waiting maintainer) | Warnings + sqlite BM25 top-up when vector underdelivers (never silent miss) |
| #1024 | open (CI green all platforms, qodo-acked, waiting maintainer) | Configurable chunk_size, chunk_overlap, min_chunk_size |
| #1086 | open (`MERGEABLE`) | `mempalace export` CLI wrapper for `export_palace()` (fork-ahead Row 1) |
| #1087 | open, **rewritten 2026-04-26** per @igorls's review | `mempalace purge --wing/--room` CLI. Rewrite (commit `e9a59de`) replaces nuke-and-rebuild with `collection.delete(where=...)` after tracing #521's stack — the race is on the upsert path, not delete-by-where. Preserves embedding fn, no rmtree window, routes through `ChromaBackend`, reuses `confirm_destructive_action`. End-to-end test added. |
| #1094 | open (`CLEAN`, 6/6 CI green) | Coerce `None` metadatas → `{}` at `ChromaCollection.query/.get` boundary (closes #1020) |
| #1142 | open (filed 2026-04-23) | `docs/RELEASING.md` with `mempalace-mcp` pre-release grep — fulfills #1093's release-checklist proposal, accepted by @bensig 2026-04-23 via email |
| #1262 | **merged** 2026-05-01 (Legion345, shepherded) | Get-then-create guard at chromadb backend boundary — path 1 of #1089. Cleared fork-ahead Row 15 via develop sync 2026-05-03. |
| #1286 | **closed** 2026-05-06 | Closed by @igorls — remote PR branch had drifted to carry fork-only files (`FORK_CHANGELOG.md`, `docs/superpowers/**`, `scripts/{deploy,preflight,rebase-on-develop}.sh`, version bumps without matching plugin manifests) and the convo_miner.py changes regressed `test_convo_mining` + `test_mine_convos_rebuilds_stale_drawers_after_schema_bump` on Linux/macOS/Windows. Surgical retry-once extracted as #1377. |
| #1377 | **merged** 2026-05-06 (igorls, JP credited as co-author) | Surgical `_get_collection` retry-once + log-on-failure, ~25 LOC + 2 unit tests. Composes with #1322 — clearing `_client_cache` between attempts forces `make_client()` to re-run `quarantine_stale_hnsw`, so transient-stale-handle case self-heals on second call. Ships in v3.3.5 (due 2026-05-08). Originated from this fork via #1286; igorls extracted with `Co-authored-by: Jeffrey Hein <jp@jphein.com>`. |
| #1289 | **merged** 2026-05-01 | MCP-server-side companion to #1262 — `_get_collection(create=True)` uses try `get_collection` / except `NotFoundError` → `create_collection`. Stop-hook diary path was the crash route #1089 was filed for. |
| #1303 | **merged** 2026-05-01 | Pass `embedding_function=` on collection reopen in `mcp_server._get_collection` (#1299). Without it, ChromaDB 1.x falls back to its `DefaultEmbeddingFunction` whose lazy ONNX provider selection SIGSEGVs on Py3.14 / Apple Silicon. |
| #1173 | **merged** 2026-04-26 | `quarantine_stale_hnsw()` in `make_client()` + cold-start gate + integrity sniff-test; threshold 3600→300s. Saved healthy 253MB segments from being quarantined under async-flush drift. |
| #1177 | **merged** 2026-04-26 | `.blob_seq_ids_migrated` marker guard — skip `sqlite3.connect()` on already-migrated palaces. Closes #1090. |
| #1198 | **merged** 2026-04-26 | `_tokenize` None-document guard — closes the gap upstream's #999 None-metadata audit left in BM25 helpers. Three regression tests in `TestBM25NoneSafety`. |
| #1201 | **merged** 2026-04-26 | `palace_graph.build_graph` skips None metadata — daemon `/stats` was 500-ing on a single legacy drawer; same gap class as #999 / #1094 in a read path the audit didn't reach. |
| #1171 | **closed** 2026-04-25 | Cross-process write lock at `ChromaCollection` adapter — superseded by [#976](https://github.com/MemPalace/mempalace/pull/976) (`mine_global_lock` at the right layer) plus this fork's daemon-strict architecture. |
| #659 | **merged** 2026-04-23 | Diary wing parameter (`tool_diary_write` / `tool_diary_read` accept `wing`, hook derives from transcript path) |
| #661 | **merged** 2026-04-22 | Graph cache with write-invalidation |
| #673 | **merged** 2026-04-22 | Deterministic hook saves (broader than upstream's #966) — config-flag-gated, strictly safer save semantics |
| #1021 | **merged** 2026-04-22 | Hook stdout routing + `silent_save` guard fixes for Claude Code 2.1.114 |
| #681 | **merged** in v3.3.2 (2026-04-21) | Unicode checkmark → ASCII (#535) |
| #1000 | **merged** in v3.3.2 (2026-04-21) | `quarantine_stale_hnsw()` for HNSW/sqlite drift |
| #1023 | **merged** in v3.3.2 (2026-04-21) | PID file guard prevents stacking mine processes + Windows `os.kill` cross-platform fix |
| #999 | **merged** 2026-04-18 | `None`-metadata guards in `searcher.py`, `miner.status()`, and 4 `mcp_server.py` handlers |
| #664 | **merged** | BLOB seq_id migration repair |
| #682 | **merged** | --yes flag for init (#534) |
| #683 | **merged** | Unicode sanitize_name (#637) |
| #684 | **merged** | VAR_KEYWORD kwargs check (#572) |
| #635 | **merged** via #667 | New MCP tools, export |
| #629 | **closed** | Superseded — upstream shipped batching + file locking |
| #632 | **closed** | Superseded — `--version`, `purge`, `repair` all shipped in v3.3.0 |
| #662 | **closed** | Hybrid search fallback (superseded by upstream BM25) |
| #738 | **closed** | Docs: MCP tools reference (stale after v3.3.0) |
| #663 | **closed** | Stale HNSW mtime detection (upstream wrote #757) |
| #626 | **closed** | Split into #681-684 |
| #633 | **closed** | Resubmitted as #673 |
| #1115 | **closed** 2026-04-23 | `.claude-plugin/` venv-aware Python + MCP — withdrew as premature pending #1069 arbitration; CI correctly caught the #942 PATH-only contract violation |
| #1146 | **closed** 2026-04-24 | #1145 bugs 1+2 — duplicate; @igorls filed [#1147](https://github.com/MemPalace/mempalace/pull/1147) 4 min later with cleaner `.claude/projects/-` primary regex. Fork main keeps `34e36ae` for local use until upstream merges #1147, then we merge develop→main and take upstream's version. |

## Two-Layer Memory Architecture

Claude Code has two complementary memory layers, used in tandem:

- **Auto-memory** (`~/.claude/projects/*/memory/`) — lightweight preferences, context, feedback. Manual writes only. (Unreleased "Auto Dream" consolidation exists in source but is behind a disabled feature flag.)
- **MemPalace** (`~/.mempalace/palace/`, 134K+ drawers) — verbatim conversations, tool output, code. Write-only archive, searchable via MCP. Completeness is the feature.

Both systems coexist. Hook saves are scoped to MemPalace ("For THIS save, use MemPalace MCP tools only") — this is not a permanent ban on auto-memory.

## Hook Save Architecture

Two save modes, controlled by `hook_silent_save` in `~/.mempalace/config.json`:

- **Silent mode** (default, `hook_silent_save: true`): Direct Python API call to `tool_diary_write()`. Plain text, no AI involved, deterministic — save marker advances only after confirmed write. Shows `"✦ N memories woven into the palace"` as terminal notification.
- **Block mode** (legacy, `hook_silent_save: false`): Returns `{"decision": "block"}` asking the AI to call MCP tools. Non-deterministic — AI may ignore, summarize, or fail. Save marker advances before AI acts (data loss risk).

**v3.3.0 change:** Upstream hooks now return `"decision": "allow"` (background save, no AI blocking) instead of `"decision": "block"`. This aligns with our silent mode direction — the AI never needs to act on saves.

## Integration

- **Claude Code plugin**: installed at user scope via marketplace
- **MCP server**: global user scope — available in all projects
- **Stop hook**: fires every 15 messages, saves diary entry + auto-mines transcript
- **PreCompact hook**: emergency save before context compaction, auto-mines transcript, finds transcript by session_id fallback

## Testing

Always run `python -m pytest tests/ -x -q` after changes. Benchmark and stress tests are excluded by default (use `-m benchmark` or `-m stress` to include).

## Documentation maintenance

The fork-ahead narrative was previously hand-maintained in four places
(README's fork-change-queue table, this file's row inventory,
`FORK_CHANGELOG.md`, and `~/.claude/projects/-home-jp-Projects-memorypalace/scratch/promises.md`).
Drift was inevitable. As of 2026-04-26 the **canonical source** is
`docs/fork-changes.yaml`; render targets are generated.

### Workflow for new fork-ahead changes

1. Land the code change with a focused commit on `main`.
2. Add an entry to `docs/fork-changes.yaml` (top of the `entries:`
   list, newest first). Schema is documented at the top of the YAML.
3. Run `scripts/render-docs.py` to regenerate `FORK_CHANGELOG.md`.
4. Run `scripts/check-docs.sh` to verify nothing has drifted (test
   count, commit hashes, render parity, upstream PR states).
5. Commit the YAML + the regenerated `FORK_CHANGELOG.md` together.

### Targets

| Target | Status |
|--------|--------|
| `FORK_CHANGELOG.md` | rendered from YAML (today) |
| README fork-change-queue table | hand-maintained for now |
| CLAUDE.md row inventory (rows 1–27 above) | hand-maintained for now |
| `scratch/promises.md` tracker entries | hand-maintained for now |

The renderer's `--target` flag is wired to take `changelog` or `all`;
`all` is the same as `changelog` until the README/CLAUDE/promises
renderers land.

### Lint

`scripts/check-docs.sh` runs four checks:

1. README test count vs `pytest --collect-only`
2. every fork commit hash referenced in docs resolves via `git cat-file -e`
3. `FORK_CHANGELOG.md` matches the YAML (re-render idempotent)
4. every `#NNNN` reference has an upstream state matching the doc's claim

Run before committing any doc change. Exit code 1 on drift.

