# System

Decisions, numbers, stores and accepted residuals that hold across modules.
`MODULES.md` says which module owns each path; this file says what the whole
system promises. A change that alters a decision, a number, a store or a
residual updates this file in the same change; `docs-check` enforces that
(see "Keeping this file current").

Labels: **M** = measured (from code or a run), **T** = target, **G** = guess
that rollout must measure. `file:line` references are to the tree in which the
entry was written.

## Decisions

### Restricted-origin card content

Trusted agents and authorized board coordinators may update only the title
and body of an unassigned todo card marked as restricted-origin. Board write
access and proposal approval remain required. Each edit records its actor in
the existing audit log; it neither releases the origin nor changes claiming,
activation, reservation or other update permissions.

### Optional agent orchestration

- **Default off.** The workspace owner explicitly enables orchestration; absent
  settings mean off. A settings read error refuses admission. Off retains the
  existing claim/start behavior and board JSON shape.
- **One project count.** Claims, queued starts, leases and configured starts share
  a runner transaction. Ordinary agents count by connection, workers by card;
  card/job overlaps count once. Default cap is 3; project rows override it,
  configured boards use the smaller config/row cap. Board `used` shows active slots,
  excluding the queued reservations counted at start creation.
- **Authority.** Unconfigured starts require the active board coordinator PAT or
  a signed-in owner. Owners separately trust seats and grant machines atomically;
  switching off demotes all seats and removes only their requester ids. Trusted
  seats cannot create boards. Connect alone never grants this trust.
- **Lock order.** Control snapshots precede waits. Claims hold boards RESERVED then
  runner admission until board commit; runner admissions read boards SHARED.
- **Residual.** Without a runner or subscribed `card.created` webhook, a new card
  waits for the orchestrator's next session. An always-running orchestrator is
  separate work. Splitting and inbox behavior are not introduced by this switch.

### Orchestrator config as code

- **The store is the source of truth.** Each board has versioned orchestrator
  configs (`v<n>`, with parent version, channel, source ref and sha256) in the
  workspace's `runner.sqlite3`. `.commonplace/orchestrator.toml` in a repo is
  only a proposal; `orch submit` proposes it and the owner approves its sha256.
- **The owner approves every change.** Wing writers propose; only `orch_owner`
  accepts, rejects, retires an experiment, forces a lease, voids a step or
  acknowledges a restore. `orch_owner` is a signed-in person with role owner, or
  the local CLI (data-dir access). Agent tokens with an owner role are refused.
- **Channels.** `live` and `exp:<name>`. An experiment may change only
  `prompt`, `workflow`/`workflows`, `topic`, `model_role` and `skills`. Triggers
  always use live. Only accepted versions run.
- **What is enforced.** For the runner role: the launch message
  (`SPAWN_MESSAGE` + prompt + workflow steps), `harness = "t3"`, the topic (grant
  checks), `model_role` (resolved once at insert and pinned on the job),
  `runner.machine`/`project`/`runtime_mode` (grant checks), `card.todo`
  triggers, `limits.max_agents` and planner `connections`. Every other role,
  `skills` and `mcps` are advisory: rendered for sessions to read.
- **Boards without an accepted live `[runner]` config behave exactly as
  before.** No new columns change their digest, no sweep scans them, no lease
  exists for them. The tool list does change: the `orch_*` tools are listed on
  every workspace, so a config can be proposed before one exists (see
  Residuals).

### Starts on a configured board

