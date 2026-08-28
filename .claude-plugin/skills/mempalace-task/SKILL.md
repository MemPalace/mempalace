---
name: mempalace-task
description: Create, hand off, claim, execute, and close agent tasks through the MemPalace logstream. Use when the user wants to delegate work, prepare a ready-to-paste task for another agent, receive a MemPalace task id, or explicitly launch a supported headless agent in controlled mode.
---

# MemPalace Task

A quick-start workflow for active work moving between agents. Tasks belong in
the logstream, not in memory drawers. The canonical lifecycle and watcher
discipline live in the public
[coordination protocol](https://github.com/MemPalace/mempalace/blob/main/integrations/shared/coordination-protocol.md).

## Verify the coordination seam

1. Confirm `mempalace --version` succeeds.
2. Confirm the MemPalace MCP tools are connected and the active palace is the
   intended shared brain.
3. Establish the current agent's stable identity. Never impersonate another
   agent.
4. Check the destination agent's monitoring status when possible. A pasted
   handoff can wake a turn-based agent; a logstream event alone cannot.

If setup is incomplete, stop and use the `mempalace` setup skill.

## Create a task

Gather these fields from the user or current repository state:

- project routing name;
- requesting and destination agent identities;
- exact goal;
- target branch;
- exact hexadecimal base commit id (never a branch or tag);
- definition of done.

Resolve the branch and hexadecimal base commit id from the intended worker
checkout, not from an unrelated repository. Never pass a mutable branch or tag
as the base commit. Before appending anything, show the user the **exact normalized task**:
the identities, project, goal, branch, base commit, definition of done, and the
fact that delivery must close through MemPalace. Logstream events are immutable,
so obtain approval of that exact content unless the user already explicitly
approved it in the current turn.

Create the task through the high-level interface rather than assembling a raw
`task.request`. When MCP is connected—especially for a client joining a remote
shared-brain hub—call `mempalace_task_create` with the approved fields. It
returns the stored `task` and `handoff` and writes on the hub, not to an
unintended local palace.

On the palace-owning machine, or when intentionally operating a local palace
through the shell, the equivalent CLI is:

```bash
mempalace task create \
  --project <project> \
  --from-agent <requester> \
  --to-agent <worker> \
  --goal <exact-goal> \
  --branch <branch> \
  --base-commit <commit> \
  --done <exact-definition-of-done>
```

Use `--goal-file` or `--done-file` for multiline CLI content; never flatten or
paraphrase the user's wording. The CLI prints a **Ready to paste** line; the MCP
tool returns the same line as `handoff`. Return it unchanged so the user can
wake the destination agent without copying the whole task body. Never use the
local CLI fallback merely because the remote MCP call failed—surface and fix
the connection instead.

## Receive and execute a pasted task

When given `Open MemPalace task <id> as <agent>...`:

1. Fetch the exact `task.request` by `correlation_id=<id>` with
   `mempalace_event_list`; do not work from the short pasted line alone.
2. Verify it is addressed to this agent (or is a broadcast explicitly being
   accepted), and verify the workspace, branch, and base commit before edits.
3. Claim the request with `mempalace_event_ack(status=claimed)`.
4. Do the work and run the stated verification.
5. Deliver a patch with `mempalace_patch_submit`. If blocked or failed, send a
   verbatim `task.reply` instead. Silence is not a valid outcome.
6. The requester fetches and verifies the artifact, applies it with explicit
   user-visible intent, runs verification, then acknowledges `applied` or
   `failed`.

For a remote-only shared-brain client, inbox monitoring is MCP-backed: use
`mempalace_event_wait` with the stable destination identity and carry the last
event id forward as `since_event_id`. Use `mempalace_event_list` for the wake-up
sweep. Do not run the local SQLite `mempalace logstream watch` command unless
this machine owns the palace or a deliberately synchronized replica.

## Controlled headless mode

Only when the user explicitly asks to launch an agent, first prepare a trusted,
clean Git checkout on the task's exact branch and base commit. Then run:

```bash
mempalace task launch <task-id> --runner codex --workspace <path>
mempalace task launch <task-id> --runner claude --workspace <path>
```

Those commands resolve a task from a local palace. On a remote-only MCP client,
fetch the single full `task.request` with `mempalace_event_list`, save that exact
event object as JSON on the destination machine, and launch without consulting
an unrelated local palace:

```bash
mempalace task launch --task-file <task-request.json> --runner codex --workspace <path>
```

The task file is a transport snapshot, not a replacement task: do not edit or
reconstruct it. Headless launch always occurs on the destination machine that
owns the trusted checkout; the requester cannot remotely spawn a process merely
by appending to the hub.

This resolves the stored request, proves the workspace is a clean Git checkout
on the stored branch with `HEAD` equal to the stored base commit, validates the
addressed identity and its conventional `*-codex` or `*-claude` harness suffix,
and starts the selected runner without a shell. Use `--agent <identity>` only
to accept a broadcast; it must never override a task addressed to somebody
else. The launcher does not bypass sandboxing, approvals, or harness
permissions. Do not add dangerous permission flags on the user's behalf.

Task creation and process launch are separate actions. The pasteable handoff is
the portable default; headless launch is an explicit controlled-mode adapter.

## Unhappy paths

- **Task missing or duplicated:** stop rather than guessing which request to
  execute.
- **Wrong identity:** refuse it and tell the user who the task addresses.
- **Base commit mismatch:** do not edit; ask the requester to supersede the
  task with a corrected request.
- **Destination not monitoring:** give the user the Ready to paste line and
  explain that a human wake-up is required.
- **MCP/logstream unavailable:** run `mempalace status` and return to setup;
  never silently replace coordination with a memory drawer.
