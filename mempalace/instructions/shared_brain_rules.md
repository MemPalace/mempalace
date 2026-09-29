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
