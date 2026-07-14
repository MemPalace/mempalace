# Async MCP Mining and Tiered Project Memory Design

- **Status:** Runtime, source metadata, scoped retrieval, sales policy, and migration dry run implemented; copy/apply/activation pending
- **Date:** 2026-07-14
- **Target:** MemPalace MCP/daemon runtime plus the sales-enablement `se` rollout
- **Baseline:** MemPalace `develop` at the start of this design

> **Runtime implementation note (2026-07-14):** Daemon progress, asynchronous
> MCP mine submission, durable short writes, job inspection, linked-worktree
> protection, and maintenance-time BM25 search are implemented behind
> `MEMPALACE_MCP_DAEMON_WRITES=1`. The daemon must be started separately. Unset
> the flag and restart MCP to restore legacy direct mode; enabled mode never
> falls back to direct writes.
>
> **Migration implementation note (2026-07-14):** New mines stamp canonical
> source metadata, retrieval supports wing/source-kind/hot-cold scopes, and the
> canonical sales repository has an `se-code` mining policy. A SQLite-only,
> plan-only command produced an owner-only manifest from the active palace
> without changing its SHA-256. Applying the reviewed manifest to a copied
> palace and activation remain separate gated work.

## Summary

Move long-running MCP mining out of the MCP request process and into the existing durable MemPalace daemon queue. An MCP mine call validates and submits work, returns a job ID, and never waits for the repository scan or index rebuild. Job/status requests stay responsive, and search falls back to the existing read-only SQLite/BM25 path while a maintenance job is changing the vector index.

For the sales-enablement repository, separate curated memory, canonical code, and session history into `se`, `se-code`, and `se-sessions`. Remove verified worktree duplicates, retain unique worktree artifacts, mark old sessions cold without summarizing or deleting their verbatim content, and add source metadata that makes future cleanup deterministic.

This is a targeted evolution of the current MCP and daemon architecture. It reuses the durable queue, incremental miners, Chroma SQLite fallback, and existing palace structures. It does not introduce an external service, remote storage, lossy summaries, or a second workflow engine.

## Problem and evidence

The MCP HTTP server uses `ThreadingHTTPServer`, but every POST wraps the whole `handle_request()` call in `_HTTP_REQUEST_LOCK`. `mempalace_mine` invokes the miner synchronously inside that lock. If a client times out or disconnects, the Python handler continues mining and retains the lock. All later MCP POSTs, including search and status, queue behind the abandoned request.

The existing daemon already provides the primitives needed for a better execution model:

- durable SQLite-backed jobs;
- `queued`, `running`, `succeeded`, `failed`, and `cancelled` states;
- active-job deduplication;
- retry and restart recovery;
- authenticated loopback job and health APIs;
- `mine`, `sync`, and allowlisted MCP-write job execution.

The MCP mine tool does not currently use those primitives. It also competes with the MCP process's lifetime peer-writer lease, so simply wrapping the current function in a thread would not solve ownership or responsiveness.

The current `se` palace also mixes distinct retrieval concerns. The implemented read-only inventory measured 13,653 active drawers:

| Category | Drawers | Intended destination |
|---|---:|---|
| Codex/session transcripts | 5,306 | `se-sessions` |
| Canonical current repository | 4,667 | `se-code` |
| Temporary linked-worktree copy | 3,588 | delete if equivalent; otherwise preserve as artifacts |
| Curated records without a mined source | 92 | `se` |
| Other/unclassified | 0 | preserve if found by a future inventory |

The linked-worktree copy alone accounts for 26.3% of the current records. Default retrieval is therefore paying for duplicate and historical material that should not compete with current decisions and code.

## Goals

1. Return from `mempalace_mine` after durable submission, not after mining.
2. Keep job status and search usable throughout a mine.
3. Ensure client timeout or disconnect never cancels accepted mining work.
4. Serialize writes through the existing daemon when daemon-backed MCP mode is enabled.
5. Deduplicate equivalent queued/running mine requests.
6. Expose useful job progress without requiring a conversation or UI to wait.
7. Reject accidental mining of linked Git worktrees by default.
8. Separate current decisions, canonical code, and session history for precise retrieval.
9. Reduce duplicate and low-value records while preserving unique verbatim content.
10. Provide an auditable, dry-run-first migration with a complete rollback copy.

