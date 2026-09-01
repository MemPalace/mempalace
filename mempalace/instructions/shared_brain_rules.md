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

Coordination (logstream):
- Check your inbox when starting work and before long tasks:
  mempalace_event_list with to_agent=<AGENT_ID>, since_event_id=<last
  event id you processed>, preview=true. Remember that id — it is your
  cursor. Never resume with since_created_at: events are ordered by
  append order, so a peer's event can arrive already "older" than a
  timestamp cursor and be skipped forever.
- Monitoring: at the start of every session, right after the inbox
  check, launch a background watcher — do not wait to be asked, and do
  not wait for a coordinated task to begin:
  `mempalace logstream watch --agent <AGENT_ID> --state-file
  ~/.mempalace/watch/<AGENT_ID>.json --json`, run as a background
  process. Treat its exit as "you have mail" (exit 0 = match, 2 = idle):
  process your inbox from YOUR cursor — the watcher's state file is not
  your inbox cursor, and one wake can cover several events — then
  relaunch the watcher with the same state file. Keep this loop alive
  all session. Use --agent, not --to-agent: it also excludes your own
  events, which otherwise wake you via the '*' broadcast match. Repeat
  --type to wake only for what needs you; if you delegate, include
  task.reply — blocked and failed arrive as replies. In-turn, waiting on
  one known correlation, mempalace_event_wait is enough — it complements
  the watcher, never replaces it. Before a coordinated task, post a
  status event to to_agent=* naming your filter and your cursor so others
  know you are listening. Only if your harness truly cannot run
  background processes are you turn-based: say so and publish your
  cursor — never claim a watch you do not have.
- Acks: acknowledge with mempalace_event_ack (CLI: `mempalace logstream
  ack`) — it fills type=event.ack and the ack_of link for you; don't
  hand-roll event.ack appends.
- If your harness gates shell commands or MCP writes behind approval
  prompts, ask the operator to allowlist the mempalace tools and the
  watch command: an unnoticed prompt stalls the loop silently, and to
  your peers it looks like "claimed but gone quiet".
- To delegate: mempalace_event_append (type=task.request, stream=
  project/<name>, room=delegation, correlation_id=task_..., status=open,
  body = goal + branch + base commit + definition of done), then
  mempalace_event_wait on that correlation_id for the reply.
- When you accept a task: ack it with status=claimed. Deliver code as a
  patch via mempalace_patch_submit (never just push a branch and go
  silent). If blocked, reply with status=blocked and verbatim notes.
- When you receive a patch: mempalace_artifact_get, verify sha256,
  apply only with explicit user-visible intent, run the stated tests,
  then mempalace_event_ack with status=applied or failed.
- Events are append-only and verbatim. Close every loop — no task you
  touched stays open without an applied/failed/blocked ack.
