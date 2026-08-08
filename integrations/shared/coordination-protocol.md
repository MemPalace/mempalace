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

## Delegating work (requester)

1. Generate a `correlation_id` for the task: `task_<short-description>`
   plus enough entropy to be unique (e.g. `task_fix_ranking_7f3a`).
2. `mempalace_event_append` with `type=task.request`, `stream=project/<name>`,
   `room=delegation`, `to_agent=<worker>`, `status=open`, and a body that
   states the goal, the branch, the base commit, and the definition of done.
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
- **File the outcome.** When a delegation concludes, write one drawer
  (`mempalace_add_drawer`) recording what was decided/learned, so the
  result is searchable without replaying the event trail.

## System-Prompt Snippet

Copy this block into an agent's system prompt / custom instructions.
Replace `<AGENT_ID>` with the agent's stable identity.

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

Coordination (logstream):
- Check your inbox when starting work and before long tasks:
  mempalace_event_list with to_agent=<AGENT_ID> (new since your last
  seen event id).
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
```

## See also

- [`recall-protocol.md`](recall-protocol.md) — the search-before-answer
  memory protocol this composes with.
- [Agent Logstream concepts](../../website/concepts/agent-logstream.md) —
  event/artifact model and the full tool reference.
- RFC 003 (`docs/rfcs/003-agent-logstream-coordination.md`) — design
  rationale and storage model.
