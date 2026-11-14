# RFC 007: Source Revision Publication

Status: Draft — for Igor's review. D1 decided (2026-11-13, refined 2026-11-14, Igor); D2–D9 proposed.
Owner: Igor (decider)
Created: 2026-11-13
Tracking issue: [#2793](https://github.com/MemPalace/mempalace/issues/2793)
Related: #1593, #1628, #2226, #2980, #2978, #2799, #2439, #2608
Prior art: RFC 001 (storage backends), RFC 002 (source adapters), RFC 004 (replicated palace)
Baseline: develop `0ed1be82dcbe4f59081b87c4eaa0986af0768ff4`; compared #2980 at `ecbff26c83724c57db387793817f94a001a0ecb8` and #1628 at `42331530ca55319639dae609c1d60d3ab6a18a06`.

## Summary

Mining today identifies a drawer by its position in a source, overwrites that position on every re-mine and deletes positions that disappeared. For a conversation that shrinks, is rewritten, or is re-parsed by a newer chunker, the prior words are destroyed. That is #2793.

This RFC separates two kinds of source by what the palace is the authority for:

- **Preserved memory** — what was said in human–AI conversations and what was written into the palace by hand. The palace is the only authority. Every accepted revision is kept verbatim until the user explicitly deletes it, and recall can reach all of it.
- **Indexed project files** — files in a folder the user mined. The file on disk (and git) is the authority. The palace keeps a complete, fast, fully searchable index of the **current** version and a metadata history of past versions (fingerprint, commit, timestamps). It does not keep past versions' bodies.

Both kinds publish through one small authority — an owned SQLite catalog per palace — that makes a new generation visible in one transaction, so readers never see a torn mix and a crash never loses the previous state. Vector and lexical indexes are rebuildable projections of that catalog.

One sentence: **conversations and manual entries are never forgotten; project files are always indexed as they are now, and remembered as references to what they were.**

## Decisions

| # | Decision | Status | Recommendation / ruling |
| --- | --- | --- | --- |
| D1 | Retention boundary between preserved memory and indexed project files | **Decided** 2026-11-13, refined 2026-11-14 (Igor) | Preserve conversations and manual entries verbatim with full history. Index the current version of project files fully; keep past project versions as references, not bodies. See [Retention boundary](#retention-boundary). |
| D2 | Default recall scope for preserved memory | Proposed | Search every committed unique preserved passage, folded and labeled current/historical; `current_only` and `as_of` are explicit views. |
| D3 | Public drawer identity | Proposed | Public `drawer_id` names an immutable occurrence for preserved memory and a generation-scoped chunk for project files; `content_id` (full SHA-256 of the logical text) is an internal storage key. |
| D4 | Relationship to RFC 004 | Proposed | Single-replica precursor of RFC 004 step 2a for preserved memory, carrying op-envelope fields from day one; project index generations are origin-local projections that do not replicate as drawer ops. |
| D5 | Interim containment before the catalog lands | Proposed | See [Delivery plan](#delivery-plan) step 1. |
| D6 | Disposition of #2980, #2978, #2799, #2439, #2608, #2226, #1628 | Proposed | See [Converging the open work](#converging-the-open-work). |
| D7 | Closing condition for #2793 | Proposed | See [Closing #2793](#closing-2793). |
| D8 | Classification of diary-directory files (`diary_ingest`) | Proposed | Preserved memory when ingested from a configured diary source; recorded as capture policy, not inferred from the folder. |
| D9 | v1 scope versus later work | Proposed | See [Scope](#scope-v1-and-later). |

## Retention boundary

### What was decided

Summary of the ruling (Igor, 2026-11-13, refined 2026-11-14): mining a project folder expresses the intention to capture *and index* what happens there. A project's past file versions are not useful for memory search, and git exists for that history, so past project data is referenced but not kept verbatim. The verbatim requirement is for recall, above all recall of conversations between humans and AIs, which is where decisions are written. Indexes are required to keep the palace at maximum performance.

| Record class | Authority | What the palace keeps |
| --- | --- | --- |
| Human–AI conversation content (transcripts, live exchanges, sweeper captures) | Palace (preserved memory) | Every accepted message verbatim, its occurrences, provenance and revisions, until explicit deletion |
| Manual palace entry (`add_drawer`, `diary_write`, `checkpoint`, manual edits) | Palace (preserved memory) | Exact accepted text and every revision, until explicit deletion |
| Diary-directory files (`diary_ingest`) | Palace (preserved memory) when the directory is a configured diary source (D8) | As above |
| File in a mined project folder (project and format miners) | File on disk / repository | **Current version:** full index — chunk text, vectors, lexical (BM25/FTS) index, closets, AAAK, entities — as a rebuildable projection. **Past versions:** reference metadata only — path, content fingerprint, commit when available, chunk count, observed/mined times. No past bodies, and no derived artifacts (vectors, lexical rows) of past bodies. |
| Explicit snapshot or excerpt of a file | Palace (preserved memory), by validated user action | Exactly the requested content, with file provenance and snapshot scope |

The current-version chunk text is a projection, not a second archive: it is bound to the observed fingerprint, replaced when the file changes, and rebuildable from disk. Removing a superseded project generation from the palace is correct behavior for project files and is not a #2793 violation.

### What this supersedes

This boundary deliberately narrows earlier positions. They are amended by this decision, not silently contradicted:

- The founder's ruling on #1593 ([comment, 2026-05-24](https://github.com/MemPalace/mempalace/issues/1593#issuecomment-4528522461)), which rejected a user-type-dependent split and proposed "MemPalace preserves every version of every file you mine". This RFC keeps the ruling's core — behavior does not depend on which tools a user has — but changes what is promised for files: the palace indexes the file as it is and remembers that earlier versions existed; it does not archive their bodies. The decision should be posted on #1593 and #2793 when this RFC is accepted.
- RFC 004 §9 "superseded revisions are kept forever" (L456–461) applies to preserved memory only.
- RFC 004 §6.2 (drawer content is a grow-only revision set), §6.4 (miner re-mine → `drawer.revise`) and step 2's promotion of every local drawer to a `drawer.add` op apply to preserved memory only. Project index generations are origin-local projections rebuilt from disk; only their reference metadata replicates.
- RFC 004 §6.3 (v4 `drawer_<hash(content)>` as the drawer id): under D3 the content hash becomes the internal `content_id`, and the public id is the occurrence. See D4.
- #2793's acceptance criteria "retain every unique prior body" and "tests cover both project and conversation callers". Body retention applies to preserved memory. Project callers are still tested for atomic publication, crash safety, retry, failure reporting and cross-class safety (they must never delete preserved rows).
- CLAUDE.md "Verbatim always" is scoped, when the implementation lands, to preserved memory and to returning the current file's exact text for project hits.

### Classification rules

Classify by provenance and explicit capture intent, not by extension, directory, or git status:

- A transcript stored under a project root is conversation memory when a conversation miner, sweeper or live capture files it.
- A document an agent wrote into the project is a project file. Decisions belong in the conversation that produced them, which is preserved.
- A quoted excerpt inside a chat message stays intact as part of that message.
- A manual MCP entry may carry `source_file` provenance and is still preserved memory.
- An agent-supplied snapshot flag alone does not authorize archiving a project file. A snapshot is created only through a user-validated surface (a CLI verb, or an MCP tool whose host confirms user intent), and its requested scope is checked against that action.
- Unknown legacy records stay preserved pending classification.

Persist `origin_kind` and `retention_class` (`preserve` or `index_current`) on every source, with the validated capture policy. Moving a file, matching a path, or untrusted metadata cannot downgrade preserved memory to `index_current`.

**A path is not an ownership scope.** The same path can hold a project index, conversation drawers and manual entries at once. Every write, prune, sync and delete operates on a source stream — `(source_id, producer, extract_mode)` — never on the bare path. A project re-mine replaces only its own stream.

Bulk tool payloads (file reads, writes, patches) inside transcripts are external-source context, not authored memory. Classify typed content blocks before flattening (a `role=user` envelope can contain a `tool_result`) and keep that distinction where the format exposes it. Authored messages are preserved verbatim; their quotations are not stripped and old chats are not rewritten to deduplicate them.

An explicit snapshot creates a separate preserved record linked to the file's reference observation, with immutable range/scope, digest, commit and intent. It does not change the source's retention class or archive later revisions.

## Why the current code and open PRs are insufficient

On develop at `0ed1be82`:

- **Every successful re-mine destroys prior conversation text.** `write_file_drawers` ([writes.py:150](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/mempalace/palace/writes.py#L150)) uses positional IDs, so each changed chunk overwrites its prior text; a shrink deletes the tail; AAAK summaries of rewritten and stale IDs are dropped. A failure between batches can additionally leave a torn mix, and `chunk_total` finalization in a later batch cannot make that atomic.
- **The prune is path-wide.** The project miner ([miner.py:1883](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/mempalace/miner.py#L1883), "Every drawer for this path is replaced, whichever miner filed it") and diary ingest ([diary_ingest.py:219](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/mempalace/diary_ingest.py#L219)) pass no `extract_mode`, so they delete every row whose `source_file` matches, including conversation drawers and manual entries for a `.md`/`.jsonl` path that is also mined as a project file.
- **Diary overwrites and prunes on change.** Diary IDs are positional per entry, so an edited or front-inserted entry overwrites the prior text, and `full_rebuild = force or content_changed` (diary_ingest.py:176) prunes the rest.
- **`BaseCollection`** ([base.py:477](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/mempalace/backends/base.py#L477)) has no cross-call transaction, compare-and-swap or pinned read view; its default update is a non-atomic get/merge/upsert. A final metadata flag cannot manufacture atomicity.
- **`revisions.py`** ([L28](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/mempalace/revisions.py#L28)) is a useful fsynced prior-text JSONL for manual updates, but it has no head or manifest, lives client-side for remote backends, and is purged on delete. It records only the pre-edit text; the edited text of a mined drawer is overwritten by the next re-mine.
- **`sync --apply`** ([sync.py:490](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/mempalace/sync.py#L490), `sync_palace` at L703) deletes missing and gitignored-source rows across every producer in scope, conversation and live-exchange rows included.

At #2980's head `ecbff26c`:

- It archives complete current-schema conversation generations before replacement and adds useful reader-race and real-kill tests.
- `_backup_changed_conversation_drawers` skips rows with stale parser/chunker metadata; the later prune still removes them, so unique prior text is lost across parser upgrades.
- `_source_file_delete_ids` skips `historical_version` rows only when `extract_mode is not None` ([writes.py:39](https://github.com/MemPalace/mempalace/blob/ecbff26c83724c57db387793817f94a001a0ecb8/mempalace/palace/writes.py#L39)), so a project or diary mine of the same path deletes the archived history.
- Incoming rows still overwrite positional IDs, and publication is inferred from several row updates.
- The vector visibility guard runs only on Chroma ([query.py:359-366](https://github.com/MemPalace/mempalace/blob/ecbff26c83724c57db387793817f94a001a0ecb8/mempalace/searcher/query.py#L359-L366)) after a bounded candidate query ([query.py:336](https://github.com/MemPalace/mempalace/blob/ecbff26c83724c57db387793817f94a001a0ecb8/mempalace/searcher/query.py#L336)), widening ×4 up to 500 and then setting a truncation flag. Hidden rows can starve eligible ones, and non-Chroma backends get no torn-read protection.

## Alternatives considered

| Approach | Assessment |
| --- | --- |
| Keep positional IDs; archive before overwrite (#2980) | Good containment. Cannot give source-wide atomic publication or consistent reader eligibility; backup rows plus completion flags do not supply them. |
| Immutable content-addressed rows in the existing collection plus a small SQLite head/manifest table | The smallest credible design, and close to v1 here. Its weak points are current-only reads (an `is_current` metadata flip is again a multi-row non-atomic update across vector-store calls) and verbatim survival when the vector store is lost. v1 adopts its shape but keeps text and eligibility in the catalog, which removes both weaknesses. |
| Native revision transactions in every backend now | Right for Postgres later. Generic vector adapters cannot offer transaction/CAS/read-view today, so it cannot be the universal first release. |
| Complete RFC 004's replicated op-log first | Compatible in direction, but much larger and not landed. Not a prerequisite (D4). |
| **Owned SQLite catalog; indexes as rebuildable projections** | Recommended. Exact text and publication are independent of vector-SDK behavior; costs are a format migration, an owned database, and old-writer fencing. |

## Required behavior

1. Mining never destroys the last accepted copy of preserved memory because a source changed, shrank, disappeared, was reclassified, or used an old parser.
2. A preserved-memory read observes one complete committed generation. A project read observes one complete committed index generation and returns the current file's exact text with an explicit stale/unavailable state when the file no longer matches.
3. A lost acknowledgment can be retried without duplicating a logical observation.
4. Failed storage or publication never reports success and never advances a mined-success marker.
5. Historical preserved text stays addressable with original provenance; index failure cannot erase it. Project history keeps reference metadata only.
6. A project re-mine, sync or delete never removes a row of another retention class or source stream at the same path.
7. Explicit user deletion remains an effective, separately authorized operation.
8. Durable capture, published currentness and searchable readiness are distinct, reported states.
9. Existing palace/namespace/collection isolation and capture exclusions still apply.
10. Hooks stay under 500 ms (PreCompact keeps its existing synchronous-capture exemption, below) and startup injection under 100 ms.

The fidelity promise is exact preservation of *accepted* preserved text: the input after the existing declared validations (NUL rejection and lone-surrogate stripping in manual capture, `checkpoint`'s near-duplicate threshold). It does not recover bytes lost before migration, CRLF, or discarded host context. Raw transcript retention would widen capture policy and needs its own decision.

## Caller interface

Illustrative, not final Python:

```python
receipt = revisions.ingest_memory(            # preserved memory
    source=source_key,
    origin_kind="conversation",               # manual_entry / diary / explicit_snapshot / derivation
    observation=observed_input,
    extraction=Extraction(profile=profile, chunks=ordered_chunk_stream),
    expected_head=previous_head,              # optional; see Conflicts
    request_id=retry_token,                   # optional for miners; see Idempotency
)

receipt = revisions.index_project_file(       # indexed project file
    source=project_file_key,
    locator=repository_relative_locator,
    fingerprint=observed_content_digest,
    commit=commit_or_none,
    extraction=Extraction(profile=profile, chunks=ordered_chunk_stream),
)

with revisions.view(scope="recall") as view:  # logical epoch, not a held transaction
    hits = view.search(query, filters=filters, limit=10)
    text = view.read_source(source_key, revision="current")
    old = view.get(occurrence_id)

revisions.erase(explicit_user_intent)
```

Miners and MCP handlers stop sequencing archive, prune, finalize and index steps themselves. A receipt identifies the observation and reports its capture, publication and index states. An enqueued job is not a saved revision; a captured revision awaiting publication is durably retained and reported as pending, and must not advance a mined-success marker.

| Consumer | Waits for |
| --- | --- |
| Stop hook | Nothing in the foreground (mine runs detached, as today) |
| PreCompact hook | Durable capture of the transcript prefix only; project indexing is enqueued, not awaited. It keeps today's synchronous-mine exemption from the 500 ms budget, with a stated timeout after which capture is reported pending |
| `diary_write` / `checkpoint` acknowledgment | Durable capture (sealed observation) |
| CLI "mined" marker, `file_already_mined` | Publication |
| Startup injection | Reads the published head; never waits on search fallbacks |

Dependencies: hashing and manifest validation are in-process; the catalog is local SQLite, tested with real temporary databases; Hub transport is remote-but-owned; external embedders keep the existing explicit-provider policy. No new network dependency.

### Idempotency

Miners rely on content-derived no-op identity: a request is a no-op only when `(source_stream, input_fingerprint, extraction recipe)` equals the **current head's**; otherwise it is a new observation, so A→B→A records a third observation. Retries that must survive an intervening publication carry a `request_id`. Hook-spawned processes derive that token deterministically (source key + input fingerprint + recipe), as `sweeper.py` already keys by `(session_id, message_uuid)`.

Request IDs are namespaced by authority and client, or are globally unique UUIDs. A request fingerprint covers source, accepted input, extraction profile, semantic provenance and expected head; the same token with a different fingerprint is a conflict, never an old success. A retry resends the original request, including its original `expected_head`, and receives the committed result.

## Identities

| Identity | Meaning |
| --- | --- |
| Source | Stable identity within palace, namespace, collection and origin, with `retention_class`. Paths are locators with validity over time. |
| Source stream | `(source_id, producer, extract_mode)`: the unit of currentness, pruning and conflict. |
| Content | `content_id` = full, untruncated SHA-256 over a fixed canonical encoding of the logical string (UTF-8 with `surrogatepass`, lossless for any Python `str`). Independent of storage codec, location, time and position. |
| Occurrence | One placement of a content block in a source stream with its semantic provenance (speaker, authored time, boundaries). Immutable. Its ID is derived deterministically from `(source_stream, semantic occurrence key, content_id)`, so A→B→A reuses the same occurrences. |
| Generation | Immutable ordered membership of occurrences produced by one extraction recipe. Reusable across observations. |
| Observation / publication | A durably identified accepted capture, then a publication event (current or history-only) with predecessor, origin, HLC, retry key and commit order. |
| Read view | `(catalog_epoch, serving profile)` for a request or cursor. |

A chunk ordinal is position inside one generation, not identity across versions; matching positions cannot establish that two chunks are the same idea. A→A with unchanged content, recipe and provenance is a no-op. A→B→A records three observations and reuses A's occurrences and generation. Two genuinely repeated turns stay two occurrences sharing one content blob. Identical text in two sources keeps distinct provenance and placement.

Generation identity excludes polling time, mtime, `observed_at`, `filed_at`, initiating actor and request ID; those live on observation/attempt records. An extractor upgrade on unchanged input is a **new observation** with the same `input_fingerprint` and a new generation; the prior generation stays retrievable with its recipe label. A stored-text repair (for example `encoding_repair`) is a new observation with `origin_kind=derivation`, `predecessor` = the repaired observation and a recipe naming the repair; it publishes as current and the original stays in history. An embedding upgrade is a new index profile, not a new memory version.

### D3: public identity (proposed)

- For preserved memory, the public `drawer_id` returned by MCP tools and stored in KG `source_drawer_id`, tunnels and closets names an **occurrence**. It is stable across re-mines that keep the occurrence.
- For project files, `drawer_id` names a chunk of one index generation and changes when the file changes. Durable references to project content use `(source, anchor)` — path plus range/symbol, optionally commit.
- `legacy_alias` maps preserved legacy IDs to occurrences and project legacy IDs to `(source, anchor)`. Resolving a project alias returns the current chunk at that anchor, or `stale`/`unavailable`.
- `content_id` is an internal storage and dedup key. #1628's `parent_drawer_id` maps to generation; its `stack_id` stays a presentation hint.
- This must be agreed with #1628's owner before delivery step 3.

## Canonical storage

One dedicated palace-local catalog file, outside every legacy index file (`chroma.sqlite3`, `sqlite_exact.sqlite3`, the Rust exact store) and separate from the knowledge-graph database. Placing it outside legacy files also keeps old binaries from mutating it.

- **Preserved payloads** are BLOBs in the catalog, which avoids a second filesystem commit protocol.
- **Current project chunk text** is held in the catalog as a projection record of the current index generation only. It is the source for lexical search, previews, closets and hydration, is rebuildable from disk, and is removed when its generation is superseded.
- **Past project observations** keep reference metadata only.

Logical records (v1):

```text
catalog_meta(catalog_id, schema_version, current_epoch)
source(source_id, palace_scope, origin_kind, retention_class, capture_policy)
source_stream(source_stream, source_id, producer, extract_mode)
source_locator(source_id, locator, valid_from_epoch, valid_to_epoch)
placement(source_id, wing, room, valid_from_epoch)
presence_event(source_id, epoch, state)                   -- present / missing / ignored

content_blob(content_id, codec_version, exact_payload, byte_length)       -- preserved only
occurrence(occurrence_id, source_stream, content_id, provenance)          -- immutable
generation(generation_id, source_stream, recipe_digest, member_count,
           manifest_hash, integrity_state)
manifest_page(page_id, ordered_occurrence_ids_or_child_pages)             -- shared across generations
generation_pages(generation_id, ordered_page_ids)
occurrence_revision(occurrence_id, revision_observation_id, content_id,
                    author, epoch)                                        -- manual edit overlay
snapshot(snapshot_id, reference_observation_id, scope, digest, commit, intent)

project_reference(observation_id, repository_id, relative_path, content_digest,
                  commit_or_none, chunk_count, anchors, availability_state)
project_chunk(generation_id, ordinal, content_id, chunk_text, anchor)     -- current generation only

source_observation(observation_id, source_stream, predecessor, input_fingerprint,
                   generation_id, origin_kind, origin_replica, op_id, hlc,
                   author, observed_at, request_scope, request_id,
                   request_fingerprint, capture_state, publication_state,
                   UNIQUE(request_scope, request_id))
attempt(attempt_id, observation_id, lease_expires, fence_token, state)
publication(epoch, observation_id, generation_id, kind, previous_head)    -- kind: current / history
source_head(source_stream, observation_id, generation_id, epoch)

lexical_index         -- FTS5 over preserved payloads and project_chunk text
embedding_artifact(profile_input_digest, vector_payload, refcount, integrity_state)
occurrence_published(occurrence_id, first_epoch)          -- derived by the projector
projection_outbox(work_id, profile, publication_epoch, kind, state)       -- kind: index / gc
projection_checkpoint(profile, verified_epoch)
view_lease(cursor_id, epoch, expires_at)
erasure_fence(fence_id, explicit_scope, state)
legacy_alias(old_id, target)                              -- occurrence or (source, anchor)
legacy_raw_record(record_id, raw_bytes, label)            -- undecodable/torn journal lines
```

Currentness and history are derived from the publication log, not stored on occurrences: generation G is current for its stream over `[epoch of its current publication, epoch of the stream's next current publication)`, and an occurrence is historically eligible from the first publication (current or history-only) of any generation that contains it. Publication therefore writes O(1) rows. `occurrence_published` is a projector-maintained index of that derivation; members of publications after the projection checkpoint are covered by the exact delta (see below).

Manifest pages are shared between generations, so a growing transcript reuses unchanged pages instead of copying its full membership list on every re-mine. `codec_version` concerns storage encoding only; `stored_text` escaping applies at projection boundaries, not to `content_id`.

Immutability: sealed preserved payloads, occurrences and generations are immutable; erasure is their only authorized overlay (see [Explicit deletion](#explicit-deletion)). `index_current` generations other than the current head, with their `project_chunk` rows, lexical rows and embedding artifacts referenced by nothing else, are removed by projection GC; their `project_reference` rows stay. Operational records (attempts, outbox, checkpoints, view leases) may change; they never authorize deleting unique preserved payloads.

Content, occurrences and manifest pages are staged in short bounded transactions and sealed in one short transaction. Parsing, hashing and embedding stay outside the publication lock; a million-chunk extraction never runs inside one SQLite write transaction.

Embedding reuse is keyed by the full embedder/input contract: model identity, preprocessing/tokenization revision, modality, prefix, input text and vector settings. Equal dimensions or equal text alone are not enough. Artifacts are reference-counted across live occurrences and current project chunks of both classes, so removing a superseded project chunk never removes a vector a preserved occurrence still uses.

Durability: WAL with `synchronous=FULL`, verified after open; macOS `fullfsync` and supported filesystems evaluated explicitly. Process-kill tests are not power-loss proof. Shared SQLite over SMB/NFS is not a remote authority.

### Ownership and locking

The owner of the catalog is whoever holds the existing palace writer lease (`mine_palace_lock`): the hub while it runs, otherwise the leased process. Publication runs under that lease; `mine_lock` still guards per-source capture. Canonical recovery runs idempotently when the lease is acquired. Projection replay (index and GC work) is decoupled from admitting publication: a short-lived leased process drains its own outbox best-effort, and the hub drains the rest.

## Capture, prepare, publish

Both retention classes use the same publication point.

1. Read a coherent input observation, or identify the exact accepted prefix being captured. Transcript adapters use a verified append-only complete-record prefix where available.
2. Reserve the observation identity, attempt and retry binding, then stage content (preserved), `project_chunk` rows (project), occurrences and manifest pages in bounded transactions. The previous head is untouched.
3. Seal the generation after member counts, ordered manifest hash, provenance and payload integrity validate. For preserved memory the observation is now durably captured and reported pending. For a project file, a sealed generation records the fingerprint it was read from; recovery publishes it only if the file still matches that fingerprint, otherwise discards it and re-reads from disk. A zero-member extraction is valid with an explicit reason.
4. Prepare the serving path: reuse or compute embeddings, update the catalog lexical index, and persist the vectors the exact delta needs.
5. In one short transaction: append the publication event, move `source_head` and the catalog epoch, and enqueue projection work — including, for project files, a GC item for the superseded generation. A validated identical no-op returns the existing result.
6. Return the receipt. A retry with the same request returns the same committed result.

Projection GC for a superseded project generation is idempotent and runs only after no unexpired `view_lease` references an epoch at which that generation was current (and after a short grace window covering in-flight single-call reads). A crash at any point — including after publication and before GC — leaves either the old or the new generation fully current; leftover superseded rows are removed by recovery and are never eligible meanwhile (see eligibility). The project index is always rebuildable from disk.

There is no atomic transaction spanning the catalog and Chroma/Qdrant/Milvus; the protocol does not pretend otherwise. Source mtime is an optimization hint, never a version identity or destructive trigger. If a coherent source view cannot be established, the attempt is retained with honest provenance and the previous head stays current.

Embedder outage: publication may wait for vectors (so a published state is fully searchable), or, after a timeout, publish degraded with per-member `index_pending` and semantic search reporting incompleteness. Direct current reads serve the previous head until publication. Out-of-disk, short-write and embedder failures at any stage leave the receipt `failed` or `pending`, never committed.

### Conflicts

`expected_head` is optional. The writer lease and `mine_lock` are the normal serializers; a head conflict is expected only from lease takeover, an offline client, or a manual edit racing a mine.

- **Preserved memory.** A sealed observation that loses the head CAS is still published, as a `history` publication with its predecessor recorded, so its occurrences are eligible in historical views and default recall from that epoch. Currentness among competing observations of one stream is decided by `hlc`, ties broken by `origin_replica` then `op_id`, matching RFC 004 §6.2. No preserved observation is left sealed but unreachable.
- **Project files.** An observation that loses CAS is discarded if its fingerprint no longer matches the file on disk; otherwise it is re-published under the lease.

Unsealed, unacknowledged staging older than its attempt lease is reclaimable. Takeover of an abandoned attempt uses the attempt fence token; sealing re-verifies referenced `content_id`s, and staging GC only removes blobs with no sealed or live-attempt reference.

### Manual edits

A content edit to a preserved occurrence (`update_drawer`) is an `occurrence_revision` overlay recorded as its own observation of `origin_kind=manual_entry`. Current reads of that occurrence return its latest manual revision. A re-mine that keeps the occurrence keeps the overlay; a re-mine that drops the occurrence leaves occurrence and revision in history. Mined publications never supersede a manual revision. Editing a project chunk is refused; capturing it is an explicit snapshot through the user-validated path.

## Reader and search contract

All public reads — semantic, lexical/BM25, direct/bulk get, list/pagination, previews, neighbors, export and history — go through one read view.

- A read view is `(catalog_epoch T, serving profile)`. It is **never a held SQLite transaction**; every read is a short transaction filtered by `T`, so long-lived readers do not block WAL checkpoints.
- Cursors encode `(catalog_id, T, profile, sort key)` and can be resumed by any process (stateless MCP calls, proxy hops, hub restart). Issuing a cursor registers a `view_lease` with a TTL. Cursors expire explicitly on TTL, erasure, schema change, or reclamation of a project generation they pin — never silently.
- Preserved candidates hydrate from canonical payloads (with manual overlays); neighbors come from the matched generation. Project candidates hydrate from `project_chunk`; reading the file on disk is verified against the recorded fingerprint and returns `current`, `stale` or `unavailable` — never current bytes labeled as an older version.
- The view pins palace-owned data. It does not claim a snapshot of the KG database or graph sidecars.

### D2: recall defaults (proposed)

- **Conversation and manual memory:** recall searches every committed unique preserved passage — current and historical — folds duplicate text, keeps occurrence evidence, and labels currentness, version and provenance. A passage that vanished from a rewritten or compacted transcript is still something that was said, and conversations are where decisions are recorded.
- **Project files:** recall searches the current index generation only. History is reference metadata ("`path` had fingerprint X at commit C on date D"), surfaced through history views, not search hits.
- `current_only` and `as_of` are explicit views for both classes.
- This amends RFC 004 §9 "search surfaces head revisions only" for preserved memory. Because project history is not indexed, historical recall does not flood search with stale file versions.

### Eligibility

```text
eligible(o, T, view) =
    scope_ok(o)
    AND NOT suppressed_by_committed_erasure_fence(o)          -- regardless of T
    AND ( retention_class = index_current
            AND o ∈ head generation of its stream at T
        OR retention_class = preserve
            AND ( view ∈ {current_only, as_of}: o ∈ head generation of its stream at T
                  default/historical:          first publication containing o ≤ T ) )
```

All epochs are catalog epochs.

### Eligibility before the candidate budget

A fixed top-N query followed by a visibility filter cannot guarantee that eligible committed text appears; hidden or erased rows can consume the window.

- **Projection entries** are keyed per class and stream, `(content_id, retention_class, source_stream, generation_id)`, with vectors reused through `embedding_artifact`. Each entry's metadata is immutable.
- **First path (default backend):** Chroma's native `where` pre-filter on those immutable fields — `retention_class`, scope, and for project files `generation_id` of the current heads — applied before top-k, with over-fetch, then joined against catalog eligibility in SQL. Mutable state (currentness of preserved generations, erasure) is never written into vector metadata.
- **Delta:** an exact scan over the members of publications after the last verified projection checkpoint.
- **Ineligible backlog:** rows that became ineligible since the checkpoint (superseded project generations awaiting GC, erasure-fenced rows) are counted from the outbox. If that backlog could displace eligible hits within the over-fetch window, the query passes a bounded explicit `generation_id` exclusion or reports `incomplete`.
- Both the delta and the backlog are bounded with backpressure. A result is complete, or it is reported `incomplete` — never silently partial.
- **Full exact scan:** only for unsupported filters or a declared rebuild, reported as degraded.
- **Lexical:** FTS5 in the catalog over preserved payloads and `project_chunk`, using the same eligibility join. Backend-native lexical indexes (Chroma's `embedding_fulltext_search`, `sqlite_exact`'s FTS, the Rust exact store) are legacy once the catalog lands; public lexical/BM25 reads use only the catalog FTS5.

This does not turn HNSW into exact nearest-neighbor search. ANN approximation quality and revision-visibility starvation are tested separately against an exact oracle.

## Backend and deployment scope

| Deployment | Authority and initial guarantee |
| --- | --- |
| Local Chroma / `sqlite_exact` / `rust_exact` | Catalog is authority; vector stores are projections. For the exact backends the projector can apply vectors synchronously right after publication. |
| Hub with any local backend | Catalog lives in the hub's palace; all clients use the hub (current hub-forwarding behavior). |
| Shared Postgres/pgvector | Later: native revision/head tables in Postgres. |
| Independent clients on Qdrant/Milvus | Later: requires a shared publication authority. Per-client sidecars cannot provide shared currentness. |

Unsupported adapters do not silently downgrade retention: managed source-replacement paths fail explicitly at cutover until their authority is configured.

**Relationship to RFC 001.** Backends become projections of the catalog. `mempalace migrate` moves the catalog and rebuilds or passes through projections instead of reading documents through `BaseCollection.get` as the canonical source. RFC 001 needs a minor-version amendment with capability tokens for revision-aware eligibility; the ABC and conformance details follow once D1–D3 are accepted.

**Relationship to RFC 002.** Source adapters declare `retention_class` alongside ingest mode, following RFC 002 §6.2's no-downgrade pattern. RFC 002 §2.6 deletion tombstones and §1.7 closet purge become source-presence events: they remove project index generations and never purge preserved drawers. Reference mode is not RFC 002's `metadata_only` (a synthesized description drawer). "Accepted text" is the payload after RFC 002's declared transformations. Open PR #2226 (RFC 002 staged generation currentness) must be reconciled with this RFC; see [Converging the open work](#converging-the-open-work).

### D4: relationship to RFC 004 (proposed)

For preserved memory, this catalog is a single-replica precursor of RFC 004 step 2a's drawer-content state, not a competing store. Observations and publications carry `op_id`, `origin_replica` and `hlc` from day one; head-CAS is scoped per stream at the origin replica, consistent with RFC 004 §8 origin-only source maintenance. When step 2a lands, its op-log folds these records by mapping, with no payload re-import. Step 2a's `drawer.add`/`drawer.revise` ops reference occurrences, so no second public-ID migration or alias rewrite happens; RFC 004 §6.3's content-pure hash survives as `content_id`.

For project files, index generations are origin-local projections rebuilt from disk. They are not promoted as `drawer.add` ops; only `project_reference` metadata replicates, and a non-origin replica answers project hits from reference metadata with `unavailable` content unless it has the file. The Hub-authority row above is a current-deployment constraint, not the target replication model.

## Callers

Every caller below is routed through the catalog or fenced at cutover. Line numbers are at `0ed1be82`.

| Caller | Current effect | Target |
| --- | --- | --- |
| `convo_miner.py:716` (`write_file_drawers`) | Positional overwrite + prune | `ingest_memory` (preserve) |
| `convo_miner.py:78` `file_conversation_exchange` (live capture), `:253` registry sentinel | Upsert, no prune | `ingest_memory` (preserve); sentinel becomes catalog metadata |
| `sweeper.py:286` | Upserts message-level drawers | `ingest_memory` (preserve), occurrence key `(session_id, message_uuid)` |
| `diary_ingest.py:219` (positional entry IDs; prune when content changed, `:176`) | Overwrite + path-wide prune | `ingest_memory` (preserve) under D8 |
| `miner.py:1883` | Path-wide prune, every producer | `index_project_file`; replaces only its own stream |
| `format_miner.py:586` (empty sentinel, prune), `:690` | Prune within `format` mode | `index_project_file` |
| `tools_write.py:489` `add_drawer`; `tools_diary.py:9` `diary_write`; `tools_diary.py:550` `checkpoint` | Upsert (checkpoint dedups at 0.9) | `ingest_memory` (preserve); dedup stays an accepted-input rule |
| `tools_write.py:1439` `update_drawer` | Journals old text, overwrites same ID; next re-mine overwrites the edit | Preserved: `occurrence_revision` overlay. Project chunk: refused; capture is an explicit snapshot via the user-validated path |
| `tools_write.py:714` `delete_drawer`, `:1584` `delete_drawers` | Hard delete + revision purge | Explicit erasure intent, occurrence scope (fence first) |
| `tools_write.py:1047` `delete_by_source` | Path-wide delete across classes | Explicit erasure intent scoped by class and stream |
| `sync.py:703` `sync_palace` / `:490` `_delete_in_batches` | Deletes missing/gitignored rows of every producer | Preserved: presence event, history kept. Project: index generation removed, reference marked unavailable |
| `repair.py:723` `prune_corrupt` | Deletes rows flagged corrupt | Preserved: mark `integrity_state`, never delete canonical payload. Projections: rebuild |
| `encoding_repair.py:535, 595, 604` | Rewrites stored text in place | Preserved: `derivation` observation, original kept. Project: projection rebuild |
| `rooms.py:876`, `wing_split.py:260` | Placement update | `placement` record plus projection metadata; full epoch-versioned placement later |
| `palace/closets.py:234, 255` | Closet purge by path | Projection maintenance, keyed by generation, never by bare path |
| `media.py:320`, `sources/context.py:92` `upsert_drawer` | Upsert | Routed by the adapter's declared `retention_class`, or fenced at cutover |
| `revisions.py:28, 111` | Prior-text journal; purge on delete | Imported once byte-exactly, then frozen; purge becomes erasure |
| `file_already_mined` / prefetch, direct get/list/search, export, repair, migrate, rebuild | Read or rebuild from collection | Read view / catalog |

## Migration

1. Quiesce writers and take a consistent timestamped backup.
2. Classify provenance. Import preserved records (conversation, manual, diary, snapshot) with exact payload, original provenance and old IDs as aliases, **including stale parser/chunker rows and #2980 `historical_version` rows**. Import `drawer_revisions.jsonl` byte-exactly: read raw bytes, `json.loads` per line, keep undecodable or torn lines as `legacy_raw_record`; never through `load_drawer_revisions` (which opens with `errors="replace"`).
3. Project-file rows become the current index generation (`project_chunk`) of an `index_current` source with a reference observation; no past body is retained. KG `source_drawer_id`, tunnel and closet references to project drawers become `legacy_alias` entries resolving to `(source, anchor)`. Uncertain records stay preserved; ownership is never inferred from a path match alone.
4. Label incomplete or ambiguous legacy groups honestly. Do not invent a complete historical snapshot or reconstruct lost words.
5. Validate identities, hashes, aliases, KG/tunnel references and sampled current/history reads on a palace copy.
6. Shadow new captures against the old path, then cut over writers and all public readers together. Old destructive replacement paths stay unavailable for managed sources.
7. Roll back only through an explicit storage-format and backup plan; new observations are never silently discarded.

A format marker cannot fence an old binary that does not understand it. Stop or upgrade old hooks and services and verify an older writer cannot mutate the migrated catalog. Direct bypass clients are unsupported after cutover.

Repair rebuilds preserved projections from the catalog and project projections from the files on disk. Backup and restore include the catalog, pending attempts, aliases, placements and erasure fences. Export does not bundle project source files; absent files are unresolved after restore. Cached indexes carry catalog ID, epoch, schema and embedder identity; a mismatch triggers rebuild, never an empty successful palace.

## Explicit deletion

Mining never erases preserved content. User deletion becomes an explicit scoped erasure intent, consistent with #1593's delete verb:

| Scope | Maps to |
| --- | --- |
| One occurrence | `drawer_id` |
| One source revision | one observation/generation (`parent_drawer_id` in #1628) |
| A source's full history | source (+ optional ingest fence until capture is re-enabled) |
| Every occurrence of shared text | `content_id` |

#1628's `stack_id` delete target is not carried forward as its own scope, because `stack_id` is a presentation hint under D3; a stack resolves to its occurrences.

- Erasure is an overlay. A committed fence suppresses the scope first, so replay and re-mine cannot resurrect it; occurrences and generations stay immutable and fenced members verify as `erased`, not corrupt.
- Physical reclaim then reference-counts blobs, embedding artifacts, lexical rows and projection entries across sealed generations, live attempts, aliases and imports, and runs `secure_delete` or `VACUUM` plus `wal_checkpoint(TRUNCATE)`. Outstanding physical cleanup is tracked; a tombstone alone is not advertised as physical erasure. The content hash remaining in fences is documented.
- Erasing the current revision suppresses the head; it never silently promotes an older revision to current.
- Old read views honor erasure; restoring an older backup applies known later fences before serving. Copies the system does not control are not claimed erased.
- Deleting a project reference removes palace metadata and index rows; it never deletes the file or preserved records at the same path.

RFC 004's "tombstone hides, never deletes" needs an amendment for user-requested erasure when step 2a lands.

## Converging the open work

| PR / issue | Disposition |
| --- | --- |
| #2980 (draft) | Extract the containment slice (delivery step 1); keep its preservation, equal-content retry, body/scope-race and real-kill tests. Its positional overwrite/finalize machinery is scaffolding removed when the catalog owns publication. |
| #2978 (draft, tests) | Adopt its two xfail cases (injected second-batch failure; successful rewrite keeps history) as the first failing gates for the rewrite and interrupted-publication rows. Shrink and real process-kill gates come from #2980's tests and new cases. |
| #2799 | Close as superseded: its classifier purges older normalize/chunker generations, the defect this RFC rejects, and it cannot recover overwritten positional text. Its "Fixes #2793" would close the issue prematurely. |
| #2439 | Close or reduce: develop already reuses vectors for unchanged drawers (`writes.py:62`, `_split_unchanged`). Keep only its skip-upsert cases as A→A no-op gates; its separate conversation write path and orphan deletion are dropped. |
| #2608 | Accept for project files: a file that shrinks below `min_chunk_size` becomes an empty current index generation and its old rows leave the index. For conversations, an empty current generation keeps its history. |
| #2226 (RFC 002 currentness) | Reconcile before merge: its staged generation and activation map to observation/generation/publication here; its generation cleanup/GC may apply only to `index_current` sources and must never collect preserved generations. |
| #1628 | Its project-miner body retention and `filed_at` IDs are not carried forward (D1). Its delete verb, parent/version navigation and stack presentation are reused on current code; its old diff, which targets the removed `palace.py`/`searcher.py` monoliths, is not merged wholesale. |
| #1593 | Satisfied for preserved memory; amended for project files by D1. Post the decision on the issue. |

## Scope: v1 and later

**v1** — the catalog file and the records above; preserved payloads; shared manifest pages; publication-derived currentness; conflict-to-history publication; manual-edit overlays; project index generations with reference history and projection GC; SQL eligibility with Chroma pre-filter, bounded exact delta and backlog handling; catalog FTS5; embedding artifacts; view leases; erasure overlay with restore-time fences; byte-exact migration/import; every caller in the table.

**Later** — native Postgres adapter; remote/shared authority for Qdrant/Milvus; streaming request reservations; full epoch-versioned placement and aliases; RFC 001 ABC and conformance arms; ANN projection qualification beyond the immutable-field pre-filter.

## Delivery plan

1. **Containment (D5)**, small and independently reviewable, before the catalog. It narrows loss; it does not close #2793.
   - No caller, whatever its `extract_mode`, deletes `historical_version` rows. A project or diary mine deletes only rows it can positively identify as its own (its miner metadata signature, no `ingest_mode`/`extract_mode` of another producer, no manual-tool `added_by`, not `historical_version`); ambiguous rows are kept and reported.
   - Stale parser/chunker conversation rows are archived before replacement, not pruned.
   - Diary archives a changed entry body before an upsert reuses its entry-index ID, and stops pruning on content change.
   - `sync --apply` skips conversation, sweep, live-exchange, manual and `historical_version` rows and reports them as source-missing; `delete_by_source` requires an explicit class scope and defaults to project rows.
   - Regressions: convo-mine a `.md`/`.jsonl` transcript, edit it, then project-mine its parent folder (current and archived conversation text both survive); edit and front-insert a diary entry and re-ingest (both texts survive); `sync --apply` with a rotated-away transcript (drawers survive).
2. Agree D2–D4 and D8 with the maintainer and #1628's owner; freeze identities, receipts and view semantics.
3. Implement the catalog: sealing, idempotent ingest, conflict handling, recovery, projection GC and view leases, under the writer lease.
4. Route conversations, sweeper, live capture, manual and diary writers through `ingest_memory`, and all public readers through the read view, on an isolated palace. Add eligibility with Chroma pre-filter, exact delta and backlog handling, and catalog FTS5.
5. Route project and format miners through `index_project_file`; update sync, export, repair, closets and contributor guidance (CLAUDE.md scope).
6. Route `delete_drawer(s)`, `delete_by_source`, `update_drawer`, `prune_corrupt`, `encoding_repair`, `rooms`/`wing_split`, adapter writers and the `revisions.py` import through the catalog; implement erasure fences, physical reclaim and restore-time fence application.
7. Migrate a copied real-scale palace, measure, then activate.

## Closing #2793

#2793 closes when, on the default local backend (Chroma, plus the `sqlite_exact`/`rust_exact` local adapters):

- no caller deletes a prior unique preserved body, and no project caller deletes another class's rows;
- current reads resolve to one committed head for both classes, with immutable staging and atomic publication;
- eligibility is applied before the candidate budget, or the result is reported incomplete; concurrent readers see one view;
- failed storage or publication never reports success;
- the release-gate rows marked **#2793** pass through both the conversation and project callers where applicable.

Postgres and remote adapters, streaming reservations, full epoch-versioned placement and ANN qualification beyond the pre-filter are separate follow-up issues.

## Release gates

Rows apply to preserved memory unless marked *project*. Rows marked **#2793** are the closing set.

| Scenario | Required result |
| --- | --- |
| **#2793** A→A / repeated request | No duplicate payload or logical observation, no unnecessary embedding |
| **#2793** Same token, different input/profile/head | Explicit idempotency conflict, never an old success |
| **#2793** A→B→A | Three observations; A's occurrences and `drawer_id`s reused; `as_of` between the two A epochs returns B |
| **#2793** Shrink / rewrite / front insertion / rechunk | Every prior exact body retrievable; current generation complete and ordered |
| **#2793** Stale parser/chunker / same-mtime edit | Old payload retained with recipe label; no false mined-success shortcut |
| **#2793** Empty or below-threshold source | Valid empty current generation with reason; history retained |
| **#2793** Kill at every stage/seal/publish/receipt point, and after publish before GC — conversation **and** *project* callers | Fresh process sees complete old or complete new head; pending never becomes committed; leftover superseded project rows never eligible |
| **#2793** Lost acknowledgment, retry — conversation **and** *project* | Same committed result, no duplicate |
| **#2793** Out-of-disk / short write / embedder failure at stage, seal or publish | Receipt `failed` or `pending`, never committed; mined marker not advanced; previous head current |
| **#2793** *Project* re-mine, sync or delete at a path that also holds conversation/manual rows | Only the project stream changes |
| **#2793** Edit a mined drawer, then source change, then re-mine | Manual revision survives and stays current for its occurrence |
| **#2793** More hidden candidates than the old pool cap | Eligible hits still returned, or explicit incomplete |
| **#2793** Concurrent get/list/search/neighbor/page reads, including a *project* cursor across a re-mine | One view; no old-body/new-metadata mix; project cursor completes or expires explicitly |
| Competing or stale publisher | Losing preserved observation returned by default recall; deterministic currentness by HLC |
| *Project* chunk superseded whose text also occurs in a preserved conversation | Conversation passage stays semantically and lexically searchable |
| Two sources share text / repeated turns | Storage may dedup; authorship, placement, occurrence count and times stay distinct |
| Presence/placement/alias change during a view | Epoch-consistent, subject to erasure barriers |
| Lost/corrupt/lagging index | Canonical text survives; rebuild or explicit degraded state |
| Export/restore / old aliases / explicit deletion | Exact data and provenance survive; fences prevent resurrection |
| *Project* file changed, moved or missing | Current index generation replaced or marked unavailable; reference history kept; never mislabeled content |
| *Project* file shrinks below threshold (#2608) | Empty current index generation; old rows leave the index |
| *Project* indexing / backup / export | Current index and reference history only; no past bodies or their vectors |
| Transcript under a project root / agent-written project document | Transcript preserved; document indexed as a project file |
| Explicit file snapshot / reference deletion | Only the requested scope preserved, via a user-validated action; deleting a reference never deletes the file or preserved records |

Run with real temporary SQLite and native Chroma on Linux, macOS and Windows, with multiple readers, actual process kills, out-of-disk/short-write cases and source mutation during capture. In-memory mocks alone are insufficient.

Measure, on long real-equivalent transcripts **and** large project corpora: cold/warm read latency, retrieval quality before and after on project corpora, wall time, RSS, disk/WAL growth under concurrent readers, embedding calls, rewritten manifest pages on rechunk, delta and backlog size and cost, cold-process search latency at the maximum delta, and queued-reader latency. Hooks stay under 500 ms and startup injection under 100 ms. No performance gain or power-loss qualification has been demonstrated yet.

## Open questions

1. Manifest page size and fan-out for very long transcripts — decide by measurement.
2. Whether `checkpoint`'s 0.9 near-duplicate threshold should stay an accepted-input rule once dedup is content-addressed.
3. Timeouts: embedder outage before degraded publication; PreCompact capture before reporting pending; view-lease TTL and GC grace window.
4. Which user-validated surface creates explicit snapshots in v1: a CLI verb, an MCP tool with host-confirmed user intent, or both.

## Sources

- [#2793](https://github.com/MemPalace/mempalace/issues/2793) and its [triage brief](https://github.com/MemPalace/mempalace/issues/2793#issuecomment-6846879987); [#1593](https://github.com/MemPalace/mempalace/issues/1593) and the [2026-05-23 question](https://github.com/MemPalace/mempalace/issues/1593#issuecomment-4525408932) / [2026-05-24 ruling](https://github.com/MemPalace/mempalace/issues/1593#issuecomment-4528522461).
- Develop: [writer](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/mempalace/palace/writes.py#L150), [project miner](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/mempalace/miner.py#L1883), [backend contract](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/mempalace/backends/base.py#L477), [prior-text journal](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/mempalace/revisions.py#L28), [sync deletion](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/mempalace/sync.py#L490).
- #2980: [writer](https://github.com/MemPalace/mempalace/blob/ecbff26c83724c57db387793817f94a001a0ecb8/mempalace/palace/writes.py#L19), [visibility guards](https://github.com/MemPalace/mempalace/blob/ecbff26c83724c57db387793817f94a001a0ecb8/mempalace/convo_visibility.py#L1), [bounded candidate query and guard](https://github.com/MemPalace/mempalace/blob/ecbff26c83724c57db387793817f94a001a0ecb8/mempalace/searcher/query.py#L336-L366), [real-kill regression](https://github.com/MemPalace/mempalace/blob/ecbff26c83724c57db387793817f94a001a0ecb8/tests/test_convo_real_crash_restart.py).
- [#1628](https://github.com/MemPalace/mempalace/pull/1628); [RFC 004 merge semantics, id purity and op mapping](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/docs/rfcs/004-replicated-palace.md#L350-L411), [step 2a decision](https://github.com/MemPalace/mempalace/blob/0ed1be82dcbe4f59081b87c4eaa0986af0768ff4/docs/rfcs/004-replicated-palace.md#L452-L461).
- [SQLite isolation](https://www.sqlite.org/isolation.html), [synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous), [Chroma query pipeline](https://cookbook.chromadb.dev/core/advanced/queries/), [pgvector filtering](https://github.com/pgvector/pgvector#filtering).