## Non-goals

- Replacing Temporal, adding a distributed queue, or adding remote workers.
- Running multiple concurrent writers against one palace.
- Making every Chroma/HNSW operation safe during maintenance.
- Providing a transactionally frozen search snapshot during mining. SQLite fallback returns a committed point-in-time view and labels it as updating.
- Summarizing, paraphrasing, or otherwise reducing verbatim user content.
- Deleting old sessions merely because they are old.
- Automatically deleting unique worktree content.
- Changing synchronous CLI `mempalace mine` semantics in this work.
- Building a general-purpose retention-policy language in the first release.

## Architecture

### 1. One durable writer lane

When daemon-backed MCP mode is enabled, the daemon is the only MCP mutation executor for a palace:

- read tools continue to execute in the MCP process;
- `mempalace_mine` submits a daemon `mine` job;
- write-classified tools submit daemon `mcp_tool` jobs;
- maintenance jobs and short writes execute serially in the daemon worker;
- the MCP process does not retain the lifetime peer-writer lease in this mode.

The daemon worker acquires the per-palace writer lease before executing a job. Lock acquisition becomes re-entrant within that daemon process so the outer job lease and existing miner-internal lock cannot deadlock each other. The cross-process lease remains exclusive; direct CLI or legacy MCP writers still receive the existing structured lock error.

This mode is introduced behind `MEMPALACE_MCP_DAEMON_WRITES=1` for safe rollout. The sales-enablement installation enables it immediately. Legacy direct-write behavior remains available while upstream compatibility is evaluated; it is not used as a fallback when daemon mode is enabled.

### 2. Async MCP mine contract

`mempalace_mine` keeps its existing mining arguments and adds:

- `allow_linked_worktree: boolean = false`
- `priority: integer = 0`

In daemon-backed mode it performs only:

1. schema and mode validation;
2. source existence and canonical-path validation;
3. linked-worktree protection;
4. construction of the daemon payload and dedupe key;
5. durable submission with `wait=False`;
6. return of the accepted job.

Successful submission returns:

```json
{
  "success": true,
  "accepted": true,
  "job_id": "<id>",
  "state": "queued",
  "deduplicated": false,
  "source": "<canonical source>",
  "mode": "projects",
  "wing": "se-code",
  "submitted_at": "<UTC ISO-8601>"
}
```

If an equivalent active request already exists, `job_id` identifies that job and `deduplicated` is `true`. There is no in-process or synchronous fallback. If daemon health/submission fails, the tool returns a structured `DaemonUnavailable` error quickly with the command needed to start or inspect the daemon.

The dedupe key is a SHA-256 of the canonical palace path, canonical source path, mode, wing, agent, limit, dry-run flag, extraction strategy, relevant mining options, and `mempalace.yaml` checksum. Only `queued` and `running` jobs participate; a later request may run after a terminal job.

This is an intentional MCP behavior change: callers that previously consumed the final `output` must poll job status. The CLI remains synchronous unless its existing background option is used.

### 3. MCP job tools

Add read-classified tools that do not open Chroma and never take the palace request lock:

#### `mempalace_job_status`

Input: `job_id`.

Returns identity, kind, state, attempts, timestamps, progress, result, and structured error. Result/output is present only for a terminal job.

#### `mempalace_list_jobs`

Inputs: `limit` (default 20, capped), optional `state`, and optional `kind`.

Returns recent jobs and aggregate counts. Payloads containing verbatim write content are never returned; source paths and safe operational fields are returned for mine jobs.

The existing daemon bearer token, loopback binding, owner-only queue permissions, body caps, and retention policy remain mandatory.

### 4. Progress and heartbeat

