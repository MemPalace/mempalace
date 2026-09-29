# RFC 005: Agent Identity & Routing

Status: Draft — for Igor's review
Owner: mac-claude (backend/hub owner; identity is palace/fleet infrastructure, not a meshguard concern)
Created: 2026-07-04
Branch: `feat/shared-brain-dogfood`
Prior art: RFC 003 (logstream — the routing surface this refines), RFC 004 (the replicated palace — provides the stable replica identity a host label derives from), the `rfc005_agent_identity_routing` correlation thread

## Summary

An agent's coordination identity is the triple **`host:agent:project`** — the
granularity at which a working actor actually shares filesystem, local config,
and knowledge. Two chat sessions open in the same project on the same machine
are the **same** actor and carry the **same** identity; process/session/PID is
event *metadata*, never part of who you are. Routing stays exactly as RFC 003
defines it — exact match plus `*` broadcast over an opaque string — so this RFC
adds a naming convention and one durable renderer change, not a new matcher.

One sentence: **identity = `host:agent:project`, minted once by the block that
already renders it, routed by the string equality the logstream already has.**

## Motivation

The shared brain identifies each agent by a flat name — `mac-claude`,
`windows-claude`, `blade-claude`. That name is not chosen by the agent; it is
**rendered into** the agent's instructions by a managed block MemPalace writes
to `~/.claude/CLAUDE.md`:

```
<!-- mempalace-shared-brain:start -->
## MemPalace shared brain
You share a MemPalace hub with other agents. Your agent identity is
windows-claude — use it as from_agent/created_by ...
<!-- mempalace-shared-brain:end -->
```

The flat name conflates two things that have now come apart in the live fleet:

1. **Two sessions, one name.** The windows box ran two concurrent `claude`
   sessions — one on the palace/mesh track, one on meshguard — both rendering
   `from_agent=windows-claude`. Their inboxes, their claims, and their diary
   entries interleaved under one identity with no way to tell "which window."
   A `status=claimed` from one window looked, to the other, like *someone else*
   had claimed it — or worse, like *itself* had, with no way to be sure.

2. **No project seam.** Recall, delegation, and the diary all key off the flat
   name, so a meshguard session and a mempalace session on the same box share
   one knowledge wing. Recall for the meshguard actor surfaces mempalace facts
   and vice-versa; the routing name carries no notion of *what work this is*.

Igor's framing resolves both: identity is the tuple at which knowledge scope,
workspace, and local configuration are actually shared. Same box + same project
= same actor, however many windows are open. Different project on the same box
= a different actor with its own knowledge seam.

## Requirements

- **I1 — Identity is `host:agent:project`.** The three components are the axes
  along which real sharing happens: `host` (same machine → same filesystem,
  same daemon, same local config), `agent` (which assistant/runtime), `project`
  (which workspace → which knowledge scope).
- **I2 — Same-project sessions share one identity.** A second window in the
  same project on the same box inherits the same fs, config, and knowledge — it
  *is* the same actor. Identity must not fragment per session, per window, or
  per PID.
- **I3 — Session/PID is metadata, never identity.** Which process holds a port,
  whose PID to signal on cleanup — that belongs in event `metadata`, not in
  `from_agent`/`to_agent`.
- **I4 — Claims are held by the identity, not the session.** A `status=claimed`
  by *any* session of an identity is a claim by that identity; a second session
  of the same identity must treat its own identity's open claim as a mutex
  ("don't work what you've already claimed").
- **I5 — No new routing primitive required.** The tuple must route on the
  logstream exactly as flat names do today (RFC 003: `to_agent` exact match, or
  `to_agent='*'` broadcast). Hierarchical/glob routing is explicitly deferred
  (see Decision 1).
- **I6 — Minted at the source, not hand-edited.** The identity is *rendered*
  into each box's instructions; the durable fix changes the renderer so every
  box re-derives its tuple on the next sync. No per-box hand-editing as the
  steady state.
- **I7 — Backward compatible.** Existing flat names (`mac-claude`, …) remain
  valid and keep routing. Migration to the tuple is incremental and only forced
  where a real collision exists.

## Non-Goals

- **A lease/lock server.** Claim safety is achieved by append-only events and a
  deterministic post-hoc tiebreak (Decision 2), consistent with RFC 004's R3.
  Nothing here introduces a coordinator or a mutex service.
