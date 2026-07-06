# The Replicated Palace

Memory is identity — and you don't park your identity on a single machine.

The replicated palace turns MemPalace from *one palace on one computer* into
**one logical palace, fully replicated across every machine you own**. Agents
always talk to the MemPalace service on `127.0.0.1`; the services converge
with each other in the background. If your desktop sleeps, your laptop still
remembers everything. When it wakes, the machines reconcile on their own.

This is the design from RFC 004, and it is running today as the project's own
production infrastructure — the maintainers' agent fleet coordinates and
remembers through it.

## The availability invariant

One rule governs everything here: **recall reads and capture writes never
block on the network — only convergence may wait.** A memory system that adds
a network round-trip to remembering has stopped being local-first, so every
mesh feature is judged against offline operation as the default posture, not
an edge case.

## Three layers

```
  Machine A                       Machine B                    Machine C
  ┌─────────────────────┐         ┌─────────────────────┐      ┌──────────────────┐
  │ agents → 127.0.0.1  │   ops   │ agents → 127.0.0.1  │ ops  │ agents → local   │
  │ ┌─────────────────┐ │ ◀─────▶ │ ┌─────────────────┐ │◀────▶│ ┌──────────────┐ │
  │ │ mempalace hub   │ │         │ │ mempalace hub   │ │      │ │ mempalace hub│ │
  │ │  op-log         │ │         │ │  op-log         │ │      │ │  op-log      │ │
  │ │  derived index  │ │         │ │  derived index  │ │      │ │ derived index│ │
  │ └─────────────────┘ │         │ └─────────────────┘ │      │ └──────────────┘ │
  └─────────────────────┘         └─────────────────────┘      └──────────────────┘
```

1. **Transport** — how replicas reach and trust each other. Today: any
   mutually reachable network (a Tailscale-style tailnet works well) with
   per-hub bearer tokens. The transport lives behind a seam
   (`MEMPALACE_TRANSPORT`), so a decentralized mesh-identity transport can
   replace tokens without touching anything above it.
2. **Sync** — append-only logs of *ops*, merged by union. Every mutation
   (a filed drawer, a knowledge-graph fact, a coordination event) becomes an
   immutable, provenance-stamped op that travels between replicas.
3. **Derived state** — vector indexes and caches are rebuilt or folded
   locally, never copied. **Sync the facts, derive the senses**: ops are
   kilobytes; vector indexes are gigabytes. Every machine remembers
   everything; each machine senses with its own hardware.

## Ops, clocks, and version vectors

Each replica has a stable identity (`replica.json`, e.g.
`rep_1a2b3c4d5e6f`) and stamps everything it authors with:

- `origin_replica` — which machine authored it (provenance, forever)
- `origin_seq` — the author's own counter
- `hlc` — a hybrid logical clock: physical time + logical counter + replica
  tiebreak, rendered as a sortable string. Total order across machines
  without trusting anyone's wall clock.

A replica's knowledge is summarized by a **version vector** —
`{origin → highest sequence applied}`. Peers exchange vectors, compute
exactly which op ranges each is missing, and pull them. The engine is
idempotent end to end: re-delivering an op is a no-op, a crash mid-round
means the next round re-pulls the tail, and **every replica carries every
origin's ops** — so two machines that have never exchanged credentials still
converge through a common peer. Gossip, in the practical sense.

## What syncs today

| Layer | Mechanism | Status |
|---|---|---|
| Coordination events + artifacts (the [agent logstream](/concepts/agent-logstream)) | op sync, multi-master | shipping |
| Memory content (drawers) | snapshot bootstrap + memory ops | shipping (ops in shadow) |
| Knowledge graph | snapshot bootstrap + memory ops | shipping (ops in shadow) |
| Vectors | never synced — derived locally, or folded from a peer's [vector cache](#distributed-embedding) | shipping |
| Organization (wings/rooms/tunnels as ops) | op vocabulary reserved | next |

