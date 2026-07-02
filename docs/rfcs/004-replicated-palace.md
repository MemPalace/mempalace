# RFC 004: The Replicated Palace

Status: Draft — architecture settled, sections in progress
Owners: mac-claude (storage layers, §6–§9), windows-claude (transport & lifecycle, §5 and Appendix A), decided by Igor
Created: 2026-07-02
Branch: `feat/shared-brain-dogfood`
Prior art: RFC 003 (logstream), the `rfc004_replicated_palace_position` correlation thread (position debate, verbatim in the logstream)

## Summary

Each human has ONE palace — an extension of their brain — replicated in full
across every machine they own. Agents always talk to the MemPalace service on
`127.0.0.1`; services converge with each other over an encrypted mesh. The
hub stops being a dependency and becomes a role (rendezvous, and the home of
*shared* palaces). The design is judged against offline operation as the
default posture, not as an edge case.

One sentence: **N equal replicas of the facts, each with locally-derived
indexes and a local writer, converging through provenance-stamped ops over
the mesh — with origin as the home-of-record for source-bound maintenance.**

## Motivation

The shared-brain dogfood proved the hub topology works — and watched it fail:
when the hub machine slept, every other machine lost recall, capture, and
coordination simultaneously. A brain does not stop remembering because
another brain is asleep. Concretely, today a remote machine without the hub
has **no palace at all**.

The mission statement ("memory is identity") implies the requirement
directly: you do not rent your identity, and you do not park it on a single
machine.

## Requirements

The availability invariant, and the offline requirements distilled from the
fleet (logstream correlation `rfc004_replicated_palace_position`):

- **R0 — Mission invariants hold everywhere**: verbatim always, local-first,
  zero external API for core operations, hooks < 500 ms, startup injection
  < 100 ms. A design that meets availability by adding a network round-trip
  to recall fails R0.
- **R1 — Availability invariant**: recall reads and capture writes never
  block on the network; only convergence may wait.
- **R2 — Task freshness**: delegations to offline agents must not execute
  stale on rejoin (optional `expires_at` on `task.request`; re-check the
  correlation for `superseded` before acting on an old claim).
- **R3 — Partition claims**: duplicate task claims across replicas resolve
  deterministically post-merge (earliest HLC wins; the loser yields with
  `superseded`). Append-only makes double work safe-but-wasteful, never
  corrupting.
- **R4 — Offline capture**: hooks write to the local replica unconditionally;
  organization-op conflicts merge LWW-by-HLC and the merge is **surfaced**
  to the user, never silent.
- **R5 — Presence**: per-agent last-seen derived from log activity (plus
  device-level liveness from the transport layer) so requesters route around
  dead machines instead of burning `event_wait` timeouts.
- **R6 — Rejoin via snapshot + tail**: op-log compaction and snapshot
  bootstrap are v1 requirements; a months-offline replica cannot replay
  history.
- **R7 — Lost-device threat model**: N replicas put the whole brain on every
  device; encryption at rest per replica is mandatory, and the mesh must
  support membership revocation for a lost machine.
- **R8 — Replication is not backup**: tombstones propagate; snapshot backups
  remain a separate concern.

## Non-Goals

- **Replacing the hub for shared palaces.** Federation (team palace,
  `shared_agent_brain`) is a different problem: a shared organ legitimately
  has a home and benefits from strong consistency. This RFC covers the
  *personal* palace; federation keeps the RFC 003 hub model.
- **Cloud as a system of record** — constitutionally excluded. The only
  admissible cloud role is an optional end-to-end-encrypted blob courier for
  op-sync when no two personal machines are online simultaneously, plus
  encrypted offsite snapshots. Zero knowledge; never queryable server-side.
- **Thin/partial replicas (phone-class devices) in v1.** They remain remote
  clients of a nearby full replica; partial replication is future work.
- **Multi-user merge semantics.** One human, N devices. Trust boundary =
  palace boundary.

## Architecture Overview

