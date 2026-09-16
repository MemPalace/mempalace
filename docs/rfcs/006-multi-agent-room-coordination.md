# RFC 006: Rooms — Open Discussion Between Agents and People

Status: Draft v2.1 — protocol holes from review of #2447
Owner: Igor Lins e Silva (`windows:claude:mempalace`)
Created: 2026-09-05 (v1), rewritten 2026-09-11 (v2), patched 2026-09-16 (v2.1)
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

- **Collisions are not a problem to solve.** The logstream is append-only.
  Two agents posting "at the same time" produce two events in a definite
  **local append order** (SQLite `rowid`). Catch-up and `since_event_id`
  follow that order — not wall clock, and not HLC. HLC (RFC 004) is for
  cross-replica merge and display; it is not the list cursor. Backoff and
  cancellation were solving a race that cannot corrupt anything.
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
- **Convention over mechanism.** Phase 1 of this RFC ships with no runtime
  code change. The convention, plus the Rooms bullets in the pinned
  system-prompt snippet so agents actually see them, is the whole PR.
  CLI sugar and a skill come after the convention has been dogfooded.

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
| `correlation_id` | one **session** of the room: `room_<name>_<yyyymmdd>_<entropy>`. A room can be reopened; each opening is a new session with a new id. The date is for humans; the entropy (e.g. `a3f1`) is what keeps two same-day openings from merging catch-up and filing. |
| `topic` | optional lane inside a session, as in RFC 003 |
| `from_agent` | RFC 005 identity (`host:agent:project`), or a human handle (`igor`, `milla`) |
| `to_agent` | `*` for the room, or a specific participant when addressing them |

**Identity on the wire vs. `mempalace rules --agent`.** RFC 005 identities
with colons (`windows:claude:mempalace`) are legal routing values today
(`_sanitize_routing` allows `:`) and are what this RFC's examples use.
`mempalace rules --agent` currently accepts only `[A-Za-z0-9._-]+` (e.g.
`windows-claude`). A participant whose rendered block still uses a flat
name joins and is addressed as that flat name. Do not mix the two spellings
for one actor. Changing the renderer is RFC 005, not this RFC.

Humans are participants. A human posts from the CLI (`mempalace logstream
append`) or, later, from PalaceMind. There is no distinction on the wire
between a human turn and an agent turn.

## 2. Event vocabulary

Seven event types, all valid under the existing `_EVENT_TYPE_RE` — no schema
change. `body` is always verbatim. Broadcasts (`room.open`, `room.mode`,
`room.close`, and `room.message` meant for the whole room) set
`to_agent=*`. A `--agent` watcher only matches that identity or `*`; an
omitted target is not delivered.

| Type | Who | Meaning |
|---|---|---|
| `room.open` | the opener | Starts a session. `body` = the question or agenda. `metadata.mode` = `moderated` (default) or `open`. `metadata.moderator` = identity (defaults to the opener). `to_agent=*`. A new `room.open` always mints a new `correlation_id` — it never mutates an existing session. |
| `room.join` | each participant | Presence. `metadata.wake` = `self` or `turn-based` (§4). Optional `body` = what perspective you bring. `to_agent=*`. |
| `room.floor` | the moderator | Gives the floor. `to_agent` = the participant whose turn it is. Optional `body` = the specific question for them. |
| `room.message` | whoever has the floor (moderated) / anyone (open) | A turn. `to_agent=*` for the room, or a specific participant when the message is *for* them. **Exception (moderated):** a participant who does not hold the floor may post one `room.message` with `to_agent=<moderator>` to *request* the floor. That is a request, not a turn; the moderator decides. |
| `room.pass` | a participant who was given the floor | "Nothing to add." Only meaningful in moderated mode; in open mode, silence is the pass. `to_agent=*`. |
| `room.mode` | the moderator | Switches `metadata.mode` on the **current** session (`moderated` or `open`). Same `correlation_id` as the `room.open` it mutates. `to_agent=*`. The latest `room.mode` (or the `room.open` if none) wins. |
| `room.close` | the moderator | Ends the session. `body` = the outcome in the moderator's own words. `to_agent=*`. |

