# RFC 006: Rooms — Open Discussion Between Agents and People

Status: Draft v2 — complete rewrite, for Milla's review
Owner: Igor Lins e Silva (`windows:claude:mempalace`)
Created: 2026-09-05 (v1), rewritten 2026-09-11 (v2)
Branch: `feat/multi-agent-room-coordination`
Prior art: RFC 003 (logstream), RFC 005 (identity), `integrations/shared/coordination-protocol.md`
Supersedes: the v1 draft of this RFC and its `examples/multi_agent_room/` simulation

---

## Summary

RFC 003 gives agents a way to hand work to each other: `task.request` →
`claimed` → `patch.ready` → `event.ack`. It is deliberately rigid — every
event has one addressee and one obligation.

Rooms are for the other kind of collaboration: a design review, a
brainstorm, a critique, a "what should we do about X" where several agents
and one or more humans think together and nobody owes anybody a patch.

A room is **a conversation held on the logstream** that any participant can
join *from the session it is already running in* — a Claude Code terminal, a
Codex CLI, a ChatGPT/Claude/Codex desktop app, Antigravity, Astra, or a human
with a shell. Nothing is spawned. There is no room server, no room process,
no simulation. Participation is three operations every agent already has:

1. **Catch up** — read what was said since you last looked.
2. **Speak, or stay quiet.**
3. **Find out when it is your turn** — in the way *your* harness can.

Everything else in this RFC is convention: how the floor is handed around,
how to keep a room from turning into an echo chamber, and how a finished
discussion becomes memory.

One sentence: **a room is a `room` on the project stream; the floor is an
event; your turn arrives the way your harness can receive it; silence costs
nothing.**

## What the first draft got wrong

The v1 draft modelled a room as a Python program: three scripted personas in
daemon threads, each running a four-factor gating heuristic
(relevance × novelty × anti-echo × urgency), an urgency-weighted jitter
backoff to "win the floor", and pre-flight cancellation to avoid "append
collisions". Reviewer feedback (Milla, PR #2447):

> Complicated and doesn't account for agents that don't argue and when you
> just need to have one person at a time speak. Doesn't work with the agent
> that needs to speak on the desktop app — it connects to the app and then
> spawns a brand new window so the instance there has no clue what he's
> doing there.

All three points are correct, and they share a root cause: v1 designed the
*agents* instead of the *room*. Concretely:

- **Collisions are not a problem to solve.** The logstream is append-only
  and totally ordered (HLC, RFC 004). Two agents posting "at the same time"
  produce two events in a definite order. Backoff and cancellation were
  solving a race that cannot corrupt anything.
- **Chatter is a prompt problem, not a protocol problem.** An agent that
  replies "Understood" to every broadcast is following bad instructions.
  The fix is a four-line rule in its instructions (§5), not a scoring
  function in a subprocess.
- **The only real constraint v1 ignored is how each participant wakes up.**
  A Claude Code session with a background watcher can be woken by an event.
  A desktop chat app cannot — it acts only when its human (or its host app)
  gives it a turn. `coordination-protocol.md` already says this plainly:
  *"A pasted handoff can wake a turn-based agent; a logstream event alone
  cannot."* A room protocol that assumes every participant is a long-running
  Python loop excludes exactly the agents Milla uses.
- **"Agents that don't argue"** — most agents in a room are there to answer
  when asked, not to compete for the floor. v1 had no place for them; the
  default mode here (§3, *moderated*) is built for them.

v1 is superseded in full. Its simulation is removed from the tree; nothing
in it was reusable by a real agent.

## Design principles

- **Zero-spawn.** Joining a room never starts a process, opens a window, or
  requires code beyond the MCP tools / CLI the agent already has.
- **Wake-model aware.** The protocol works for participants that can be
  woken by events *and* for participants that can only be woken by a human.
  Both are first-class; neither is degraded.
- **One speaker at a time is the default.** Open, free-for-all discussion
  is available but opt-in.
- **Silence is free.** Choosing not to speak writes zero bytes. Nobody has
  to say "I have nothing to add" unless they hold the floor.
- **Verbatim, then memory.** Every turn is a verbatim logstream event. When
  the room closes, the transcript is filed as drawers so it is recallable.
  Nothing is summarized in place of the original words.
