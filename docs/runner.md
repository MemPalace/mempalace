# Optional machine runner

The runner lets a signed-in workspace owner or a granted coordinator request an agent start for a card the machine's worker could claim. It is inert by default: no machine means no change; an `unbound` machine refuses every start, including the owner's, and leases return 204. No config or service activation ships enabled. D1–D3 below remain open.

## Trust and authority

Only a signed-in person owner can enroll, attest, change grants, rotate credentials or revoke a machine through `/api/runners`. Enrollment returns separate machine and worker tokens once. Attestation records an owner statement as `owner_attested`, always with `verified:false`: **attested, not verified**. Topic or wing changes, worker rotation and revocation reset the binding to `unbound`; queued and leased jobs are cancelled on the next sweep.

Coordinators are CLI-created trusted PATs explicitly listed in the machine's `requesters`; OAuth and ordinary standard or untrusted connections do not qualify. A start checks both the requester's card access and whether the worker could claim the unassigned, released `todo` card in its write wing. It does not assign or claim the card.

The worker PAT bounds tools and wing access. Its tools are `task_show`, `task_take_next`, `task_update`, `task_comment`, `task_heartbeat`, `task_handoff` and `ask`; it cannot coordinate starts or perform owner actions. The fixed `SPAWN_MESSAGE` tells the agent to claim only the requested card, stop if refused, and touch no other card. Card scope from the spawn message is guidance only. A worker can claim any startable todo card in its write wing. Also, a card manually assigned to the worker by an owner remains editable outside its write wing under the existing assigned-card rule. The runner never assigns cards.

The T3 grant supplied through `t3_grant_env` is outside the worker PAT ceiling. It reaches whatever T3 permits; local runtime ceilings constrain an honest runner only. The default runtime ceiling is `approval-required`; a higher local ceiling needs `runtime_ack == "<label>:<mode>"`, and requests must also fit the hub grant.

## Local checks and checkout

Use `python -m commonplace runner check --config F` before `python -m commonplace runner run --config F [--once]`. The owner writes the JSON config with mode 0600 on POSIX. `check` never contacts the hub. Every topic needs a local `binding_acks` entry, `"<label>:<topic>"`.

Every configured project must have `shared_checkout_ack == "<label>:shared-checkout"`. A missing or different value makes both commands exit 4 before any hub contact, lease or spawn, with `runner: config_invalid: project <label> needs shared_checkout_ack "<label>:shared-checkout"`. Two project labels sharing one `project_id` also cause exit 4 (`config_invalid`): exclusivity is per distinct `project_id` on each machine.

The fixed workspace strategy is `{"type":"root"}`, **unverified until D2**. Its assumed meaning is that agents share the T3 project's checkout over time. It is not configurable. For isolation, point each project at a distinct T3 project rooted in a dedicated clone. `runner check` prints the shared-checkout warning and the uncaptured-launch-shape notice; passing it does not verify T3 behavior or worker identity.

## Capacity and uncertain launches

`max_in_flight` (default 1, range 1–4) counts this machine's `leased`, `starting` and `uncertain` jobs, plus `started` jobs held for `STARTED_HOLD_SECONDS = 3600` after `started_at`. Lease selection enforces both that machine cap and one in-flight agent per project. This bounds “agents started in the last hour or starting.” An agent running past the one-hour hold is uncounted, so another agent may start in its checkout. A real end-of-thread bound needs D3. The hold is fixed, not a config key; hourly starts and the queue have separate limits.

An ambiguous launch becomes `uncertain`, never automatically queued again. It keeps its machine/project slot and blocks another start on the card. An owner sees it with `card_start_status(task)` (or `GET /api/card-starts?task=<n>`), and inspects the machine's grant and binding through `GET /api/runners`. A person owner must investigate and use `card_start_resolve(job, outcome, thread_id?)`: `started` requires a thread id; `failed` releases the slot. A late `started` result from the same machine with its stored lease token can also resolve uncertainty.

`card_start_cancel` on `uncertain` returns 409 for everyone, including the owner: `start outcome is uncertain; a workspace owner must resolve it`. Cancelling a `starting` job only records the request; it does not stop a remote thread or free the slot. Resolve spans the control and workspace databases: a crash between commits can leave a resolution without its audit row, although the job retains `resolved_by` and `resolved_at`.

## Hub failures

Continuous mode backs off on disabled-machine 404s and transient failures (429, 5xx, redirects, timeouts, connection errors or non-JSON success bodies). The delay is `min(poll_seconds * 2**k, 300)` seconds for consecutive non-ok calls, reset by success. Redirects are never followed. Journals replay outcomes before new leases and never re-spawn a recorded launch. The hub timeout is at least 10 seconds to cover the plan delay of up to 2 seconds.

| Exit | Meaning |
|---|---|
| 0 | Success or no job; a fenced job (409) is dropped. |
| 2 | CLI usage/error path. |
| 3 | Machine credential revoked or expired; journal kept. |
| 4 | `config_invalid`, including missing checkout acknowledgement or duplicate project ids. |
| 5 | Hub unavailable or refused in `--once` mode; continuous mode backs off. |

## Owner checklists (open; no agent performs any item)

**D1–D3 (decisions).**
- [ ] **D1 worker identity.** The owner decides whether "attested, not verified" is acceptable. The setup would be a dedicated T3 provider instance whose Commonplace MCP entry uses the worker token, mapped to the granted topics, recorded through attest. Anything stronger needs a future T3 per-thread credential field. The card goal is not done until D1 is decided.
- [ ] **D2 launch shape.** One owner-approved real `t3_thread_launch` capture on an owner machine. It settles `structuredContent.threadId/runId/status`, `workspaceStrategy` `root` vs. `existing_worktree`, `modelSelection`, and what "modes cannot exceed yours" means for the T3 grant. Record the artifact path. An optional later detective read-back (the `t3.py:115–148` pattern) is design only.
- [ ] **D3 recovery.** Capture the `thread list --json` keys before any reconcile. Until then, `uncertain` goes to a person resolve.

**Activation (F10; strict order, each a separate owner act).**
1. [ ] D1 decision recorded.
2. [ ] D2 capture artifact path recorded; the parser contract **and the `root` workspace strategy** re-checked against it (OD-B).
3. [ ] T3 provider instance and topic mapping set up by the owner; T3 grant scoping known, or its absence accepted.
4. [ ] Only then: a real `POST /api/runners` enrollment and `attest` in the owner's own session.
5. [ ] Runner config written by the owner (0600), with `shared_checkout_ack` for each project only after the owner accepts that agents work in that project's checkout (or points the project at a dedicated clone, OD-B); `runner check` exit 0. Any service install or activation is a separate approval.
