# RFC 006: Agent Rooms

Status: Implemented (MCP tools; CLI is future work)
Owner: Igor Lins e Silva
Created: 2026-10-02
Branch: `feat/agent-rooms`
Prior art: RFC 003 (logstream), RFC 004 (replication), RFC 005 (identity),
PR #2447 (the convention-only design this replaces), PR #2457

## Summary

MemPalace is one person's shared brain, worked by several agents. RFC 003
gives those agents delegation: one addressee, one obligation, a patch at the
end. A **room** is the other kind of collaboration. Several agents think
through a design, a review, or a "what should we do about X" together, in a
trusted environment, with the single human as operator.

That can already happen on the raw logstream, but only by making every
participant learn the mechanics. A room moves those mechanics into the hub.
A participant does two things: it reads the room, and it speaks only if it
has something new.

## What #2447 got wrong

#2447 specified rooms as a convention over `event_append` and `event_list`.
It had seven event types, a floor-passing protocol, wake models declared on
join, and agents carrying their own cursor between turns. Agents paged
through results by hand and filed one drawer per turn themselves. About 175
words of it went into every agent's always-on prompt. Review kept finding
holes in exactly those mechanics:

- a cursor advanced to the agent's own write skips whatever landed in between
- a single `event_list` page silently drops the rest of a long room
- the first watcher arm misses a floor handed out right after the join
- per-turn filing in the chat window costs tokens on bookkeeping

Each fix added another rule for agents to follow. In this design the rules
are code on the hub instead.

## Design

### Four tools

| Tool | Does |
|---|---|
| `mempalace_room_open` | Appends `room.open` with the agenda and returns the room id and a one-line handoff. |
| `mempalace_room_read` | Returns everything the reader has not read yet, oldest first, and moves the reader's position. |
| `mempalace_room_say` | Appends one `room.message`, to the room or to one participant. |
| `mempalace_room_close` | Appends `room.close` with the outcome, then files the transcript. |

The light server exposes the same operations as `palace_coordinate ROOM
OPEN|READ|SAY|CLOSE`.

### Storage

A room is one correlation on the project stream:
`stream=project/<project>`, `room=rooms`, `correlation_id=room_<name>_<hex>`.
`rooms` is a logstream lifecycle channel like `delegation` or `status`, so
a room's name cannot collide with them. Every room event is a broadcast
(`to_agent=*`) unless a message addresses one participant. `status` stays
empty, because a room is not work. The random suffix makes every opening a
new room, so two discussions with the same name on the same day never mix.

### Read positions live on the hub

A new `read_cursors(scope, agent, last_seq)` table in `logstream.sqlite3`
holds each reader's position as a local rowid. `read_correlation` selects
the page and advances the position in one transaction under the logstream
lock. The position moves to the last event returned, never past one the
reader was not shown. `more` tells the reader the page was full.

- The first read returns everything, the reader's own turns included, so a
  fresh session sees its earlier words. That holds across pages: the first
  read records the newest event in the room, and the reader's own turns up
  to that mark are returned however many reads the catch-up takes. After
  it, the reader's own turns are left out: it wrote them.
- `say` returns `unread`, the number of messages from others since the
  speaker's last read, so an agent knows when the room moved while it was
  composing.

**Positions are local to one hub and never replicated.** A rowid is the
local arrival order, and RFC 004 gives remote events their own local rowid
on arrival. That cuts both ways:

- An event authored earlier on another replica but applied after a read is
  still delivered, because its local rowid is newer than the reader's
  position.
- A reader that switches replicas has no position there and starts over.
  That repeats messages but never skips one.

Agents normally reach one hub over MCP, so this is the common case. A
replicated position needs a position that means the same thing on every
replica (a version vector per reader). That belongs to RFC 003's
"server-side cursor" future work, not to rooms.

### Floor control is the operator

There is no floor lock, no admission, no moderation state machine. The
environment is trusted and there is one human. When the operator wants one
speaker at a time, they say so in the chat ("your turn"). The only rule
agents carry is the anti-chatter rule: speak only to add a fact, a
constraint, a proposal, an objection, or an answer to something addressed to
you; never post agreement, acknowledgement, or a restatement. Silence writes
nothing.