- **Convention over mechanism.** Phase 1 of this RFC ships with no code
  change at all. CLI sugar and a skill come after the convention has been
  dogfooded.

## 1. What a room is

A room is a `room` sub-channel on an existing RFC 003 stream — the same
field the fleet already uses for `delegation`, `patches`, `reviews`,
`status`. A brainstorm about MemPalace search lives at
`stream=project/mempalace, room=search-brainstorm`. No new namespace,
no new matcher, and the events sit next to the project's task traffic where
the participants are already looking.

| Field | Value |
|---|---|
| `stream` | `project/<project>` (or any stream the participants share) |
| `room` | the room's name: short, kebab-case, e.g. `search-brainstorm` |
| `correlation_id` | one **session** of the room: `room_<name>_<yyyymmdd>`. A room can be reopened; each opening is a new session. |
| `topic` | optional lane inside a session, as in RFC 003 |
| `from_agent` | RFC 005 identity, or a human handle (`igor`, `milla`) |
| `to_agent` | `*` for the room, or a specific participant when addressing them |

Humans are participants. A human posts from the CLI (`mempalace logstream
append`) or, later, from PalaceMind. There is no distinction on the wire
between a human turn and an agent turn.

## 2. Event vocabulary

Six event types, all valid under the existing `_EVENT_TYPE_RE` — no schema
change. `body` is always verbatim.

| Type | Who | Meaning |
|---|---|---|
| `room.open` | the opener | Starts a session. `body` = the question or agenda. `metadata.mode` = `moderated` (default) or `open`. `metadata.moderator` = identity (defaults to the opener). |
| `room.join` | each participant | Presence. `metadata.wake` = `self` or `turn-based` (§4). Optional `body` = what perspective you bring. |
| `room.floor` | the moderator | Gives the floor. `to_agent` = the participant whose turn it is. Optional `body` = the specific question for them. |
| `room.message` | whoever has the floor (moderated) / anyone (open) | A turn. `to_agent` = `*`, or a specific participant when the message is *for* them. |
| `room.pass` | a participant who was given the floor | "Nothing to add." Only meaningful in moderated mode; in open mode, silence is the pass. |
| `room.close` | the moderator | Ends the session. `body` = the outcome in the moderator's own words. |

`status` is left empty on room events. The RFC 003 status vocabulary
(`open`, `claimed`, `applied`, …) describes work, and a room is not work.

Example — a moderator handing the floor:

```json
{
  "type": "room.floor",
  "stream": "project/mempalace",
  "room": "search-brainstorm",
  "correlation_id": "room_search-brainstorm_20260911",
  "from_agent": "igor",
  "to_agent": "mac:codex:mempalace",
  "body": "You own the sqlite_exact backend — does the fused index proposal break the read-only snapshot guarantee?"
}
```

## 3. Floor control: two modes

### Moderated (default)

One participant is the **moderator** — usually the human who opened the
room, sometimes an agent. The moderator decides who speaks next by
appending `room.floor to_agent=<X>`. `X` replies with exactly one
`room.message` (or `room.pass`), and the floor returns to the moderator.

That is the whole mechanism. It gives:

- **One speaker at a time**, by construction.
- **A home for agents that don't argue.** They never have to decide whether
  to speak; they answer when asked and are otherwise silent.
- **Round-robin, standup, panel, cross-examination** — these are moderator
  *policies*, not modes. A moderator that hands the floor in join order is
  running a round-robin. The protocol does not need to know.
- **Compatibility with turn-based participants.** A `room.floor` addressed
  to `X` is precisely the ping `X` needs (§4). The moderator hands the floor
  and, if `X` declared `wake=turn-based`, hands its human the paste line.

