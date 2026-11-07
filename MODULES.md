# Modules

Who owns each path, what it promises, how it fails and which tests hold it to
that. `SYSTEM.md` holds the cross-module decisions and residuals.

Format (read by `commonplace orch docs-check`):

- One `## <module>` block per module. Each block has exactly the five fields
  **Paths**, **Owns**, **Promises**, **How it fails** and **Tests**; a missing
  or empty field fails the check.
- **Owns**, **Promises**, **How it fails** and **Tests** use indented
  sub-bullets, separated by blank lines. Keep one topic or test area per bullet.
  Add a NEW sub-bullet next to its related topic instead of extending an existing
  line or appending every change at the end of the field. Separate insertion
  positions let independent topics merge; concurrent edits at the same position
  can still conflict. Inline fields remain accepted for older maps.
- **Paths** lists repository paths in backticks, separated by commas. A
  directory ends in `/`. There are no wildcards.
- Every entry of the repository root and of `commonplace/` is listed exactly
  once. A path belongs to the block that lists its longest matching entry, so a
  more specific path (for example `docs/runner.md`) may be listed in another
  block than its directory (`docs/`). `commonplace/` itself is split into its
  entries.
- A change that touches a module's paths updates that module's block, unless a
  `Docs-waiver` from `SYSTEM.md` applies.

## system-docs

- **Paths:** `SYSTEM.md`, `MODULES.md`
- **Owns:**

  - The system-wide decisions, numbers, stores, residuals and waiver list (`SYSTEM.md`) and this ownership map.

- **Promises:**
  - Thread mutation rules and accepted provenance/read residuals are recorded below as separate additions.

  - Imported-history consent: Imported-history reader authority is recorded as an explicit consent boundary, with shared-holder and withdrawal residuals.

  - Every root and `commonplace/` entry has exactly one owner block with all five fields; `SYSTEM.md` changes with every change that `docs-check` covers, unless a base-listed waiver applies.

- **How it fails:**

  - A stale block passes the mechanical check while describing old behaviour; reviewers judge whether the text is true.

  - A path added without a block fails `docs-check`.

- **Tests:**

  - `tests/test_docs_obligations.py` runs `docs-check` against the merge base; `tests/test_orch_checks.py` covers unowned paths, missing obligations, empty blocks and waivers.


## project-docs