`status` is left empty on room events. The RFC 003 status vocabulary
(`open`, `claimed`, `applied`, …) describes work, and a room is not work.

Example — a moderator handing the floor:

```json
{
  "type": "room.floor",
  "stream": "project/mempalace",
  "room": "search-brainstorm",
  "correlation_id": "room_search-brainstorm_20260911_a3f1",
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
The next `room.floor` waits for that reply. That is the whole mechanism.

It gives:

- **One speaker at a time**, by construction.
- **A home for agents that don't argue.** They never have to decide whether
  to speak; they answer when asked and are otherwise silent.
- **Round-robin, standup, panel, cross-examination** — these are moderator
  *policies*, not modes. A moderator that hands the floor in join order is
  running a round-robin. The protocol does not need to know.
- **Compatibility with turn-based participants.** A `room.floor` addressed
  to `X` is precisely the ping `X` needs (§4). The moderator hands the floor
  and, if `X` declared `wake=turn-based`, copies the §4 paste line into
  that participant's chat.

Anyone who is not holding the floor may still post a `room.message` with
`to_agent=<moderator>` to *request* the floor ("I have a constraint on
this"). The moderator decides. This keeps the floor strict without making
participants mute.

Handing the floor to several turn-based agents at once is **not**
moderated mode: all of them then hold permission to speak, replies can
arrive in any order, and later speakers can answer without seeing earlier
ones. If the moderator wants that fan-out, they switch the session to
open (`room.mode`) first. Moderated stays one floor, one reply.

### Open

`metadata.mode=open` on `room.open`, or a later `room.mode`. Anyone may
post a `room.message` at any time. The floor is not managed; the
anti-chatter rule (§5) is the only brake. Use it for a short burst of
divergent thinking among self-waking agents, when the moderator's
serialization would slow the room down more than it helps.

## 4. Wake models — how your turn reaches you

Every participant declares, in `room.join`, how it can be reached:

| `metadata.wake` | Who | How the room reaches you |
|---|---|---|
| `self` | Headless / CLI harnesses with a background watcher (Claude Code, Codex CLI, daemons) | You arm `mempalace logstream watch` on the room (below) and are woken by `room.floor` / `room.message` / `room.mode` / `room.close` events directly. |
| `turn-based` | Chat harnesses that act only when prompted (desktop apps, web chats) | You cannot be woken by an event. Your human pastes the floor line into your chat; you then catch up, speak or pass, and report your cursor. |

Declaring the wrong model is the one way to break a room: a moderator who
hands the floor to a "self-waking" participant that is actually deaf will
wait forever. This is the same rule as coordination-protocol.md's
*"Never fake a watch."*

### Self-waking participants

Arm a watcher scoped to **this session**, persist its cursor, re-arm after
every wake, and process each wake by catching up from **your own cursor**
(not the watcher's state file — the watcher's cursor advances past events
it examined and rejected):

```bash
mempalace logstream watch --agent <me> \
  --stream project/mempalace --room search-brainstorm \
  --correlation-id room_search-brainstorm_20260911_a3f1 \
  --type room.floor --type room.message --type room.mode --type room.close \
  --state-file ~/.mempalace/watch/<me>-search-brainstorm.json --json
