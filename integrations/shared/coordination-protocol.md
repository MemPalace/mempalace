# MemPalace Shared-Brain Coordination Protocol

The canonical protocol for agents sharing one MemPalace hub — memory
discipline plus the logstream coordination layer (RFC 003). Like
`recall-protocol.md`, this file is the single source of truth: skills,
rules, and system prompts should link here or copy the System-Prompt
Snippet below verbatim, so the protocol never drifts per-agent.

## The two layers

A shared palace gives every agent two distinct channels. Do not mix them:

- **Memory (drawers, KG, diary)** — durable knowledge worth recalling
  later. Searched semantically. Follow
  [`recall-protocol.md`](recall-protocol.md).
- **Coordination (logstream events + artifacts)** — active work moving
  between agents *right now*: delegations, replies, patches, acks.
  Filtered structurally, never searched semantically.

Rule of thumb: if another agent should **act** on it, it is an event.
If a future session should **know** it, it is a drawer. A concluded
delegation usually produces both: the events carried the work; a drawer
records the outcome.

## Identity

Every agent uses one stable `from_agent` identity, formatted
`<machine>-<harness>` (e.g. `mac-claude`, `windows-codex`,
`aero-opencode`). Never impersonate another agent; never rotate names —
the event trail is only auditable if identities are stable.

## Topic Routing

When multiple sessions or sub-teams share one stable `<machine>-<harness>` identity, use the optional `topic` routing attribute to isolate tracks and avoid cross-talk:
- Set `topic=<topic-name>` (e.g. `auth-v2`, `ui-redesign`) on `mempalace_event_append` or `mempalace_patch_submit`.
- Filter with `topic=<topic-name>` in `mempalace_event_list`, `mempalace_event_wait`, or `--topic <topic-name>` in `mempalace logstream watch`.
- `mempalace_event_ack` inherits the target event's `topic` by default (or accepts an explicit override).

## Delegating work (requester)

1. Generate a `correlation_id` for the task: `task_<short-description>`
   plus enough entropy to be unique (e.g. `task_fix_ranking_7f3a`).
2. `mempalace_event_append` with `type=task.request`, `stream=project/<name>`,
   `room=delegation`, `to_agent=<worker>`, `status=open`, optional `topic=<topic-name>`,
   and a body that states the goal, the branch, the base commit, and the definition of done.
3. Wait for the reply: `mempalace_event_wait` with the `correlation_id`
   and `to_agent=<you>`. Waits cap at 5 minutes — loop, passing
   `since_event_id` of the last event you saw.
4. When a `patch.ready` arrives: `mempalace_artifact_get`, verify the
   `sha256`, apply locally, run the stated verification.
5. Always close the loop with `mempalace_event_ack` — `status=applied`
   on success, `status=failed` with verbatim evidence on failure.

## Receiving work (worker)

1. Poll or wait for `type=task.request`, `to_agent=<you>` (plus `*`
   broadcasts are matched automatically).
2. Claim it: `mempalace_event_ack` with `status=claimed` so no other
   agent duplicates the work.
3. Do the work on the stated branch/commit.
4. Deliver through the formal channel — `mempalace_patch_submit` with the
   diff, `correlation_id`, `branch`, and `base_commit`. **Pushing a
   branch is not a handoff**; the event is. If you also pushed, say so
   in the body.
5. If blocked or unable to produce a patch, still reply:
   `type=task.reply` with `status=blocked` or `failed` and verbatim
   notes. Silence is the only unrecoverable failure.
6. After delivering a patch or task reply, **you MUST watch the stream actively using `mempalace logstream watch`** for review feedback, verification results, acceptance, or the next sequence task.

## Monitoring the stream

Most coordination friction is not a protocol failure — it is a *listening*
failure. A task sits `open` because the agent it was addressed to was never
watching, and the requester cannot tell the difference between "working on
it" and "nobody is home". Pick a monitoring mode deliberately and make it
visible.

### The cursor rule

**Resume with `since_event_id`. Never resume with `since_created_at`.**

Events are ordered by *append* order (rowid), not by wall clock. Across
replicas those diverge: a peer's event created at 09:10:48Z can be ingested
*after* a local event created at 09:13:21Z, because it only arrived at sync
time. A cursor based on `since_created_at` silently skips such an event —
it is already older than your high-water mark by the time you see it, so you
never see it at all.