```
  Machine A (mac)                Machine B (windows)          Machine C (laptop)
  ┌─────────────────────┐        ┌─────────────────────┐      ┌──────────────────┐
  │ agents → 127.0.0.1  │        │ agents → 127.0.0.1  │      │ agents → local   │
  │ ┌─────────────────┐ │  ops   │ ┌─────────────────┐ │ ops  │ ┌──────────────┐ │
  │ │ mempalace svc   │◀┼───────▶│ │ mempalace svc   │◀┼─────▶│ │ mempalace svc│ │
  │ │  op-log (SoT)   │ │  mesh  │ │  op-log (SoT)   │ │ mesh │ │  op-log (SoT)│ │
  │ │  derived index  │ │        │ │  derived index  │ │      │ │ derived index│ │
  │ └─────────────────┘ │        │ └─────────────────┘ │      │ └──────────────┘ │
  └─────────────────────┘        └─────────────────────┘      └──────────────────┘
         encrypted mesh transport (Layer 1) · anti-entropy op sync (Layer 2)
```

Three layers, separable by design:

1. **Transport (Layer 1)** — encrypted peer connectivity, membership, and
   device identity between the machines. Owner: windows-claude (§5).
2. **Sync (Layer 2)** — a canonical append-only op-log per replica, merged by
   union with small domain-specific semantics. Owner: mac-claude (§6).
3. **Derived state (Layer 3)** — vector indexes, embeddings, caches: rebuilt
   locally per device, never synced. Owner: mac-claude (§7).

The decisive property: **sync the facts, derive the senses.** Ops are
kilobytes; HNSW graphs are gigabytes. Every machine remembers everything;
each machine senses with its own hardware.

## Alternatives Considered

| Dimension | Replicated mesh | Self-hosted central server | Cloud backend |
|---|---|---|---|
| Offline / partition | Full function, converge later | Dead when server/link down | Dead without internet |
| Recall latency | Local, sub-ms–ms | LAN ms / tailnet 10–30 ms | 50–150 ms+ |
| Privacy | Never leaves your devices | Your hardware, one exposed box | Provider sees plaintext or E2EE cripples search |
| Durability | N live replicas (+R8 backups) | One box | Best-in-class |
| Ops burden on user | ~zero if software earns it | Forever (patching, TLS, backups) | ~zero |
| Engineering complexity | High, paid once by us | ~zero (exists today) | Zero for user, trust cost |
| Consistency | Eventual + merge semantics | Strong, trivially | Strong |
| Teams / sharing | Wrong tool | Natural | Natural |
| Exit / lock-in | SQLite files you hold | Files you hold | Provider's mercy |

Verdicts: the mesh is the only option satisfying R0+R1 for the personal
palace (a cloud round-trip spends the entire hook budget on network; central
fails the availability invariant we watched fail in production). The central
server remains the *correct* model for shared organs (federation). Cloud is
admissible only as the E2EE courier of the Non-Goals section.

## §5. Layer 1: Transport — OWNER: windows-claude (stub)

Agreed constraints the section must satisfy (from the position thread):

- **Transport-agnostic seam.** Layer 2 requires only mutually-authenticated
  request/stream channels between named peers. Concrete transports plug in
  per-link: MeshGuard (preferred), Tailscale (fallback/bridge), bare LAN.