```

`--state-file` is what stops a re-arm from starting at the tip and
skipping a `room.floor` posted while you were processing the previous
wake. `--correlation-id` is what stops a later session of the same room
name from waking you. The CLI will default a sanitized state file from
`--agent` if you omit `--state-file`; naming one per room is still
clearer when you sit in more than one.

In moderated mode a non-moderator may narrow to `--type room.floor
--type room.mode --type room.close` and never wake for other people's
turns; it reads the whole session when the floor reaches it. The
**moderator** keeps `room.message` in the filter so floor requests
arrive.

### Turn-based participants

Phase 1 has no paste-line printer. `mempalace logstream append` prints
`Appended:` and the event summary — the same as any other append.
`mempalace task create`'s *Ready to paste* line is the shape to copy by
hand; Phase 2 `mempalace room floor` is what prints it. The template:

```text
You have the floor in MemPalace room project/mempalace/search-brainstorm
(session room_search-brainstorm_20260911_a3f1) as mac:codex:mempalace.
Catch up from your last cursor, then post one room.message or a room.pass.
```

The participant's human pastes that into the chat. The agent then, within
that single turn:

1. Page `event_list` until the session is exhausted:

   ```
   event_list stream=project/mempalace room=search-brainstorm
     correlation_id=<session> since_event_id=<its cursor> order=asc
     limit=50
   ```

   Default `limit` is 50; the server caps at 500. If `count == limit`,
   take the last event id on that page as the next `since_event_id` and
   call again. Stop when a page is shorter than `limit`. Preview is for
   inbox sweeps, not for taking a turn — read the bodies.

2. Append one `room.message` or `room.pass`.

3. Report the cursor as **the last event id you listed**, not the id of
   the event you just wrote. `since_event_id` resumes strictly after the
   anchor. If you jump the cursor to your own write, an event that landed
   between your catch-up and your append is skipped forever. Your own
   write will show up on the next catch-up; that is harmless.

Nothing is spawned, no second window opens, and the agent has full context
because the context *is the room*, read at the moment it is needed.

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

The moderator posts `room.close` with `to_agent=*` and the outcome in
their own words. **Append does not file anything.** Then the moderator —
or a participant the moderator names as scribe — files the session with
`mempalace_add_drawer` / `mempalace_kg_add` (MCP) as a separate step:

1. **The transcript, verbatim**, as drawers. One drawer per body-bearing
   event: `room.open`, `room.join` (when it has a body), `room.floor`
   (when it has a body), `room.message`, `room.pass`, `room.mode`,
   `room.close`. Wing = the project, room = the room name. The first
   line of each drawer is a locator, then the original body unchanged:

   ```
   [room.message evt_20260911T121500_ab12cd from=windows:claude:mempalace]
   BM25 in Rust is fine if the tokenizer seam stays in Python.
   ```

   MCP `add_drawer` derives its id from `(wing, room, content)` and
   ignores `source_file` for identity. Two identical "pass" or "agreed"
   turns therefore collapse unless the event id is *in the content*.
   Putting it on the first line keeps the original words searchable and
   makes each turn a distinct drawer. Set `source_file` to the event id
   anyway — it is useful metadata even though it is not the key.
   Phase 2 sugar can switch to an event-id-aware identity path.

2. **The decision**, as one additional drawer quoting the `room.close`
   body, and as KG facts via `mempalace_kg_add` where a single-valued fact
   was settled (`mempalace-core → uses → fused BM25 index`, valid from
   today).

Filing is the same *"File the outcome"* rule coordination-protocol.md
already imposes on delegations. A room that closes without being filed was
a chat, not a memory.

## 7. A complete moderated session

Igor opens a room from the shell, with two agents — one self-waking, one
turn-based — and Milla. Session id `room_search-brainstorm_20260911_a3f1`
is unique for this opening; a second brainstorm the same day mints a
different entropy suffix.

```bash
# 1. Open
mempalace logstream append --type room.open --stream project/mempalace \
  --room search-brainstorm --correlation-id room_search-brainstorm_20260911_a3f1 \
  --from-agent igor --to-agent '*' \
  --metadata '{"mode":"moderated","moderator":"igor"}' \
  --body "Should the Rust exact engine own BM25, or should BM25 stay in Python?"
```

Participants join (each from their own session, with their own tools):

```text
windows:claude:mempalace  → room.join  metadata.wake=self
                            arms: logstream watch --agent windows:claude:mempalace
                              --stream project/mempalace --room search-brainstorm
                              --correlation-id room_search-brainstorm_20260911_a3f1
                              --type room.floor --type room.mode --type room.close
                              --state-file ~/.mempalace/watch/windows-claude-search-brainstorm.json
