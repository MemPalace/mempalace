# RFC 004: The Replicated Palace

Status: Draft complete — all sections drafted (storage: mac-claude; transport/lifecycle/appendix: windows-claude); awaiting Igor's review

Shipped so far: step 0 (logstream multi-master replication) and step 1 (memory read replicas — snapshot pull, local fold, distributed embedding), plus the transport seam and the estate endpoint. Step 2a (the memory op-log, anti-entropy, fold/promote) and the v4 content-pure id migration + write-flip are designed here but not yet landed on develop — they carry a palace migration and are staged for a later release.
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
- **Multi-user merge and authorization semantics.** One human, N devices.
  Trust boundary = palace boundary. Mesh membership is full-replica trust,
  not family/team scoped read or write permission.

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

## §5. Layer 1: Transport

Layer 1 exists so Layer 2 never has to think about networks, keys, or which
machines are awake. It carries ops between replicas and answers one question
for the layers above: *which of my peers can I reach right now, and are they
who they claim to be.* Nothing about merge semantics lives here; nothing
about sockets leaks up.

### 5.1 The seam (what Layer 2 is allowed to assume)

Layer 2 sees peers only through this interface — the entire contract:

```
Transport {
  self(): ReplicaId                      // stable, = mesh node identity
  peers(): Map<ReplicaId, Presence>      // membership + liveness snapshot
  onPresenceChange(cb)                   // SWIM/heartbeat deltas → R5

  request(peer, path, body): Response    // one authenticated round-trip
  openStream(peer, path): EventStream    // long-lived, resumable by cursor
  onInbound(path, handler)               // serve anti-entropy pulls
}
```

Two channel shapes cover every need: **request** for anti-entropy pulls
(`GET /sync/ops?origin=X&after=N`, §6.2) and **stream** for push-notify of
new ops (the RFC 003 SSE surface, re-pointed peer-to-peer). Both are
mutually authenticated — a `request` whose caller identity isn't a current,
non-revoked member is refused at Layer 1, before Layer 2 sees a byte. That
refusal is the replication ACL (R7) and it is the *only* authorization Layer
2 relies on: an op that arrived is an op from a trusted replica.

This is deliberately a v1 device-trust boundary, not a user/role permission
model. Admitting a replica means admitting it to the entire personal palace,
verbatim content included. Delegated, family, team, or other partial-access
workflows need a separate palace-level capability layer; they must not be
modeled as "just add this device to the replication mesh."

`ReplicaId` is the transport's node identity — no separate replica registry
to drift. Under MeshGuard it is the Ed25519 public key; that same value is
the `origin_replica` stamped into every op (§8), so provenance and
authentication are one fact, established once at the transport and trusted
everywhere above.

### 5.2 Peer addressing & rendezvous

Replicas are named by identity, never by address — a laptop's IP changes
between café and home; its `ReplicaId` does not. Address discovery is the
transport's job:

- **MeshGuard / Tailscale**: the mesh's own coordination plane maps identity
  → current endpoint(s); we never hardcode IPs. A replica "moves" and peers
  re-resolve transparently.
- **Bare LAN** (no mesh): mDNS/`_mempalace._udp` discovery within a
  broadcast domain, identity verified by the membership key (§5.5) — an
  address hint is never a trust grant.
- **No global directory.** Rendezvous is peer-to-peer within the mesh
  membership set. The old hub becomes *a* rendezvous helper for personal
  replicas (and stays the home of shared palaces per Non-Goals), never a
  required broker: two personal machines on the same LAN converge with the
  hub asleep.

### 5.3 NAT traversal posture

Convergence must survive both machines being behind NAT on different
networks (R1 across the internet, not just the LAN):

1. **Direct** when a route exists (same LAN, or one side reachable).
2. **Hole-punched P2P** via the mesh coordination plane (WireGuard-style for
   MeshGuard/Tailscale) — the common cross-network case, still zero data
   through any third party.
3. **Relay** only when hole-punching fails: encrypted frames pass through a
   mesh relay that cannot read them (payloads are already E2E-encrypted at
   Layer 1). This is transport relay, categorically distinct from the
   constitutionally-excluded cloud-as-SoR (Non-Goals) — the relay sees
   ciphertext, never palace content, never queries.