- Every config-driven start runs one fenced transaction (`BEGIN IMMEDIATE` on
  `runner.sqlite3`): resolve and hash, refuse under the restore flag or for a
  `plan`-labelled card (`plan_pending`), look up the existing step (or, for a
  person, the runner's own request key), check the cap, insert. A same-step
  retry returns the stored job; any mismatch is a 409 with the stored step.
- `card_start` on a configured board is person-owner only; every other caller
  gets `no: use orch_card_start`. Cancelling a job that belongs to a step is
  person-owner only.
- The cap counts distinct cards: queued, leased, starting or uncertain jobs;
  jobs started within `STARTED_HOLD_SECONDS`; and `doing` cards with any
  connection assignee. The limit is `min(config.max_agents,
  project_limits.max_agents)` when both are set.
- A `plan`-labelled card never starts from config until the plan is approved;
  the plan-approval row fires the trigger once. The plan is also re-checked when
  the runner reports `starting`; a refused job ends `cancelled` with outcome
  `plan_pending`.

### Triggers

- Only boards with an accepted live `[runner]` config are swept. The sweep
  reads committed `task_events` after a per-board cursor; it fires on a move
  into `todo` from another status (add, move, decide) and on the plan-approval
  row.
- The cursor is armed in two steps because board events and the cursor live in
  different files: a configuring accept commits the cursor as NULL with a new
  generation, then arming sets it to `COALESCE(MAX(task_events.id), 0)` only if
  the generation is unchanged. A NULL cursor is never swept. Events before
  arming are skipped (never replayed).
- Every candidate is re-checked against the recorded `(cursor, gen)` as the
  first statement inside its fenced transaction; the job or the coded miss and
  the cursor advance commit together. Only infrastructure errors leave the
  cursor where it was.
- Before the fenced transaction the sweep's only `runner.sqlite3` write is the
  arming update of an unset cursor: it opens the orchestrator store without
  creating tables or converting a restore marker.
- The actor is rebuilt from the event stamps and must pass `requester_ok`;
  a member's move does not start work.

### Durable orchestrator

- One lease per `(board, role)`; the token is returned once and stored as
  `sha256(token + incarnation)`. Expiry only fences; it never moves cards.
- The journal records `intent → started → waiting → done | failed | in_doubt`.
  External effects (merge, gate, thread launch, card move) are never re-issued
  by the hub; a successor checks the truth source first, and an unproven effect
  becomes `needs_person`.
- One gate per `(board, repo, PR, head)` across roles unless the owner voids it.
- `orch_resume` returns a checkpoint (open steps, `in_doubt`, `needs_person`,
  holds, next, jobs, misses) plus the unstarted `todo` cards of each configured
  board: no active job and no job started strictly after the card's latest
  firing event (a start in the same second as the event does not count).

### Webhook dispatcher recovery

Each delivery pass contains unexpected errors from reconciliation, due-delivery
lookup and delivery. An error is logged without its private message and the
dispatcher waits on its stop event before continuing: 5 s initially, doubling
after consecutive errors to a 300 s cap. A successful pass resets the backoff;
stopping interrupts the wait. A closed-database `sqlite3.ProgrammingError` still
ends the dispatcher, and exit always removes its event listener. This keeps a
temporary failure from silently ending background delivery.

### Design before code

- A planner role must include `design_before_code`. On a configured board,
  approving a `plan` card runs `orch plan-check` on its body; only
  `task_approve` removes the `plan` label; a planner connection's card must pass
  plan-check before it reaches `todo` or `done`.
- Plan-check G3 requires each numeric line in (a) to lead with M, T, G,
  MEASURED, TARGET or GUESS after indentation and an optional Markdown list marker.
  G7 requires each mapped test file to exist within the current repository root
  at check time; `tests/test_orch_acceptance.py` is allowed. The root is the
  nearest enclosing Git checkout, or the current directory outside a checkout.
  Absolute paths and paths resolving outside the root are refused.
- `orch_card_start(board, role, card, step_key, epoch, token, config_ref?)` uses
  `role` to identify the lease and step lineage. Acceptance cases R1, R1b, R1d,
  R1e, R8b, R10a and R11e live in `tests/test_orch_acceptance.py`.
- `SYSTEM.md` and `MODULES.md` are kept current by `docs-check`, which runs in
  the test suite.

### Defaults chosen for slice 1

| Question | Default |
|---|---|
| Who approves a config | The owner only. |
| Who may hold an orchestrator lease | A principal that passes `requester_ok` for the configured machine (a listed trusted PAT or the person owner). |
| Lease TTL | 1800 s (G). |
| Wait budgets | Guesses until measured (see Numbers). |
| CLI acts as owner | Yes, through data-dir access. |
| Members' moves fire triggers | No. |
| Model slots | Two (`main`, `worker`). |

## Numbers

| Label | Number | Source |
|---|---|---|
| M | `max_agents` range 1–20, counting distinct connection assignees | `commonplace/projects/limits.py:26,34-57` |
| M | One worker PAT per machine | `commonplace/runner/registry.py:61` |
| M | Runner store writes use `BEGIN IMMEDIATE`; busy timeout 10 s | `commonplace/runner/jobs.py:68-95` |
| M | One active start per card | `commonplace/runner/jobs.py:47-50` |
| M | A non-person start within 24 h of the last start is refused | `commonplace/runner/jobs.py:236-243` |
| M | Started hold 3600 s (`STARTED_HOLD_SECONDS`) | `commonplace/runner/jobs.py:17,280-283` |
| M | T3 launch timeout 2–60 s; a timed-out launch is `uncertain` and never re-queued | `commonplace/runner/agent.py:127,333-336` |
| M | Runner hub timeout 15 s | `commonplace/runner/agent.py:247` |
| M | Machine queue limit 8 queued jobs | `commonplace/runner/jobs.py:16` |
| M | `request_key` at most 128 characters | `commonplace/runner/tools.py:92,157` |
| M | T3 launch message at most 120,000 characters | T3 MCP launch schema |
| T | A resume is one call; zero duplicate jobs after a lost acknowledgement | acceptance tests |
| T | Config at most 128 KiB, at most 16 roles, a prompt at most 32 KiB | `commonplace/orch/schema.py` |
| T | Per board at most 8 experiments and 20 open proposals, at most 5 per principal | `commonplace/orch/store.py` |
| T | `step_key` matches `[A-Za-z0-9._:-]{1,80}`, so `orch:<role>:<step_key>` fits 128 | `commonplace/orch/runner_bridge.py` |
| G | Lease TTL 1800 s; waits in slices of at most 10 min with heartbeats | rollout |
| G | Wait budgets: start 10 min, gate 90 min, merge 10 min, thread 4 h | rollout |
| G | The owner reviews at most 5 config changes a week | rollout |

## Stores

| Store | Scope | Holds | Backed up |
|---|---|---|---|
| `control.sqlite3` | hub | users, workspaces, memberships, connections, model settings, audit | yes |
| `boards.sqlite3` | workspace | boards, cards, `task_events` (append-only) | yes |
| `runner.sqlite3` | workspace | runner jobs; orchestrator configs, cursors, leases, steps, journal, restore flag | yes |
| palace and other workspace SQLite files | workspace | verbatim memory, indexes, threads | yes |

Backups copy every SQLite file in the data directory (`commonplace/backup.py`).
`restore()` publishes a verified copy into a new directory and leaves a restore
marker there.

## Restore runbook

1. Stop the service that uses the old data directory. Never run two services
   on copies of the same data.
2. Restore into a new directory (`commonplace restore BACKUP NEW_DIR`); the
   destination must not exist. Keep the old directory unchanged and read-only.
3. Start the service on the restored directory. On the first explicit open of
   the orchestrator store (a surface, the CLI, a lease call or a start; never
   the trigger sweep, which skips the workspace while a marker is pending), each
   workspace's restore marker becomes a durable flag in `runner.sqlite3` (new
   incarnation, all orchestrator leases dropped); the marker file is removed
   only after that commit.