- **MeshGuard is the target transport** (dogfooding both directions):
  Ed25519 node identity doubles as `origin_replica` identity; SWIM
  membership supplies device-level presence (R5); permissioned membership is
  the replication ACL and the R7 revocation mechanism. Security gate status:
  the 2026-06-06 adversarial review's H1 (inner-source-IP spoofing on the
  userspace plane) is remediated with regression tests (meshguard PR #101);
  remaining pre-integration checks are a trust-path sweep of post-review
  commits and an FFI-consumer pass at integration time.
- Sections to write: peer addressing & rendezvous, NAT traversal posture,
  connection lifecycle, replica join/leave/revoke ceremony, key custody,
  transport fallback matrix, failure detection → presence mapping.

## §6. Layer 2: The Canonical Op-Log

### 6.1 The op envelope

Every mutation of the palace becomes an immutable op:

```json
{
  "op_id": "op_<origin>_<counter>",
  "origin_replica": "<transport identity of the writing machine>",
  "author_agent": "mac-claude",
  "hlc": "0189f3a2-0007-mac",
  "authored_at": "2026-07-02T21:14:09Z",
  "kind": "drawer.add | drawer.revise | drawer.tombstone | org.file | org.move | org.tunnel.add | org.tunnel.remove | kg.assert | kg.close | kg.entity.upsert | registry.entity.upsert | event.append | artifact.put | ...",
  "payload": { "...kind-specific, verbatim content inline or by sha256..." }
}
```

- `hlc` is a hybrid logical clock (physical ms + logical counter + replica
  tiebreak): total order across replicas without clock trust.
- Per-origin logs are strictly ordered by a local counter; a replica's state
  is a **version vector** {origin → highest counter applied}.
- Storage: `oplog.sqlite3` in the palace dir, append-only, WAL — the
  logstream pattern (RFC 003) generalized; that pattern is production-proven.

### 6.2 Merge semantics (complete list — nothing else exists)

| State | Op kinds | Merge rule |
|---|---|---|
| Drawer content | add / revise / tombstone | Grow-only set of content-addressed revisions; head = latest by HLC; tombstone hides, never deletes (verbatim survives) |
| Artifacts | artifact.put | True G-set, union by sha256 — conflicts impossible |
| Organization | org.file / org.move / org.tunnel.* | LWW-by-HLC register per drawer (placement) / OR-set (tunnels); merges surfaced to user (R4) |
| Knowledge graph | kg.assert / kg.close / kg.entity.upsert | Assert = G-set; close = interval-close (idempotent, min valid_to wins); entity upsert = LWW-by-HLC |
| Registry | registry.entity.upsert | LWW-by-HLC per entity key (replaces whole-file JSON write) |
| Logstream | event.append | Append-only union; cross-replica order by HLC; per-origin `seq` preserved; consumer contract additive (`origin_replica`, `hlc` are new fields) |
| Diary | drawer.add in diary rooms | Same as drawer content (already append-only) |

Anti-entropy: peers exchange version vectors and pull missing per-origin
ranges (`GET /sync/ops?origin=X&after=N`), push-notified over the existing
SSE channel. No broker, no framework: automerge/yjs are document-CRDTs
(wrong shape, heavy deps); cr-sqlite is a native extension whose generic
table-CRDTs know nothing of id purity or verbatim; file-level sync of live
SQLite corrupts. The merge logic above is ~hundreds of lines we fully own.

### 6.3 Id purity (prerequisite, not footnote)

Verified against the code (2026-07-02): today's drawer identities are NOT
content-addressed. Miner drawers hash `(source_file, chunk_index)` — re-mining
rewrites content in place under the same id; MCP drawers hash
`(wing, room, content)` — organization lives inside identity;
`tool_update_drawer` mutates in place; dedup deletes.

**v4 identity recipe**: `drawer_<hash(content)>` — identity is the verbatim
content alone. Organization (wing/room) becomes op-carried metadata
(`org.file`); location provenance (`source_file`, `chunk_index`) becomes
plain metadata; revision chains link content-addressed revisions. `ids.py`
already versions recipes (`ID_RECIPE` v1→v3 precedent) and drawers carry
`id_recipe` metadata, so migration is an audited rewrite with a legacy-id
alias table for inbound references (tunnels, KG `source_drawer_id`).

### 6.4 Mutable-state inventory → op mapping (verified, with file refs)

| Today (mutable) | Where | Becomes |
|---|---|---|
| `tool_update_drawer` in-place update/upsert | mcp_server.py | `drawer.revise` (new content-addressed revision) |
| Miner re-mine upsert over same id | miner.py:1336,1478 | `drawer.revise` at origin replica only (§8) |
| `delete_drawer` / `delete_by_source` / dedup batch delete | dedup.py:127 | `drawer.tombstone` (hide, never destroy) |
| Entity registry whole-file `json.dumps` | entity_registry.py:328 | `registry.entity.upsert` op stream |
| `hallways.json` whole-file rewrite | hallways.py:140 | `org.tunnel.add/remove` OR-set ops |
| KG `invalidate` UPDATE of valid_to; entities INSERT OR REPLACE | knowledge_graph.py | `kg.close` interval op; `kg.entity.upsert` |
| repair / migrate / dedup wholesale rewrites | repair.py, migrate.py | replica-local maintenance of derived state (never synced) |

Hardest today, cleanest after: the two whole-file JSONs (currently pure
last-writer-wins with silent loss) gain real merge semantics for free.

## §7. Layer 3: Derived State

The vector store stops being the system of record — that single change
dissolves the fleet-level writer lease, the stdio proxy's reason to exist,
and the cross-machine integrity gates. Chroma/Qdrant/pgvector/sqlite_exact
become **fold-and-index consumers** of the op-log, each rebuilt or
incrementally folded locally.

- Precedent already in-repo: `repair --mode from-sqlite` rebuilds the vector
  index from content; embeddings are already treated as re-derivable.
- Embedder identity stays per-replica (RFC 001): pin one model fleet-wide or
  accept per-device vector spaces — legal because queries execute locally.
- **The lease demotes, it does not dissolve**: per-replica single-writer
  over local index state remains (HNSW physics); what disappears is
  cross-machine write arbitration.
- Organization is NOT derived state. Filing decisions sync as ops (§6.2):
  the method of loci means the layout IS the memory; two replicas clustering
  differently would give the user two different palaces.

## §8. Provenance & Source-Bound Maintenance

Every op carries `(origin_replica, author_agent, hlc, authored_at)` —
simultaneously the sync unit, the conflict tiebreak, the audit trail, and
the answer to "which machine/agent did this memory come from."

Local references are replica-local: a drawer mined from `P:\...` on Windows
references a path that exists only there. The memory replicates everywhere;
**source-bound maintenance does not**: re-mining after file edits,
`repair` against origin files, and `delete_by_source` execute only at the
origin replica (other replicas receive the resulting ops). The mesh must
know: every memory lives everywhere, but its umbilical cord attaches to one
machine.

## §9. Sequencing

0. **Logstream multi-master (pilot)** — already append-only; add
   `origin_replica` + `hlc` (additive to the viewer contract), per-origin
   logs, HLC ordering, artifact union by sha256. Smallest surface; fixes the
   pain that motivated everything (coordination dies with the hub).
1. **Read replicas for memory** — content snapshot + op tail; indexes
   derived locally (never copy `chroma.sqlite3` — that replicates Chroma's
   fragility to N machines). Cheap availability for recall; builds the R6
   snapshot machinery.
2. **Canonical op-log + v4 id migration** — §6 in full; backends demoted to
   derived consumers.
3. **Full multi-writer** — every replica captures locally, all converge.

Each step ships value alone; none blocks on Layer 1 choice (seam, §5).

## §10. Security — shared ownership (stub)

R7 expansion: per-replica encryption at rest (whole brain on every device),
mesh membership as replication ACL, revocation ceremony for lost devices,
key custody across replicas. windows-claude drafts transport-side; mac-claude
drafts at-rest side. Explicitly: replication ≠ backup (R8) — encrypted
offsite snapshots remain a separate mechanism.

## Open Questions

- HLC skew bounds and how loudly to surface clock anomalies.
- Op-log compaction policy (R6): checkpoint cadence, tombstone retention.
- Does the E2EE cloud courier ship in v1 or wait for demand?
- Partial replicas for phone-class devices (deferred; Non-Goals).
- Federation bridge: can a personal replica project shadow wings into a
  team hub palace mechanically, or is that manual today?

## Appendix A: PalaceMind — OWNER: windows-claude (stub)

Replica status surfacing (the Live/Polling honesty flag generalizes to
replica lag / version-vector drift), merge-surfacing UX (R4), presence
rendering (R5), and the viewer against a multi-origin logstream.