4. **Never-simultaneously-online** is a real fleet state (desktop by day,
   laptop by night). Two escape hatches, both opt-in: LAN sync when they do
   overlap, and the Non-Goals E2EE blob courier (a replica pushes an
   encrypted op-bundle the other pulls later). The courier is a §5 consumer
   of the same op ranges, not a new path.

### 5.4 Connection lifecycle

- **Discover → authenticate → sync-on-connect → tail.** On establishing a
  link a replica exchanges version vectors (§6), pulls the deltas each side
  is missing, *then* subscribes to the peer's live op stream — snapshot then
  tail, the pattern windows-codex asked us to name (identical to the viewer's
  `event_list` cursor → SSE `Last-Event-ID` bootstrap, RFC 003). No live-only
  connect: the gap between snapshot and stream-open is closed by cursor, so
  no op is ever missed on reconnect.
- **Backpressure & retry** mirror the logstream tail already shipped:
  bounded concurrent streams, `503 + Retry-After` under load, exponential
  reconnect with the version vector as the resume cursor. A flapping link
  degrades to periodic anti-entropy pulls, never to lost ops.
- **Idempotent by construction.** Re-delivering an op is a no-op (union
  merge, §6.2), so the lifecycle can be as dumb and as retry-happy as it
  likes — correctness lives in Layer 2, not in careful delivery.

### 5.5 Replica join / leave / revoke ceremony

Membership *is* trust; there is no other authorization in the system.

- **Join** (adding a new personal device): the new replica generates its
  Ed25519 identity locally (private key never leaves it). An existing
  authenticated replica admits it to the mesh membership set — a physical-
  presence / existing-device action by the human, not a password. First sync
  is a full snapshot + tail (§6, R6). Because organization syncs as ops, the
  new device reproduces the *same* palace, not a re-clustered one.
- **Leave** (graceful): a replica can announce departure; peers keep its
  historical ops forever (they are provenance) but stop expecting presence.
- **Revoke** (lost/stolen device — R7): the human revokes a `ReplicaId` from
  any surviving replica; the removal propagates as a membership op and every
  peer refuses further connections from that identity. Revocation cannot
  reach into the lost device — hence R7's mandatory at-rest encryption
  (mac-claude, §10): revocation stops the *network*; encryption protects the
  *disk already gone*. Ops that identity authored before revocation remain
  valid history (revoking a device is not disavowing its memories); only its
  future write access is severed.

### 5.6 Key custody

- **Device key**: Ed25519 private key per replica, generated on-device,
  non-exportable, OS keystore where available (Keychain / DPAPI / kernel
  keyring). It is the node's whole identity — losing it = re-join as a new
  replica; it is never synced.
- **Membership authority**: which identities are trusted is itself
  op-carried state (a membership OR-set with revocation tombstones), signed
  by an admitting device — so "who is in the mesh" converges like everything
  else and survives any single machine's loss.
- **At-rest data key** (the palace on disk, R7) is a *separate* concern
  owned by mac-claude in §10; §5 owns only the identity/membership keys that
  gate the wire. The two never mix: a compromised transport key exposes no
  plaintext, a stolen disk exposes no network.

### 5.7 Transport fallback matrix

Per-link, best available wins; the seam (§5.1) makes the choice invisible
to Layer 2.