Extend the daemon queue schema additively with `updated_at`, `heartbeat_at`, and `progress_json`. Queue startup performs an idempotent schema migration for existing databases.

Mine progress has this stable shape:

```json
{
  "phase": "scanning|mining|post_processing|verifying",
  "files_total": 4667,
  "files_processed": 1200,
  "files_changed": 14,
  "files_skipped": 1186,
  "files_failed": 0,
  "current_source": "apps/.../file.ts",
  "message": "optional safe status"
}
```

Miner callbacks update progress at phase boundaries and no more than once per second during file loops. The daemon refreshes `heartbeat_at` at least every five seconds while a job is running, independently of client polling.

The heartbeat is operational evidence that the daemon worker is alive; it does not keep an MCP request open and it is not a user-facing wait loop. A stale heartbeat is reported by status, but it does not by itself mutate the job state. Restart recovery remains the authority for re-queuing an interrupted `running` job.

### 5. Short writes during maintenance

All write-classified MCP calls enter the durable daemon queue in daemon-backed mode. The MCP dispatcher waits for at most 250 ms for an idle-daemon fast path:

- if the write finishes, return its normal tool result plus `delivery: "completed"`;
- if it remains queued or running, return `accepted`, `job_id`, and `delivery: "durable_queue"`;
- if submission fails, return `DaemonUnavailable`; never execute directly.

Therefore a curated checkpoint made during a large mine is accepted immediately and executes after the maintenance job. No person or MCP connection has to wait. Queue order remains deterministic; this design does not preempt a miner mid-file or introduce a second writer.

### 6. Responsive reads and transport lock scope

Remove the whole-request `_HTTP_REQUEST_LOCK` critical section. Replace it with operation-level protection:

- protocol initialization, tool listing, health, and daemon job reads do not take a palace lock;
- daemon submissions only use the daemon client and queue database;
- direct cached Chroma reads remain serialized where the backend requires it;
- when a maintenance job is active, `mempalace_search` sets `vector_disabled=True` and uses the existing read-only SQLite/BM25 path;
- other HNSW-dependent reads may return a structured `IndexUpdating` response rather than block behind the mine.

Searches served during maintenance include:

```json
{
  "index_state": "updating",
  "active_job_id": "<id>",
  "retrieval_mode": "bm25_sqlite"
}
```

The fallback sees SQLite commits that completed before its read transaction. It is not described as a stable pre-mine snapshot. After a successful maintenance job, the MCP process invalidates cached clients, verifies SQLite/HNSW readiness, and resumes hybrid search. If post-mine verification fails, vector search stays disabled and the terminal job reports the integrity error while BM25 remains available.

### 7. Daemon availability

Daemon-backed MCP mode expects the local daemon to be supervised and already running. MCP tool calls use short health/submission timeouts and do not silently start a long-lived process or fall back to unsafe direct writes. Sales-enablement local setup starts the daemon with the existing MemPalace daemon command as part of the developer service bootstrap.

This keeps latency predictable and makes missing supervision visible. A cold or failed daemon is a configuration/health error, not a reason to put long work back inside an MCP request.

## Source identity and linked-worktree protection

### Canonical source identity

Newly mined drawers receive scalar metadata compatible with Chroma:

| Field | Purpose |
|---|---|
| `source_kind` | `curated`, `code`, `documentation`, `session`, or `worktree-artifact` |
| `memory_tier` | `hot` or `cold` |
| `source_root` | canonical real path of the mining root |
| `source_identity` | stable kind/repository/relative-path or kind/session identity |
| `source_revision` | Git commit, session ID, or importer revision when available |
| `source_sha256` | hash of the full source bytes when available |
| `content_sha256` | hash of this exact verbatim drawer content |
| `source_canonicality` | `canonical` or `linked-worktree` |

Missing fields on legacy drawers are treated compatibly: missing `memory_tier` means hot, and existing `source_file` continues to work.

### Git worktree rule

For `projects` mode, source validation detects a linked worktree by resolving its Git directory and checking whether it lives below the common Git directory's `worktrees/` area. This avoids confusing ordinary repositories or submodules with linked worktrees.

