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

- **Workspace Admin foundation:** `/app/#/admin` is discoverable only when
  `/api/me.can_admin` is true (signed-in person and current-workspace owner).
  The additive `can_admin_instance` uses the operational predicate below. New GET-only users/connections/runtime/limits/stuck
  inventories bind the decoded path ID to the authenticated workspace before any
  projection source read. Members and owner connections receive the exact
  person-owner refusal; foreign workspace IDs are concealed with 404. Personal
  data remains workspace-local; no global directory or mutation action is added.
  Operational deploy and backup GETs require a signed-in person + current-workspace
  owner + exact server-fetched `users.is_admin=1`, read per request through the
  bounded RO connection, including `/api/me`. Authenticated ordinary failures use
  exactly “Instance administrator access required”; missing users deny and read
  faults on instance data routes fail closed with 503 before source reads.
  A capability-read fault in `/api/me` preserves the 200 bootstrap response with
  `can_admin_instance:false`; data routes always recheck authority. Signed-out keeps
  the existing gateway 401 challenge. No authority extends to other workspaces’ personal data.
  The installed route inventory mirrors both authenticated Admin GET prefixes.

- **Admin read and lifecycle bounds:** New projections use an existing SQLite
  `mode=ro` connection, connection-local `query_only`, at most 250 ms busy/connect
  wait and a 2 second monotonic request budget with progress interruption. They
  acquire no application locks, initialize no stores and invoke no memory worker.
  They capture UTC once through `gateway.admin_clock.utc_now`; the paired
  `monotonic` seam controls deadlines. Credential expiry authentication retains
  its existing clock. Explicit field allowlists omit credentials, raw policy and
  login links; display redaction does not modify stored content. Lists page in batches of 100 with full filtered counts, using creation time/ID,
  project slug/ID or card number as appropriate. Each UI load,
  including initial identity/body decoding, settles within its 5,000 ms client
  deadline independently of other sections and the shared refresh fan-in. Abort,
  page epochs and request generations clear private state on disposal or expiry.
  Only the exact Admin route bypasses shared refresh; card navigation restores a
  regular base view and loads the requested card without new inventory requests.
  Inventory counts share the row membership/user joins; Retry preserves the failed
  cursor and append or replacement mode. Sign-out and expiry stop an existing Admin
  controller; it is null until mounted.


### Inert area configuration preview

Area declarations can be parsed, rendered, diffed and proposed, with up to eight
distinct sub-orchestrators directly under the single parentless orchestrator.
Area, parent and kind stay frozen in experiments. Store acceptance and direct
write reparse then refuse any area-bearing config with `area_not_supported`
(400, path `roles`) before effects, for live and experiment channels alike.
Refused accepts preserve the proposal; refused writes roll back their insert.
Accepted versions, cursors/generations, leases, jobs and events stay unchanged.
This preview creates no area seats, credentials, subscriptions or runtime, adds
no activation switch, and keeps existing owner/local CLI and no-area behavior.

### Legacy client memory safety stop

Legacy sync refuses memory installs and old/unmarked configurations. Transcript-only
reinstall binds versioned local consent to the exact explicit destination; no hosted
default or automatic migration remains. CLI checks precede tokens/state; daemon checks
precede all state and transport. Import-kind OR native-memory-source operations are
inactive at every scan, read, retry, rearm and dispatch boundary. Existing rows, source
statuses, seen history and SQLite schema are preserved. Status reports retained rows
as disabled with coverage unverified, excluding them from pending/parked counts.
Caught-up transcript cursors end continuous backfill despite retained memory rows;
no-stream and capture-off semantics remain intact, and status inspection is read-only.

Consent binds the endpoint only. A changed workspace token at the same endpoint is
still accepted; principal/rotation binding belongs to a later slice. Import credentials
are not feed credentials: feed selection accepts only Claude or Codex capture tokens.
This safety refinement was selected from the plan review; missing selected-stream
tokens still fail during initialization even when another capture token serves the feed.
Stop old processes before update and explicitly reinstall
transcript-only or uninstall: unchanged service restart policies retry old rejected
configs. No deployment, replacement importer or memory-coverage completion is claimed.

### Runner-start void semantics and limits

- A same-step retry of an unvoided step returns the stored job; any mismatch is a 409 with the stored step.

- A voided runner-start key instead returns `step_voided` (409), even for a changed request; other kinds retain `step_conflict`.

- Cancelling a job that belongs to a step is person-owner only through the standalone cancel route.

- Owner/local void also cancels linked queued or leased jobs atomically.

- Voided steps keep counting their jobs; refusal is `at_cap` with workspace orchestration off and `project_limit` with it on.

- Owner/local void permanently retires a runner-start key while preserving its request, link, state, evidence and history.

- Only queued/leased jobs are cancelled, in the void transaction; starting/uncertain/started and finished jobs are untouched.

- A new key goes through ordinary admission, including non-person rules for owner orchestrator calls.

- Repeat void preserves the first actor/time and writes nothing; one journal event may produce repeated successful-call feed notifications.

- Checkpoint jobs retain linked statuses.

- Void cannot stop or prove completion of a launched thread.

- Active jobs block concurrent starts, but the 24-hour non-person rule can later allow a new job on a todo, unassigned card while an earlier thread still runs.

- Caps count cards, not threads: started jobs age out after one hour unless a doing card with a connection assignee still counts; opt-in orchestration retains the todo/doing recent-start hold and ordinary connection slots.

- Uncertainty needs separate owner truth checks and resolution; empty thread search proves nothing.

- Void does not launch or replay triggers, but a pending unswept todo event can start the card on a later sweep after cancellation frees it.

- Trigger keys (`trig:<event>`) and owner `card_start` are independent of step keys; the latter also bypasses the non-person 24-hour rule.

- These limits predate void semantics.

### Rules initialization deadline

- Owner summary-edit checks use a fixed injection clock and a forced-expiry regression, retaining verbatim injection and acceptance assertions without changing the production deadline.

- Rules-injection functional tests run on a frozen deadline clock and a slow-host regression re-runs them with a clock that exhausts the budget on every read; the production 80 ms budget and its fail-open empty text are unchanged.
- Shared-proposal acceptance is checked with a fixed injection clock; scheduler-delay regression coverage retains exact rule text and spacing for both agents without changing the production fail-open deadline.

- Rules initialization deliberately returns the legacy response when its deadline expires, even after rendering; the 500-rule incomplete marker is checked with a fixed clock, separately from the unchanged informational 25 ms added p95 budget.

### Operator-blind token slots: primitives with production locked

- B3 is opt-in via the proposed `encryption.key_mode=token-v2` marker. This slice rejects that marker (and unknown key modes) with an encryption error before operator-held B2 keys or content are opened, including backup snapshots. Existing workspaces without the marker retain their existing behavior; no workspace is automatically migrated or modified.

- The isolated token-slot primitive uses HKDF-SHA256 over raw random token bytes and a fresh random salt, then AES-256-GCM with a fresh nonce to wrap a caller's 256-bit workspace DEK. Its authenticated context binds format version, workspace, epoch, authority, token/grant kind, token digest and slot ID. Strict records reject unknown fields, mixed versions and server/recovery wraps. No raw token, DEK or operator wrapping key is persisted by this module. User-only recovery will use a separate envelope in a later slice. Synthetic tests exercise two tokens and two workspaces and exact payload bytes.

- This primitive does not authenticate current control state or establish runtime authority. A caller must authenticate current workspace/epoch/grant state before unwrapping; digest-only metadata cannot unlock a slot. A scoped bearer exposes the whole workspace DEK, so wing scope remains API policy. Active code/TLS/RAM control can capture secrets; sizes, timing and routing remain visible. Slot-only restore must not restore revoked authority; full control-state rollback, deletion and client compromise are outside this threat model.

- Production enrollment, OAuth schema, capabilities, worker leases, job/cache closure and refresh serialization belong to the runtime slice. Fresh-DEK cutover, user-only recovery, exact-data comparison, crash/restore proofs and activation belong to the final slice. Production remains locked pending those slices, complete encrypted-content inventory acceptance and owner decisions. No cutover, pointer publication, archive deletion or B2 wrap reuse is implemented.

- Raw-key `cipher_connect` is a low-level SQLCipher utility used by B2 migration, not a B3 authority boundary. Future B3 callers must use `connect` or a dedicated guarded entry point that authenticates current control state before content opens.


### Chroma session rebuilds

- If a session's cached palace database disappears, invalidate that palace's shared Chroma System before constructing another client.

- Closing only the session owner leaves a System retained by dropped clients; its SQLite pool still targets the renamed database and may fail when it opens a connection.

- The reset uses the existing palace-scoped lifecycle path.

- The collection configuration repair remains unchanged and still adds missing `_type` markers.

### Email session cookie domain migration

- When a shared session cookie domain is configured, successful email verification first expires the same-named host-only cookie at Path=/, then sets the new domain cookie.

- The redirect destination and session security attributes stay the same.

- This prevents an older host-only session from taking precedence in the browser's Cookie header.

- Invalid or used email links still return a visible error without changing cookies; requesting a link returns the same confirmation for every account.

### Release email login readiness

- The disposable release journey considers email login ready only after authenticated identity checks and the signed-in Board region appears; step failure reports retain exception messages with shared credential redaction.

### Restricted-origin release

- Cards created by subagents or restricted agents start inactive in backlog, including create-and-claim requests.

- Activation and claiming require release by the board's live coordinator, or by the signed-in workspace owner when no coordinator is configured or live.

- `task_approve` supports both pending proposals and unreleased restricted submissions, with an audit entry and an `approved_by` release marker.

- Nonproposal releases leave `proposal_status` unset.

- Proposal approval by a parent alone does not activate the card without that release.

- An inactive card's holder may update only its status line without release; this does not activate, claim or approve the card.

- Rejected-proposal handoffs retain the proposal-approval guard before origin authorization.

### Owner manual assignment starts

- **Manual assignments.** An owner assignment to an active agent connection claims the card immediately, including project admission and the busy-agent guard. With orchestration enabled, an assignment to the configured machine's worker requests a fresh start through the owner orchestrator path. A disabled switch, missing runner config, a different worker or a refused start leaves an explicit reason in the card's status line. Assignment never bypasses origin release. The worker can confirm its existing claim with an explicit take without a second claim event or lease renewal.

### Privacy scanner timing checks

- Large-input and delimiter-heavy near-miss correctness checks run in the behaviour suite without measuring time.

- Each input also retains a separate `timing`-marked check of the 20 ms average scan budget over 20 iterations.

- The duration assertion is outside loops so a slow host produces an informational timing failure while incorrect flags remain blocking.

### Rules initialization timing checks

- Rules initialization tests check remaining SQLite wait budgets with a fake clock; wall-clock budgets stay in separate timing-marked tests.

### Orchestrator no-progress wake

- The hub measures progress, not liveness: a runner-configured board with actionable open work and no card done for N minutes (default 20, per board, 0 = off) emits one `board.stalled` wake through the existing event and webhook path, with backoff and owner escalation after three unanswered nudges. Boards whose open cards all wait on a blocker or a person are never nudged.

### Orchestrator trigger sweep never wedges

- A trigger start that cannot be planned is a coded miss that advances the board's cursor; only infrastructure errors leave the cursor. A split start falls back to the `[runner]` role when the config has no `main` role, and an origin release fires the trigger.

- Repeated sweep failures are visible: the second consecutive failure of a board alerts the owner once (`orch.trigger.failing`, `needs_me.orchestrator_failing`) until a sweep succeeds.

### Mirror and search-index test waits

- Tests that wait for background mirror or search-index work drain on the actual completion condition (no sweep active, no mirror job pending) through one shared test helper, with a 120 s stuck-job limit, rather than one fixed `wait_for_mirrors` budget that returns silently when it runs out.

- A slow-host regression re-runs the converted selections with late-starting jobs and a wait that returns at once; the production sweep, its 20-thread and unit bounds and `wait_for_mirrors` are unchanged.

### Console activity query checks

- Console activity schema and query checks freeze the store clock, keeping both dated rows within the seven-day retention window while asserting ordering, latest timestamps and redaction.

### Agent questions

Agent `ask` calls and `task_update status=review` questions target the board's
appointed coordinator by default when that connection is live, unrestricted,
can read and write the board wing, and can use `space_read`, `space_post`,
`needs_me` and `decide`. They appear in the coordinator's queue instead of the
owner's. Supply a nonempty `person_reason` (at most 500 characters) to explain
why a person is required; the reason is preserved in context and opening history.
Without an authorized coordinator the question reaches the owner. Completion
reviews, person callers and explicit person handoffs keep their owner/person route.
A status line never hides a person decision or an item needing person release.

Only the addressed, still-authorized coordinator or a signed-in workspace owner
can answer; agents never answer their own asks. The coordinator may instead use
`decide(then="escalate", reply=<reason>)` to send the unchanged question, options,
context and return recipient to the owner; an empty reason is refused. An appointed
coordinator may withdraw only an agent-directed decision it requested itself,
using `task_update status=todo|doing` with optional `reason`. Current appointment,
recipient and requester are rechecked. Other agents, member people and coordinators
of other boards have no withdrawal authority. Withdrawal keeps the original ask
in append-only history and does not imply approval. Person decisions cannot be
withdrawn by agents. Existing asks are never rewritten by this routing change.

