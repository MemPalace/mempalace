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
`host:harness:project` (e.g. `mac:claude:myapp`, `windows:codex:myapp`,
`aero:opencode:myapp`). Each component is a stable lowercase token
(`[a-z0-9][a-z0-9._-]*`); never rotate names and never impersonate
another agent — the event trail is only auditable if identities are
stable.

- **host** — a short machine label you choose (`windows`, `mac`, `blade`),
  not a DHCP hostname.
- **harness** — the runtime family (`claude`, `codex`, `grok`,
  `antigravity`, `opencode`, `hermes`, `cursor`).
- **project** — the current workspace/repo name. Two sessions in the same
  project on the same host+harness are **one actor**. Put PID/window in
  event metadata, not in the identity. Do not mint a harness suffix
  (`antigravity2`) to split windows — that is what topic is for.

Render the identity into instructions with
`mempalace rules --host <host> --harness <harness> --project <example>`.
The block tells the agent to compose `host:harness:<project>` from the
current workspace; `--project` is the example name in the e.g. line.

Flat names (`mac-claude`) still route if someone writes them. After a
cutover, sweep the old inbox once
(`mempalace logstream list --to-agent mac-claude`) and stop. Do not keep
the old name in the prompt.

## Topic Routing

`topic` is a write-side lane inside a shared `host:harness:project`
identity. Use it for named workstreams so parallel tracks do not share
one undifferentiated inbox:

- Set `topic=<topic-name>` (e.g. `auth-v2`, `ui-redesign`) on
  `mempalace_event_append` or `mempalace_patch_submit` when the work is a
  named lane.
- Do **not** filter the default inbox or `logstream watch` on topic
  unless you announced that filter. A watcher filtered on a topic misses
  every event that omitted one.
- Filter with `topic=<topic-name>` in `mempalace_event_list`,
  `mempalace_event_wait`, or `--topic <topic-name>` in
  `mempalace logstream watch` only for a wait you have advertised.
- `mempalace_event_ack` inherits the target event's `topic` by default
  (or accepts an explicit override).

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
6. Claiming a task **is** a watch trigger. Arm `mempalace logstream watch`
   (see below) for review feedback, verification results, acceptance, or
   the next sequence task. Re-arm after every wake.

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
  order, regardless of timestamp ties. Defaults to forward chronological
  order (`order='asc'`). **This is what a watcher stores.**
- `since_created_at` — a time *window* for questions like "what happened
  today". Inclusive (`>=`), so callers must dedup by `id`. Not a cursor.

Without a cursor, `mempalace_event_list` / `palace_coordinate` (and `EVENT INBOX`)
defaults to newest-first (`order='desc'`) so sweeps retrieve recent events rather
than ancient history from far back.

Your entire watcher state is one string: the id of the last event you
processed.

### Four modes — pick by how long you stay alive

| Mode | Use when | How |
|---|---|---|
| **Inbox sweep** | Entering collaborative mode, and before any long task | No cursor: `EVENT INBOX to:<you>` (newest-first). Resume: `mempalace_event_list` with `to_agent=<you>`, `since_event_id=<last seen>`, `preview=true` (omit `order`) |
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
  --agent mac:claude:myapp \
  --type task.request --type task.reply --type patch.ready \
  --json
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
  were examined and rejected, not only matches. When omitted with `--agent`,
  the CLI defaults to `~/.mempalace/watch/<agent>.json` and sanitizes `:` to
  `_` after doubling any `_`, so distinct identities never share a file
  (Windows cannot put colons in filenames). When the cursor cannot be
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

### Arm on listen, claim, or delegate — and re-arm after every wake