If linked, submission fails with `LinkedWorktreeRejected` unless `allow_linked_worktree=true`. The error reports the detected canonical repository root when available. Explicitly allowed content is stamped `source_canonicality=linked-worktree`; it never masquerades as canonical code.

The sales-enablement repository keeps `allow_linked_worktree` false. Worktree-specific conversation or design artifacts belong in `se-sessions`, not `se-code`.

## Tiered `se` memory model

### Wings

#### `se`

Small, curated, durable knowledge:

- decisions and their rationale;
- architecture boundaries;
- incidents and root causes;
- release/check-in facts;
- repository policy and operating conventions.

These records are immediately checkpointed, permanently hot unless explicitly retired, and not created by repository mining.

#### `se-code`

The canonical repository's current code, tests, and useful documentation. It is populated only from the canonical checkout using checked-in `mempalace.yaml`. Re-mining is incremental; changed source replaces that source's older mined drawers according to existing miner semantics.

#### `se-sessions`

Verbatim Codex/conversation history and preserved worktree-only artifacts. Sessions authored in the last 90 days are hot by default. Older sessions are marked cold, not deleted. Pinned sessions stay hot regardless of age.

Cross-wing tunnels connect curated architecture/decision rooms to relevant code rooms and session history without merging the three retrieval concerns.

### Retrieval policy

| Question | First search | Fallback |
|---|---|---|
| Why was this decided? | `se` | hot `se-sessions` |
| What is implemented now? | `se-code` | `se`, then hot sessions |
| What did we discuss? | hot `se-sessions` | cold sessions |
| Ambiguous project question | `se` + `se-code` | hot sessions, then cold sessions |

Search accepts optional `wings`, `source_kinds`, and `include_cold` filters while preserving the existing singular `wing` argument. `wing` and `wings` are mutually exclusive. Legacy records without tier metadata remain visible. Cold records are included only when requested or when a caller intentionally performs the historical fallback.

This policy reduces the default semantic candidate set; it does not discard historical words.

### Repository configuration

The sales-enablement canonical checkout gains a checked-in `mempalace.yaml` with:

- `wing: se-code`;
- rooms for backend, workers, shared helpers, architecture/docs, and tests/operations;
- `source_kind: code` and linked-worktree rejection;
- exclusions for dependency directories, build output, coverage, caches, generated bundles/maps, lockfiles, and other reproducible low-value artifacts;
- normal Git ignore behavior enabled.

Source files, tests, migrations, hand-written documentation, and repository agent instructions remain included.

The configuration extension is additive. A representative shape is:

```yaml
wing: se-code
source_kind: code
reject_linked_worktrees: true
rooms:
  - name: backend
    description: NestJS API code
    keywords: [apps/sales-enablement-be, controller, service, dto]
  - name: workers
    description: Temporal worker and workflow code
    keywords: [apps/sales-enablement-workers, workflow, activity, temporal]
  - name: helpers
    description: Shared libraries and infrastructure
    keywords: [libs/helpers, mongo, gcs, pubsub]
  - name: architecture_docs
    description: Hand-written architecture and operating documentation
    source_kind: documentation
    keywords: [docs, architecture, agents]
  - name: tests_operations
    description: Tests, migrations, scripts, and operational configuration
    keywords: [test, spec, scripts, migration, docker]
exclude_patterns:
  - node_modules/
  - .nx/
  - dist/
  - coverage/
  - "*.map"
  - workflow-bundle.js
  - package-lock.json
```

The implementation validates new scalar fields and per-room `source_kind`; older configuration files retain their current behavior.

## Cleanup and migration

Cleanup is a separate, one-time, project-specific operation. It is not hidden inside ordinary mining and does not make broad deletion available through MCP.

### Phase 1: inventory and backup

1. Record palace, daemon, backend, and MemPalace versions.
2. Produce a read-only JSON/Markdown inventory by wing, source root, source kind, age, and exact-content hash.
3. Stop/suspend writers briefly, checkpoint SQLite WAL, and copy the full palace, daemon queue, hallways/tunnels, and configuration to an owner-only rollback directory.
4. Verify SQLite integrity and checksums on the rollback copy before continuing.