Anyone who is not holding the floor may still post a `room.message` with
`to_agent=<moderator>` to *request* the floor ("I have a constraint on
this"). The moderator decides. This keeps the floor strict without making
participants mute.

### Open

`metadata.mode=open`. Anyone may post a `room.message` at any time. The
floor is not managed; the anti-chatter rule (§5) is the only brake. Use it
for a short burst of divergent thinking among self-waking agents, when the
moderator's serialization would slow the room down more than it helps.

The moderator can switch modes mid-session by posting a new `room.open`
with the same `correlation_id` and a different `metadata.mode`; the latest
one wins.

## 4. Wake models — how your turn reaches you

Every participant declares, in `room.join`, how it can be reached:

| `metadata.wake` | Who | How the room reaches you |
|---|---|---|
| `self` | Headless / CLI harnesses with a background watcher (Claude Code, Codex CLI, daemons) | You arm `mempalace logstream watch` on the room (below) and are woken by `room.floor` / `room.message` events directly. |
| `turn-based` | Chat harnesses that act only when prompted (desktop apps, web chats) | You cannot be woken by an event. Your human pastes the floor line into your chat; you then catch up, speak or pass, and report your cursor. |

Declaring the wrong model is the one way to break a room: a moderator who
hands the floor to a "self-waking" participant that is actually deaf will
wait forever. This is the same rule as coordination-protocol.md's
*"Never fake a watch."*

### Self-waking participants

Arm a watcher scoped to the room, re-arm after every wake, and process
each wake by catching up from **your own cursor** (not the watcher's state
file — the watcher's cursor advances past events it examined and rejected):

```bash
mempalace logstream watch --agent <me> \
  --stream project/mempalace --room search-brainstorm \
  --type room.floor --type room.message --type room.close --json
```

In moderated mode a self-waking participant may narrow to `--type
room.floor --type room.close` and never wake for other people's turns; it
reads the whole session when the floor reaches it.

### Turn-based participants

The moderator's `room.floor` produces a one-line handoff, in the same
shape as `mempalace task create`'s *Ready to paste* line:

```text
You have the floor in MemPalace room project/mempalace/search-brainstorm
(session room_search-brainstorm_20260911) as mac:codex:mempalace.
Catch up from your last cursor, then post one room.message or a room.pass.
```

The participant's human pastes that into the chat. The agent then, within
that single turn:

1. `event_list stream=project/mempalace room=search-brainstorm
   correlation_id=<session> since_event_id=<its cursor> order=asc` — reads
   every turn it has not seen, verbatim.
2. Appends one `room.message` or `room.pass`.
3. Tells its human the new cursor (the id of the last event it read or
   wrote), so the next paste resumes exactly there.

Nothing is spawned, no second window opens, and the agent has full context
because the context *is the room*, read at the moment it is needed.

A turn-based participant may also be handed the floor pre-emptively: the
moderator can give the floor to three turn-based agents in a row, and their
humans paste when they get to it. Turns land in the order they are posted;
the moderator reads them as they arrive.

## 5. The anti-chatter rule

This replaces v1's gating heuristic. It goes into a participant's
instructions, not into code:

> **Before posting, read everything since your cursor.** Post only if you
> add a fact, a constraint, a concrete proposal, a specific objection, or an
> answer to something addressed to you. **Never post agreement,
> acknowledgement, or a restatement** — if it has been said, it has been
> said. **At most one message per wake** unless a message is addressed to
> you by name. Silence is the default; it costs nothing and nobody is
> waiting for it.

In moderated mode the moderator enforces this by not handing the floor to
someone who has nothing new. In open mode the "one message per wake" bound
is what makes the room converge: a room of N agents produces at most N
messages per round of wakes, and a round in which everyone stays silent
ends the discussion with no closing ceremony.

## 6. Closing a room, and what becomes memory

The moderator posts `room.close` with the outcome in their own words.
Then the moderator — or a participant the moderator names as scribe —
files the session:

1. **The transcript, verbatim**, as drawers: one drawer per `room.message`,
   wing = the project, room = the room name, `source_file` = the session's
   `correlation_id`. This is what makes the discussion searchable next
   month. It is the original words, not a digest.
2. **The decision**, as one additional drawer quoting the `room.close`
   body, and as KG facts via `mempalace_kg_add` where a single-valued fact
   was settled (`mempalace-core → uses → fused BM25 index`, valid from
   today).

Filing is the same *"File the outcome"* rule coordination-protocol.md
already imposes on delegations. A room that closes without being filed was
a chat, not a memory.

## 7. A complete moderated session

Igor opens a room from the shell, with two agents — one self-waking, one
turn-based — and Milla.

```bash
# 1. Open
mempalace logstream append --type room.open --stream project/mempalace \
  --room search-brainstorm --correlation-id room_search-brainstorm_20260911 \
  --from-agent igor --to-agent '*' \
  --metadata '{"mode":"moderated","moderator":"igor"}' \
  --body "Should the Rust exact engine own BM25, or should BM25 stay in Python?"
```

Participants join (each from their own session, with their own tools):

```text
windows:claude:mempalace  → room.join  metadata.wake=self
                            arms: logstream watch ... --type room.floor --type room.close
mac:codex:mempalace       → room.join  metadata.wake=turn-based
milla                     → room.join  (human, posts from CLI)
```

Igor hands the floor, in turn:

```bash
mempalace logstream append --type room.floor ... --to-agent windows:claude:mempalace \
  --body "You wrote the sqlite_exact backend. What does BM25-in-Rust cost us?"
# → the watcher on the windows box exits 0; that session catches up and posts one room.message.

mempalace logstream append --type room.floor ... --to-agent mac:codex:mempalace \
  --body "Same question from the packaging side."
# → prints the paste line; Igor pastes it into the Codex desktop app; it reads the room and posts.

mempalace logstream append --type room.floor ... --to-agent milla
# → Milla reads the transcript and replies from her shell.
```

Igor closes and files:

```bash
mempalace logstream append --type room.close ... \
  --body "Decision: BM25 moves into mempalace-core behind the existing tokenizer seam; Python keeps the tokenizer. Windows Claude to draft the task."
# → transcript filed as drawers under wing=mempalace room=search-brainstorm;
#   KG: mempalace-core → owns → bm25 (valid_from 2026-09-11)
```

Every line above is an existing command. Phase 1 needs nothing built.

## 8. Implementation plan

### Phase 1 — Convention (this PR; docs only)

- This RFC.
- A `## Rooms` section in `integrations/shared/coordination-protocol.md`
  carrying §2–§6 in the same voice as the delegation protocol, so every
  harness's system-prompt block inherits it.
- The v1 simulation under `examples/multi_agent_room/` is removed.

### Phase 2 — Sugar (follow-up PR, after dogfood)

Thin wrappers over `logstream`, each printing `--json` and the paste line
where relevant. None of them are required to participate:

```bash
mempalace room open   <room> --stream project/x --mode moderated --body-file agenda.md
mempalace room join   <room> --wake self|turn-based
mempalace room floor  <room> --to <agent> [--body "..."]      # prints the paste line
mempalace room say    <room> --body-file turn.md [--to <agent>]
mempalace room pass   <room>
mempalace room catchup <room> [--since <event-id>]              # verbatim transcript + new cursor
mempalace room close  <room> --body-file outcome.md --file      # --file: file transcript + decision drawers
```

Plus a `mempalace-room` skill mirroring `mempalace-task`: verify the seam,
declare your wake model honestly, catch up from *your* cursor, one message
per wake, report the cursor back.

### Phase 3 — Viewer

A room view in PalaceMind: the session transcript, who holds the floor, each
participant's declared wake model, and a "hand floor to…" button that
appends `room.floor` and copies the paste line. This is where turn-based
participation becomes one click instead of one paste.

## Non-goals

- **No room server, scheduler, or floor lock.** The floor is an event;
  ordering is the logstream's.
- **No in-process multi-agent simulation.** A room's participants are real
  sessions. Anything that "demonstrates" a room by spawning fake ones
  demonstrates the wrong thing.
- **No summarization of turns.** The transcript is filed verbatim. A
  moderator's `room.close` body is their own words about the outcome, not a
  digest of others'.
- **No new routing.** `to_agent` exact-match plus `*` (RFC 005, Decision 1)
  is sufficient: the floor is addressed to one identity, everything else is
  broadcast.
- **No general chat product.** Rooms are for agents and the people working
  with them, on the project streams they already share.

## Open questions

- **Should `room.floor` carry a deadline?** A moderator might want "you have
  the floor for the next ten minutes, then I move on." Leaning towards
  leaving it to the moderator's `body` in v1 and adding `metadata.until` only
  if dogfood shows moderators actually re-handing the floor on timeouts.
- **Cursor storage for turn-based participants.** Today the agent reports
  the cursor to its human, who carries it between pastes. A per-identity
  server-side cursor (RFC 003 "future work") would remove that chore; it is
  useful well beyond rooms and should be its own change.
- **Filing granularity.** One drawer per message keeps recall precise and
  verbatim; one drawer per session keeps the palace tidy. Starting with
  per-message because it is the faithful choice; revisit if it floods the
  taxonomy.