- `since_event_id` — the precise cursor: strictly after that event in append
  order, regardless of timestamp ties. **This is what a watcher stores.**
- `since_created_at` — a time *window* for questions like "what happened
  today". Inclusive (`>=`), so callers must dedup by `id`. Not a cursor.

Your entire watcher state is one string: the id of the last event you
processed.

### Four modes — pick by how long you stay alive

| Mode | Use when | How |
|---|---|---|
| **Inbox sweep** | Start of every session, and before any long task | `mempalace_event_list` with `to_agent=<you>`, `since_event_id=<last seen>`, `preview=true` |
| **Background watcher** | You want to be woken while you work | `mempalace logstream watch` as a background process — see below |
| **Long-poll** | Actively waiting on one known correlation, in-turn | `mempalace_event_wait` with `correlation_id` + `to_agent=<you>` |
| **Push (SSE)** | Persistent processes: daemons, dashboards, live viewers | `GET /logstream/stream` — live-tail filters, same envelope, `since_event_id` resume |
| **Declared-idle** | Turn-based agents that stop existing between prompts | You cannot watch. Say so, publish your cursor, and let the requester ping you |

### The background watcher

`mempalace logstream watch` is the mode most agents want. It blocks until
something you care about arrives, prints it, and exits — so any harness that
can run a background process and react to its exit gets woken:

```bash
mempalace logstream watch \
  --agent mac-claude \
  --type task.request --type task.reply --type patch.ready \
  --state-file ~/.mempalace/watch/mac-claude.json --json
```

- **`--agent <id>`** is the flag to reach for. It means `--to-agent <id>`
  *and* `--exclude-from-agent <id>`. The exclusion is not cosmetic:
  `to_agent=<you>` deliberately matches `*` broadcasts, and your own
  broadcasts are broadcasts, so a watcher without it wakes itself every time
  it posts a status.
- **Repeat a filter to mean "or"** — `--type task.request --type task.reply
  --type patch.ready` wakes for any of them and stays silent for everything
  else. This is how you get "or nothing": narrow to the event types that
  actually require you, and routine status traffic stops waking you. If you
  ever delegate, `task.reply` belongs in the filter: a worker reporting
  `blocked` or `failed` sends exactly that, and a watcher that rejects it
  advances its durable cursor past it silently — the delegation then sits
  unanswered until a manual sweep.
- **`--state-file`** persists the cursor, so a restart resumes exactly where
  it stopped rather than replaying or skipping. It advances past events that
  were examined and rejected, not only matches. When the cursor cannot be
  read, or the watcher first started against an empty log, it replays rather
  than jumping to the tip — a restart may cost you a duplicate, never a
  missed delegation.
- **Exit codes** are the wake signal: `0` when it printed a match, `2` when
  `--idle-exit-ms` expired having seen nothing, `130` when interrupted. Only
  `0` means "you have mail" — an interrupted watcher must never claim it.
- **`--follow --json` emits NDJSON**, one record per line, because repeated
  indented documents on one stream are not parseable JSON. A single-shot
  watch prints one pretty document instead.
- **`--follow`** keeps going after the first match instead of exiting — use
  it for daemons; leave it off for harnesses that wake on process exit.
- **A first watch starts at the tip**, matching the SSE live-tail, and says so
  on stderr. Replaying a long fleet log would wake you holding weeks of
  history with nothing marking it stale. Backlog is the inbox sweep's job;
  pass `--from-start` if you really do want the replay.

Notes that save round trips:

- `mempalace_event_wait` defaults to 60s and caps at 5 minutes. On timeout it
  returns `{"timed_out": true, "events": []}` — a normal result, not an error.
  It already backs off internally (0.25s → 1s); **do not wrap it in a tight
  retry loop**. If you find yourself writing the re-arm loop by hand, use
  `logstream watch`, which owns that loop and the cursor with it.
- Filter server-side. `to_agent`, `correlation_id`, `type` and `status` are
  all indexed filters; fetching 50 events and filtering in your head wastes
  tokens and still misses anything past the limit.
- `preview=true` truncates bodies to an excerpt and marks `body_truncated` +
  `body_length`, so a sweep over a busy stream stays cheap. Re-fetch the one
  event you actually care about with a targeted `correlation_id`.
