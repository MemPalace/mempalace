# Source-generation lifecycle: local incremental ingestion

`mine --source` supports drawer-only incremental adapters on Chroma, sqlite_exact
and rust_exact. Supported-platform release gates and independent review remain
required for this unpublished candidate.

## State and ownership

State lives at `<resolved palace>/source_lifecycle.sqlite3`, independently of the
KG resolver. Owner keys are exact `(adapter_name, source_file)` pairs. `begin`
allocates a UUID staging record without changing currentness. `activate` checks
immutable version/predecessor/deletion fields, retires the predecessor and
activates its replacement in one transaction. Failure rolls back retirement.
`abandon` only changes the exact owned staging record. The runner holds the
palace writer lease over extraction, publication and recovery, not just individual
backend writes.

The unpublished schema is version 2. Readers and every writer transaction check
table definitions, primary/check constraints, both partial unique indexes and the
singleton epoch. Damaged state is rejected, never silently repaired. All registry
mutations increment the epoch, including staging creation: a backend may observe
a newly staged row before activation. The public `visibility_epoch` token supports
cache invalidation without polling loops.

## Managed stamps and legacy rows

Managed drawer stamps are `adapter_name`, `source_file`, `source_generation` and
`source_version`. Presence of either generation or source-version stamp requires
the entire valid identity. Registered version must match exactly. Unknown,
malformed, incomplete or mismatched state fails closed, including when the
registry is missing. Active non-deletion generations are visible; staged,
retired, abandoned and deletion generations are hidden.

Ordinary legacy adapter/source metadata remains compatible until the first
managed activation for that exact owner. After activation, its old untagged
rows become logically hidden, including obsolete tails; their stored bytes are
retained, and old snapshots still see them. Legacy rows belonging to another
adapter, or lacking an attributable adapter/source pair, remain visible. There
is no source-path-only migration or physical legacy deletion. In-tree source
tracing at the port base found no persisted `source_version` producer outside the
old lifecycle proposal; `migrate.py` uses that spelling only as a local Chroma
version variable. `version`, `adapter_version`, `normalize_version`, and other
legacy metadata are not lifecycle discriminators. External producers that used
`source_version` without a lifecycle generation need an explicit compatibility
migration; this implementation cannot safely distinguish those rows from damaged
managed stamps and does not downgrade them silently.

## Operation snapshots and read surfaces

Logical SQL readers open registry first, backend second, then compare the current
registry epoch after pinning the backend snapshot. An opening race closes both
and retries at most three times. A fresh unknown generation fails closed. This is
coordinated retry-based consistency, not a cross-database atomic transaction.

Reader-only temporary views resolve the configured drawer collection through the
shared predicate. They do not modify stored rows or affect other collections.
Raw Chroma BM25, recent records, metadata streams, hydration, diary/singular reads,
taxonomy, graph and source grouping use these connections. Exact/Rust grouping
uses the same contract. Physical embedding/HNSW counts and embedder-identity probes
remain physical. Public collection `count` is logical; `physical_count` is explicit.

Chroma `get` applies visibility before logical offset/limit and fetches lifecycle
metadata before projection. Bulk exports use one native row-id cursor with bounded
hydration batches and retain the snapshot until iterator close. Chroma ANN refill
uses increasing candidate windows capped at `max(requested, min(4096,
requested*16))`, preserves candidate order and warns when truncating. Returned
metadata/documents are hydrated from the pinned logical SQL snapshot, not a later
ANN response. Exact queries rank the logical SQL view. The Rust native loader
opens an independent physical database, so managed logical queries deliberately
use the Python exact fallback rather than claiming native snapshot participation.

Taxonomy, graph, known-wing and metadata caches include the durable epoch. Exact
managed vector matrices do not persist across operation snapshots. Legacy exact
handles cache their tag-free check by database-change token; the check and read
share a pinned backend transaction. Legacy Chroma readers retain ordinary indexed
SQL plans when no lifecycle keys/registry exist.

## Reader lifetime and reclamation

Logical readers hold a shared palace-local reclamation lease until close. GC
acquires both the palace writer lease and a nonblocking exclusive reclamation
lease. It deletes only the exact retired/abandoned `(adapter, source, generation)`
rows and prunes registry state only after physical deletion succeeds. Failure
retains state for idempotent retry. Process death releases OS locks.