4. While the flag is set, lease claims, every config-driven start and the
   trigger sweep are refused. Boards without a `[runner]` config are unaffected.
5. Check the truth sources for anything that may have happened after the
   backup time T (merged PRs, gate verdicts, launched threads, card states).
6. Acknowledge as the owner, typing the exact text:
   `commonplace orch restore-ack --text "effects after <T> may be unknown"`.
   This clears the flag and re-arms the trigger cursor of every configured
   board, so events from before the acknowledgement never fire.
7. Resume orchestrators with `orch_resume`; its checkpoint lists unstarted
   `todo` cards so nothing that was skipped is silently lost.

## Residuals

Accepted, known gaps. Each is a reason a check can pass while the system is
still wrong.

- **Same OS user.** Anyone who runs as the service's OS user can use the CLI
  (which is `orch_owner`) and read the data directory.
- **Restore outside `restore()`.** A hand-copied data directory or a deleted
  `runner.sqlite3` carries no restore marker: the journal resets and nothing is
  blocked.
- **Effects after T.** After a restore, effects made after the backup time are
  unknown to the journal; the truth checks in step 5 are the only guard.
- **Outside the journal.** External effects and gate runs started outside the
  journal are invisible to resume.
- **Revocation window on trigger starts.** The trigger's requester and grants
  are read in a control snapshot before the fenced transaction. A grant or
  requester revocation that commits between that snapshot and the in-lock
  insert is not re-read; the start can still be inserted. The runner re-checks
  the requester and grants at `starting`, so the job is refused before launch.