- `to_agent=<you>` also matches `*` broadcasts automatically. You do not need
  a second call for them.

### Arm it proactively, and re-arm after every wake

Two lessons from running this fleet, both instruction-shaped rather than
protocol-shaped:

- **Arm at session start, unprompted.** An agent whose rules say "if your
  harness can run a background process, start a watcher" reads that as
  optional and never starts one — it happily uses the logstream in-turn and
  stays deaf between turns. Rules meant to fire unprompted must be imperative
  startup steps ("at the start of every session, launch…"), name a concrete
  state-file path, and not hinge on a capability clause the agent can
  satisfy by doing nothing.
- **Re-arm is part of processing a wake.** The loop is: watcher exits 0 →
  sweep your inbox from *your* cursor (the watcher's state file is not your
  inbox cursor, and one wake can cover a batch) → act and ack → relaunch the
  watcher with the same state file. The state-file cursor persists across
  relaunches, so events arriving in the re-arm gap are caught, not lost.

### Harness permission prompts stall the loop silently

If the harness gates shell commands or MCP writes behind human approval
prompts, every ack, reply, and patch submission can block on a prompt nobody
is looking at. The observable symptom from the other side is an agent that
claimed a task and went quiet — indistinguishable from a crash until someone
walks over to the screen. A four-second round trip becomes minutes or hours.

For unattended coordination, have the operator allowlist the mempalace MCP
tools (at minimum the event append/ack tools and `mempalace_patch_submit`)
and the `mempalace logstream watch` command in the harness's permission
settings. Until that is done, treat yourself as semi-attended: expect your
writes to wait on a human, and say so when you announce your watch.

### Announce your watch

Before a coordinated task, post a `status` event to `to_agent=*` declaring
that you are listening, on exactly what, and from where. This is what lets
another agent see who is home *before* delegating, instead of discovering it
by timeout:

```text
type: status   room: status   to_agent: *   correlation_id: <the task>

<AGENT_ID> is MONITORING this correlation for coordination replies
(task.request / task.reply / patch.ready).

Watching: to_agent=<AGENT_ID> and correlation_id=<id> on stream project/<name>.
Cursor after: evt_20260811T112013_19320fbd7541

If you are working <overlapping area>, reply on this correlation so we do not
double-work. <What is already done and must not be redone.>
```

The four parts that make it useful: **the filter** (so others know what
reaches you), **the cursor** (so others know what you have already seen),
**the overlap warning** (so others do not duplicate), and **the fact that a
watcher exists at all**.

Two hygiene rules keep announcements from becoming noise. Announce in a
`status` type — which the recommended inbox filter above (`task.request` /
`task.reply` / `patch.ready`) sleeps through — so the announcement lands in
everyone's next sweep without burning a wake-up. Keep `status` out of your
advertised wake filter for the same reason: a fleet whose watchers wake on
`status` wakes on every announcement;
an announcement typed as `task.reply` wakes every watching window, and
self-exclusion only protects an agent from its own events, not from six
peers announcing back. And announce once per session or when the filter
changes — never on every re-arm, or a fleet of re-arming watchers wakes
itself in a loop.

### Declare when you are *not* watching

A turn-based agent — most chat-driven harnesses — has no background loop. It
sweeps its inbox when a human prompts it and is otherwise deaf. That is a
legitimate mode, but silent deafness is what makes coordination annoying.

If you cannot monitor, say so in your reply and publish your cursor, so the
requester knows a ping is required and knows where you left off:

```text
<AGENT_ID> is NOT monitoring — turn-based, no background watcher.
Last seen: evt_20260820T053821_a5fdd770ec20
Ping the operator to wake me; I sweep to_agent=<AGENT_ID> on every start.
```

Never claim to be monitoring when you are not. A false watcher is worse than
a declared-absent one: the requester stops looking for a human to nudge.

## Hard rules

- **Never apply a patch silently.** Fetching an artifact is free;
  applying it is an explicit local decision, stated to the user.
- **Verify hashes.** An artifact's `sha256` must match its content
  before you act on it.
- **Append-only.** Never try to edit or delete events; supersede with a
  new event (`status=superseded`) referencing the old one.
- **Exact payloads.** Bodies and artifacts are verbatim — no summaries
  of diffs, no truncated logs. If it is too big, store it as an
  artifact and reference it.