### Phase 2: build a migrated copy

Apply the migration to a second full palace copy, leaving the active palace and rollback copy untouched:

1. Reclassify curated records into `se`.
2. Reclassify canonical repository records into `se-code` with canonical source identities.
3. Reclassify transcripts into `se-sessions` and apply the 90-day hot/cold rule.
4. Map each temporary-worktree source to the canonical repository relative path.
5. Delete a worktree drawer only when canonical source identity, full-source hash where available, chunk index, and exact verbatim content hash prove equivalence.
6. Preserve non-equivalent worktree content in `se-sessions` as `worktree-artifact`, cold by default, with its original source path and revision.
7. Collapse repeated transcript imports only when session/import identity and exact content hash match. Identical words from unrelated legitimate sources are not deduplicated.
8. Leave any unclassified records untouched until each is assigned or explicitly approved for removal. The current dry run found none after all known roots were supplied.
9. Rebuild derived closets, hallways, tunnels, and vector indexes in the migrated copy.

The migration manifest records every old drawer ID, action, destination, reason, and checksum. It is resumable and idempotent.

### Phase 3: verify and activate

Before activation:

- SQLite `integrity_check` passes;
- HNSW/collection readiness checks pass;
- retained content hashes match the source palace;
- expected category counts reconcile with the manifest;
- no verified duplicate worktree records remain;
- unique worktree artifacts are still retrievable;
- representative decision, code, recent-session, and cold-session searches return expected verbatim drawers;
- default searches exclude cold sessions;
- explicit historical searches include them.

Pause writers, confirm the active source palace did not change since the migration manifest snapshot, and atomically activate the migrated copy. Keep the previous palace for a defined rollback window. Rollback switches the active path back; old data is not deleted during activation.

### Expected reduction

The dry run classified all 3,588 known worktree records and proved 3,515 exact duplicate candidates. Removing only those reviewed candidates would reduce the current drawer count by 25.7%, from 13,653 to 10,138. The remaining 59 unique and 14 uncertain worktree records are preserved cold in `se-sessions`. Generated/lock/cache exclusions may reduce future mine growth; no repeated-session deletion is proposed by this checkpoint.

Cold session classification reduces the default search set but not total storage. Reports always distinguish `records_deleted_as_verified_duplicates`, `records_preserved_cold`, and `records_excluded_from_future_mines`.

## Failure handling

| Failure | Behavior |
|---|---|
| Daemon unavailable | Fast structured error; no direct fallback |
| Client disconnects after submission | Job continues; caller can recover by listing jobs |
| Duplicate submission | Existing active job returned |
| Daemon exits during job | Existing recovery re-queues until max attempts, then fails visibly |
| Mine fails | Job stores structured error/output; prior incremental data remains |
| Post-mine integrity fails | Job fails; vector stays disabled; BM25 remains available |
| Write arrives during mine | Durable queued acceptance; executes after maintenance |
| Search arrives during mine | SQLite/BM25 result marked `index_state=updating` |
| Migration equivalence uncertain | Preserve and classify; never guess-delete |
| Active palace changes before swap | Abort activation and rebuild migrated copy |

## Observability

- Job status exposes state, attempts, phase progress, timestamps, and heartbeat age.
- `mempalace_list_jobs` provides recent failures without exposing verbatim queued payloads.
- Mine completion includes changed/skipped/failed counts and post-processing results.
- Search responses identify hybrid versus maintenance fallback mode.
- Structured logs include job ID and palace key but avoid drawer content.
- Optional desktop notification may announce terminal mine state; it is not required for correctness.
- Terminal queue records retain the existing bounded retention window.

## Performance budgets

Measured on a warm local daemon and a palace comparable to the current 13.6k-drawer `se` palace:

- MCP mine validation and durable submission: p95 under 500 ms.
- Job status and recent-job listing: p95 under 100 ms.
- Search during maintenance via SQLite/BM25: p95 under 1 second for ordinary queries.
- No MCP request remains open for repository mining duration.
- Progress writes are throttled to at most one per second; heartbeat gap is at most five seconds while healthy.
- Hybrid-search latency after maintenance does not regress by more than 10% from the pre-change warm baseline.

## Test strategy

### Unit tests

- canonical source and linked-worktree detection, including submodules;
- deterministic mine dedupe keys and config-checksum changes;
- additive queue schema migration;
- progress throttling and heartbeat serialization;
- job response redaction;
- metadata defaults and hot/cold filtering;
- single- and multi-wing filter validation;
- exact source/session dedup predicates.

### Integration tests

- block a fake mine indefinitely and prove health, job status, job listing, and BM25 search remain responsive;
- disconnect/time out the submitting MCP client and prove the job reaches a terminal state;
- submit the same mine concurrently and prove only one active job executes;
- restart the daemon during a mine and prove recovery/attempt limits;
- queue a curated write behind a mine and prove immediate accepted response plus eventual execution;
- prove daemon-backed mode never invokes direct-write fallback;
- prove MCP and daemon writer ownership cannot deadlock;
- prove cached HNSW clients are invalidated and hybrid resumes only after readiness checks;
- prove linked worktrees are rejected unless explicitly allowed.

### Migration tests

- run the full migration against a synthetic copy with canonical duplicates, unique worktree artifacts, repeated transcript imports, and ambiguous records;
- verify dry-run makes no changes;
- interrupt and resume without duplicate actions;
- verify every deletion has exact-equivalence evidence;
- verify cold records are verbatim and explicitly retrievable;
- exercise activation and rollback;
- run SQLite and vector-index integrity checks before and after.

### Baseline and completion gates

Before implementation, the current focused suite must pass. Before completion:

```bash
uv run pytest tests/ -v --ignore=tests/benchmarks
uv run ruff check .
uv run ruff format --check .
```

The sales-enablement rollout additionally validates the migration manifest/counts and representative retrieval queries against the copied palace before activation.

## Rollout

1. Land daemon progress/status and lock-scope tests without changing defaults.
2. Add daemon-backed MCP routing behind `MEMPALACE_MCP_DAEMON_WRITES=1`.
3. Enable it in the local sales-enablement MemPalace service and soak async mine, queued checkpoints, and maintenance search fallback.
4. Add source metadata/filtering and the repository `mempalace.yaml`.
5. Run cleanup inventory and migration dry-run; review the manifest and exact projected counts.
6. Create/verify rollback copy, build migrated copy, run acceptance searches, and activate.
7. Retain the old palace through the rollback window and monitor job/search errors.
8. After upstream compatibility feedback, make daemon-backed MCP writes the default in an appropriate release and retain an explicit legacy escape hatch for one deprecation cycle.

## Security and privacy

- All processing remains local.
- Daemon endpoints stay loopback-only and bearer-token protected.
- Palace, token, queue, logs, manifests, and backups are owner-only.
- Job tools redact verbatim write payloads.
- Logs and progress use relative/safe source labels and never drawer content.
- Migration copies and rollback data receive the same protections as the active palace.
- No telemetry, cloud service, or external embedding/LLM provider is introduced.

## Acceptance criteria

The work is complete only when:

1. An MCP mine call returns an accepted durable job without waiting for mining.
2. Search and job status meet their responsiveness budgets during a blocked mine.
3. Client timeout/disconnect does not stop accepted work.
4. Only one writer lane mutates a palace in daemon-backed mode.
5. Curated writes made during mining are durably accepted and eventually visible.
6. Linked-worktree project mining is rejected by default.
7. `se`, `se-code`, and `se-sessions` follow the approved retrieval policy.
8. No unique verbatim record is lost in cleanup.
9. Every removed record is backed by exact duplicate evidence in the migration manifest.
10. The migrated palace passes integrity, count, duplicate, representative retrieval, activation, and rollback checks.