### Waking

Waking is unchanged from RFC 003:

- A turn-based agent (a desktop chat app, for example) acts when the
  operator pastes the handoff line or says "check the room".
- A self-waking agent can watch the room with
  `mempalace logstream watch --agent <me> --correlation-id <room id>
  --type room.message --type room.close`.

Nothing is spawned, and no window opens.

### Closing files the transcript

`room_close` appends `room.close` first, then files the transcript. If it
filed first, a message landing between filing and close would be lost.

Two guards keep "the transcript" well defined:

- **Nothing is accepted after a close.** `say` and `close` append with
  `unless_correlation_has=room.close`: the check for a close and the insert
  are one `BEGIN IMMEDIATE` transaction. A message that saw the room open
  but lost the race to a close is refused rather than stored outside the
  transcript. A second close reports `already_closed` rather than appending.
- **The boundary is the close's HLC, not its local arrival order.** The
  transcript is every room event whose HLC is at or before the close's.
  HLC is the same on every replica, so a turn written before the close on
  another replica belongs to the transcript even when it arrives here after
  the close, and closing again files it. A turn whose HLC is after the
  close stays in the logstream, verbatim, but outside the filed transcript.
  If replicas each recorded a close, the one with the earliest HLC is the
  room's close everywhere.

Each body-bearing turn (agenda, messages, outcome) becomes one drawer:

- `wing` = the project slug, palace `room` = the room name. A discussion
  room files into the palace room of the same name.
- `source_file` = the room id. It is a relative path, so `sync` classifies
  these drawers as having no source and never prunes them.
- The drawer text is a locator line, then the body unchanged:

  ```
  [room.message evt_20261002T121500_ab12cd34ef56 from=host:claude:myapp at=2026-10-02T12:15:00Z]
  BM25 in Rust is fine if the tokenizer seam stays in Python.
  ```

  Drawer ids derive from content, so the event id in the locator keeps two
  identical turns as two drawers.

A drawer holds at most 100,000 characters, while the logstream accepts
bodies up to 256 KiB. A turn that could not be filed would fail every close,
so open, say and close refuse a body over the drawer limit minus room for
the locator line (`MAX_ROOM_BODY_CHARS`).

Filing can fail, for example when the vector index is unavailable. In that
case close reports the error and leaves the room closed. Calling close again
appends nothing and re-files only what is missing: identical content means
an identical drawer id.

This is one tool call by the operator's agent, not N drawer calls in the
chat window. KG facts stay a judgment call for the operator or their agent
(`mempalace_kg_add`, or `mempalace_kg_supersede` when a single-valued fact
changed). Close does not derive them.

### Guard classification

| Tool | Mutating | Peer-writer exempt | HTTP lock-free | Integrity-gate exempt | Vector write |
|---|---|---|---|---|---|
| `room_open`, `room_say` | yes | yes | yes | yes | no |
| `room_read` | no | n/a | yes | yes | no |
| `room_close` | yes | no | no | no | yes |

`room_read` writes only the reader's position, which is bookkeeping rather
than palace state, so `--read-only` servers keep it. Close files N drawers
under the hub's request lock. That is fine for discussion-sized rooms;
batching the upsert is a later optimization.

## Prompt cost

The shared-brain snippet gains one bullet (about 55 words): when pointed at a
room, read it as your identity, then say something only if it is new. The
tool descriptions carry the rest.

## Future work

- `mempalace room` CLI for operators who drive from a shell. It needs a name
  that does not sit next to the existing `mempalace rooms` (palace rooms).
- A room view in PalaceMind: transcript, participants, a "paste to agent"
  button for the handoff line.
- Replicated read positions, together with RFC 003's server-side cursor.
- Batched transcript filing.

## Non-goals

- Several independent humans in one room. MemPalace is one person's shared
  brain. The operator is the human in the room.
- Summarizing turns. The transcript is filed verbatim; the outcome is the
  operator's own words.
- A room server, scheduler, or floor lock.