| Situation | Transport | Notes |
|---|---|---|
| Same LAN, mesh up | MeshGuard direct | Lowest latency; hub not involved |
| Cross-network, both online | MeshGuard hole-punched (Tailscale bridge if MeshGuard not yet integrated) | E2E, P2P |
| Hole-punch fails | Encrypted mesh relay | Ciphertext only; distinct from cloud-SoR |
| Mesh unavailable, same LAN | Bare LAN + mDNS, membership-key auth | Degraded discovery, full trust |
| Never simultaneously online | E2EE blob courier (Non-Goals) | Async op-bundle exchange |
| MeshGuard pre-integration | Tailscale (today's dogfood transport) | Ship Sequencing step 0–1 before MeshGuard lands |

MeshGuard is the target; **Tailscale is the shipping fallback that unblocks
Sequencing steps 0–1 today** (it already carries this very logstream). The
seam guarantees swapping in MeshGuard later touches no Layer 2 code.

MeshGuard pre-integration checklist (gate, not blocker): the 2026-06-06
review's H1 (inner-source-IP spoofing on the userspace plane) is remediated
with regression tests (meshguard PR #101, RX cryptokey-routing check in
`decryptTransport`); all Criticals/Highs fixed across 17 hardening commits.
Before MeshGuard becomes the default link: (a) a trust-path sweep of
post-review commits, (b) an FFI-consumer pass where the daemon binds it.
Until both pass, Tailscale carries production and MeshGuard rides behind the
seam in test.

### 5.8 Failure detection → presence (R5)

Presence has two sources; the transport fuses them so the layers above ask
one question:

- **Device liveness** (Layer 1): SWIM-style failure detection from the mesh
  (MeshGuard membership; heartbeat pings on bare LAN). Answers "is the
  machine reachable *now*," sub-second, and drives reconnect.
- **Agent liveness** (derived, Layer 2): per-agent last-seen from op/log
  activity (RFC 003), answering "when did this seat last do anything."

`Presence = { reachable: bool (device), lastSeen: hlc (agent), replicaId }`.
Requesters use it to route around dead machines instead of burning
`event_wait` timeouts, and it is what the PalaceMind viewer renders
(Appendix A). This is also the substrate for **R2 (task freshness)** and
**R3 (partition claims)** on the wire:

- **R2**: a `task.request` with `expires_at` is still *delivered* to an
  offline agent (ops never drop), but presence tells the requester the agent
  was unreachable across the gap, so the etiquette check — "re-read the
  correlation for `superseded`/expiry before acting on a claim older than
  its transport gap" — has the data it needs. Layer 1 supplies the gap;
  Layer 2 supplies the rule.
- **R3**: partitions *cause* duplicate claims (two replicas each admit a
  claim while unable to see each other). Layer 1's job is to make partitions
  observable (presence shows the split) and healable (anti-entropy on
  rejoin); the deterministic resolution — earliest-HLC wins, loser yields
  `superseded` — is mac-claude's merge rule (§6.2). Presence makes the
  window small; the merge rule makes the outcome safe.

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

`org.tunnel.remove` is the remove half of the tunnel OR-set: it hides the edge
from the current organization view, not from history. If MemPalace adopts a
first-class dormant-tunnel product state, ship it as an explicit state op
(`org.tunnel.set_state(active|dormant)` or equivalent) rather than overloading
remove with dormancy semantics.

Anti-entropy: peers exchange version vectors and pull missing per-origin
ranges (`GET /sync/ops?origin=X&after=N`), push-notified over the existing
SSE channel. No broker, no framework: automerge/yjs are document-CRDTs
(wrong shape, heavy deps); cr-sqlite is a native extension whose generic
table-CRDTs know nothing of id purity or verbatim; file-level sync of live
SQLite corrupts. The merge logic above is ~hundreds of lines we fully own.

Future domain-specific state needs the same explicit treatment before it
enters Layer 2. Closet/card semantics, succession edges, contradiction
intervals, or other semantic structures are not automatically drawer
placement registers. Each adopted state must name its own op kinds, conflict
surface, and any merge-exempt or intentionally non-LWW transitions; otherwise
replicas can silently erase meaning while still "converging."

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
   derived consumers. Decided 2026-07-02 (Igor): ships as **2a**
   (drawers + KG ops — the waiting customers: mining promotion and the
   multi-writer foundation) followed by **2b** (registry + hallways/tunnels
   op conversion); superseded revisions are kept **forever** (verbatim
   maximalism — search surfaces head revisions only; no GC path exists);
   the v4 migration runs **staged on a palace copy first**, validated, then
   live with a timestamped backup and a brief read-only window; the mac
   origin runs a **dual-write shadow period** (Chroma writes + op emission,
   divergence detectable) before cutover. Step 3's write-flip on remote
   replicas begins only **after the local-capture promotion validates**
   end-to-end. Two commitments this step MUST honor:
   - **Local-capture promotion.** A step-1 replica may mine machine-local
     data (projects, conversations) into its own palace before step 2
     exists — such drawers carry no `replica_origin` stamp, so read-replica
     reconciliation cannot touch them. Step 2 ships a one-time promotion
     pass: every unstamped local drawer becomes a `drawer.add` op under the
     replica's identity and flows to the whole mesh. Capture-now is
     forward-compatible by contract, not by luck; nothing mined early is
     ever re-mined or lost.
   - **v4 ids are the cross-machine dedup.** Content-pure identity means
     the same content mined on two machines yields the same drawer id; the
     grow-only merge collapses duplicates into one drawer with multiple
     provenance records. The migration is the dedup mechanism — no separate
     dedup pass across origins.
3. **Full multi-writer** — every replica captures locally, all converge.

Each step ships value alone; none blocks on Layer 1 choice (seam, §5).

## §10. Security — shared ownership

Threat model: N replicas mean the entire brain — every drawer, verbatim —
sits on every device. Two independent exposures follow, and the design keeps
them independent: the **wire** (in transit between replicas) and the **disk**
(at rest on each replica). A compromise of one must not yield the other.

### 10.1 Transport-side (windows-claude)

- **In transit**: every op crosses the mesh E2E-encrypted (MeshGuard /
  WireGuard-class); relays and couriers (§5.3) see ciphertext only. No
  palace bytes ever traverse a third party in the clear — the constitutional
  no-cloud-SoR line holds even when a relay is used.
- **Membership as the only ACL**: an op is authorized iff it arrived over a
  channel authenticated to a current, non-revoked `ReplicaId` (§5.1). There
  is no per-op signature check in Layer 2 — trust is established once at the
  transport and is total above it. Membership itself is convergent,
  revocation-aware state (§5.6).
- **Revocation ceremony** (R7, network half): revoke a lost device's
  identity from any survivor; the tombstone propagates and every peer
  refuses it thereafter. This severs *future* access only — see 10.3.
- **Identity-key custody**: per-device Ed25519 key, on-device, non-exportable
  (§5.6). Compromising it grants mesh access (mitigated by revocation) but,
  by construction, decrypts nothing at rest.

### 10.2 At-rest side (mac-claude)

Two tiers, both grounded in one rule: **data keys are per-replica and never
traverse the mesh.** Ops arrive over the wire (10.1), are held decrypted
only in memory, and are written under the receiving replica's own key.
Compromising one device's disk therefore never yields a key that opens any
other device — the same independence 10.3 demands between wire and disk
holds between disks.

- **Tier 0 (baseline, v1-mandatory to document and detect)**: full-disk
  encryption — FileVault / BitLocker / LUKS. Zero code, protects the
  powered-off stolen device, and is the only tier that also covers derived
  indexes we do not control internally (Chroma's own SQLite holds verbatim
  documents). Setup surfaces a loud warning when the palace directory lives
  on an unencrypted volume.
- **Tier 1 (target): per-palace data key over the canonical stores.** A
  symmetric data key encrypts `oplog.sqlite3`, the content store, the
  logstream, and the KG (SQLCipher-style page encryption; AEAD, hardware-
  accelerated — page crypto on hot recall paths fits the R0 budgets). The
  data key is wrapped by the OS keystore (macOS Keychain/Secure Enclave,
  Windows DPAPI/TPM, Linux libsecret/TPM), never stored in the palace
  directory, never synced, and is distinct from the 10.1 identity key by
  construction — neither derives from the other.
- **Derived indexes under Tier 1**: rebuildable by definition (§7), so they
  get the cheaper policy — rely on Tier 0, or (paranoid profile) treat them
  as ephemeral: discarded on lock, refolded from the encrypted op-log on
  unlock. Never the canonical stores' key.
- **Rotation is replica-local**: re-encrypt local files under a new data
  key and re-wrap; no mesh coordination, no ops emitted, because keys are
  not replicated state.
- **R7 composition**: a lost powered-off device presents Tier 0 + Tier 1 to
  the attacker; the revocation ceremony (10.1) has already severed its
  future sync regardless of whether the disk ever yields.

### 10.3 The seam between them

Revocation stops the network; encryption protects the disk that is already
gone. A stolen, powered-off device is defended only by 10.2; a live device
still on the mesh is defended by 10.2 (unlocked-disk exposure) *and* 10.1
(revocation cuts its future sync). Neither half covers the other — which is
why both are mandatory, not alternatives.

Explicitly: **replication ≠ backup (R8)**. Tombstones propagate, so a
fat-fingered mass delete replicates faithfully to every device; encrypted
offsite snapshots remain a separate mechanism, out of this layer's scope.

## Open Questions

- HLC skew bounds and how loudly to surface clock anomalies.
- Op-log compaction policy (R6): checkpoint cadence for snapshot bootstrap.
  (Content-revision retention is settled: superseded revisions are kept
  forever; compaction only concerns op-replay bootstrap cost, never
  content.)
- Does the E2EE cloud courier ship in v1 or wait for demand?
- Partial replicas for phone-class devices (deferred; Non-Goals).
- Federation bridge: can a personal replica project shadow wings into a
  team hub palace mechanically, or is that manual today?

## Appendix A: PalaceMind

The desktop app is where replication stops being infrastructure and becomes
something the human can see and trust. Its guiding principle is the one
already shipping in the connected-palace and Agents surfaces (ADR-0028):
**honest status over hidden magic.** Four consumer surfaces.

### A.1 Replica status (generalizing Live/Polling)

The Agents viewer already renders a `Live · seq N` vs `Polling` honesty flag
off the logstream. That generalizes directly to the fleet: a compact
**estate health** panel — one row per known replica showing `reachable`
(device presence, §5.8), last-seen, and **version-vector drift** ("this
device is 14 ops behind mac"). Drift is the replication analogue of the
Polling flag: it never lies about how converged you are. The Wings-page
estate map (already an outer orbit for the connected palace) becomes the
natural home — replicas as nodes, edges dimming when a peer goes
unreachable, so "the house breathes when the fleet works" is literal.

### A.2 Merge surfacing (R4 — the load-bearing UX)

R4 forbids silent organization merges: when two replicas filed the same
drawer differently during a partition and LWW-by-HLC picks a winner, the
loser must be *shown*, not swallowed. PalaceMind owns that moment:

- A quiet, dismissible notice — "This capture was filed in *Meds* on your
  laptop and *Billing* here; kept *Billing* (more recent). Move it?" — with
  one-click accept-the-other. Never a modal, never blocking; the calm
  enterprise register, matching the assistant-offline notice we just shipped.
- A **Reconciliation** view listing recent auto-merges with their HLC
  reasoning and undo, so a power user can audit what convergence decided.
  The op-log makes this free: every merge is two ops and a rule.

### A.3 Presence rendering (R5)

Presence (§5.8) surfaces wherever a human waits on another machine: the
Agents inbox marks a delegate's device offline so the user doesn't expect a
reply from a sleeping laptop; agent pickers show a liveness dot; the
open-loops panel flags a thread blocked on an unreachable replica distinctly
from one merely awaiting a claim. The rule: never make the user infer from
silence what the system already knows.

### A.4 Multi-origin logstream

The viewer must not fracture when ops carry `origin_replica` + `hlc`
(additive fields, per §6.2 — the contract stays compatible):

- **Order by HLC**, not per-origin `seq`, once events span replicas; keep
  `seq` as the per-origin resume cursor.
- **Dedupe by `op_id`/event id** across origins (the same op can arrive via
  two peers) — the merge is idempotent, the UI must be too.
- **Origin as provenance**, alongside the existing agent identity: "filed by
  mac-claude on *laptop*." This also lands windows-codex's identity-alias
  request cleanly — a rename is just another provenance fact the log already
  carries; render "windows-claude (formerly claude-fable-5-windows)" from
  the correction op, thread the identities together in filters, keep raw
  `from_agent`/`origin_replica` per event for audit.

Consumer contract, restated for implementers: bootstrap is always
**snapshot (`event_list`/`/sync` cursor) → set version vector → tail
(SSE `Last-Event-ID`)**; never live-only connect (the gap loses ops). This
is the same pattern the Agents page already uses and the one windows-codex
asked us to name canonically.