Subagents still need an existing approved and released card before asking. If a
routed coordinator becomes unavailable or is replaced, the pending ask appears
in the owner's queue without changing its stored words or audit log.

The shipped agent skill teaches the same routing and withdrawal boundaries. The
pinned MCP contract adds only optional `person_reason` to `ask` and `task_update`,
with matching descriptions for `ask`, `task_update` and `decide`; tool names and
visibility for slim, standard and full profiles remain unchanged.


### Project seat wake recommendations

- Connect recommends a board-scoped webhook for `space_message` and `card.moved`, so messages and worker completion moves can wake the orchestrator.

- Setup is advisory: Connect and reconnect neither register nor modify subscriptions.

- Existing users must explicitly update their webhooks to receive card moves.

- Message coordinator filtering and reply-loop suppression still apply.

- With orchestration enabled, the list appends `card.created` and `project.report`.

### Restricted-origin card content

- Trusted agents and authorized board coordinators may update only the title and body of an unassigned todo card marked as restricted-origin.

- Board write access and proposal approval remain required.

- Each edit records its actor in the existing audit log; it neither releases the origin nor changes claiming, activation, reservation or other update permissions.

### Token-slot refusal verification

- Marked-workspace connection, snapshot and database classification regressions use real synthetic B2 keyslots and exact-data controls before setting the marker. Direct provider tests likewise prove valid unmarked wraps and workspace-key reads. Malformed direct metadata raises an encryption error, and marked-workspace errors retain the locked diagnostic rather than implying settings corruption.


### Browser GitHub connection (experimental, default off)

- One GitHub worker owns a control-database lease shared by every serve on the same volume, including containers with private temporary directories or different mount paths. Claim and recovery occur in one short write transaction; no lifetime lock file is held in the data tree. The lease lasts 30 seconds and renews every 5 seconds. A second serve fails closed for GitHub writes and retries acquisition every 5 seconds after the owner closes or its lease expires. Restored backups may retain the lease until expiry. The Node lifecycle harness exercises connected rendering, stale reads, card switches and logout without a browser installation.

- Browser consent requires a person session, JSON, exact Origin and session-derived CSRF; agents and MCP receive no GitHub capability. Scope is exactly empty; inherited sign-in scopes require confirmation, every repeated scope header is cross-checked, and refresh material is discarded before encryption.

- Expired grants purge locally, increment generation and permit a new Connect without acknowledgement or a GitHub request. The expired view links to GitHub app access. Only ambiguous revocation requires acknowledgement.

- The ten-minute flow binds PKCE, browser, session, workspace, visible card and generation. Callbacks reopen the selected Needs item. Login and grants share subject ownership without provisioning a new login identity during connect.

- One person owns at most one grant. Another workspace reports the existing workspace explicitly and offers Disconnect; it does not expose Open PR or reuse that grant for work. The same person's browser consent may revoke the grant from either workspace.

- `COMMONPLACE_GITHUB_CONNECT` defaults off and prevents new start/callback/confirm work. Existing grants stay visible and browser-authenticated DELETE stays available while disabled; missing app credentials make revocation unconfirmed and purge locally.

- Confirmation expiry cleanup runs on the person's next Connect, Confirm or Disconnect write. GET status remains local-only and never revokes remotely. Failed cleanup marks revocation unconfirmed and settles exchange counters in finally blocks.

- Serve closes its control database on normal return and signal unwind, stopping registered workers and releasing only its own GitHub lease in a short transaction. HTTP server close also stops email admission before GitHub joins its heartbeat and releases its lease; database callbacks use the same order so GitHub cannot delay the email stop signal while waiting for SQLite. Repeated GitHub closes are serialized and perform no SQL after successful cleanup, including after SQLite closes. Never-owners retry claim and recovery; an expired former owner stays fenced because remote work may still be in flight.

- Generation, remote-work and exchange counters fence disconnect, confirmation and late callbacks until all cleanup settles. Lease ownership and expiry are checked in each state-changing transaction, including late remote completions. An expired owner cannot renew itself or alter a successor’s counters. A new owner recovers after lease expiry without replaying remote DELETE; orderly close releases only its own lease. No writable temporary directory or platform-specific file locking is required.

- Disconnect purges local grants and confirmations even if stored ciphertext is tampered or the wrapping key is unavailable. It retains the reconnect acknowledgement requirement for unconfirmed remote revocation and never retries unavailable credentials.

- AES-256-GCM encrypts tokens/verifiers before SQLite/WAL using per-user DEKs and user/provider/schema AAD. `COMMONPLACE_GITHUB_KEY_FILE` is a private, owner-held 32-byte key; code rejects it within the control database's parent directory. Operators must also exclude it from separately located workspace and backup trees, which this store does not inventory. Install the `commonplace` extra for cryptography.

- Back up the wrapping key separately. Rewrap DEKs offline with unchanged AAD/version while workers are stopped; replacing only the key file is unsupported. Retain the matching old key/database backup until a migrated copy is verified. Online rotation and migration tooling remain deferred.

- Needs disables loading controls, contains GitHub-only errors without hiding card actions, drops stale reads and clears rendered connection details on teardown. Ordinary decision details retain their existing teardown contract. PR links require HTTPS, exact github.com, a default port and a positive pull-request number; reports stay unverified and connection proves no merge rights.

- Full account-deletion integration, composite multi-worker HTTP acceptance and a live browser workspace-switch flow remain deferred. The GitHub deletion helper fences its own cleanup only, not other stores or the user record.

- Absent token expiry requires dated `COMMONPLACE_GITHUB_NONEXPIRING_EVIDENCE` in `YYYY-MM-DD|OAuth App|client-id|expiration disabled` form. Before enabling, record fresh-account/prior-login scope, header and expiry outcomes plus ingress/callback/backend no-secret logging canaries. Live GitHub OAuth behavior remains unproven. Merge, repository consent, refresh and reconciliation remain deferred.

### Optional agent orchestration

- **Default off.**

  - The workspace owner explicitly enables orchestration; absent settings mean off.

  - A settings read error refuses admission.

  - Off retains the existing claim/start behavior and board JSON shape.

- **One project count.**

  - Claims, queued starts, leases and configured starts share a runner transaction.

  - Ordinary agents count by connection, workers by card; card/job overlaps count once.

  - Default cap is 3; project rows override it, configured boards use the smaller config/row cap.

  - Board `used` shows active slots, excluding the queued reservations counted at start creation.

- **Authority.**

  - Unconfigured starts require the active board coordinator PAT or a signed-in owner.

  - Owners separately trust seats and grant machines atomically; switching off demotes all seats and removes only their requester ids.

  - Trusted seats cannot create boards.

  - Connect alone never grants this trust.

- **Lock order.**

  - Control snapshots precede waits.

  - Claims hold boards RESERVED then runner admission until board commit; runner admissions read boards SHARED.

- **Residual.**

  - Without a runner or subscribed `card.created` webhook, a new card waits for the orchestrator's next session.

  - An always-running orchestrator is separate work.

  - Splitting and inbox behavior are not introduced by this switch.

### Optional project completion and stuck reports

Owner-enabled orchestration adds a durable `project.report` route for transitions
from a non-done state to done and for observed stuck episodes. It is off by
default and carries only board, card number, report id, reason, generation and
captured coordinator. No card words, exception contents or credentials are sent.
Reports grant no start, split, claim or other authority.

Connect the project seat again after enabling, then remove and re-add its webhook
using Connect's returned event list
(`["space_message", "card.moved", "card.created", "project.report"]`).
Enabling alone never edits subscriptions. Setup gaps do not replay old reports.
Raw subscriptions retain their behavior: a legacy completion hook still gets its
raw wake; subscribing to both channels can deliver raw plus report. Cross-channel
deduplication is deferred.

Done capture shares the source transaction. An unavailable settings/coordinator
snapshot rejects the transition with `settings_unavailable` (503), before a lesson
is saved. Ordinary edits remain available. A board created by the same transaction
has no coordinator and stays history only when the snapshot is available; an
unavailable snapshot still rejects a relevant done transition. This is fail-closed,
not a guarantee to undo arbitrary external memory side effects after a lesson.
Existing raw event emission can still fail after the board commit.

Report installation registers recovery without resolving workspace directories.
Recovery installs the report reset trigger only when all watched task columns
exist and removes an existing incomplete trigger otherwise. For sources missing
only `heartbeat_at`, this keeps raw status writes usable until BoardStore adds
that column and installs the trigger. The other watched columns belong to the
base schema; BoardStore does not add them during this migration. Until migration,
no episodes are recorded, while reporting is enabled, a pass that reaches a coordinated task logs an error, and watched writes do
not bump `report_reset_rev`. Product writers open BoardStore before writing.
The first dispatcher scan recovers and migrates existing board databases without creating
workspace directories, followed by monotonic 30-second scan deadlines independent
of requests and webhook traffic. Source initialization is cached by database path,
inode and SQLite schema version. Zero-byte files are deferred without opening a
connection; a read-only probe defers nonempty files without both base tables
(`boards` and `tasks`). Neither case opens a write transaction or runs report
migrations; a later pass picks up the source after its creator initializes it.
Ready sources have their schema version read through the same read-only probe.
Ordinary data writes preserve the cache; a new inode or a changed schema version
triggers initialization again, including repair of dropped report tables.
Failures are isolated per workspace and logged with exception type and stack
locations, excluding exception text and local values. Each workspace pass reads
at most 100 cards, publishes at most 100 source rows separately and cleans at most
100 deleted episode keys. Persisted pagination provides fair continuation; a cycle can
take longer than 30 seconds. The first observation in a generation baselines
existing staleness without sending it. Heartbeat and leave/reclaim revisions
separate later observed episodes, even at equal timestamps across restart.
Episodes beginning and ending entirely between observations are lost. There is
no historical-done replay. A workspace failure leaves other workspaces progressing.

Source access closes before control publication. Event and receipt commit together;
source acknowledgement happens later. Receipts are retained without age pruning
because a publisher may still retry an old source row. Report reconcile saves an
immutable per-event routing envelope atomically with cursor advancement. Unknown
routing facts defer only that report with persisted 1/2/4/8/16/30-second capped
backoff, while later raw events and due HTTP deliveries keep progressing. Enqueue
and retry-work deletion are atomic. Inability to persist control work, cursor or
retry metadata uses the existing infrastructure-error path and can delay dispatch.

Send rechecks the captured coordinator, current connection/wing authority, board
and activation/binding generations across separate control/source/control phases;
HTTP holds no database lock. Positive invalidation cancels only reports. Busy,
locked, wrong-key and unknown failures retry indefinitely without using HTTP
attempts or producing dead-letter failures. Existing delivery inspection shows
`error=report_authorization_retry`, `count` and stable `retry_since`; ALLOW and
cancellation clear them. All unresolved auth rows remain visible beyond the latest
100 ordinary deliveries. This unresolved set is unbounded by design; inspection
reads all rows for that hook. Operators should repair authority/storage or cancel
the affected hook. Exception contents never enter inspection or payloads.

A receiver deduplicates `report_id`, fetches the current card and version, and
reports progress through existing authorized tools. For done, inspect the current
result and continue only work already authorized. For stuck, inspect the current
holder and person-wait state before an authorized follow-up. HTTP is at least
once: duplicate requests must not repeat the receiver's action. Broader receiver
playbooks and the original wider orchestration acceptance remain separate work.


### Orchestrator config as code

- **The store is the source of truth.**

  - Each board has versioned orchestrator configs (`v<n>`, with parent version, channel, source ref and sha256) in the workspace's `runner.sqlite3`.

  - `.commonplace/orchestrator.toml` in a repo is only a proposal; `orch submit` proposes it and the owner approves its sha256.

- **The owner approves every change.**

  - Wing writers propose; only `orch_owner` accepts, rejects, retires an experiment, forces a lease, voids a step or acknowledges a restore.

  - `orch_owner` is a signed-in person with role owner, or the local CLI (data-dir access).

  - Agent tokens with an owner role are refused.

- **Channels.**

  - `live` and `exp:<name>`.

  - An experiment may change only `prompt`, `workflow`/`workflows`, `topic`, `model_role` and `skills`.

  - Triggers always use live.

  - Only accepted versions run.

- **What is enforced.**

  - For the runner role: the launch message (`SPAWN_MESSAGE` + prompt + workflow steps), `harness = "t3"`, the topic (grant checks), `model_role` (resolved once at insert and pinned on the job), `runner.machine`/`project`/`runtime_mode` (grant checks), `card.todo` triggers, `limits.max_agents` and planner `connections`.

  - Every other role, `skills` and `mcps` are advisory: rendered for sessions to read.

- **Boards without an accepted live `[runner]` config behave exactly as before.**

  - No new columns change their digest, no sweep scans them, no lease exists for them.

  - The tool list does change: the `orch_*` tools are listed on every workspace, so a config can be proposed before one exists (see Residuals).

### Foreground refresh rendering

- A background workspace refresh that supersedes a pending foreground render inherits that render on the same screen.

- Navigation epochs reject stale reads and Settings completions; ordinary events leave an already rendered Settings view intact.

- Settings bounds each of its six section reads and three prerequisite reads to ten seconds, including response decoding. Failures name the section or request and offer Retry; successful reads show saved values. Orchestration settings lock contention returns retryable HTTP 503 after a one-second lock wait.