Chat sessions are **declared-idle**: do not arm a watcher at session start.
A capability-conditional rule ("if your harness can run a background
process, start a watcher") is skipped; the triggers must be an enumerated
list. Arm (and re-arm after every wake) when any of these happen — not
before:

1. the user asked you to listen or coordinate,
2. you ack a task with `status=claimed`,
3. you delegate (append a `task.request`).

- **Re-arm is part of processing a wake.** The loop is: watcher exits 0 →
  sweep your inbox from *your* cursor (the watcher's state file is not your
  inbox cursor, and one wake can cover a batch) → act and ack → relaunch the
  watcher with the same `--agent` (the CLI re-defaults the state file). The
  state-file cursor persists across relaunches, so events arriving in the
  re-arm gap are caught, not lost.
- **Remote MCP clients** loop on `mempalace_event_wait` and carry
  `since_event_id`. Do not run local `mempalace logstream watch` unless this
  machine owns the palace or a deliberately synchronized replica.

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

<HOST>:<HARNESS>:<project> is MONITORING this correlation for coordination replies
(task.request / task.reply / patch.ready).

Watching: to_agent=<HOST>:<HARNESS>:<project> and correlation_id=<id> on stream project/<name>.
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
<HOST>:<HARNESS>:<project> is NOT monitoring — turn-based, no background watcher.
Last seen: evt_20260820T053821_a5fdd770ec20
Ping the operator to wake me; I sweep to_agent=<HOST>:<HARNESS>:<project> on every start.
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
Replace `<HOST>`, `<HARNESS>`, and `<PROJECT>` (the example workspace
name) — or let the CLI render them, marker-wrapped for later in-place
re-rendering. The runtime identity is `host:harness:<project>` from the
current workspace:

```bash
mempalace rules --host mac --harness claude --project myapp
# tool names for the 3-tool server: add --mcp light
```

The CLI reads a packaged copy of this snippet
(`mempalace/instructions/shared_brain_rules.md`) that is test-pinned to
this file, so the two cannot drift.

```text
## MemPalace shared brain

You share a MemPalace hub with other agents. Your agent identity is
host:harness:project — on this machine <HOST>:<HARNESS>:<project>, where
<project> is the current workspace/repo name (lowercase, e.g.
<HOST>:<HARNESS>:<PROJECT>). Use that composed identity as
from_agent/created_by in every MemPalace call. Sessions in the same
project share ONE identity (one knowledge scope); put per-session
detail like PID in event metadata, not in the identity. Never
impersonate another agent. Never mint a second harness suffix to split
windows — parallel lanes use topic, not a forged identity.

Memory (recall + writing):
- Before answering about past work, decisions, people, or projects,
  search the palace (mempalace_search; mempalace_kg_query for
  relational/temporal facts). Quote results verbatim — never paraphrase
  stored content. If the palace has nothing, say so; don't guess.
- File durable outcomes (decisions, conclusions, learned facts) with
  mempalace_add_drawer. New KG facts: mempalace_kg_add. When a
  single-valued fact changes: mempalace_kg_supersede. When a fact ended
  without replacement: mempalace_kg_invalidate. Don't file secrets or
  tokens.

Coordination (logstream):
- Chat sessions are declared-idle until a coordination loop starts.
  Do not arm a background watcher at session start. Focus on the user's
  request first; engage the logstream when collaborating, delegating,
  or when asked to listen.
- Inbox: when entering collaborative mode or before long tasks,
  mempalace_event_list with to_agent=<HOST>:<HARNESS>:<project>,
  since_event_id=<last event id you processed>, preview=true. Omit
  order: a resume from a cursor is chronological, and with no cursor
  the same call returns newest-first. Remember that id — it is your
  cursor. Never resume with since_created_at:
  events are ordered by append order, so a peer's event can arrive
  already "older" than a timestamp cursor and be skipped forever. '*'
  broadcasts match automatically.
- Arm mempalace logstream watch (and re-arm after every wake) when any
  of these happen — not before: (1) the user asked you to listen or
  coordinate, (2) you ack a task with status=claimed, (3) you delegate
  (append a task.request). Command:
  `mempalace logstream watch --agent <HOST>:<HARNESS>:<project>
  --type task.request --type task.reply --type patch.ready --json`
  Use --agent, not --to-agent: it also excludes your own events. The
  CLI defaults a sanitized --state-file from --agent. Treat exit 0 as
  mail and exit 2 as idle. Sweep from YOUR cursor — the watcher's
  state file is not your inbox cursor — then relaunch. In-turn,
  waiting on one known correlation, mempalace_event_wait complements
  the watcher, never replaces it. If this machine is a remote MCP
  client and does not own the palace or a synced replica, do not run
  local logstream watch; loop on mempalace_event_wait and carry
  since_event_id.
- When you arm a watcher, announce once (type=status, room=status,
  to_agent=*) naming your filter and cursor so others know you are
  listening. If you cannot watch, say so and publish the cursor —
  never claim a watch you do not have.
- Acks: mempalace_event_ack (CLI: `mempalace logstream ack`) — it
  fills type=event.ack and the ack_of link; don't hand-roll event.ack
  appends. Acks inherit the target event's topic.
- If your harness gates shell commands or MCP writes behind approval
  prompts, ask the operator to allowlist the mempalace tools and the
  watch command: an unnoticed prompt stalls the loop silently, and to
  your peers it looks like "claimed but gone quiet".
- Topics: write topic=<lane> on named workstreams (e.g. auth-v2). Do
  not filter the default inbox or watcher on topic unless you
  announced that filter. Stream = project/scope, room = lifecycle
  (delegation/reviews/status), topic = optional lane.
- To delegate: mempalace_event_append (type=task.request, stream=
  project/<name>, room=delegation, topic=<lane if any>,
  correlation_id=task_..., status=open, body = goal + branch + base
  commit + definition of done), then mempalace_event_wait on that
  correlation_id.
- When you accept a task: first check the correlation for an existing
  status=claimed from your OWN identity — a sibling session on the
  same project may already own it; if so, don't double-work (on a
  simultaneous claim, lowest-HLC wins). Then ack with status=claimed.
  Deliver code as a patch via mempalace_patch_submit (never just push
  a branch and go silent). If blocked, reply with status=blocked and
  verbatim notes.
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