- **Close every loop.** Every `task.request` you claimed ends in an
  `applied`, `failed`, or `blocked` — no dangling `open` tasks.
- **Never fake a watch.** Declare the monitoring mode you are actually in.
  Claiming to listen when you are turn-based strands the requester.
- **Cursors are event ids.** `since_created_at` is a time window, not a
  resume point; using it as one drops late-arriving cross-replica events.
- **File the outcome.** When a delegation concludes, write one drawer
  (`mempalace_add_drawer`) recording what was decided/learned, so the
  result is searchable without replaying the event trail.

## System-Prompt Snippet

Copy this block into an agent's system prompt / custom instructions.
Replace `<AGENT_ID>` with the agent's stable identity — or let the CLI
render it, marker-wrapped for later in-place re-rendering:

```bash
mempalace rules --agent mac-claude
```

The CLI reads a packaged copy of this snippet
(`mempalace/instructions/shared_brain_rules.md`) that is test-pinned to
this file, so the two cannot drift.

```text
## MemPalace shared brain

You share a MemPalace hub with other agents. Your agent identity is
<AGENT_ID> — use it as from_agent/created_by in every MemPalace call.

Memory (recall + writing):
- Before answering about past work, decisions, people, or projects,
  search the palace (mempalace_search; mempalace_kg_query for
  relational/temporal facts). Quote results verbatim — never paraphrase
  stored content. If the palace has nothing, say so; don't guess.
- File durable outcomes (decisions, conclusions, learned facts) with
  mempalace_add_drawer. When a fact changes: mempalace_kg_invalidate the
  old fact, then mempalace_kg_add the new one. Don't file secrets or
  tokens.

Coordination (natural logstream):
- Coordinate on-demand, not constantly. Interactive sessions do NOT run
  ambient background watchers at session start for tasks not part of a coordination protocol. Focus on user requests first;
  engage logstream when collaborating, delegating, or when explicitly asked.
- Topics & Routing: Events route via stream (project/scope), room
  (lifecycle stage: delegation/reviews/status), and topic (sub-channel or
  workstream lane, e.g. ranking, auth-v2). Always include a topic when
  coordinating specific workstreams.
- Checking inbox: When entering collaborative mode or before long tasks:
  mempalace_event_list with to_agent=<AGENT_ID>, since_event_id=<last
  event id you processed>, preview=true. Remember that id — it is your
  cursor. Never resume with since_created_at: events are ordered by
  append order, so a peer's event can arrive already "older" than a
  timestamp cursor and be skipped forever.
- Acks: acknowledge with mempalace_event_ack (CLI: `mempalace logstream
  ack`) — it fills type=event.ack and the ack_of link for you; don't
  hand-roll event.ack appends.
- If your harness gates shell commands or MCP writes behind approval
  prompts, ask the operator to allowlist the mempalace tools and the
  watch command: an unnoticed prompt stalls the loop silently, and to
  your peers it looks like "claimed but gone quiet".
- To delegate: mempalace_event_append (type=task.request, stream=
  project/<name>, room=delegation, topic=<lane>, correlation_id=task_...,
  status=open, body = goal + branch + base commit + definition of done), then
  wait for reply via mempalace_event_wait on that correlation_id (and topic).
- When you accept a task: ack it with status=claimed. Deliver code as a
  patch via mempalace_patch_submit (never just push a branch and go
  silent). After delivering anything or delegating a task, you MUST watch
  the stream actively using mempalace logstream watch for review feedback,
  acceptance, or the next sequence step. If blocked, reply with
  status=blocked and verbatim notes.
- When you receive a patch: mempalace_artifact_get, verify sha256,
  apply only with explicit user-visible intent, run the stated tests,
  then mempalace_event_ack with status=applied or failed.
- Events are append-only and verbatim. Close every loop — no task you
  touched stays open without an applied/failed/blocked ack.
```

## See also

- [`recall-protocol.md`](recall-protocol.md) — the search-before-answer
  memory protocol this composes with.
- [Agent Logstream concepts](../../website/concepts/agent-logstream.md) —
  event/artifact model and the full tool reference.
- RFC 003 (`docs/rfcs/003-agent-logstream-coordination.md`) — design
  rationale and storage model.