"Shadow" is deliberate engineering honesty: the memory op-log currently runs
in a **dual-write shadow** — every drawer and knowledge-graph write also
emits an op, ops travel and fold into peer replicas, but each machine's
vector store remains its system of record until the shadow proves itself.
`mempalace oplog verify` replays the op-log against the live store and must
stay clean over a real usage period before ops become authoritative. A fold
never mutates a locally-authored drawer from a remote op during the shadow;
conflicts are counted and preserved for review, not applied silently.

## Bootstrapping a new machine

A new replica doesn't replay months of history — it bootstraps from a
snapshot, then tails ops:

```bash
# on the new machine, hub not yet running
mempalace replica pull --with-vectors
```

`peers.json` in the palace directory names the peers:

```json
{
  "peers": [
    { "name": "desktop", "url": "https://desktop.example.com", "token": "..." },
    { "name": "laptop",  "url": "https://laptop.example.com",  "token": "..." }
  ]
}
```

Each origin serves only the content it *authored* (never copies it received
from someone else), so bidirectional pulls converge without echo. Pulled
drawers carry a `replica_origin` stamp naming where they came from — ask
"who is X?" on any machine and the answer arrives with its provenance.

Once the hub starts, the background sync loop (every
`MEMPALACE_SYNC_INTERVAL` seconds, default 15) keeps everything converging:
logstream events, memory ops, and the fold — no cron jobs, no manual steps.

## Distributed embedding

Vectors are a pure function of *(content, embedder identity)* — so they can
be computed wherever the best hardware lives:

```bash
# on the machine with the GPU: precompute vectors into a portable cache
mempalace replica embed-cache

# on any other machine: fold content WITH those vectors — zero local embedding
mempalace replica pull --with-vectors
```

The cache is a sidecar keyed by `(drawer_id, embedder identity)`; the fold is
identity-gated, so vectors are only ever reused under the exact same model.
Measured on real palace content (MiniLM, 384-d): a desktop CPU manages
~100 docs/s, a commodity CUDA GPU ~1,300 docs/s, and a current-generation
Windows GPU via DirectML ~2,550 docs/s — a one-line
`pip install onnxruntime-directml`, no vendor toolchain. A six-figure drawer
count that would take half an hour of CPU embedding folds in about a minute
of GPU time computed once, anywhere in your mesh.

## Watching the mesh

Every hub answers `GET /sync/peers` (bearer-authenticated) with its view of
the estate: its own identity and version vector, each configured peer's
reachability, last sync outcome and remote vector, and — the interesting
part — `unnamed_origins`: replicas it knows about *only through gossip*.
Drift between any two machines is one vector comparison. This endpoint is
what mesh dashboards draw from; tokens are never included in the payload.
The same payload is exposed as the `mempalace_mesh_peers` MCP tool, so
desktop apps consume the estate through the bridge they already have.

Every node also advertises a **profile** — roles (`replica` / `agents` /
`compute`), resolved accelerator and embedder, live drawer count, hardware
string — derived entirely from what the daemon can observe about itself,
never from configuration. Profiles ride the sync surfaces, so a carrier
relays them for replicas it only knows transitively: dashboards render
what each machine *reported about itself*, not what a UI guessed.

## Trust, today and next

Today, authorization is a bearer token per hub, exchanged out-of-band by the
human — workable, but it costs one manual credential relay per edge and has
no real revocation story. The planned transport binds the mesh identity
itself: each device's cryptographic key becomes its replica id (provenance
and authentication as one fact), mesh membership becomes the only ACL,
admission becomes a one-time join ceremony, and revoking a lost laptop is a
single command that propagates. The transport seam exists so that swap
touches none of the sync machinery above it.

Two things replication is **not**:

- **Not a cloud.** No third party ever holds queryable palace content. The
  mesh is your machines converging with your machines.
- **Not a backup.** Deletions propagate faithfully — a mass delete
  replicates like anything else. Keep snapshots separately.

## See also

- [Shared Brain guide](/guide/shared-brain) — the operational setup, hub and
  agents included
- [Agent Logstream](/concepts/agent-logstream) — the coordination layer that
  pioneered the op-sync machinery
- [CLI reference](/reference/cli) — `mempalace replica` and `mempalace oplog`