- **Same-second ties are listed.** Job starts and board events are stamped
  in whole seconds, so a start in the same second as a card's latest firing
  event cannot be ordered against it. Resume lists such a card as unstarted;
  it may be a false positive.
- **Skipped events.** Board events committed between a configuring accept (or
  a restore acknowledgement) and arming are skipped, not replayed. Resume lists
  the affected `todo` cards as unstarted.
- **Refused triggers are not retried.** A `todo` card whose trigger start is
  refused (`busy`, `at_cap`, `started_recently` or any other code) is a final
  miss: the cursor moves past it and nothing re-fires it, even after the queue
  drains or a slot frees up. It shows in the checkpoint's unstarted list and
  as a miss; move the card out of `todo` and back to retry.
- **Orchestrator tools on every workspace.** The `orch_*` tools appear in the
  tool list on every workspace, also where no board has a config (runner
  children never see them; owner-only tools are listed for owners only).
  Without a config only proposing and reading do anything.
- **Docs-check checks change, not accuracy.** It refuses a touched module
  whose block did not change and a change without a `SYSTEM.md` edit
  (whitespace-only edits do not count); it never judges whether the new text
  is right.
- **Cap overshoot.** The cap reads card state from `boards.sqlite3` as a
  snapshot, so a concurrent claim outside the runner can overshoot it. The 1 h
  started hold is conservative.
- **D1 shared worker PAT.** All runner children use the machine's worker PAT,
  so per-child provenance and per-child limits are not available.
- **D2 launch shape.** Whether T3 accepts `modelSelection` at runtime is
  unverified; when it is given, effort comes from T3's defaults.
- **D3 thread-list keys.** An empty thread list is no proof that a launch did
  not happen; such steps go to `needs_person`.
- **Plaintext job lease tokens.** The runner's job lease token is stored in
  `runner_jobs` and in the machine journal (pre-existing).
- **Waiver list.** Approval of the waiver list below is a manual owner review.

## Open dependencies (D1–D3)

These are owner decisions or captures tracked in `docs/runner.md` ("Owner
checklists"). Until each is closed, the residual above stands.

- **D1 worker identity.** "Attested, not verified" or a dedicated provider
  instance per worker; stronger identity needs a per-thread credential.
- **D2 launch shape.** One real launch capture settles the result fields, the
  workspace strategy and `modelSelection`.
- **D3 recovery.** The thread-list keys must be captured before any automatic
  reconcile; until then `uncertain` and unproven launches go to a person.

## Keeping this file current

`commonplace orch docs-check --base <ref> --head <ref>` exits non-zero on a
changed path no `MODULES.md` block owns, a touched module whose block did not
change, no change to this file (a whitespace-only edit is no change), an
empty or missing block, a bad ref or any error. The test suite runs it
against the merge base with the development branch and fails closed when the
base is missing. The only exception is a
commit trailer `Docs-waiver: W<n>` whose id is listed below **in the base
commit**; an id added in the same change does not count.

## Waivers

Owner-approved waiver ids, one per line as `W<n>: <scope>`. None are approved
yet.