- A current request failure clears the pending render; a later background refresh retains its usual form-preservation policy.

- On Settings, a background prerequisite failure preserves already-rendered sections and mounted webhook drafts, reporting the named failure through the separate screen error with Retry. Initial and foreground failures may replace content with the error.

### Starts on a configured board

- Every config-driven start runs one fenced transaction (`BEGIN IMMEDIATE` on `runner.sqlite3`): resolve and hash, refuse under the restore flag or for a `plan`-labelled card (`plan_pending`), look up the existing step (or, for a person, the runner's own request key), check the cap, insert.

- A same-step retry returns the stored job; any mismatch is a 409 with the stored step.

- `card_start` on a configured board is person-owner only; every other caller gets `no: use orch_card_start`.

- Cancelling a job that belongs to a step is person-owner only.

- The cap counts distinct cards: queued, leased, starting or uncertain jobs; jobs started within `STARTED_HOLD_SECONDS`; and `doing` cards with any connection assignee.

- The limit is `min(config.max_agents, project_limits.max_agents)` when both are set.

- A `plan`-labelled card never starts from config until the plan is approved; the plan-approval row fires the trigger once.

- The plan is also re-checked when the runner reports `starting`; a refused job ends `cancelled` with outcome `plan_pending`.

### Explicit imported-history reader consent

  - Imported view proofs share the response access checker with normalized citation and private-source authorization; thread-bound mutation and source-delete checks remain enforced.
  - Ordinary recall checks imported identity fields only for imported threads; the release journey verifies owner read-back and the write-only bearer’s metadata-free not-found response.
  - Legacy import compatibility: Existing three-part import identities retain ordinary filer-bound read access without data migration; consented reader views require complete four-part identities.

- `memory:import` stays write-only.

- New PAT and OAuth readers need a separate, unticked consent and `memory:read` plus `memory:import:read`.

- No defaults, person policies, migration, parent relationship, wing PATCH, refresh or CLI issuer adds this scope.

- PAT trust remains untrusted; OAuth trust remains standard.

- Exact filing account, workspace, private-import identity and current personal ownership authorize the read-only view.

- Import views bypass read-wing grants; explicit filters still select results.

- Full-ACL imports, other accounts, captures and unrelated private data remain outside the exception.

- REST and Memory reads share Recall authorization; MCP thread tools retain threads:read.

- A holder's complete archive ranges must fit within the import offset.

- Forks contribute only the bounded imported suffix.

- Unknown or inconsistent bounds fail closed, without read-time adoption.

- Mirror words require stored source, complete range and reconstructed unit proof before previews or CLI rendering; room authority and caller labels are insufficient.

- Indexed candidates may fall back to exact-view matches.

- Startup preserves the live eligibility callback and its deadline without opening new stores or scanning history.

- Revoking the connection stops all credentials; rotation preserves consent.

- Refresh narrowing affects descendants, while earlier access tokens and the connection policy retain their original authority.

- Revoke and reconnect with the reader box unticked to withdraw all authority.

- Stored imports and text already returned remain.

- No authority cache survives a response.


### Triggers

- Only boards with an accepted live `[runner]` config are swept.

- The sweep reads committed `task_events` after a per-board cursor; it fires on a move into `todo` from another status (add, move, decide) and on the plan-approval row.

- The cursor is armed in two steps because board events and the cursor live in different files: a configuring accept commits the cursor as NULL with a new generation, then arming sets it to `COALESCE(MAX(task_events.id), 0)` only if the generation is unchanged.

- A NULL cursor is never swept.

- Events before arming are skipped (never replayed).

- Every candidate is re-checked against the recorded `(cursor, gen)` as the first statement inside its fenced transaction; the job or the coded miss and the cursor advance commit together.

- Only infrastructure errors leave the cursor where it was.

- Before the fenced transaction the sweep's only `runner.sqlite3` write is the arming update of an unset cursor: it opens the orchestrator store without creating tables or converting a restore marker.

- The actor is rebuilt from the event stamps and must pass `requester_ok`; a member's move does not start work.

### Durable orchestrator

- Card tool discovery defaults to the off schema when no control database is available or a settings read fails.

- One lease per `(board, role)`; the token is returned once and stored as `sha256(token + incarnation)`.

- Expiry only fences; it never moves cards.

- The journal records `intent → started → waiting → done | failed | in_doubt`.

- External effects (merge, gate, thread launch, card move) are never re-issued by the hub; a successor checks the truth source first, and an unproven effect becomes `needs_person`.

- One gate per `(board, repo, PR, head)` across roles unless the owner voids it.

- `orch_resume` returns a checkpoint (open steps, `in_doubt`, `needs_person`, holds, next, jobs, misses) plus the unstarted `todo` cards of each configured board: no active job and no job started strictly after the card's latest firing event (a start in the same second as the event does not count).

### Thread-search ranking

- For a verified thread index, search finds the scoped BM25 cutoff using the FTS rank iterator, then includes every hit at or above that cutoff before ordering by BM25, thread ID and chunk index.

- The top rowids are materialized before fetching canonical chunk fields.

- Each ID batch keeps the same limit and the final merge uses the same ordering.

- Snapshot verification, external-change repair and the read-only logical query remain unchanged; a selected missing or mismatched canonical identity reruns the complete logical query.

- The synthetic thread-search benchmark reports p50/p95 and exports full result lists for revision comparisons.

- Timings are informational, never a gate.

### Owner-configured task types on cards (slice 1)

- Owner assignments that claim todo cards, including handoffs, validate the target connection’s type grant before committing a claim or requesting its configured start. Existing holders can repeat an explicit take after grant removal without renewing the lease. Automatic selection applies both task-type eligibility and enabled blocker filtering.

- Cards default to an empty task type and retain existing selection, authority, reservations, capacity and text rendering; JSON includes `task_type: ''`. Only a signed-in workspace owner may supply, replace or clear the field; omission preserves it, null is invalid. Nonempty types are single lowercase slugs of at most 48 characters.
- Each workspace’s boards store keeps exact stable connection IDs and their allowed labels, with at most 128 distinct labels per connection. GET/POST `/api/task-types/connections/<id>` require a person owner and an exact live workspace connection ID. POST accepts only `{allowed_types: [...]}`, atomically replaces the sorted unique list, and uses last-commit-wins semantics. A stale client can re-grant a removed label; there is no concurrency token. GET does not create a database. Non-object POST JSON is rejected with 400. Owner status changes to doing preserve a typed card’s assigned holder and check that holder’s grant; only person self-acquisition is exempt.
- New typed doing entries require the final holding connection’s exact grant, with orchestration on or off; automatic take skips ineligible cards in the existing order. People acquiring for themselves bypass only type eligibility. Person-assigned connection work receives no person exemption. Missing mappings, malformed types and unreadable mappings refuse. Runner queue, configured owner starts and starting validation use the worker’s grant; starting retains `card_not_startable` translation.
- Claim, retype and grant replacement serialize in the board transaction. Explicit denial rolls back expiry marks, versions and events; an automatic no-result commits ordinary expiry. Connection resolution precedes board transactions; control locks are never held while waiting for a board store. Runner admission is advisory; authoritative claiming can refuse after a concurrent removal. There is no cross-database atomicity or snapshot guarantee.
- Existing doing holders retain reporting, renewal, edits, finishing, escalation and release after label removal or retyping, preserving assignee and expiry. Revocation still denies authentication. Same-ID token rotation preserves grants; new IDs inherit none. Todo reservation is not acquisition; owner recovery remains todo → backlog → todo.
- Interim choices pending owner: untyped cards behave as today, member people may acquire for themselves, labels are free slugs, missing eligibility adds no notification, and stale full replacements may re-grant labels. Deferred groups remain catalog/multiple types/agent attachments; mapping versions/audit/grant API; runner fences/replay/terminalization; handoff/ask/approval routing and recovery/notifications; platform grants/browser settings; send/pay confirmation and secret protection. The full task-types release still requires those requirements or explicit owner-approved exclusions.

### Opt-in blockers, waiting reasons and split delivery

- Disabled orchestration retains legacy capacity refusals; shared waiting text is applied only while the switch is enabled.

- Split completion markers obey the same MCP and REST rules-freshness checks as other card decisions. Card schemas advertise blocker arguments only while the workspace switch is on; properties are derived per request without changing shared extension schemas. Split labels cannot be added to roots or removed from keyed children through label edits. REST marker references must be bounded positive integers. Only lock timeouts become busy responses, through the shared admission classifier; settings read errors retain their retryable status. Checkpoint inbox reads are best effort. A failed inbox snapshot after lease commit logs sanitized diagnostics without exception messages and retains the pinned launch instructions and asks the agent to read the inbox before acting. Board waiting summaries share one runner connection and count usage once for active slots and once for queued reservations. Card lists likewise share one read connection and one usage calculation per board. Capacity refusals retain the original error if a display waiting reason is unavailable. Release waiting applies only to todo/backlog cards. Existing runner rows migrate with a NULL launch role, which preserves their configured role and stored job fields.

- The workspace switch defaults off. Enabled boards add same-board blocker
  edges (maximum 16 per card, no self/cycles, DFS bounded to 200 nodes).
  Only Done unblocks. SQL selection skips unresolved blockers and the runner
  checks them before starting. Views use the pure `boards.why` predicate in
  blocker/person/release/cap order; explicit claims share its waiting text.
  Dependency waiting alone does not enter `needs_me`.
- `boards.sqlite3` adds `task_blockers` and `card_splits`. Keyed children use
  `split:<parent>:<key>` labels; key checks and inserts share the board write
  transaction. Inbox reads include open unsplit roots, excluding children and
  coordinator-created roots. Only the coordinator PAT or a signed-in owner
  reads or marks them. The first split/kept marker wins and appends card history.
- Configured todo triggers retain their prior behavior with the switch off.
  Enabled roots launch `main`; children never trigger split starts. Jobs pin
  the selected `launch_role` in an additive runner column, so later switch
  changes cannot change a queued job's launch instructions. Messages include
  inbox context; the agent creates missing stable keys and marks completion.
- Each runner sweep reconciles eligible roots older than 10 minutes with no
  active/started job. Retry keys are `split-retry:<card>:<n>`; gaps are 10, 30,
  90 minutes and n is bounded to 3. Attempts (including refusals) use the existing
  `orch_misses` log with negative event ids. Eligibility and attempt number are
  rechecked under `fenced_start` before inserting a job or miss. Three refused
  attempts show `not_released` / `split stuck` in waiting reasons and checkpoints.
  A completed marker stops reconciliation. The hub never creates children.
  Retry keys share the caller key space, scoped by card and requester. Reusing
  the same key and arguments returns the stored job; different arguments refuse
  with `request_conflict` rather than launching another job.
- Board reads inside runner transactions use the read-only connection path;
  no BoardService or BoardStore migration runs there. New route lock timeouts
  return `409 busy` without mutation. No external service is called.
- Without a runner, card delivery uses a subscribed `card.created` webhook
  plus the inbox at the next session. Without either, a new card waits for that
  session. Continuous orchestration, done/stuck routing and runner-job events
  remain separate work. Stable keys are the agent's responsibility; changing
  keys after a crash can create additional children.

- `runner.sqlite3`: runner jobs with pinned launch role; orchestrator configs, cursors, leases, steps, journal, retry attempts, restore flag

### Foreground refresh rendering

- A background workspace refresh that supersedes a pending foreground render inherits that render on the same screen.

- Navigation epochs reject stale reads and Settings completions; ordinary events leave an already rendered Settings view intact.

- A current request failure clears the pending render; a later background refresh retains its usual form-preservation policy.

### Webhook dispatcher recovery

- Each delivery pass contains unexpected errors from reconciliation, due-delivery lookup and delivery.

- An error is logged without its private message and the dispatcher waits on its stop event before continuing: 5 s initially, doubling after consecutive errors to a 300 s cap.

- A successful pass resets the backoff; stopping interrupts the wait.

- A `sqlite3.ProgrammingError` ends the dispatcher silently only when its ControlDB is closed; otherwise it follows the logged, bounded-backoff recovery path. Exit always removes the event listener.

- This keeps a temporary failure from silently ending background delivery.

### Revoked-token socket guard

- The real-socket revoked-token guard records the server's completed 401 and asserts that neither body reads nor route dispatch occurred.

- A forced early close covers upload failure independently of whether the client can read the response.

- The gateway already sends `Connection: close`; this test does not add body draining to the unauthenticated request path.

### Design before code

- A planner role must include `design_before_code`.

- On a configured board, approving a `plan` card runs `orch plan-check` on its body; only `task_approve` removes the `plan` label; a planner connection's card must pass plan-check before it reaches `todo` or `done`.

- Plan-check G3 requires each numeric line in (a) to lead with M, T, G, MEASURED, TARGET or GUESS after indentation and an optional Markdown list marker.

- G7 requires each mapped test file to exist within the current repository root at check time; `tests/test_orch_acceptance.py` is allowed.

- The root is the nearest enclosing Git checkout, or the current directory outside a checkout.

- Absolute paths and paths resolving outside the root are refused.

- `orch_card_start(board, role, card, step_key, epoch, token, config_ref?)` uses `role` to identify the lease and step lineage.

- Acceptance cases R1, R1b, R1d, R1e, R8b, R10a and R11e live in `tests/test_orch_acceptance.py`.

- `SYSTEM.md` and `MODULES.md` are kept current by `docs-check`, which runs in the test suite.

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

### Board coordinator settings

- Owners can read coordinator candidates through the board settings API; reads and writes share the same active PAT eligibility rule and never return tokens.

- Restricted-origin backlog, todo and doing cards can require deliberate origin release. Healthy active work, released/rejected/terminal cards and explicit person decisions create no origin item. Existing configured plan-check and person-decision rules remain in force.
- A live appointed independent coordinator requires memory read/write, board-wing read/write and visible space_read, space_post, task_approve and needs_me. Standard member coordinators can discover release work without decide; release boards never confer authority to answer asks or read unrelated proposal/plan reviews.
- The creator and its descendants cannot release their own restricted origin; the creator's ancestors remain eligible. Missing, cyclic or foreign lineage and unverifiable creator stamps fail closed, including on stored agent markers. A coordinator may release origin while holding the card, preserving status, holder, lease timestamps, provenance and any separate proposal/plan review. Release requires an affirmative decision and a nonempty reason up to 500 characters, grants no self-review, and never renews the lease. Its reason-bearing audit event keeps the existing decide kind with choice="release origin".
- The signed-in owner receives one approvals item per origin awaiting person recovery. An absent/non-live configured record, or a verified independent record lacking any required release grant, yields approval_kind=origin, need=release and actions=[approve]. Live creator/descendant conflicts, unsafe lineage or an unverifiable stamp with any live configured record yield origin_configuration, need=you and actions=[configure_coordinator], with can_approve=false. Owner-role tokens receive no person fallback. An independent fully eligible coordinator yields no owner origin item or notice.
- Configuration navigates to existing board settings. An invalid creator stamp requires clearing the coordinator before existing owner recovery; this is board-wide and sends other unreleased cards on that board to owner recovery too. Changing to another live coordinator cannot cure an invalid stamp.
- Origin release never approves a pending proposal/plan. Owner recovery is two actions: release origin, then review. Outside owner fallback, owners retain ordinary rejection of pending proposals without releasing origin, including while coordinator configuration is required. The configuration item itself offers only board settings; owner rejection in these states is available via the card/API approve route with approve=false, not via that configuration item. Explicit person-targeted backlog proposals and plan reviews remain visible independently; while the person cannot release their unreleased origin, these review items advertise only reject and explain that release must precede approval. An applicable owner origin item takes precedence until recovery, then the separate review remains with its full actions.
- The exact validated choice "release origin" is reserved for origin-release audit events: user decisions and ask options refuse it. Validation preserves case and whitespace as for other text. Claim delivery ignores decide events whose choice is "release origin", preserving the earlier return-routed decision and its verbatim reply, including reply-only decisions.
- The signed-in owner's list includes at most one coordinator_waiting notice per visible non-archived board, counting unique owner-release and configuration cards separately. Non-person/non-owner callers omit that bucket. Digest omits notices but retains origin card reasons, approval kind, actions and can_approve, with release ordered after you and before plan. Counts deduplicate cards. Existing opt-in phone needs-you notifications also consume actionable Approvals, preserving coordinator-revocation recovery notifications. Notices use "card needs" for one card and "cards need" otherwise.
- The existing Needs me consumer renders Origin release and Coordinator configuration in Approvals. Release uses a required reason and the direct approve route, preserving separate review without a proposal hold; configuration opens existing board settings. Proposal Decline and plan Reject buttons render whenever reject is allowed, independently of approve; approval controls still require approve permission. Both rejection buttons use the existing decline handler.
- Shell project attention counts and refresh selection include origin/configuration approvals. Release items rank after person decisions and before plan review; a selected origin stays open until it leaves the queue.


### Thread-derived mutations and citations

- Within one workspace, a fact with any readable thread source is mutable.
  The read rule considers both flat provenance and source rows. The worker
  rechecks each authorized fact's wing and source set inside its transaction;
  rows changed or added after the probe do not acquire authorization.
- Every lookup and worker call uses the authenticated principal's workspace.
  A foreign workspace identifier resolves as missing. Internal mutation scopes
  are stripped from client arguments and hidden from tool schemas.
- Every principal, including an owner, needs read access to cited threads,
  private source ids, drawers and closets. Missing, unreadable or undecodable
  citations use the same not-found reply. Existing mutations on unreadable
  facts and rooms are no-ops; owners have no bypass.
- Provenance is decoded up to four percent-decoding rounds and checked for
  control/format characters after decoding, before whitespace is stripped.
  Recognized tokens anywhere in a provenance string must resolve readable;
  original strings are forwarded and stored unchanged. Plain strings with
  encoded controls are also refused, and partial hex tokens in filenames
  such as `thr_2024_notes.md` are recognized. Private source ids allow path
  separators, so the complete private token remains literal rather than
  guessing where a path-embedded id ends.
- Source deletes enumerate explicit ids and filter drawers, closets, AAAK
  rows and revisions by their own readable room. A roomless row needs a
  readable current drawer or, when its drawer is unresolved/absent, readable
  normalized source tokens. A resolved unreadable drawer or resolver error
  always keeps the row. Snapshots are rechecked before current drawers go;
  revision checks hold the revision lock. Unassociated legacy rows remain,
  even for an owner's MCP call. Unscoped local calls retain source-wide purge.
- Tunnel deletion rechecks endpoint snapshots, including drawer ids. Hallway
  deletion rechecks every room under its lock. Taxonomy and room-list results
  remove unreadable thread keys for every principal.
- Non-reader extraction leaves its cursor unchanged so a later reader can
  finish filing. A missing optional closet collection means no closet rows;
  other probe failures propagate. Parser/access caches expire with a response.
- No scope is persisted and no existing user text is rewritten. Rollback is
  a code revert; the per-request revocation window remains unchanged.

## Numbers

| Label | Number | Source |
|---|---|---|
| M | `max_agents` range 1–20, counting distinct connection assignees | `commonplace/projects/limits.py:26,34-57` |
| M | One worker PAT per machine | `commonplace/runner/registry.py:61` |
| M | Runner store writes use `BEGIN IMMEDIATE`; busy timeout 10 s | `commonplace/runner/jobs.py:68-95` |
| M | One active start per card | `commonplace/runner/jobs.py:47-50` |
| M | A non-person start within 24 h of the last start is refused | `commonplace/runner/jobs.py:236-243` |
| M | Started jobs hold machine in-flight capacity and per-machine project exclusion while their card remains doing with that machine's worker as assignee; failed/cancelled jobs and cards no longer doing or reassigned release capacity, irrespective of elapsed time | `commonplace/runner/jobs.py:lease` |
| M | Hourly start-rate limits, the 24 h started_recently refusal and the separate 3600 s orchestrator admission hold remain unchanged | `commonplace/runner/jobs.py:_hourly,create_in`, `commonplace/orch/runner_bridge.py:_cap_count` |
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

- Backups copy every SQLite file in the data directory (`commonplace/backup.py`).

- Online writer scans resolve the gateway PID's ancestor chain in the scanned `/proc`, so inherited data settings on supervisors do not falsely block a backup started separately from the gateway. Unrelated writers, including identical wrapper commands, remain refused; worker PID exemptions still end after pause. Offline scans retain only the backup's own ancestry exemption.

- Online enumeration permits only SQLite sidecars (`-wal`, `-shm`, `-journal`) to disappear between listing and metadata lookup: the live gateway may remove them even while file creation is frozen, and snapshots omit them.

- Missing user files, symlinks and special files still abort the backup; offline enumeration remains strict.

- The gateway names the workspace path when entering the creation gate: an existing directory lookup can finish inside a writer transaction while frozen, so it cannot deadlock against the backup's all-database barrier.

- Creating a new workspace directory still waits for thaw.

- `restore()` publishes a verified copy into a new directory and leaves a restore marker there.

- Deploy (`deploy/commonplace/deploy.sh`) requires a new successful backup invocation, a fresh snapshot and backup verification before any Compose call.

- A transient sleep service with `After=<backup>.service` keeps the backup unit referenced from before invocation capture through validation, independently of an enabled timer.

- The ordering dependency does not trigger a backup; it prevents garbage collection from discarding even a fast oneshot's invocation and execution time.

- The holder uses `Type=exec`, is stopped before Compose, and is released by EXIT and signal traps on failure or timeout.

- Missing proof always refuses deployment.

- SIGKILL or a shell crash can leave a holder that the operator must stop before retrying; see the deployment runbook.

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

### Authenticated owner progress scope, State lifetime and quota causality

- The owner-only `/api/search-index` route always carries its authenticated
  principal. Counts include every currently readable thread in the workspace,
  including another member's readable threads, using the existing indexed and
  pending predicates. Owner status grants no extra thread read authority.
- Each poll makes one current authorization decision per thread over metadata
  pages of at most 500 rows; the allowed descriptor map lives only for that response. Concrete metadata
  descriptors are revalidated in the aggregation snapshot; changed or newly
  encountered descriptors are excluded. Message and chunk content is not read.
- Successful sweep writes accrue units by stored thread id for the process
  State lifetime. Every poll reauthorizes those ids; revocation hides accumulated
  units, and a fresh State starts at zero without recovering historical totals.
  Hidden-only activity and hidden-only quota refusals do not set the public state.
- State precedence is configured `off`, then `deferred_storage` for readable
  pending work actually refused by quota, then readable active `running`, then
  `idle`. Active means inside a thread's write loop; queued or paused work
  alone does not set `running`. The global workspace storage meter is unchanged: hidden storage may
  cause genuinely blocked readable work to report `deferred_storage`. This
  causality is explicitly accepted; there is no unconditional state-equality
  claim when readable quota eligibility differs.
- Polling has one one-second proof budget, including timed snapshot/attribution
  lock acquisition and SQLite progress interruption. Owner progress uses a
  separate strict Recall probe: store and private-reader faults propagate to the
  proof boundary, while ordinary Recall consumers retain deny-on-fault behavior.
  Incomplete enumeration, authority faults, missing strict reader context or expired proof returns the fixed HTTP 503
  `{"error":"Progress unavailable"}`, with no partial totals, raw errors or
  global fallback. Successful and unavailable responses carry `no-store`.
  The proof budget is a failure boundary, not a latency guarantee.
- Local operator CLI status/run explicitly use `principal=None`, retain their
  unscoped count keys and existing enrolment behavior, and do not supply the
  authority for the HTTP projection. Other provenance and read residuals below
  are unchanged.

## Residuals

- **Admin observations:** All seven sections are implemented; operational sections
  request data only with instance eligibility. Health validates the existing public
  `healthz` response independently from installed versions and packaged build SHA.
  Token activity indicates recorded contact, not a live process. Machine and worker
  contact remain separate; owner attestation is always unverified. There is no
  workspace concurrency cap. Off uses distinct doing agents (including unlimited);
  on reuses accepted-only slot accounting, preserving actual use above the cap and
  stored worker assignments on disabled machines. The reused needs-me route has a read-first limitation: `/api/needs-me` can initialize legacy stores and bind
  rule-store metadata; the no-write promise here covers only the new Admin GETs.
  SQLite may create WAL/SHM sidecars during reads, so no-write tests compare logical
  tables, schema and non-sidecar files rather than byte equality of WAL files.
  Section requests are independent observations and can race ongoing activity.
  Runtime/limits/stuck query unarchived boards through the generation-aware supervisor
  resolver, opening existing encrypted board/runner stores with mode=ro, query_only,
  a shared 2 second request budget, at most 250 ms connect/busy waits and a progress
  handler. Missing coordinator and heartbeat columns read as unconfigured/unknown without migration.
  Cursor slugs accept the creation limit of 100 characters. Only enabled, unrevoked
  machines contribute active grants; historical assignments still count toward usage.
  Runtime tables label connection state and token-use activity explicitly; next-page
  source errors preserve earlier rows and retry the same cursor. Typed runtime
  identifiers and enums reject malformed stored values.
  Missing optional files/tables remain absent; corrupt sources fail safely. Only Admin opts into missing runner input; normal admission retains its strict input contract.
  No application/writer locks, store constructors, seat creation, project GET,
  lock-taking settings/registry methods or webhook/coordinator wrappers run on these
  paths. Parameterized workspace-scoped joins verify current membership, disabled
  users, lifecycle, runner kind and subscription policy/wing eligibility. Wakeability
  is eligibility only, never delivery. The injected Admin UTC is captured once for
  lifecycle, stale/lease predicates and recent-start accounting; monotonic time alone
  bounds reads. Independent runtime subresults preserve completed sources and mark
  unattempted sources as errors on budget exhaustion. Client loads including JSON
  decoding retain the 5,000 ms deadline and generation/epoch disposal guards.
  Needs-me keeps its exact server count and queue semantics in a separate panel
  from the deduplicated union of expired leases, active blockers, waiting-person
  facts and raw stale-agent facts, including children. Admin actions are GET reads
  or navigation to existing details/settings; no new admission or mutation API.


- **Backup receipt limits:** Opt-in CLI receipts record the last completed online or
  offline instance snapshot attempt, outside every data/staging/snapshot tree.
  Final-mode reporting suppresses a rejected offline probe when online fallback is
  taken. Check, cleanup and restore do not report. A same-directory 0600 temporary
  file is fsynced, atomically replaced, then its parent fsynced; reporting faults
  emit only a fixed diagnostic and never alter backup outcomes or retries. SIGKILL
  before reporting leaves the previous attempt; no historical backfill, running
  indicator or remote-upload/retention verification is claimed. The gateway only
  reads canonical UTC timestamps with mandatory date/time seconds and optional
  one-to-six fractional digits. Parent receipt refreshes advance child generations
  and cancel older per-mode retries before applying results. The reader
  reads fixed names through no-follow/nonblocking descriptors, fstat regular-file
  checks, strict schema/enums/timestamps and a 16 KiB cap; corrupt modes fail independently.
  The hosting operator provisions one UID/GID 10001:10001, 0700 directory and 0600
  files. The optional overlay mounts it at `/run/commonplace-backup-status-ro` RO
  for the gateway's configuration and `/run/commonplace-backup-status-rw` RW for a
  snapshot-only CLI environment override. Compose must not create the host path.
  Both aliases are visible in the shared container namespace: the configured gateway
  path is RO and its code only reads, but this is not OS isolation from the RW alias.
  This accepted residual requires no alternate container or host reconfiguration by
  the application. Build identity is a validated 40/64 hex SHA installed as package
  data; absent/invalid markers remain unknown, never runtime git/environment identity.


- Accepted, known gaps.

- D9.2 = A is LOCKED: hidden storage may cause `deferred_storage` only for
  actually quota-blocked readable pending work; incomplete progress proof stays
  unavailable without partial counters or a global fallback.

- Each is a reason a check can pass while the system is still wrong.

### Scoped wing counts and search fan-out

- Authenticated `list_wings` and `get_taxonomy` enumerate complete metadata
  pages, authorize each pair with the existing workspace-bound thread reader,
  and sum only identities revalidated against current metadata. Owners use
  the same thread boundary. Readable imported rooms retain their existing
  presence/count behavior; message reads still require the consented range
  proof. No stored content or authorization policy is changed.
- The workspace store opens before deadline-bound pair authorization, so
  cold and warm requests retain the same readable rooms. Ordinary room names
  such as `thr_notes` use the existing thread-room predicate.
- Standalone mediators with a workspace-bound principal keep metadata
  projection even without gateway or handoff context: ordinary readable pairs
  remain visible, and unresolved thread pairs stay hidden. Principals without
  workspace context receive empty wing lists, taxonomy and search fan-out;
  no raw worker listing can bypass wing policy or thread authorization.
- SQLite identity seeks and the generic metadata fallback read at most 500
  records per page or final batch, without document columns. Missing, null
  and question-mark wing/room values normalize to `unknown`; bookkeeping
  stays excluded. Local CLI/stdio reads with no scope keep legacy behavior.
- Search fan-out uses that same visible wing projection before requested-wing
  selection, CLI compatibility filtering or the fan-out limit. Hidden-only
  wings cannot appear in `wings_searched` or change the denial count. Internal
  web and memory consumers inherit the scoped listing; connection validation
  rejects ordinary wings with only unreadable content.
- Worker scopes, identity descriptors and advancing cursors remain private.
  The gateway strips client-supplied scopes and removes them from discovery.
  Raw overview caches never hold a principal's projection; every response
  authorizes afresh, including after revocation or changed associations.
- Malformed pages, storage faults, partial acquisition and deadline exhaustion
  discard every accumulated page. Listings return exactly `{"wings": {}}` or
  `{"taxonomy": {}}`; fan-out returns the query with empty results and
  `wings_searched`, plus its normal facts attachment. No raw exception,
  partial count, hidden marker or cursor is published. One caller deadline
  bounds acquisition; this contract makes no timing or constant-time claim.
- Imported holder-room counts can include rows outside a consented range,
  as before this projection; content reads retain their separate range gate.
  Search `total_before_filter` can include hidden candidates inside a visible
  wing before the response gate filters them.
- Other overview, legacy
  provenance and owner search-index progress remain separate residuals.

### Visible logical drawer pagination

- Authenticated drawer listings enumerate metadata in bounded pages, authorize
  current physical identities in the caller's workspace, and revalidate them
  before logical grouping, date filtering, totals and slicing. Owners use the
  same reader. An unreadable chunk cannot authorize readable siblings or
  contribute to their preview, date or count. Wing aliases retain their meaning.
- SQLite final membership and page hydration share one snapshot. SDK fallback
  revalidates metadata and hydrates only visible page identities in batches of
  at most 500, checking their metadata again with the fetched documents.
  One request deadline bounds acquisition and final reads; no timing claim is made.
- Missing workspace context, incomplete enumeration, malformed pages and read
  faults return the normal empty pagination envelope with zero totals. No raw
  exception, partial global total or private descriptor is published. Raw caches
  contain no principal projection; local unscoped calls retain existing behavior.
- The final response gate retains the proven visible total when it only scrubs
  provenance metadata. If it removes a drawer after projection, the listing
  fails closed to the same empty envelope rather than returning a page gap.
- Literal-wing readers with imported-history read consent retain proved visible
  totals through both response gates, including provenance-only metadata scrubs.
  Existing wing and thread authority still controls membership.
- Page arguments use the worker's shared schema rules after policy checks:
  JSON null selects the default, invalid integers retain JSON-RPC invalid-params,
  and worker filter validation errors remain error results. Date validation stays
  in the worker; invalid filters never masquerade as empty successful pages.
  A metadata-free worker call with an empty allow-list validates first, preserving
  worker ordering without loading the worker server in the gateway process.
  Non-owner wing refusals retain their existing messages. Incomplete membership
  proof still returns the empty envelope rather than a worker failure message.
- Consented `private_chats` drawer listings use the same worker validation before
  reading imports, under one request deadline. Null defaults and integer/range
  errors match the worker route; invalid dates retain the worker error result.
  Import listing receives the validated limit and offset unchanged. Valid date
  windows still exclude imports without a proved date. Incomplete validation or
  import-listing faults yield the existing empty listing shape, with no raw error.
- The import branch of `list_rooms` also validates through `list_drawers`, so
  validation errors name `mempalace_list_drawers`. Its public schema declares only
  `wing`; legacy extra filters are still accepted through drawer validation,
  including the raw `room` filter passed to import listing. This is not parity
  with the worker's own `list_rooms` argument validation.
- Stored bytes, imported-history consent/range rules, provenance alias scrubbing
  and legacy source-file proof retain their separate contracts and residuals.

### Hallway read eligibility and visible pagination

- Authenticated hallway reads require readable wing policy and every room. Every
  returned string, including embedded room tokens, ids, labels, entity/author and
  timestamp values, uses the same independent raw thread/private token scan and
  current read proof. Missing proof drops that row before visible counts/paging.
  Kept strings, emoji joiners and literal percent text remain byte-identical.
- One shared public field tuple is applied before count/sort/page and again by the
  final gate: id, wing, rooms, label, entity_a, entity_b, co_occurrence_count,
  created_at, created_by, strength, stability, last_activated and access_count.
  Existing dynamics fields remain present. Present string fields must be strings;
  rooms must be a list of nonempty strings. Strength, stability, access_count and
  co_occurrence_count must be finite int/float values (not bool/text); invalid
  rows are excluded, with absent legacy fields left absent. No other key returns.
- Metadata pages contain at most 500 descriptors in SQLite rowid or JSON insertion
  order. Request-local authorization yields concrete allow descriptors, revalidated
  in final read snapshots. Eligible rows retain stable strength ties. Ineligible
  rows drop before counting; only a failed final-gate recheck discards the entire
  page and its counts. Readable mirror rows avoid singular-room drawer scrubbing.
  Client discovery hides internal scope, mediation replaces client-supplied scope,
  and worker scope requires verified gateway identity. Local unscoped reads remain.
- Every scanned string is capped before scanning by the public shared
  provenance_refs.MAX_REFERENCE_CHARS. Duplicate tokens collapse. Overlapping
  raw scans cost O(C²) time/memory per capped string of at most C characters;
  the cap bounds a scan but does not promise it completes within a deadline.
  Deadline checkpoints precede each field scan and each token lookup, rather than
  waiting for an entire 500-row batch. Oversized/unproved values exclude the row.
- Missing context, incomplete acquisition, malformed protocol or deadline expiry
  discards all projected counts and returns the fixed empty pagination envelope.
  Reads do not recompute strings, clean up stored legacy rows (O4), or extend import
  consent. Non-canonical spellings outside the raw token grammar (percent escapes,
  fullwidth/NFKC forms) remain the accepted limitation; no returned string field is
  exempt from scanning. Other graph/provenance residuals below remain unchanged.

### Authenticated room and status counts

- For `status`, `list_rooms`, `list_wings` and `get_taxonomy`, backup pauses
  retain the gateway retry contract: JSON-RPC error -32603 with `retry_after_s`,
  and an internal ToolError explaining the backup pause. Pair acquisition and
  status diagnostics propagate that pause rather than returning zero counts.
  Consented `private_chats` import listings retain the same retry errors for
  `list_rooms` and `list_drawers`. Shared pair-projection search fan-out also
  propagates the pause. Other acquisition faults retain the zero-count empty
  response. Other drawer, hallway, tunnel, graph and KG projection pause behavior
  remains separate work.

- Authenticated `list_rooms` and `status` sum the complete authorized pair
  projection, including wing aliases and unknown loci. Wing policy and the
  current workspace thread reader both apply, including to actual owners.
  Malformed private mirror rooms need readable association proof.
- Metadata pages and final snapshot batches stay bounded to 500 under one
  request deadline. Changed descriptors are excluded. Incomplete acquisition
  returns zero counts/maps without partial global totals or raw failures.
- Authenticated status suppresses global `hnsw_capacity` and raw capacity
  probe messages. A disabled vector index reports `vector_disabled: true`
  with the fixed reason `Vector index unavailable; repair required.`, including
  after incomplete acquisition. Local operator status retains its raw diagnostics.
- Authenticated diagnostics omit `sqlite_integrity`, `library_versions` and
  `updates`, including embedded palace/database paths. Integrity failure still
  reports `sqlite_integrity_failed`, the fixed error and `partial`. Unexpected
  fields fail closed. Principal projections never enter raw caches.
- The worker status schema retains the private scope property required by
  internal argument whitelisting; authenticated discovery strips it and client
  scopes are discarded. Non-object local scopes receive an argument error.
  Invalid room-list wing types return the ordinary name-validation error;
  whitespace-only wing filters retain the existing all-rooms convention.
- Imported-history listing consent and source-range rules remain in force;
  these counts create no new read grant. The merged hallway, tunnel, graph and
  KG projections retain their own read contracts. Legacy provenance remains
  separate work. Stored rows stay intact.

### Thread-boundary residuals

- Token-free plain-file provenance still has no local association proof on read.
  Typed drawer/closet citations now resolve current local metadata as described
  below; foreign identifiers already stored before this change are not purged.
- Non-canonical spellings outside the token grammar remain possible, including
  inserted characters that are neither Cc nor Cf (combining marks or Unicode
  spaces), fullwidth letters, and missing underscores. Subject, predicate and
  object text stays verbatim and is outside provenance parsing.
- `add_drawer` source publication is outside KG citation checks. External
  `ext-`, `mcp:` and `import:` ids hidden only by wing policy remain ungated.
  Drawer update/id-delete closet cleanup still follows the complete source
  across rooms; scoped replies omit its closet count.
- An any-readable-source fact remains mutable despite other hidden sources.
  A single-valued save leaves an unreadable rival open; a new racing row can
  produce a duplicate. Scoped cleanup can leave stale unassociated legacy
  index rows, while unscoped local cleanup retains its broader purge.

- **Same OS user.**

  - Anyone who runs as the service's OS user can use the CLI (which is `orch_owner`) and read the data directory.

- **Restore outside `restore()`.**

  - A hand-copied data directory or a deleted `runner.sqlite3` carries no restore marker: the journal resets and nothing is blocked.

- **Effects after T.**

  - After a restore, effects made after the backup time are unknown to the journal; the truth checks in step 5 are the only guard.

- **Outside the journal.**

  - External effects and gate runs started outside the journal are invisible to resume.

- **Revocation window on trigger starts.**

  - The trigger's requester and grants are read in a control snapshot before the fenced transaction.

  - A grant or requester revocation that commits between that snapshot and the in-lock insert is not re-read; the start can still be inserted.

  - The runner re-checks the requester and grants at `starting`, so the job is refused before launch.
  - The runner re-checks requester, grants and binding epoch at `starting`, including idempotent retry. Each job snapshots the validated machine epoch; legacy jobs have NULL snapshots and are refused.

  - Topic/wing reset, worker rotation and revoke increment the machine epoch once. Same-provider confirmation and project-only edits preserve it; changing an attested provider returns 409 `provider_change_requires_reset` until explicit reset and owner review.

  - Board prechecks finish before runner admission; fresh control authority is then held through the guarded starting write and runner commit in both switch modes. A committed reset before the write refuses; one after the write leaves starting work to become uncertain at the next sweep, retaining capacity and never relaunching. Owner resolution takes runner first, then control audit.

  - Stop old hub processes before upgrading/restarting: old reset writers do not increment epochs, so mixed-version writers cannot uphold this fence.

- **Same-second ties are listed.**

  - Job starts and board events are stamped in whole seconds, so a start in the same second as a card's latest firing event cannot be ordered against it.

  - Resume lists such a card as unstarted; it may be a false positive.

- **Skipped events.**

  - Board events committed between a configuring accept (or a restore acknowledgement) and arming are skipped, not replayed.

  - Resume lists the affected `todo` cards as unstarted.

- **Refused triggers are not retried.**

  - A `todo` card whose trigger start is refused (`busy`, `at_cap`, `started_recently` or any other code) is a final miss: the cursor moves past it and nothing re-fires it, even after the queue drains or a slot frees up.

  - It shows in the checkpoint's unstarted list and as a miss; move the card out of `todo` and back to retry.

- **Orchestrator tools on every workspace.**

  - The `orch_*` tools appear in the tool list on every workspace, also where no board has a config (runner children never see them; owner-only tools are listed for owners only).

  - Without a config only proposing and reading do anything.

- **Docs-check checks change, not accuracy.**

  - It refuses a touched module whose block did not change and a change without a `SYSTEM.md` edit (whitespace-only edits do not count); it never judges whether the new text is right.

- **Cap overshoot.**

  - The cap reads card state from `boards.sqlite3` as a snapshot, so a concurrent claim outside the runner can overshoot it.

  - Started jobs hold machine in-flight capacity and per-machine project exclusion only while their cards are doing with that machine's worker as assignee, even after an hour.

  - A terminal job state, a card leaving doing, or reassignment releases that capacity.

  - The lease resolves the worker identity before its runner transaction and reads cards through the read-only boards helper inside it, without constructing BoardService/BoardStore or acquiring a boards write lock.

  - Card state can change after that read; the next lease reads it again.

- **D1 shared worker PAT.**

  - All runner children use the machine's worker PAT, so per-child provenance and per-child limits are not available.

- **D2 launch shape.**

  - Whether T3 accepts `modelSelection` at runtime is unverified; when it is given, effort comes from T3's defaults.

- **D3 thread-list keys.**

  - An empty thread list is no proof that a launch did not happen; such steps go to `needs_person`.

- **Plaintext job lease tokens.**

  - The runner's job lease token is stored in `runner_jobs` and in the machine journal (pre-existing).

- **Waiver list.**

  - Approval of the waiver list below is a manual owner review.

## Open dependencies (D1–D3)

- These are owner decisions or captures tracked in `docs/runner.md` ("Owner checklists").

- Until each is closed, the residual above stands.

- **D1 worker identity.**

  - "Attested, not verified" or a dedicated provider instance per worker; stronger identity needs a per-thread credential.

- **D2 launch shape.**

  - One real launch capture settles the result fields, the workspace strategy and `modelSelection`.

- **D3 recovery.**

  - The thread-list keys must be captured before any automatic reconcile; until then `uncertain` and unproven launches go to a person.

## Keeping this file current

- Add a NEW bullet next to the related decision, number, store or residual; do
  not extend a shared paragraph or collect unrelated additions at one section end.
  Keep each topic on its own lines, separated by blank lines. Independent
  insertion positions merge cleanly; edits at the same position can still conflict.
  In `MODULES.md`, add a NEW indented sub-bullet to each touched module block
  instead of extending its **Promises** or **Tests** line.

- `commonplace orch docs-check --base <ref> --head <ref>` exits non-zero on a changed path no `MODULES.md` block owns, a touched module whose block did not change, no change to this file (a whitespace-only edit is no change), an empty or missing block, a bad ref or any error.

- The test suite runs it against the merge base with the development branch and fails closed when the base is missing.

- The only exception is a commit trailer `Docs-waiver: W<n>` whose id is listed below **in the base commit**; an id added in the same change does not count.

## Waivers

- Owner-approved waiver ids, one per line as `W<n>: <scope>`.

- None are approved yet.

### Reassigning active work

- **Claim boundary.** Immediate owner-assignment claims apply only when handing off a todo card. Reassigning doing work, including a stuck card from needs-me, returns it to todo with no lease or automatic start so the receiver can explicitly claim it.
### Legacy surface comparison and agent guidance

- The pinned handoff baseline overlays current board definitions and applies their live availability and workspace schema seam before strict HTTP and audit comparison; disabled opt-in tools remain absent.
- Agent instructions keep the existing size budget; the project guide holds runner retry and delivery details.

### Compatibility fixture coverage boundary

- The overlaid board definitions share schemas between baseline and current servers; the strict fixture protects visibility, HTTP and audit compatibility, while focused card tests cover opt-in schema fields.

### Canonical Space REST foundation

`GET /api/spaces` lists `{spaces:[Space]}`, `POST /api/spaces` creates a `Space`
with status 201, and `GET /api/spaces/<slug>` reads metadata. A Space contains
`slug`, `name`, `wing`, `coordinator`, top-level status `counts`, `can_configure`,
`open_url`, `version:1` and `deleted:false`. `open_url` points to
`/app/#/board/<encoded slug>`; only a signed-in workspace owner person has
`can_configure:true`. Legacy board responses remain supported.

The existing board row is the Space. Board, Memory and Chat are views of that
Space, with Board as the default view. Identity is the authenticated workspace and
exact slug; no new identity object or identity migration is needed.
List retains claim expiry; metadata is a plain active-row read without expiry,
transactions, orchestration, seats, settings or credential creation.
`POST /api/spaces` creates through `board_add`, preserving board authorization,
the transaction and the `space_created` event with `data.board`.
Non-person callers must pass the existing board tool visibility gate. Checked
create accepts freshness only through `Commonplace-Rules-Seq`; refresh failures
retain status 428 and their original payload. Optional null name/wing/repo use
slug/shared/no-repo defaults; distinct slugs may share a name.

Canonical validation is 400 `invalid_arguments`; policy or seat refusal is 403
`forbidden`; missing, hidden or archived metadata is 404 `space_not_found`.
Readable slug/repo collisions are 409 `space_exists` / `repo_assigned`, including
archived reservations. Hidden slug collisions are concealed as 404 and hidden repo
owners produce 403 without identity details. These different outcomes do not
eliminate existence oracles. Unknown subpaths are 404 `not_found`; collection
PATCH/DELETE and item POST/PATCH/DELETE are 405 `method_not_allowed` with
`Allow: GET, POST` or `Allow: GET`. Unregistered methods retain gateway errors.
An Idempotency-Key or include_deleted query is rejected with 400
`unsupported_idempotency` or `unsupported_include_deleted`.

Rename, archive, versions/retries/tombstones, project adapters, MCP/UI vocabulary,
new-user migration and desktop synchronization are deferred.


### Authorization-only provenance spelling recognition

Provenance authorization keeps the existing ordinary thread/private reference scan
and at most four strict percent-decoding rounds, refusing controls and undecodable
citations. A separate NFKC authorization copy recognizes only complete protected
shapes: `capture:<actor>:<source>:<identity>`,
`import-private:<user>:<source>:<identity>`, and
`capture-fork:<user>:<32 lowercase hex characters>`. ASCII space or NBSP may follow
structural colons; the namespace colon alone may be omitted when the actor starts
with an ASCII letter or digit and contains a digit or underscore (as production
user/connection ids do). This excludes prose actors such as `ing-notes` in
`capturing-notes:codex:v2`. New shadows begin at the field start or after
space, NBSP, `/`, `?`, `&`, `#`, `=`, `(`, `<`, `"` or `'`, or inside an already
recognized reference run. Every namespace start in a run is checked, including
overlapping ordinary and spelled candidates with no intervening boundary. A run
must begin before a candidate to waive its boundary check: a non-boundary
`notes.capture-fork:<user>:<digest>)` retains ordinary whole-run proof rather than
gaining a digest-only shadow. Internal separators stay mandatory; actor/source
components exclude whitespace, colons, path separators and
query delimiters. Identity suffixes retain internal colons and complete `#sha256:`
qualifiers through the existing whitespace/`?` boundary for capture/import ids.
Fork identities end after exactly 32 lowercase hex characters; any following
character (including another hex digit, punctuation, wrapper, text or a hash
suffix) is citation context and cannot hide the protected digest. Any protected
reference embedded in that context requires its own readable proof; proving the
leading fork never proves or suppresses a later candidate. Both ordinary
and spelled forks authorize that digest while preserving the complete citation
bytes. Fewer than 32 lowercase hex characters and uppercase digests do not create
new fork shadows. This does not detect arbitrary Unicode insertion.

Capture source labels are exactly `claude-code` and `codex`. Private import labels
are exactly `claude-code`, `codex`, `cursor`, `t3`, `grok-bot`, `chatgpt`, `claude-ai`
and the grant-only `native-memory`. New shadow namespace/source matching ignores
case and forms lowercase producer labels before lookup; actor and identity case
remain literal. Existing ordinary legacy tokens retain authorization outside these
new domains. A complete shadow authorizes the entire spaced reference rather than
an incomplete ordinary prefix split at an internal structural colon. Embedded
thread references are still independently authorized.

Every recognized reference requires readable local proof in the caller's
workspace. Missing and unreadable production-shaped shadows receive the same
not-found write response as canonical citations. Readable fork spellings are
readable with trailing citation context only when every embedded reference is
also proved. Capture/import identities remain literal,
including trailing punctuation: a spaced spelling with an appended wrapper can
therefore fail closed when no such exact identity exists. Punctuation is not
stripped from those variable-length ids, since it can be part of a real identity.
Read projections omit unproved citation fields while retaining D1-readable fact text. Authorization does not rewrite stored/displayed citation
bytes or subject, predicate, object and content. Ordinary URL/path citations such
as `https://example.test/capturing-notes` and `capture-notes.md`, unshaped trigger
words, and readable canonical fork citations remain compatible. A complete
protected reference embedded in a URL/path still requires proof. Existing
navigable-URL validation applies to new `source_url` writes; KG exposes that
citation as `origin_link`, and file citations in source observation rows.

The decoded field and its NFKC authorization copy each have a hard cap of
16,384 Unicode characters, counting decoded padding before trimming. An over-limit
field is refused in full, never scanned as a truncated prefix. Gateway fact reads
omit only the citation while retaining readable fact text and stored bytes.
KG writes refuse the entire operation before storage with the explicit error
`<field>: citation length limit of 16384 Unicode characters exceeded (decoded or NFKC-normalized)`.
The pending fact is not saved or rewritten; callers can correct the citation and
resubmit the same verbatim fact. Existing triples and source observations remain
unchanged; refused writes never reach a worker mutation. Protected-reference,
control and decoding refusals retain their existing
not-found responses; a length error discloses only the caller's field and limit.
This deliberately also refuses over-limit readable citations, including token-free
legacy files; citations within the cap retain their existing byte-preservation
rules. New URL writes also retain their existing 2,048-character validation.

This is a gateway fact-provenance policy, not a storage-wide citation limit.
Drawer writes retain their existing unchecked `source_file` storage; ordinary
gateway projections scrub top-level provenance fields. Import-proved non-fact
records remain exempt for the `source_path` and nested `metadata.source_file`
aliases constructed after exact import-range proof. Those aliases retain their
original citation bytes. This exception does not grant access to unproved words.

The bound covers both overlapping suffix output and omitted-colon near misses.
For input length n and fixed cap C = 16,384, four strict decoding/control passes
cost O(n) time and memory; candidate scanning costs at most O(C²) time and memory.
No per-start suffix work occurs above either cap, including NFKC expansion.
Thread tokens are deduplicated on insertion, preserving first-occurrence order
without accumulating repeated ids from every overlapping shadow.
Deterministic behavior regressions assert zero candidate enumeration above either
cap and verify public refusal, preservation and exact-boundary compatibility.
The raw decode/control passes remain input-proportional before the decoded cap;
there is no per-response character budget.

Wall-clock and RSS measurements use the serial, informational `timing` lane;
plain pytest excludes that marker by default, matching the behavior lane. Explicit
`-m timing` selects measurements. Performance never gates a PR, merge or release.
Isolated subprocesses report cap-sized and 1 MiB repeated namespaces, omitted-colon
near misses, fullwidth spellings and mixed namespace/thread-id expansion against
3-second scan and 128 MiB peak RSS reference budgets. Their 256 MiB address-space
limit and 300-second hang guard contain bad measurements; budget misses, memory
exhaustion and hang-guard results are recorded as information, not test failures.

This changes reference recognition only: typed associations, provenance aliases,
plain-file association proof and search-index progress retain their existing
contracts.


### Current typed provenance association proof

- Every outgoing source_drawer_id and source_closet resolves the complete decoded
  local id in the authenticated workspace. Physical ids and both logical parent
  keys resolve to current metadata on Chroma; every chunk requires a nonempty wing/room,
  wing authority and the existing thread reader. Owners use the same checks.
- Private metadata reads return no documents, page by at most 500 records, open
  collections read-only with create=False, and revalidate concrete descriptors.
  Chroma uses a final read snapshot. Other backends support exact physical ids;
  logical handles fail closed because the collection API offers no ordered
  snapshot enumeration guarantee. Missing targets, failed lookups, missing room proof,
  changed metadata or revoked authority omit the citation. Probes never invoke
  KG mutation handlers, write the KG or create an optional closet collection.
- Original source sets still decide any-readable-source fact eligibility before
  citation omission. Readable text and stored citations remain verbatim. Complete
  raw source_file tokens are checked before drawer metadata displays a basename;
  readable citation spellings and token-free plain-file behavior stay compatible.
- D9.1=A is locked: complete local plain-file proof is separate follow-up work.
  D9.2=A remains locked, including accepted global-quota deferral of actually
  blocked readable work. Neither decision grants new read authority. Imported
  history retains consent/range checks; unproved roomless derived rows stay stored.
  Provenance aliases and external-id wing-only limitations remain unchanged.


### Memory map citation omission

- Owner maps keep visible person relationships when a fact cites a missing,
  deleted or unreadable drawer. An unproved citation produces no memory link;
  it does not turn the whole map into an error or remove readable fact text.
- Hidden and absent targets have the same public map. Citation proof still uses
  current thread authority; stored facts and citations are never rewritten.
### Explicit tunnel read proof

Authenticated list_tunnels and follow_tunnels require both stored endpoint pairs
and every optional drawer's current metadata association to pass wing policy and
the existing reader in the authenticated workspace, including for owners. A
readable endpoint hint cannot authorize a moved drawer. Missing, malformed or
unresolved optional drawers drop the entire tunnel; room-only readable tunnels
remain eligible. Local unscoped calls keep their behavior.

Private probes page at most 250 tunnels and resolve at most 500 drawer metadata
rows per batch without loading document columns or calling get_drawers. Concrete
allow descriptors are request-local. The final read rechecks stored endpoints
and metadata descriptors, excluding changed or newly arrived associations before
follow previews or traversal recording. Reader authority is refreshed after
probing. Incomplete acquisition, malformed protocol responses and deadline errors
produce an empty public list without private markers. Read-only follow retains
record=False behavior; create/delete authorization and delete snapshot checks
are unchanged. Passive graph reads use the input projection described below.


### Explicit tunnel concurrency and import qualification

- Tunnel probes use identity keysets from one file read per worker call. Deleting
  an earlier id cannot shift later pages. Final identity lookup compares only the
  id, label and endpoint wing/room/drawer associations and returns the current row, so
  traversal potentiation and unrelated drawer metadata updates remain visible.
- Missing or changed associations still fail closed before preview hydration or
  traversal recording. Probe and allow batches remain bounded and request-local.
  Invalid name arguments retain the ordinary validation error envelope.
- Computed `imported:` drawers have no stored metadata association and are excluded
  from explicit tunnels even when the existing import reader proves their consent
  and source range. Ordinary imported-history read APIs retain their range rules.
- The read-only server seam used by the regression witness passes `record=False`;
  the public follow schema does not expose that argument. Tunnel labels now use
  independent protected-token scans before listing, following or counting them.


### Graph projection before traversal and ranking

- Authenticated graph reads first enumerate metadata in batches of at most 500,
  authorize each placement with wing policy and the live workspace reader, then
  revalidate concrete descriptors in fresh metadata snapshots. Empty allow means
  no placements; local unscoped graph calls keep their existing behavior.
- Nodes, plural wings/halls/dates, counts, edges, fuzzy suggestions, traversal and
  top-N ranking use only surviving rows. Grouped reads retain backend-specific
  ordering; collection paths retain enumeration order for tied readable candidates.
  Traversal and tunnel discovery retain the 50-result caps and stats the ten-node
  top list. Projected results never enter the raw graph or overview caches.
- Explicit tunnel totals reuse the authorized endpoint and drawer association set.
  Labels independently prove every recognized thread/private token before list,
  follow, preview hydration or counting. Kept labels (including emoji with a zero
  width joiner and ordinary percent text) and other stored fields remain unchanged.
  Label changes invalidate final tunnel descriptors; unrelated dynamics fields do
  not. Missing/unresolved optional drawers and duplicate tunnel ids fail closed as
  documented for explicit reads; computed imported drawers still require stored
  associations. Imported-history consent/range rules are not widened.
- Incomplete acquisition, malformed protocol replies, missing workspace context or
  deadline failure discard the entire graph response: zero stats, an empty tunnel
  list, or the ordinary not-found traversal envelope with empty suggestions. Odd
  client argument errors retain their JSON-RPC validation shape. No private diagnostics,
  redaction markers or partial global counts are returned. Literal-wing readers
  retain the existing all-wing-tool refusal; actual owners use the same live reader.
- Storage is never rewritten or purged. O4/O8 remain in force; external ids hidden
  only by wing policy retain their accepted limitation. The separate LOCKED
  D9.1 = A and D9.2 = A decisions, including actual-readable-work quota deferral,
  are unchanged by graph projection.


### Readable legacy graph parity

- Graph descriptors bind literal projected wing and room names, including legacy
  `?` values. Readable registry and sentinel rows remain graph inputs, matching
  the local graph paths; overview bookkeeping rules do not change.
- Graph argument names are checked against the worker's tool schema before any
  metadata probe. Unknown and missing arguments retain JSON-RPC `-32602` errors.
  Traversal retains the worker's null/default and integer-coercion conventions;
  booleans and non-coercible values retain its JSON-RPC error shape. Schema lookup
  and projection share one deadline. Worker/schema/projection faults still return
  the empty operation envelope, never a client-validation error.
- Ordering follows the reader that actually served the metadata page. Grouped
  Chroma uses room/wing/hall order. Exact and Compact readers use wing/room/hall
  with generated locus columns, or room/wing/hall on older read-only databases
  without them. Collection fallback keeps enumeration order. Sibling ties,
  suggestions and the 50-result cap follow that order; grouped statistics retain
  their separate room-name tie break.
- Exact and Compact projections read the local graph's SQL expressions and schema
  from one metadata-only snapshot. SQLite applies TEXT affinity on exact loci,
  including booleans and integers beyond int64; Compact and locus-less readers
  preserve native numeric values and their falsy-zero behavior before converting
  to text. Descriptors bind both projected values and stored SQLite sort values.
  Chroma preserves its grouped numeric text casts, ignores boolean locus columns,
  and reads dates from the string column. Collection fallback retains its existing
  metadata interpretation; overview bookkeeping rules remain separate.
- Null and missing tunnel labels are empty only for protected-token scanning.
  Returned/stored nulls, missing keys and unrelated fields remain unchanged; other
  non-string labels still fail closed. No palace data is rewritten.


### Graph compatibility residuals

- Accepted parity exception: non-string tunnel labels other than null remain
  ineligible on authenticated list/follow/count paths, although legacy local reads
  can list those records. Null and absent labels remain readable and stored intact.
- The existing explicit-follow path predates graph projection: unknown arguments
  can still become an empty list instead of a JSON-RPC invalid-params error.
  This remains a separate follow-up; graph-tool validation does not change it.
  the public follow schema does not expose that argument. Tunnel label reference
  handling predates this change and remains follow-up work for graph traversal.


### Local worker allocation observation

Storage-serving MCP entrypoints enable metadata-only allocation observation by default;
`MEMPALACE_WORKER_MEMORY_OBSERVE=0` opts out. Imports, durable jobs and forwarding-only
proxies do not activate a storage observer. Activation is deferred until the first
local storage dispatch on every entrypoint. Startup/preflight work before that
boundary remains unobserved; later fixed phases reuse the active observer.
Each activation has a random generation;
call ids are local counters, never client request ids. Fixed startup phases and actual
cache, collection, restricted-pickle and native-session branches report only observed
facts. Missing samples and untouched components stay null. No payload, result, content
hash, identity, path, arbitrary name, exception text or configuration value is recorded.

The resolved config directory owns private `diagnostics/worker-memory.jsonl` and one
backup, each at most 4 MiB. A stable nonblocking cross-process lock serializes append
and rotation; files are opened inside that lock. Symlink and nonregular destinations
are refused. There is no per-call stderr or general-log copy. At most 64 primitive
contexts and 128 serialized records of at most 4096 bytes are retained in memory.
Token-construction failures drop observation, release the active count and slot,
and let the request continue. Observer-construction failure is latched for the
process lifetime and counted in a saturating activation-loss counter, without
retrying on every request. End RSS source/status describe the same end read.
Failures drop observations and increase a saturating loss count; shutdown attempts
only a bounded 50 ms flush. This adds no network destination, database write, memory
limit change or restart policy. Whole-process RSS deltas during overlapping calls
are not uniquely attributable. Rotation, losses and unmatched pairs leave unknown
coverage: these observations alone do not establish or exclude a production cause.

Observation does consume CPU on the request thread: a synthetic interleaved trivial
call measurement found median 3.18 ms disabled versus 3.98 ms enabled (about
+0.8 ms per call). A slow sink did not add request-thread waiting in that probe.
These measurements are informational, not a merge threshold or a guarantee of
zero latency overhead. Concurrent producer contention and a full queue can drop
records; coverage claims require loss counts and complete begin/end pairs for
the exported interval, not only sequential scratch results.


### D1-visible KG pages and aggregates

- Every authenticated KG query mode and timeline authorizes wing, sensitivity and original source sets before counts, entity labels, candidates and pagination. Stats derive participating entities, predicates and wings from visible facts; orphan entities do not contribute.
- Search mode and drawer-search fact enrichment use the same metadata probe, allow descriptors and final read snapshot. Scoped search copies the authorized snapshot and uses a private in-memory lexical index with the same tokenizer, query-specific name handling, selective-term rule, BM25 seeds, 50-key candidate cap and history expansion as the local scorer. Document frequencies come only from authorized facts; hidden facts cannot affect scores, seeds or pages. Local unscoped reads keep their existing index.
- Metadata descriptor pages contain at most 500 facts. Authorization uses the workspace reader, including owner and imported source-range rules, within the shared worker request deadline. The final snapshot excludes new or changed descriptors before copying fact text. The palace lock and read transaction are released after the copy, before index construction or scoring. One absolute gateway expiry is carried to workers; copy/scan/scoring loops check it, SQLite progress handlers interrupt long statements, and temporary indexes close on failure. Authority caches last one response: a revocation during a request takes effect on the next request.
- `kg_query.search` requires a boolean at the authenticated gateway and is strict in the worker protocol; other tools retain their normal unknown-key validation. Declared pagination keys follow protocol integer conversion and minimum/maximum validation; undeclared keys return the worker-compatible `-32602` unknown-parameter error before projection. Malformed declared arguments and missing required entity retain worker-compatible protocol errors; data-independent entity/date/direction refusals retain their handler error results. An offset outside SQLite integer range retains the generic worker error. Malformed pagination returns the worker-compatible `-32602` validation error; null selects the protocol default. Non-owner wing filters first check the raw spelling against policy, then resolve matching canonical names to stored wing spellings; owners and wildcard readers retain literal filtering; string wing names from sees-all readers bypass the raw policy refusal; the worker validates names, while per-fact wing policy and the final allow-list still enforce visibility. Schema/required-argument validation and the handler retain their existing error shapes and order. Limited readers still get the policy refusal. The final authorized allow-list still bounds all results. An in-process scoped search without its detached search snapshot refuses global-index fallback. Incomplete search proof on public reads returns exactly `{"facts": []}`; other reads preserve their empty operation envelope without partial totals or raw errors.
- Fact text, temporal buckets and any-readable-source mixed facts remain intact; citation scrubbing follows eligibility. Reads do not create a missing graph or write an intent log. These projections preserve local unscoped reads, O4 stored rows, O8 external-id limitations and remaining provenance exceptions. D9.1 = A and D9.2 = A remain LOCKED, including accepted quota deferral caused by actually blocked readable work.


### Scoped KG search cost

- Authorized snapshot copy and temporary lexical indexing use O(facts + entities + sources + text bytes) space and work linear in the copied corpus, plus the existing candidate SQL and ranking costs. There is no hard corpus row cap. The 50-key seed cap controls candidate semantics, not corpus size; a deadline returns empty rather than partial facts.
- Synthetic worker-side measurements (median of three, queries `Acme`, `uses`, `uses Acme`): 1,968 facts took 82.5 ms for metadata enumeration, 518.6–681.3 ms for scoped snapshot/index/search, and 3.8–31.5 ms for local indexed search. At 9,808 facts these were 478.8 ms, 2,563.7–2,924.4 ms and 4.1–181.3 ms respectively. The scoped total includes descriptor revalidation and copying; no palace lock is retained during index construction or scoring.
- These measurements exclude gateway per-row authorization and network round trips, use synthetic data, and depend on runtime load. They describe cost rather than a latency guarantee or merge threshold. Performance does not justify truncating authorized enumeration. Cooperative worker expiry bounds work, and lock/deadline regressions verify cancellation and cleanup.

### Search enrichment compatibility note

The public search response deliberately omits `facts_error`, including on failed fact enrichment. A failure-only marker could distinguish hidden-present and hidden-absent worlds and break response equality. Failed enrichment therefore preserves drawers and returns the same empty facts shape as no eligible facts. The Unreleased changelog records this public-shape change, strict KG search booleans and restored argument/wing-filter compatibility; clients should no longer use `facts_error` as an availability signal.
### Bounded memory for overview reads

Status, wing, room and taxonomy overviews retain backend facet aggregates when
available, including pgvector, qdrant and exact/compact when their fast path is
unavailable. Facet acquisitions have explicit distinct-key limits; bookkeeping
subtraction uses a fixed number of room facets per wing: registry, sentinel
union and their overlap. Only the bookkeeping facets' returned rooms are expanded;
ordinary rooms do not cause additional facet calls. Missing rooms are reconciled
from three global wing-keyed bookkeeping facets over those same predicates.
Global room facets supply the wingless room remainder; two additional scalar
facets (registry ingest_mode and non-registry sentinel is_sentinel) reconcile
missing wing keys without double subtraction or string/numeric flag confusion.
The exact/compact SQL filter pushes scalar $ne with an explicit NULL arm so
missing ingest_mode keys remain eligible for the disjoint sentinel count.
Facet taxonomy retains its existing explicitly keyed-wing enumeration; wingless
bookkeeping cannot subtract from a literal wing named "unknown". Boolean and legacy string
sentinel aliases share one union predicate, preventing duplicate subtraction.
Taxonomy per-wing facets use at most eight concurrent calls, releasing each map
after reduction. With W explicitly keyed wings, the unscoped facet-call bounds
are status <= 9 + 3W, list_wings <= 8 + 3W, list_rooms <= 9 + 3W, taxonomy
<= 8 + 4W, plus at most 2 calls when wingless rows exist. This conservative
baseline includes the three global room facets even without wingless rows.
These count facet calls, not backend requests or elapsed time. From backend
code, sqlite_exact executes two SQL statements per facet (collection lookup
and aggregate); pgvector and qdrant perform one existence probe per facet in
addition to its aggregate request. Live remote costs have not been measured.
Up to eight completed in-flight maps can transiently retain eight times 8 MiB,
plus the separately bounded tally; the 8 MiB map limit is not a combined
transient limit. Qdrant membership unions use homogeneous string/integer
MatchAny groups, single boolean matches and numeric equality ranges, keeping
bookkeeping filters server-side without mixed-type MatchAny bodies.

Only `UnsupportedCapabilityError` in bookkeeping acquisition preserves ordinary
facet results with `partial: true` and
`bookkeeping_error: bookkeeping_unavailable`, including unsubtracted bookkeeping.
Any other `BackendError`, including transient service or closed-backend errors,
returns `overview_unavailable` in the normal refusal envelope with no drawer total
and no aggregates. A transient error cannot publish inflated counts as partial.
Live Qdrant/pgvector facet support remains unverified; local wrapper tests use
isolated test clients and do not establish live payload-index/filter parity.

Two public count corrections are deliberate: facet overviews subtract legacy
string sentinels (`"true"` and `"1"`) as well as boolean flags, and explicit
facet limits replace the previous default-1000 silent truncation. Counts are
complete within the configured aggregate bounds; over-cap acquisitions refuse
explicitly rather than publishing truncated totals.

Missing-room registry/sentinel rows and rows without either locus key are
also excluded from facet counts. Pre-existing sqlite_exact fast-path handling
of the string "yes" and scoped first-page facet probe fan-out are unchanged.

Non-facet raw fallback uses a presence-preserving projection of wing, room,
is_sentinel and ingest_mode. Chroma reads these fields in one recovery-checked
SQLite snapshot, including a drawer without any projected key. The reader also
supports a deduplicated indexed wing scope. Room alias discovery and room counts
reuse one bounded aggregate pass, avoiding a second projected scan. Unrelated
metadata and documents never enter this acquisition. General keyed metadata
iterators keep their previous semantics.

Projected batches contain at most 500 rows and 1 MiB of canonical UTF-8 JSON
(compact separators, ensure_ascii=False). Complete rows split a batch before its
byte limit. SQL checks cell lengths before decoding; a projected cell over
64 KiB is refused. Aggregates have at most 16,384 distinct wing/room pairs and
8 MiB of deep sys.getsizeof container/key/value accounting, counting shared
objects once. Conservative preflight may refuse earlier; one bounded dictionary
insertion's allocation overhead can precede the final accounting check. These
are allocation-structure bounds, not a whole-process RSS cap. The primary grouped
Chroma/exact/compact path also enforces these caps: over-cap palaces now receive
an explicit diagnostic instead of an unlimited public overview. Facet result
maps are checked under the same aggregate/key limits; they are backend
aggregates, not the byte-bounded raw metadata acquisition contract.

Only one bounded taxonomy cache is retained, keyed by palace, collection and
database fingerprint, including only `overview_too_large` refusals; transient
`overview_unavailable` errors are never cached. Writes, reconnects and a changed
or lost fingerprint invalidate successful and refused entries alike. The
existing freshness TTL applies only while both cached and current fingerprints
are absent. Raw overview metadata is never cached.

A lazy SQLite acquisition error closes the failed snapshot and discards its
entire partial tally. The same byte-bounded projected reader is reopened once
from the beginning with identical where. A second acquisition failure, including
persistent MetadataRecoveryPending, returns explicit `overview_unavailable`.
This read-only retry can recover a transient failure; it does not apply queued
Chroma writes or call an unbounded SDK reader. There is no invented Chroma byte
capability. A non-facet backend without a bounded projected reader is refused
without calling get; no production backend advertises a byte-bounded page adapter.
Unsupported non-facet raw acquisition returns `overview_unavailable`; oversized
cells/aggregates return `overview_too_large`. These refusals carry the normal
empty aggregate keys and `partial: true`; status keeps its protocol/backend
fields and uses a null total. No partial tally is published as complete.

This fixes a measured fallback defect. Production worker memory attribution
remains open. Other full metadata consumers, including delete-by-source and the
palace-graph fallback, and model/vector allocations remain separate candidates.
Storage format and verbatim drawer retrieval are unchanged.
### Outgoing provenance aliases and coherent ranges

Outgoing memory responses clean `source.thread` and `source_kept.thread` bundles through the existing workspace provenance authority, including readable private-chat source identities. Missing, malformed, hidden or unproved token parents omit the nested bundle; non-null flat `source_seq_start` and `source_seq_end` are omitted only when the provenance pass leaves `source_thread` absent or None. Token-free flat parents and ranges retain their existing wing-policy semantics (the accepted external-id residual); token-free nested bundles retain their existing wing-policy semantics. Readable bundles and verbatim drawer text are retained, including idempotent retries and independent batch siblings. Memory conversion consumes this cleaned metadata. Projection never changes stored bytes or adds a write refusal for old provenance. Typed citations and token-free plain `source_file` proof retain their existing semantics. Non-dict nested `source`/`source_kept` values retain their existing behavior: no current producer emits these shapes, and their potential token exposure remains a residual.

Alias-only nested bundles use `source_thread` when `thread` is absent and require the same provenance authority, including token-free compatibility. Only provenance-shaped nested bundles containing `thread`, `seq_start`, `seq_end` or their `source_` aliases are scrubbed. Tunnel wing/room endpoints without a drawer id remain unchanged; source and target endpoints carrying `drawer_id` require a current readable drawer association or the entire endpoint is omitted. A pre-existing residual remains in KG projection: a readable private-source primary can be omitted when a readable native `thr_` secondary is present; this slice does not change that behavior.

Accepted endpoint residual: on non-Chroma backends, a readable chunked drawer's logical handle omits its endpoint because association proof supports exact physical ids only, even when get reconstructs the drawer. This inherits the limit in “Current typed provenance association proof” above (logical handles lack ordered snapshot enumeration); no broader proof is added here.

Dependent non-null sequence values are omitted when their parent provenance is missing, hidden or unproved; earlier gateway expectations that retained orphan ranges are updated to this rule.

Tunnel reads also inherit the earlier whole-record tunnel projection: malformed, unresolved or non-Chroma logical drawer descriptors can remove the entire tunnel before this final endpoint scrub.

### Authorized room and status count responses

- Only `mempalace_list_rooms` and `mempalace_status` replies produced by the
  gateway's `respond` path skip the final generic mirror/provenance gate.
  Their room/wing maps already come from complete request-local wing and
  thread authorization plus descriptor revalidation; imported room listings
  retain their existing consented-reader proof.
- This keeps visible labels such as `source_file` or `origin_id` as count-map
  keys and preserves room/wing sums. Hidden pairs are excluded before counts;
  incomplete proof still yields zero visible counts and maps.
- Tunnel replies, drawer records, every other tool and ordinary forwarded
  replies keep the generic gate. Provenance scrubbing and typed-association
  semantics are unchanged, including omission of malformed scalar citations.
- Broader semantic record/count-map collisions and search/duplicate vector
  diagnostics remain separate work.

## Person private-read containment

Person sessions derive private read authority only from the exact current
workspace owner role. A missing, omitted or unexpected role grants no private
read scope. Membership alone is insufficient. The admin callback eligibility
reader uses that same role-aware policy. No stored membership or data migration
is involved; owner policies retain their existing scopes and behavior.

Members temporarily cannot read any private wing, including their own private
notes, private chats, thread mirrors and diaries. Workstation wing aliases that
require every wing, private-sensitive facts, secret/unwinged facts and the map's
people overlay are also unavailable. Existing input-authorized projections retain
shared overview, graph, KG, tunnel, hallway and drawer listings without hidden
labels, ranks or counts. Wildcard policies can reach those projections without
private scope; literal-wing policies keep their existing tool visibility.

For principals without every-wing access, CLI-compatible search acquires structured
hits and renders only after the same drawer gate as structured search: room and
provenance checks; body text is not scanned. This applies with a literal wing and
with no wing. Both CLI paths use the same pure formatter. The gateway explicitly
renders flat rows, so import-authorization metadata cannot replace a
readable row's wing, room or source; the owner renderer retains its metadata mode.
Restricted CLI responses contain only query, CLI text, an empty results array,
optional searched wings and
non-empty facts; structured search also omits acquisition counts and truncation
flags that could include hidden candidates. Every-wing policies retain those
diagnostics and their existing CLI output.

Content placed in an ordinary shared room is visible on every route. Body text
and arbitrary provenance labels are not scanned for private words or fingerprinted;
recognized protected references in source_file, source_path and media path/title
aliases are still checked.

Provenance containing control or format characters (for example a tab or zero-width
space) is omitted even for ordinary paths. This remains fail-closed because hidden
or encoded characters can make reference interpretation ambiguous; readable drawer
text is retained without rewriting the path to guess its meaning.

Unprojected whole-palace operations remain unavailable to members: mesh estate,
duplicate detection, checkpoint, source-wide deletion, tunnel/hallway deletion,
memories-filed-away, and low-level coordination event/artifact/patch operations.
They have the same unknown-operation refusal as unavailable operations. Explicit
private reads remain independent of private data presence. This containment does
not define a restricted estate response or restore filer-specific private reads.

### Event, write and durable-token boundaries

Non-owner person event feeds use the same current-principal visibility as
webhooks: boardless events require every-wing access, and board events require
a readable active board (space messages retain coordinator checks). Their feed
and cursor are projected onto visible events; hidden events do not change the
member feed response. Members consequently lose boardless
live Memory refresh signals. Card and board linked-memory state can remain stale
until the next board event or an explicit refresh. Owner feeds retain their
original ordered log.

Each member poll snapshots readable active boards once and filters a bounded
SQL page against that set. The opaque epoch hashes the member role and readable
board identities, never hidden activity. A missing or changed epoch, or a cursor
above the projected latest, returns `reset: true` without replaying events. The
app refetches the open screen, including an open card, before adopting the reset
cursor and epoch. Ordinary pages advance only through returned events. An owner
request carrying a non-empty member-era epoch also returns a reset with no
events, adopting the latest raw cursor before resuming the owner feed.

Deploy note: open member tabs from before this deploy stop receiving live events
until they reload. Reload those tabs after upgrading to resume live updates.

Webhook subscriptions whose effective principal lacks `policy.sees_all_wings`
receive no numbers: payload `seq` and delivery-list `event_seq` are omitted.
Their payload id is `evt_` followed by the first 32 hexadecimal characters of
HMAC-SHA256 of the raw event sequence, keyed by an independent server-only
per-subscription key. The disclosed signing secret cannot derive those ids.
A restricted retry keeps the same opaque id for the same event.
The internal `(webhook_id, event_seq)` key still deduplicates enqueue attempts.
There is no subscription counter, counter column or counter backfill. One
nullable key column is initialized atomically on first restricted use, persists
across restarts and is never returned by any API or logged. No operator setting
or manual data setup is required. Subscriptions with `sees_all_wings` retain the
raw sequence and id. Enqueue, send and listing resolve the subscription's current
effective authority, including owner-held restricted tokens; an unresolvable
principal is restricted. Send rechecks queued payloads after authority changes.
Subscription lookup exceptions log one diagnostic with exception type and stack
locations, excluding exception text and key material; admission still fails closed.

Writes into unreadable private wings remain allowed, but duplicate drawer
retries return the same successful shape as fresh writes. Replay fields and
retained source provenance are hidden; returned structural fields depend only
on the supplied request and current chunk configuration. Readable-owner retries
still report already_exists. Tunnel creation additionally requires both endpoint
wings to be readable for principals without every-wing access, and outgoing
write results pass the wing scrubber. Denial happens before any private upsert.

Token issuance refuses private read scope for a holder who is not a workspace
owner. PAT, durable subscription connection and OAuth authentication strip both
private read and admin scopes unless current membership is exactly owner;
missing membership rejects authentication. Stored policies are not rewritten.
Member PATs minted earlier with private:read silently lose it when loaded.

Health-flagged member notes are always saved privately when private writing is
allowed; they are never redirected to shared storage. The successful HTTP save
response includes a notice, and the MCP save text states: "Saved privately. You
cannot read this note for now." This also applies to other saves into unreadable
private wings. The notice uses the actual filed destination after write-wing
pinning; readable shared or inbox saves do not carry that notice. A save that
succeeds privately but fails to link to a card includes the same notice in its
saved-but-not-linked error.