POSIX uses `flock`; Windows uses `LockFileEx` shared byte locking and the existing
exclusive writer byte. CRT `LK_NBRLCK` is not treated as a real shared lock.
Cross-platform subprocess tests are present; Windows execution remains a CI gate.

At most one unreclaimed retired generation is permitted per owner. Another
activation is refused until GC succeeds; abandoned staging likewise must be
reclaimed before retry. Long readers apply backpressure instead of losing their
old generation or allowing unbounded retained history. No retention product or
mesh-wide lifetime protocol is introduced.

## Restore, filesystem and scope limits

Missing registry initialization preflights existing local stores for managed
stamps. A vector-only restore is not a fresh palace. Restore matching registry,
drawer database and committed WAL state together under the writer lease, using
SQLite backup semantics rather than copying only live main files. Tagged-only
sync/restore without registry is unsupported and must not strip stamps or invent
activation state. Full mesh lifecycle replication is separate work.

Registry readers use `mode=ro` without writable fallback or `immutable=1`.
SQLite may create WAL/SHM sidecars in a writable directory; zero-change dry runs
must avoid storage opens altogether. Read-only managed mounts that cannot open
WAL state fail safely. Local ordinary read-only behavior remains covered by the
existing exact-backend suite.

No KG lifecycle, media generation lifecycle or nonlocal backend snapshot protocol
is implemented. Adapters declaring KG/media output or selecting a nonlocal
backend are rejected before ingest. Undeclared unsupported results and direct
KG/closet writes fail the current item. Old drawer IDs referenced by external
pointers or pre-existing KG facts are not retargeted; after reclamation those
references may no longer resolve. No triple provenance migration is implied.

## Cursor handshake and guarded runner

Populated, empty and deleted items share `SourceGeneration.cursor`:
`adapter_name`, `source_file`, `source_generation`, `source_version`, and
`source_deleted`. Empty output must retain a version without depending on drawers.
A deleted cursor must not mark a recreated normal item current.

Incremental adapters must override `is_current` and yield `SourceItemMetadata`
before each item's drawers. Core passes the generic cursor to `is_current` and
only honors `True` for an exact version/deletion match. The adapter must observe
`palace._skip_requested` immediately after its item yield and avoid extraction
and writes when it is true. A drawer after skip, a mismatched source, duplicate
item or chunk index, or a stale-item skip request fails the protocol. Empty
extraction is a valid replacement. `version="__deleted__"` requires
`supports_deletion_tombstones` and permits no drawers.

Use `palace.upsert_drawer` for incremental writes. Core builds owner/generation/
chunk-specific IDs and stamps provenance; plugins cannot supply lifecycle stamps
or forge adapter/source/chunk identity. Core rereads directory inode metadata
for every drawer. The incremental collection facade permits logical reads but
refuses direct writes, physical handles, KG and closets. Protocol/write failures
remain fatal even if plugin code catches them. This is a cooperative plugin
contract, not an isolation sandbox against arbitrary Python/filesystem access.

The complete invocation holds the existing palace writer lease, plus a guard
against concurrent incremental generators in the same process. On touching an
item, orphan staging is abandoned and inactive generations are reclaimed before
another staging begins. Failed extraction or activation leaves the last good
cursor intact; failures are propagated and failed staging is reclaimed when
possible. If cleanup itself fails, hidden state remains for retry. A failure
reported after activation commits never abandons the new active generation.
Post-activation cleanup failure is logged and deferred; the new generation stays
current. A subsequent update must reclaim retained state first. Successful
preceding items remain published if a later item fails; this is per-item atomicity.

Dry runs validate/extract using empty no-op facades and no registry/backend/KG
opens or writer leases. They cannot determine currentness and report the full
would-write count even for an initialized palace. Configuration/path resolution
and source reads still occur. Normal nonincremental/media options, legacy mode
dispatch and daemon routing retain their existing behavior.

The separate #1944 adapter must compare generic `source_version` with exact owner
checks; absent adapter-specific version or generic version cannot imply current.
Its runtime format/storage work is not part of this change. A synthetic generic
cursor consumer exercises this handshake; the actual #1944 implementation is
not updated or certified by this port.