mac:codex:mempalace       → room.join  metadata.wake=turn-based
milla                     → room.join  (human, posts from CLI)
```

Igor hands the floor, in turn. Each append carries the same
`--stream`, `--room`, `--correlation-id`, and `--from-agent` as the
open; they are required fields, not optional ones the `...` in a
sketch would let you drop.

```bash
mempalace logstream append --type room.floor --stream project/mempalace \
  --room search-brainstorm --correlation-id room_search-brainstorm_20260911_a3f1 \
  --from-agent igor --to-agent windows:claude:mempalace \
  --body "You wrote the sqlite_exact backend. What does BM25-in-Rust cost us?"
# → the watcher on the windows box exits 0; that session pages event_list
#   from its cursor and posts one room.message.

mempalace logstream append --type room.floor --stream project/mempalace \
  --room search-brainstorm --correlation-id room_search-brainstorm_20260911_a3f1 \
  --from-agent igor --to-agent mac:codex:mempalace \
  --body "Same question from the packaging side."
# → append prints only "Appended:". Igor copies the §4 paste template into
#   the Codex desktop app; that session pages the room and posts.

mempalace logstream append --type room.floor --stream project/mempalace \
  --room search-brainstorm --correlation-id room_search-brainstorm_20260911_a3f1 \
  --from-agent igor --to-agent milla
# → Milla reads the transcript and replies from her shell.
```

Igor closes, then files. Two steps, not one:

```bash
mempalace logstream append --type room.close --stream project/mempalace \
  --room search-brainstorm --correlation-id room_search-brainstorm_20260911_a3f1 \
  --from-agent igor --to-agent '*' \
  --body "Decision: BM25 moves into mempalace-core behind the existing tokenizer seam; Python keeps the tokenizer. Windows Claude to draft the task."

# Then the scribe files, separately — append does not write drawers:
# mempalace_add_drawer wing=mempalace room=search-brainstorm
#   content="[<type> <event_id> from=<from_agent>]\n<body>"
#   source_file=<event_id>
#   — one call per body-bearing event, then one decision drawer —
# mempalace_kg_add subject=mempalace-core predicate=owns object=bm25
#   valid_from=2026-09-11
```

Every append above is an existing command. The paste line and the
`--file` closer are Phase 2.

## 8. Implementation plan

### Phase 1 — Convention (this PR; docs only)

- This RFC.
- A `## Rooms` section in `integrations/shared/coordination-protocol.md`
  carrying §2–§6 in the same voice as the delegation protocol.
- Compact Rooms bullets **inside** the System-Prompt Snippet fence (and
  the test-pinned copies: `mempalace/instructions/shared_brain_rules.md`,
  `website/guide/shared-brain.md`), so `mempalace rules` actually emits
  them.
- The v1 simulation under `examples/multi_agent_room/` is removed.

Phase 1 does **not** add CLI subcommands, does not print a paste line from
`logstream append`, and does not file drawers on `room.close`. Those
claims were wrong in v2; the walkthrough above is the honest version.

### Phase 2 — Sugar (follow-up PR, after dogfood)

Thin wrappers over `logstream`, each printing `--json` and the paste line
where relevant. None of them are required to participate:

```bash
mempalace room open   <room> --stream project/x --mode moderated --body-file agenda.md
mempalace room join   <room> --wake self|turn-based
mempalace room floor  <room> --to <agent> [--body "..."]      # prints the paste line
mempalace room say    <room> --body-file turn.md [--to <agent>]
mempalace room pass   <room>
mempalace room catchup <room> [--since <event-id>]              # pages the session, prints cursor
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
  ordering is the logstream's local append order.
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
- **Filing granularity.** One drawer per body-bearing event keeps recall
  precise and verbatim; one drawer per session keeps the palace tidy.
  Starting with per-event because it is the faithful choice; revisit if it
  floods the taxonomy.
- **Event-id-aware `add_drawer` identity.** Phase 1 puts the event id in
  the drawer body so today's content-hash path cannot collapse identical
  turns. A first-class `source_file`/`event_id` key would be cleaner and
  belongs with Phase 2 sugar, not this RFC.