- **A new matcher.** Prefix/glob routing (`windows:claude:*`, `*:*:meshguard`)
  is a plausible future affordance but is **not** adopted here (Decision 1).
- **Cross-user identity.** One human, N devices, per RFC 004. The tuple
  distinguishes *this* human's actors, not multiple humans.
- **Changing how `from_agent`/`to_agent` are stored or validated.** Colons
  already pass `_sanitize_routing`; the tuple is a legal value today.

## The Identity Tuple

```
host : agent : project
```

- **`host`** — a stable, human-readable label for the machine. It derives from
  the replica identity RFC 004 already establishes (each replica has a durable
  id), rendered to a friendly label (`mac`, `blade`, `windows`) rather than an
  opaque hash. One host = one replica = one local daemon.
- **`agent`** — the assistant/runtime family (`claude`, `codex`, `hermes`, …).
  This is the existing suffix of today's flat names, lifted out intact.
- **`project`** — the workspace the session is operating in, derived from the
  session's working directory (its project/repo name). This is the new axis and
  the one that carries the knowledge seam.

Today's flat names are exactly `host-agent` with the `project` axis missing.
That is why migration is cheap: `mac-claude` → `mac:claude:<project>` is an
append, not a rename, and a single-project agent may stay flat until a real
collision appears.

**Delimiter.** The colon is deliberate: it is already legal in routing fields
(`_sanitize_routing` rejects only control characters, null bytes, and
over-length values), it does not collide with the stream delimiter `/`
(`project/mempalace`), and it reads unambiguously. The tuple is treated by the
hub as one opaque string — the colons are a *convention for humans and
renderers*, not something the matcher parses (see Decision 1).

## Same-Session Equivalence (I2)

Two sessions with the same `host:agent:project` are one actor. Concretely:

- They watch the **same inbox** (`to_agent=<tuple>`, which also catches `*`).
- They write with the **same `from_agent`**.
- They share **one knowledge wing and one diary** (see Knowledge Partition).
- A task claimed by one is claimed by the identity (I4).

The second window is not a new participant to be tracked; it is the same
participant with a second process. When the fleet needs to know *which process*
(e.g. which one holds a hardcoded port, which PID to signal), that detail rides
in event `metadata` — `{"pid": …, "session": …}` — and never in the identity.

## Claim Safety Under a Shared Identity (I4, Decision 2)

A shared identity introduces one race: two sessions of the same identity (or
two replicas of the same identity, post-partition per RFC 004 R3) claim the
same task before either sees the other's claim. Resolution, no lease server:

1. **Natural mutex.** Before claiming, a session lists open claims for its own
   identity on the correlation. If its identity already holds an open claim, it
   does not double-claim — it either joins or waits. This alone removes the
   common case (a second window picking up work the first already took).
2. **Deterministic tiebreak for the residual window.** If two claims land
   before either is visible, the claim with the **lowest HLC wins**; the loser
   backs off and re-acks `status=superseded`. This is the RFC 004 R3 rule
   (earliest-HLC-wins), reused verbatim — append-only makes the wasted work
   safe, never corrupting.

No new event type is required: `event.ack` with `status=claimed` /
`status=superseded` already expresses both steps.

## Knowledge Partition (I2, bonus)

Keying the palace wing and the diary by the full tuple partitions recall along
the same seam routing already uses: a `…:meshguard` actor's recall stops
surfacing `…:mempalace` facts. Today a single-agent box interleaves both
projects in one wing (e.g. `wing_windows-claude`); the tuple gives each project
its own wing without a new mechanism — it is just a longer wing key.

This is a **bonus, not a v1 requirement** for routing: an agent may adopt the
tuple for `from_agent`/`to_agent` first (fixing the coordination collision) and
partition its knowledge wing later. The two are independent adoptions.

## Decisions

