# Changelog

All notable changes to [MemPalace](https://github.com/MemPalace/mempalace) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/).

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

[Unreleased]: https://github.com/MemPalace/mempalace/compare/v3.2.0...HEAD
[3.2.0]: https://github.com/MemPalace/mempalace/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/MemPalace/mempalace/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/MemPalace/mempalace/releases/tag/v3.0.0
