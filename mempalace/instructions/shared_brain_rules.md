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
