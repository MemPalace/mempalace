# Sales-Enablement Memory Migration Apply Plan

> **Goal:** Build and verify a reduced migrated palace plus an owner-only rollback copy without mutating the active palace; keep final activation as an explicit atomic operation.

## Safety invariants

- Never update or delete a drawer in the active source palace.
- Hold the cross-process palace read gate for the complete source copy.
- Require the active SQLite+WAL snapshot to equal the reviewed physical snapshot,
  or a version-2 semantic snapshot of every SQLite schema object and durable row
  to match exactly when Chroma has only changed known bookkeeping.
- Recompute inventory, actions, hashes, and duplicate evidence before applying.
- Delete only IDs present in both `duplicate_candidate` actions and exact evidence.
- Build into new, owner-only paths; publish with an OS-native atomic no-replace
  rename so even a competing empty destination is never clobbered.
- Publish a completed migrated copy only after integrity, counts, hashes, and vector readiness pass.
- Keep activation separate, atomic, reversible, and explicit.

## Task 1: Copy-only migration bundle

- [x] Add a manifest reader/validator that rejects snapshot, count, hash, action, and evidence drift.
- [x] Copy the full active palace to a rollback directory while holding a shared palace gate.
- [x] Verify the source snapshot before/after and the rollback SQLite+WAL checksums.
- [x] Clone the verified rollback copy into a separate staging directory.
- [x] Enforce owner-only permissions, clean unpublished temporary destinations,
      and retain any completed publication for explicit cleanup after a later failure.
- [x] Cover source drift, symlinks, existing targets, and copy cleanup with tests.

## Task 2: Apply reviewed actions to staging

- [x] Recompute the migration plan from the rollback copy using the reviewed roots and hot-day cutoff.
- [x] Reconcile every planned action and evidence row with the manifest before writes.
- [x] Update retained drawer metadata in bounded batches, including destination wing and provenance/tier fields.
- [x] Delete only exactly evidenced worktree duplicate IDs in bounded batches.
- [x] Preserve all unique, uncertain, curated, session, and unclassified drawers verbatim.
- [x] Invalidate/rebuild derived closets, graph artifacts, and vector indexes in staging.
- [x] Remove only UUID HNSW directories no longer referenced by SQLite.
- [x] Make retries idempotent and record an owner-only apply report with exact counts.

## Task 3: Verify migrated copy

- [x] Run SQLite integrity checks and persisted HNSW capacity plus reopen/query checks.
- [x] Re-inventory staging and reconcile the expected 10,138 retained drawers.
- [x] Prove retained content hashes match the rollback copy.
- [x] Prove all 3,515 removals map one-to-one to reviewed duplicate evidence.
- [x] Prove 59 unique, 14 uncertain, and 33 unclassified records remain.
- [x] Verify default retrieval excludes cold records and explicit history includes them.
- [x] Run representative decision, code, recent-session, and cold-session searches.

## Task 4: Activation and rollback drill

- [x] Add an atomic activation command that validates paths/snapshots and renames the active palace to a retained rollback slot before promoting staging.
- [x] Add an atomic rollback command that reverses activation without deleting either palace.
- [x] Exercise activation, crash recovery, and rollback against synthetic palaces in tests.
- [x] Keep the user's live palace unchanged; activation remains pending explicit authorization.

## Task 5: Performance evidence and completion gates

- [x] Add/run async MCP benchmarks for mine submission, job status/listing, maintenance BM25, and post-maintenance hybrid search.
- [x] Record p50/p95 results against the design budgets.
- [x] Run the complete non-benchmark suite, Ruff, format check, migration verification, and an independent review.

## Verified evidence

The reviewed version-2 staging bundle at
`/private/tmp/mempalace-se-migration-20260714-v5`
was rebuilt after the final implementation changes. The active palace and
rollback SQLite SHA-256 remain
`7fe84a1a6641fecbd79b7000a682733b9f5963d2f1a6913e8429c7a695f7f2a5`.
Their semantic SQLite snapshot also matches across 28 schema objects, 21
tables, and 261,797 durable rows:
`f2779368031fa034ceb7cdfae2e4213aa7565c45d991d3637ea719d4bb3b7d75`.
The semantic snapshot normalizes Chroma's JSON serialization fields and omits
only 47 append-only `acquire_write` lock-history rows, which Chroma adds merely
by reopening collections. The `acquire_write` table schema and every other
schema object and durable row remain hashed, so real data or schema drift
blocks the operation. Repeated readiness reopens leave the semantic identity
unchanged.
The migrated report records 13,653 before, 10,138 after, 3,515 exact-evidence
deletions, 10,137 reused embeddings, one safely re-embedded unreadable source
vector, and two orphan HNSW directories removed. Its semantic identity is
`a72e913546bc3ab32c1402e71aebb68a6505e1fc32c517f265cda925b9c38f2e`
across 28 schema objects, 21 tables, and 226,304 durable rows. Only the two live
vector segment directories remain.

Synthetic enforced benchmark results (1,000 drawers, real loopback daemon):

| Budget | p50 | p95 / ratio | Limit |
|---|---:|---:|---:|
| Async mine durable submission | 36.6 ms | 42.4 ms | < 500 ms |
| Job status | 6.7 ms | 7.3 ms | < 100 ms |
| Job listing | 7.4 ms | 19.6 ms | < 100 ms |
| Maintenance BM25 | 12.8 ms | 14.8 ms | < 1,000 ms |
| Post-maintenance hybrid | 20.5 ms | -5.36% | <= +10% |

Real-copy search evidence (13,653-drawer rollback baseline versus
10,138-drawer migrated copy, 240 queries per route):

| Route | p50 | p95 | Median round p95 |
|---|---:|---:|---:|
| Baseline hybrid | 46.1 ms | 55.2 ms | 55.6 ms |
| Migrated BM25 | 19.7 ms | 23.6 ms | 23.6 ms |
| Migrated hybrid | 46.2 ms | 52.1 ms | 52.4 ms |

The real-copy hybrid median p95 ratio is 0.9419 (-5.81%), within the 1.10
budget. Representative decision, code, recent-session, default-hot, and
explicit-cold retrieval checks all pass. Live activation was not run.