Two choices live in the backend/hub and are resolved here so downstream
consumers (PalaceMind's viewer, the renderer, other agents) can build against a
fixed contract.

### Decision 1 — Matcher: **no change.** Route the tuple as an opaque string.

`to_agent` matching stays exactly as RFC 003 defines it:

```sql
(to_agent = ? OR to_agent = '*')
```

The tuple `host:agent:project` is matched by **string equality**; `*` remains
the only wildcard, meaning fleet-wide broadcast. Hierarchical routing —
`windows:claude:*` for "any agent on this box", `*:*:meshguard` for "whoever
owns meshguard" — is **not** adopted in this RFC.

*Rationale.* The collision that motivated this RFC is solved entirely by
minting distinct identities (Decision below on order + the renderer fix); it
does not require the hub to *understand* the tuple. A glob/prefix matcher is a
real index-and-correctness surface (partial-match semantics, index design,
interaction with `*` broadcast) and should be its own proposal driven by a
concrete need — "route to whoever owns project X regardless of host" — that the
fleet has not yet hit. Keeping the matcher on exact-plus-broadcast means this
RFC ships as a **convention + one renderer change**, with zero risk to the
routing hot path. If hierarchical routing is later wanted, it is additive: a new
optional match mode, not a migration.

### Decision 2 — Order: **`host:agent:project`** (host-first).

*Rationale.* Host-first matches how the fleet's names already read
(`mac-*`, `windows-*`, `blade-*` are host-then-agent), so migration is a pure
suffix append with no reordering. It favors the "any agent on this box" reading,
which aligns with the operational reality that a host owns one daemon and one
filesystem. Project-first would favor "who owns project X across hosts" — the
arguably more common *coordination* query — but that query is exactly the one
Decision 1 declines to make routable for now, so optimizing the string order
for it buys nothing today. If Decision 1 is ever revisited and cross-host
project routing becomes a first-class need, the order can be revisited with it;
until then, host-first is the lower-friction choice.

## The Durable Fix: mint the identity at the renderer (I6)

The identity is hardcoded because MemPalace **renders** it. The one-place fix is
to change the renderer/installer that writes the
`<!-- mempalace-shared-brain:start … end -->` block so it emits the tuple
instead of the flat name:

- **`host`** — derived from the local replica identity (RFC 004), mapped to a
  friendly label.
- **`agent`** — the runtime family, as today.
- **`project`** — derived from the session's workspace (the project/repo the
  block is being rendered for).

Then every box re-renders to its tuple on the next sync — no hand-editing. A
hand-edited stopgap on a colliding box (deriving `host:agent:<project>` locally)
is legitimate as a bridge and is simply superseded — or overwritten — by the
renderer change, which is the intended outcome.

**Compatibility.** The renderer must not thrash existing single-project boxes:
where an agent has exactly one project and no collision, rendering may keep the
flat `host-agent` form (I7) and only expand to the tuple when a second project
or a collision appears on that box. The block's managed markers make the
rewrite safe and idempotent — same rules as today.

## Sequencing

This RFC is **deferred behind the RFC 004 write-flip**: it touches no storage or
merge semantics, so it does not gate the flip, and the flip's cutover work takes
priority. Once that lands:

1. **Convention adopted** (docs-only): agents may begin using
   `host:agent:project` as `from_agent`/`to_agent`. Routes today, no code
   change (colons already valid; matcher unchanged).
2. **Renderer change** (the durable fix): the shared-brain block emits the tuple
   per the rules above. Reviewed on the windows box first, since it has the only
   live two-session collision.
3. **Knowledge partition** (optional, later): wing/diary keyed by the tuple, so
   recall partitions on the same seam. Independent of steps 1–2.

## Open Questions

- **`project` derivation edge cases.** A session with no clear workspace (a
  bare shell, a home-directory session) has no natural `project`. Proposal: fall
  back to the flat `host-agent` form (I7) rather than invent a sentinel project.
- **Friendly host labels.** The replica→label mapping (`mac`, `blade`,
  `windows`) needs a stable source of truth. Proposal: derive from the
  replica record RFC 004 already maintains; a label collision across two of the
  user's machines is resolved by the user at join time, once.
- **Whether the renderer should ever *contract* a tuple back to flat** when a
  project is abandoned. Leaning no — an identity that has appeared on the
  logstream should be stable — but the managed block technically could.

## Relationship to RFC 004

RFC 004 gives this RFC its `host` axis for free: a replicated palace already has
a durable per-replica identity, and one host is one replica is one local
daemon. RFC 004's R3 (deterministic partition-claim resolution, earliest-HLC
wins) is the exact rule Decision 2's claim-safety tiebreak reuses. This RFC does
not alter RFC 004's storage, op-log, or merge design in any way — it refines the
RFC 003 coordination surface that rides on top.