- **Paths:** `AGENTS.md`, `CHANGELOG.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `HUB-DESIGN.md`, `INSPIRATION-REPOS.md`, `LICENSE`, `MISSION.md`, `PRODUCT-DESIGN.md`, `README.md`, `REPORT.md`, `ROADMAP.md`, `SECURITY.md`, `openarena-claim.txt`, `design/`, `docs/`
- **Owns:**

  - Contributor and agent instructions, the mission and design notes, user and operator documentation, release history, the licence and the security policy.

- **Promises:**

  - Imported-history consent: Imported-history documentation distinguishes write-only import from the independent reader scope and explains exclusions and revocation.

  - Documentation describes shipped behaviour; design notes are marked as proposals.

  - Project documentation explains opt-in start authority, shared admission, seat consent and the card-webhook delivery residual.

  - Public text uses synthetic names only and carries no secrets or private hosts.

  - Board design also documents coordinator release of restricted-origin cards.

- **How it fails:**

  - Text drifts from code silently; nothing executes most of it.

  - Broken commands in docs surface only when someone follows them.

- **Tests:**

  - `tests/test_commonplace_public_surface.py`, `tests/test_commonplace_legal_pages.py` and the documentation checks in `tests/test_commonplace_release_gate.py`.


## build-and-ci

- **Paths:** `pyproject.toml`, `uv.lock`, `.python-version`, `Cargo.toml`, `Cargo.lock`, `crates/`, `.gitattributes`, `.gitignore`, `.pre-commit-config.yaml`, `.github/`, `.devcontainer/`
- **Owns:**

  - Python and Rust packaging, pinned dependencies, the coverage gate, lint and format settings, pre-commit hooks, repository metadata and the development container.

- **Promises:**

  - `uv sync --extra dev` reproduces the locked environment; `ruff check` and `ruff format --check` define style; the coverage threshold in `pyproject.toml` is the gate.

- **How it fails:**

  - A lock that drifts from `pyproject.toml` breaks installs; a lowered threshold or a widened lint ignore weakens every other module's tests without any test failing.

- **Tests:**

  - `tests/test_version_consistency.py` and the full suite under the configured coverage gate.


## container-and-deploy

- **Paths:** `Dockerfile`, `Dockerfile.gpu`, `docker-compose.yml`, `docker-entrypoint.sh`, `.dockerignore`, `deploy/`, `backup-hook.sh`, `nightly-backup.sh`
- **Owns:**

  - Container images, compose files, example reverse-proxy and service units, and the nightly backup wrappers.

- **Promises:**

  - Images run the gateway as an unprivileged user on a mounted data directory; examples hold placeholders, never real hosts or secrets; deploy holds an active transient unit reference through fresh invocation, snapshot and backup verification, then releases it, including on trapped exits.

- **How it fails:**

  - Missing execution proof, stale snapshots, failed verification or reference creation/release refuse deployment; SIGKILL can leave a transient reference holder for operator cleanup.

  - A broken image or unit only shows at deploy time.

- **Tests:**

  - `tests/test_commonplace_deploy.py`, `tests/test_commonplace_nightly_backup.py`, `tests/test_commonplace_w5_backup.py`.


## scripts-and-tools

- **Paths:** `scripts/`, `tools/`, `benchmarks/`
- **Owns:**

  - Maintenance and release scripts, review tooling, evaluation scripts and benchmarks.

- **Promises:**

  - Release email login waits for the accessible signed-in Board region after identity checks; step failure details retain exception messages through the shared credential redactor.

  - Import release checks retain owner listing and exact read-back and require the write-only bearer to receive the not-found response without metadata.

  - Scripts are run by hand or by the release process; none runs on import or ships in the package.

  - Benchmarks are informational and never gate a change.

- **How it fails:**

  - A script can rot unnoticed between releases; benchmark numbers vary with the machine.

- **Tests:**

  - `tests/test_commonplace_release_gate.py`, `tests/test_backfill_authored_at.py`; benchmarks run under the `benchmark` marker only.


## agent-integrations

- **Paths:** `.agents/`, `.antigravity-plugin/`, `.claude-plugin/`, `.codex-plugin/`, `.cursor-plugin/`, `.dsh-plugin/`, `.mcp.json`, `mcp.json`, `commands/`, `hooks/`, `integrations/`, `skills/`, `rules/`, `examples/`
- **Owns:**

  - Plugin manifests, slash commands, hook scripts, editor rules, packaged skills and integration examples for coding agents.

- **Promises:**

  - Hooks fail open and stay within the hook time budget; manifests reference files that exist; no integration enables an external provider by default.

- **How it fails:**

  - A manifest that points at a moved file breaks installation for that agent only; a slow hook delays the user's session.

- **Tests:**

  - `tests/test_claude_plugin_hook_config.py`, `tests/test_claude_plugin_hook_wrappers.py`, `tests/test_antigravity_plugin_manifest.py`, `tests/test_claude_marketplace_skills.py`, `tests/test_hooks_cli.py`.


## sites

- **Paths:** `landing/`, `website/`, `assets/`
- **Owns:**

  - The landing page, the documentation website and shared images.

- **Promises:**

  - Static content only; no tracking and no external calls beyond what the page states.

- **How it fails:**

  - Broken links or layouts show only in a browser.

- **Tests:**

  - `landing/check.mjs` and the website build.


## mempalace

- **Paths:** `mempalace/`, `seed/`
- **Owns:**

  - The memory engine: verbatim drawers, mining, search, knowledge graph, storage backends and the MCP server; plus the reviewed seed card list.

- **Promises:**
  - Scoped KG writes recheck provenance snapshots in-lock; source/id deletes use authorized rooms and explicit ids, and missing optional closets count as zero.

  - Direct B2 providers reject malformed metadata with encryption errors; marked-workspace refusals retain an explicit locked diagnostic.

  - Operator-blind token-slot primitives accept strict versioned token/grant envelopes only; opt-in B3 metadata stays locked before any B2 key provider or database open.

  - User content is stored verbatim; ingest is append-only; search returns original words; nothing leaves the machine unless the user configured a provider.

- **How it fails:**

  - Backend lock or index damage degrades recall; repair tools rebuild indexes without touching drawers.

  - When a session's cached database disappears during a rebuild, the session resets the palace's shared Chroma System as well as its client so dropped owners cannot leave a pool pointing at the renamed database.


- **Tests:**

  - The `tests/test_` files for mining, search, palace, knowledge graph, backends and the MCP server.


## tests

- **Paths:** `tests/`
- **Owns:**

  - `tests/test_commonplace_subagent_asks.py` covers released-card questions and coordinator routing, answers, escalation and recovery.

  - Shared fixtures and harnesses and every test file not listed in another block.

  - Subagent question routing, proposal/release prerequisites and recipient authority regressions.

- **Promises:**

  - The revoked-token socket guard observes the completed server-side 401, no body reads and no route dispatch, including a forced close before upload finishes.

  - Lease regressions retain started jobs beyond an hour while their cards remain doing with the machine worker, and release capacity on completion, reassignment or a terminal job state without taking a boards write lock.

  - Tests use temporary directories, loopback servers and synthetic names; they make no external network calls.

  - Deploy fakes discard inactive, unreferenced execution proof and cover synchronous fast exit, consecutive deploys and reference cleanup on failure, timeout and signals.

  - Restricted-origin content-edit regressions verify audit attribution and unchanged release and claim state through both public routes.

  - Restricted-origin replay tests verify inactive creation, coordinator release and owner fallback.

  - Inactive status annotations keep release gates unchanged; notification fixtures use unrestricted workers for unrelated feature scenarios.

  - Functional rules-injection checks isolate scheduler time, and concurrent handoff fixtures admit the full request burst.

  - Privacy near-miss correctness runs without a clock; its unchanged 20 ms average budget runs separately under the `timing` marker.

  - The 500-rule initialization checks separate deterministic truncation behavior from the unchanged informational p95 budget; a fake-clock regression covers empty injection after deadline expiry.

  - Imported startup recall freezes logical expiry and uses the functional worker allowance; a delayed real search regression retains ACL filtering and exact JSON-quoted imported text.

  - Runner-start void tests cover both orchestration switches, retired keys, lease/start ordering, rollback, capacity and owner authority.

  - Owner summary edits freeze the injection clock; forced-expiry coverage preserves exact injection, accepted status and unchanged rule sequence.

  - Shared-proposal acceptance freezes the injection clock; a forced-expiry regression exercises the functional test and checks verbatim delivery to both agents after owner acceptance.

- **How it fails:**

  - A plan fixture that names a missing test file fails G7; a shared fixture change can break unrelated suites; slow or flaky tests waste every run.

- **Tests:**
  - Thread-boundary integration tests exercise real separate workspace workers; focused scope tests cover stale snapshots, optional collections, malformed probes, decoding and response caches.

  - Release helper regressions simulate the old app disappearing before the signed-in board arrives and verify useful exception messages without credentials in JSON or Markdown reports.
  - Marked backup classification and snapshots use live synthetic B2 controls; direct-provider and version guards have observed base AssertionError regressions.

  - Synthetic token-slot proofs bind every envelope context field, reject operator/recovery wraps and test marked-workspace connection and backup refusal with an unmarked SQLCipher control.
  - Import boundary fixtures use the real Recall authorization dependency while retaining frozen continuity doubles and control-count assertions without holder reads.
  - Imported-history consent: Synthetic reader journeys check unticked consent, unchanged old credentials, bounded shared holders, not-found refusals and source-range proof.

  - The suite itself under the coverage gate; `tests/test_commonplace_webhooks.py::test_dispatcher_thread_survives_an_unexpected_pass_error` checks that an unexpected delivery-pass error is logged without its private message and the dispatcher retries while staying alive.

  - `tests/test_projects_phase_b.py` and `tests/test_projects_admission.py` keep paired pin/head assertions and independent runner-lock coverage.

  - `tests/test_backup_online_enumeration.py` forces SQLite sidecar cleanup and the writer/workspace-lookup freeze interleaving, and checks that missing user files, unsafe paths and new directory creation remain guarded.

  - `tests/test_commonplace_rules_inject.py::test_locked_store_uses_remaining_deadline` checks locked-store fallback and remaining connection/statement waits with a fake clock; the separate timing test retains the informational wall-clock budget.

  - `tests/test_chroma_client_lifecycle.py::test_session_evicts_dropped_system_before_opening_missing_database` checks eviction before client construction without depending on SQLite pool scheduling; the external-rebuild probe checks the replacement's exact drawer ids.


## hub-core

- **Paths:** `commonplace/__init__.py`, `commonplace/__main__.py`, `commonplace/gateway.py`, `commonplace/control_db.py`, `commonplace/auth.py`, `commonplace/policy.py`, `commonplace/tokens.py`, `commonplace/extensions.py`, `commonplace/_http.py`, `commonplace/http_util.py`, `commonplace/urls.py`, `commonplace/ratelimit.py`, `commonplace/workers.py`
- **Owns:**

  - The gateway (the only way into a workspace), the control database, principals, token policy, the extension seam and one memory worker per workspace.

- **Promises:**
  - Gateway mutation scopes and provenance lookups follow the principal workspace; hidden targets use absent replies, and client-supplied scopes cannot widen access.

  - Imported-history consent: Imported reader results bypass wing restrictions only after exact-view proof; ordinary scope, tool, wing and mutation gates remain in force.

  - Every request resolves to one principal or none; scopes, wings and trust are checked before any tool runs; tokens are stored hashed; writes are audited.

  - Existing workspace directory lookups pass a backup freeze, so a writer holding a database transaction can finish; new directories remain gated.

- **How it fails:**

  - A worker that does not start returns an error, never another workspace's data; the control database is the single point of failure for sign-in.

- **Tests:**

  - `tests/test_commonplace_gateway.py`, `tests/test_commonplace_control_db.py`, `tests/test_commonplace_policy.py`, `tests/test_hub_` files.


## sessions-and-sign-in

- **Paths:** `commonplace/sessions.py`, `commonplace/session.py`, `commonplace/email_auth.py`, `commonplace/mailer.py`, `commonplace/oauth/`, `commonplace/social/`
- **Owns:**

  - Web sign-in links and cookies, startup context reads, email challenges, outgoing mail, OAuth for MCP clients and provider sign-in.

- **Promises:**

  - Imported-history consent: OAuth reader consent is separate and unticked; defaults and person policies exclude the reader, while refresh descendants cannot regain a dropped scope.

  - Accounts exist only after verification; sessions are cookie-bound and revocable; mail is configured only through the environment.

- **How it fails:**

  - Mail outages block new sign-ins but not existing sessions; a misconfigured provider fails closed.

- **Tests:**

  - `tests/test_commonplace_email_auth.py`, `tests/test_commonplace_oauth.py`, `tests/test_commonplace_social.py`, `tests/test_commonplace_session.py`.


## cli-and-client

- **Paths:** `commonplace/cli.py`, `commonplace/client.py`, `commonplace/client_hooks.py`, `commonplace/client_install.py`, `commonplace/bridge.py`, `commonplace/onboarding.py`, `commonplace/seed.py`, `commonplace/README.md`, `commonplace/SETUP.md`, `commonplace/agent-skill/`
- **Owns:**

  - The `commonplace` command line, the credential-scoped client, native hooks and installers, the stdio bridge and the agent skill.

- **Promises:**

  - Imported-history consent: CLI token creation rejects the imported-history reader scope before issuing a credential; browser consent is required.

  - CLI commands act through the same services as the network routes; installers never change trust policy; hooks fail open.

- **How it fails:**

  - The CLI runs with data-dir access, so anyone with the service's OS user can use it (residual in `SYSTEM.md`).

- **Tests:**

  - `tests/test_commonplace_cli.py`, `tests/test_commonplace_client.py`, `tests/test_commonplace_client_hooks.py`, `tests/test_commonplace_client_install.py`, `tests/test_commonplace_onboarding.py`.


## boards

- **Paths:** `commonplace/boards/`, `commonplace/events.py`, `commonplace/webhooks.py`, `commonplace/holds.py`, `commonplace/callstack.py`
- **Owns:**

  - Boards, cards, the append-only `task_events` log, approvals and decisions, the hub event log, signed webhooks and delayed person approvals.

- **Promises:**

  - Every card change is one transaction with its `task_events` row; event emission happens after commit and a failed emission never undoes the change; on a configured board only `task_approve` removes `plan` and plan-check gates plan approval.

  - Trusted agents and authorized board coordinators may edit only the title/body of unassigned restricted-origin todo cards without releasing the origin or changing claim authority.

  - With workspace orchestration enabled, claims hold runner admission through board commit and person assignments or handoffs cannot reserve a busy agent.

  - Subagent and restricted-agent submissions remain inactive until coordinator release, with owner fallback only when no coordinator is live.

  - Holders may annotate an inactive status line without activation.

  - Subagent asks on released cards reach the live board coordinator, which may answer or escalate with a reason; absent a live coordinator the owner route remains, including asks already routed before a coordinator becomes unavailable or is replaced.

- **How it fails:**

  - A swallowed emission loses a notification but not the change (the trigger sweep reads `task_events`); webhook delivery retries and records failures.

  - Each unexpected dispatcher-pass exception is contained and logged without its private message, then waits on the stop event for 5 s, doubling after consecutive errors up to 300 s, before continuing.

  - A successful pass resets the backoff; stopping interrupts the wait, and a closed database still ends the dispatcher.

- **Tests:**

  - `tests/test_commonplace_boards.py`, the `tests/test_commonplace_board_` files, `tests/test_commonplace_decide.py`, `tests/test_orch_plan_gate.py`, `tests/test_commonplace_webhooks.py`.

  - `tests/test_commonplace_subagent_asks.py` checks question routing and recipient authority.


## runner

- **Paths:** `commonplace/runner/`, `docs/runner.md`
- **Owns:**

  - Machine enrollment and grants, runner jobs in `runner.sqlite3`, the requester and machine routes, the local launch client and chat answers.

- **Promises:**

  - Started jobs retain machine in-flight capacity and per-machine project exclusion while their cards remain doing with the machine worker; a terminal job state or a changed card state or assignee releases capacity, using read-only boards access.

  - A start needs a person owner or a listed trusted PAT and a card the worker could claim; one active start per card; an uncertain launch is never re-queued; on a configured board `card_start` is person-owner only, starts are fenced and capped, and the lease payload carries the pinned message and model.

  - Opt-in orchestration requires the active coordinator for unconfigured PAT requests, reserves project capacity when starts are queued, and admits leases using active slots under the shared runner lock.

  - Owner/local step void cancels only queued or leased jobs atomically through cancellation logic; standalone cancel authority and launch-state handling stay unchanged.


- **How it fails:**

  - A lost launch result becomes `uncertain` and waits for a person; a revoked machine cancels queued jobs on the next sweep; a busy queue refuses with `busy`.

- **Tests:**

  - The `tests/test_runner_` files, `tests/test_orch_runner.py`, `tests/test_orch_acceptance.py`.


## orch

- **Paths:** `commonplace/orch/`, `docs/orchestrator.md`, `tests/_orch_helpers.py`, `tests/test_orch_acceptance.py`, `tests/test_orch_schema.py`, `tests/test_orch_store.py`, `tests/test_orch_durable.py`, `tests/test_orch_runner.py`, `tests/test_orch_surfaces.py`, `tests/test_orch_checks.py`, `tests/test_orch_plan_gate.py`, `tests/test_docs_obligations.py`
- **Owns:**

  - The planner grammar, including leading numeric labels and repository-relative existing test-file mappings; the orchestrator config format and its versions and experiments, config-driven starts, triggers, leases, the journal, resume, the restore flag and acknowledgement, `plan-check` and `docs-check`.

- **Promises:**

  - Only owner-accepted versions run; every config-driven start is one fenced, capped transaction; a retry returns the stored job; external effects are never re-issued by the hub; after a restore nothing config-driven runs until the owner acknowledges.

  - Configured starts share project admission with claims and plain jobs when workspace orchestration is enabled; boards without a `[runner]` config retain earlier behavior with the workspace switch off.

  - The stored-job retry above applies only to unvoided steps.

  - Voided runner-start keys are retired with `step_voided`; history and linked job statuses remain visible, and a new key needs normal admission.

  - First void and queued/leased cancellation share one transaction; repeats preserve actor/time and one journal event while tools emit per-call feed notifications.


- **How it fails:**

  - Refusals become coded misses shown on resume; an unproven external effect becomes `needs_person`; infrastructure errors leave the trigger cursor in place for the next sweep.

  - Void cannot stop launched threads or prove completion; existing active/recent-start and per-switch capacity guards still apply, and a pending todo trigger may run on a later sweep.


- **Tests:**

  - The `tests/test_orch_` files listed in Paths and `tests/test_docs_obligations.py`; R22 in `tests/test_orch_checks.py` covers misplaced labels, missing files, accepted label forms and repository-root resolution.

  - R1, R1b, R1d, R1e, R8b, R10a and R11e map to `tests/test_orch_acceptance.py`.


## projects

- **Paths:** `commonplace/projects/`, `commonplace/plans.py`
- **Owns:**

  - Project orchestrator seats, project model choices, project limits, project wings and optional hosted plans.

- **Promises:**

  - Only a signed-in owner changes models or limits; `max_agents` counts distinct connection assignees with orchestration off; opt-in orchestration shares card/job admission accounting, default cap 3, owner-only settings and atomic seat trust; model resolution goes project, then workspace, then the runner default.

- **How it fails:**

  - An unresolved model refuses a config-driven start before insert; a limit change does not stop work already started.

- **Tests:**

  - The `tests/test_projects_` files, `tests/test_commonplace_plans.py`.


## rules-and-guards

- **Paths:** `commonplace/rules/`, `commonplace/guards/`, `commonplace/privacy.py`, `commonplace/redact.py`
- **Owns:**

  - Owner-reviewed rules and their freshness gate, guard building blocks, sensitive-text flags and redaction.

- **Promises:**

  - Rules take effect only after owner review; writes from a client with stale rules get 428; flags are deterministic.

- **How it fails:**

  - Redaction can miss a sensitive detail; flags are hints, not blocks.

- **Tests:**

  - `tests/test_commonplace_rules.py`, `tests/test_commonplace_rules_enforce.py`, `tests/test_commonplace_privacy.py`, the `tests/test_guards_` files.


## handoff-and-continuity

- **Paths:** `commonplace/handoff/`, `commonplace/continuity.py`, `commonplace/continuity_read.py`, `commonplace/continuity_schema.py`
- **Owns:**

  - Agent context handoff to a fresh agent and append-only native session continuity.

- **Promises:**
  - Provenance uses one bounded decoder and response-local access caches; extraction by a non-reader leaves the source cursor untouched.

  - Imported-history consent: Imported readers receive bounded immutable views, source-range proof and view-specific metadata; capture tails never inherit import authority.
  - Combined read boundaries: One response access checker retains normalized provenance and private-source authorization alongside import range proof, including deadline-bound startup reads.
  - Ordinary transcript compatibility: Recall checks import identity fields only for imported threads; ordinary policy-only callers retain their read path.
  - Legacy import compatibility: Ordinary reads preserve three-part filer identities without rewriting stored data; reader views still require four-part identities.

  - A handoff never loses the source thread; continuity is append-only and resumable.

- **How it fails:**

  - A failed launch of the successor leaves the source agent running; an interrupted archive adoption resumes from its last committed step.

- **Tests:**

  - The `tests/test_handoff_` files, `tests/test_commonplace_continuity_unit.py`.


## chat-and-voice

- **Paths:** `commonplace/chat/`, `commonplace/voice.py`, `commonplace/phone_push/`
- **Owns:**

  - The workspace chat front agent, server-local dictation and opt-in phone notifications.

- **Promises:**

  - Chat answers come from the configured provider or the poster's own runner, never a silent fallback; audio stays in request memory; push is opt-in.

- **How it fails:**

  - Without a provider or runner the chat shows a receipt and no answer; push delivery failures are recorded, not retried forever.

- **Tests:**

  - The `tests/test_chat_` files, `tests/test_commonplace_voice.py`, the `tests/test_commonplace_phone_push_` files.


## memory-and-search

- **Paths:** `commonplace/memory_tools.py`, `commonplace/search_index_api.py`, `commonplace/store_embed.py`, `commonplace/store_switch.py`, `commonplace/workspace_store.py`, `commonplace/capture.py`, `commonplace/capture_client.py`
- **Owns:**

  - Memory tools and routes under the caller's policy, search index progress, workspace store generations and cutovers, and opt-in chat capture.

- **Promises:**

  - Reads and writes respect the caller's wings and scopes; store cutovers are backup-gated and restartable; capture is explicit and append-only.

- **How it fails:**

  - An interrupted cutover keeps the previous store live; an index rebuild degrades search speed, not stored text.

- **Tests:**

  - `tests/test_commonplace_memory.py`, `tests/test_commonplace_capture.py`, `tests/test_workspace_store_switch.py`.


## imports

- **Paths:** `commonplace/imports.py`, `commonplace/import_grant.py`, `commonplace/import_parts.py`, `commonplace/import_sources.py`, `commonplace/import_tools.py`, `commonplace/ingest_cadence.py`
- **Owns:**

  - Consented one-time imports of conversations and facts and the shared ingest cadence.

- **Promises:**

  - Imported-history consent: Reader listing uses literal account-prefix pagination and live Recall authorization; unconsented agents receive the missing-resource response.

  - Imports are verbatim, bounded and consented; no capture, model or external call is involved.

- **How it fails:**

  - A refused or interrupted import leaves already committed batches in place and reports where it stopped.

- **Tests:**

  - `tests/test_commonplace_import.py`, `tests/test_commonplace_import_grant.py`, `tests/test_commonplace_import_list.py`.


## sync

- **Paths:** `commonplace/sync_cli.py`, `commonplace/sync_daemon.py`, `commonplace/sync_feed.py`, `commonplace/sync_protocol.py`, `commonplace/sync_service.py`, `commonplace/sync_sessions.py`, `commonplace/sync_sources.py`, `commonplace/sync_state.py`
- **Owns:**

  - Opt-in client synchronization of transcripts and memory, its local state and the content-free live feed.

- **Promises:**

  - Sync never opens a server database and never reads credentials; local state holds metadata only.

- **How it fails:**

  - A lost connection keeps the outbox and resumes from the cursor; a corrupt local cache is rebuilt from the server.

- **Tests:**

  - The `tests/test_commonplace_sync_` files.


## backup-and-encryption

- **Paths:** `commonplace/backup.py`, `commonplace/backup_online.py`, `commonplace/backup_supervisor.py`, `commonplace/backup_pause.py`, `commonplace/admission.py`, `commonplace/workspace_encryption.py`
- **Owns:**

  - Offline and online verified backups and restores of the data directory, the gateway pause socket and run fence, the admission gate for store/file creators and the successor registry, the restore marker, and opt-in workspace SQLite encryption.

- **Promises:**

  - A backup copies every SQLite file with a manifest; an online backup runs beside the serving gateway without stopping or replacing it and publishes only after the run is committed.

  - Its enumeration tolerates SQLite sidecars disappearing during commit or connection cleanup, while missing user files and unsafe paths still fail closed.

  - A restore publishes a verified copy into a new directory and never overwrites; the restore marker blocks config-driven work until the owner acknowledges.

- **How it fails:**

  - A damaged backup is refused at verify time; an online run that loses its lease, heartbeat or fence ends invalid and publishes nothing; a live in-container successor refuses the run; a hand-copied restore carries no marker (residual in `SYSTEM.md`).

- **Tests:**

  - `tests/test_backups.py`, `tests/test_backup_snapshot_deadline.py`, `tests/test_commonplace_w5_backup.py`, `tests/test_backup_online.py`, `tests/test_backup_online_enumeration.py`, `tests/test_backup_hook_online.py`, `tests/test_backup_pause.py`, `tests/test_backup_cli_fence.py`, `tests/test_admission_gate.py`, `tests/test_orch_store.py`.


## skills

- **Paths:** `commonplace/skills/`
- **Owns:**

  - Workspace skills exposed through scoped MCP tools and owner-managed routes.

- **Promises:**

  - Skills are versioned; only the owner publishes them; reads follow the caller's scopes.

- **How it fails:**

  - A missing skill version is refused, not silently replaced.

- **Tests:**

  - `tests/test_commonplace_skills.py`, `tests/test_commonplace_skill.py`.


## web-and-console

- **Paths:** `commonplace/web/`, `commonplace/console/`
- **Owns:**

  - The cookie-authenticated web app and the live console, including the owner orchestration switch, project seat-consent controls and default agent-limit display.

- **Promises:**

  - Imported-history consent: The owner form offers independent unticked import and reader checkboxes and displays reader authority beside Revoke.

  - The web app calls the same services as REST; it shows only what the signed-in person may read.

- **How it fails:**

  - Rendering errors surface in the browser; the server state is unaffected.

- **Tests:**

  - The `tests/test_workbench_` files, the `tests/test_console_` files and the `tests/test_commonplace_console_` files.
