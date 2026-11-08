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

  - The canonical Space REST foundation reuses board rows as Spaces, with Board, Memory and Chat views and Board as the default.

  - The system-wide decisions, numbers, stores, residuals and waiver list (`SYSTEM.md`) and this ownership map.

  - The system-wide decisions, numbers, stores, residuals and waiver list (`SYSTEM.md`) and this ownership map, including thread-ranking invariants.


- **Promises:**

  - Documents field-specific citation length errors, atomic whole-write refusal, surface-specific citation exceptions and informational performance measurements.

  - Documents both 16,384-character authorization caps, O(n + C²) bounds, over-limit readable citation refusal and verbatim fact/storage preservation.

  - Documents bounded authorization-only reference spellings, exact producer domains and citation compatibility.

  - Fork proof uses the fixed digest before trailing context and independently proves overlapping embedded references; omitted namespace separators require an actor id marker, and capture/import punctuation remains literal.


  - Records the strict owner-progress authority probe separately from ordinary Recall denial semantics.

  - Records authenticated owner progress, process-lifetime units, fixed unavailable responses and the accepted global-quota causality.

  - Architecture describes exact-slug, workspace-local reads without implying lifecycle support.
  - Thread mutation rules and accepted provenance/read residuals are recorded below as separate additions.

  - Imported-history consent: Imported-history reader authority is recorded as an explicit consent boundary, with shared-holder and withdrawal residuals.

  - Card task-type decisions document owner-only writes, unchanged defaults, grant semantics and the explicit slice 1 residuals.

  - Every root and `commonplace/` entry has exactly one owner block with all five fields; `SYSTEM.md` changes with every change that `docs-check` covers, unless a base-listed waiver applies.

  - **Browser GitHub connection:** Connection documentation records the default-off browser grant boundary and unproven live OAuth behavior.

  - **Connection lifecycle proof:** Records overlap fencing, process recovery proof and the deferred account-deletion integration boundary.

  - **Mutation sensitivity:** Records independently observable security assertions and explicit deferred integration boundaries.

  - **Connection recovery:** Documents expiry reconnect, revocation while disabled, explicit cleanup triggers and the enforced key-location boundary in the current documentation format.

  - **Independent scope evidence:** Keeps mandatory token evidence separate from optional response-header cross-checking in the proof record.

- **How it fails:**

  - An authority fault makes the complete progress response unavailable, even when other threads are readable.

  - A progress count is not a new thread read grant; other thread-boundary residuals remain documented.

  - Later object migration requires an explicit compatibility decision.

  - A stale block passes the mechanical check while describing old behaviour; reviewers judge whether the text is true.

  - A path added without a block fails `docs-check`.

- **Tests:**

  - Owner-route store/private-reader fault regressions and the documentation obligation.

  - Authenticated progress regressions and documentation obligations.

  - Space REST contract tests and documentation obligations.

  - `tests/test_docs_obligations.py` runs `docs-check` against the merge base; `tests/test_orch_checks.py` covers unowned paths, missing obligations, empty blocks and waivers.


## project-docs

- **Paths:** `AGENTS.md`, `CHANGELOG.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `HUB-DESIGN.md`, `INSPIRATION-REPOS.md`, `LICENSE`, `MISSION.md`, `PRODUCT-DESIGN.md`, `README.md`, `REPORT.md`, `ROADMAP.md`, `SECURITY.md`, `openarena-claim.txt`, `design/`, `docs/`
- **Owns:**

  - Live-sync instructions describe transcript-only reinstall, explicit destination and retained disabled memory history.
  - Public Space endpoint and error documentation alongside legacy board routes.

  - Contributor and agent instructions, the mission and design notes, user and operator documentation, release history, the licence and the security policy.

- **Promises:**

  - Live-sync setup makes no active memory-upload or completed-coverage claim.
  - The three canonical endpoints retain the existing authority and verbatim chat contracts.

  - Imported-history consent: Imported-history documentation distinguishes write-only import from the independent reader scope and explains exclusions and revocation.
  - Project setup documentation also explains the recommended card-move webhook and explicit updates for existing subscriptions.

  - Documents enabled blockers, waiting payloads, stable split keys and bounded runner retry delivery.

  - Documentation describes shipped behaviour; design notes are marked as proposals.

  - Project documentation explains opt-in start authority, shared admission, seat consent and the card-webhook delivery residual.

  - Public text uses synthetic names only and carries no secrets or private hosts.

  - Board design also documents coordinator release of restricted-origin cards.

  - Contributor test instructions state that the default run excludes the `timing` lane and that timing results are informational, never a merge gate.

- **How it fails:**

  - Updating installed files alone leaves an old running process unaffected.
  - Rename, archive, retries, deleted queries and desktop synchronization are deferred.

  - Text drifts from code silently; nothing executes most of it.

  - Broken commands in docs surface only when someone follows them.

- **Tests:**

  - Live-sync behavior and privacy claims are exercised by `tests/test_card199_safety.py`.
  - Space contract and retained board/project/chat regression tests.

  - `tests/test_commonplace_public_surface.py`, `tests/test_commonplace_legal_pages.py` and the documentation checks in `tests/test_commonplace_release_gate.py`.


## build-and-ci

- **Paths:** `pyproject.toml`, `uv.lock`, `.python-version`, `Cargo.toml`, `Cargo.lock`, `crates/`, `.gitattributes`, `.gitignore`, `.pre-commit-config.yaml`, `.github/`, `.devcontainer/`
- **Owns:**

  - Python and Rust packaging, pinned dependencies, the coverage gate, lint and format settings, pre-commit hooks, repository metadata and the development container.

- **Promises:**

  - Plain pytest excludes the `timing` marker from behavior checks; explicit timing selection remains available for serial informational measurements.

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

  - `benchmarks/thread_search.py` uses deterministic synthetic chunks, exports complete search lists for revision comparisons, and reports p50/p95 latency.


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

  - Synthetic legacy safety-stop cases and explicitly classified transcript-only fixture updates.
  - `tests/test_backup_online_processes.py` reproduces separate backup/gateway ancestry and preserves foreign-writer and paused-worker refusals using a synthetic `/proc`.
  - The canonical Space REST matrix and base-captured profile/tool schemas.

  - `tests/test_commonplace_subagent_asks.py` covers released-card questions and coordinator routing, answers, escalation and recovery.

  - Shared fixtures and harnesses and every test file not listed in another block.
  - Shared fixtures and harnesses and every test file not listed in another block, including binding-epoch migration, direct fences, lock-entry instrumentation and bounded reset races.

  - Subagent question routing, proposal/release prerequisites and recipient authority regressions.

- **Promises:**

  - Default provenance regressions assert zero candidate work above either cap and accurate write errors with unchanged verbatim storage. Cap-sized and 1 MiB scan measurements run separately under `timing`, reporting resource budgets without gating on them.

  - Public read/write cases cover full-field refusal, long readable controls and non-boundary fork prefixes.

  - Public KG spelling regressions retain ordinary citations and all canonical producer formats on read/write, with separate grammar, owner and exact-hash isolation checks.

  - Trailing fork regressions cover hidden read scrubbing/write refusal, readable spelling preservation, every printable ASCII suffix and ordinary URL/path/prose collision guards; concatenated references have public read/write refusal and develop byte-preservation guards.

  - Background Settings prerequisite regressions preserve rendered sections, mounted webhook input identity and unsaved values through errors, deadlines and recovery.

  - Console activity schema and query checks freeze the store clock so dated fixtures stay inside the retention window.
  - Owner progress fault regressions traverse the real wired Recall authority in paired hidden/absent worlds.

  - Progress witnesses compare authenticated hidden-present and hidden-absent worlds through the real scheduler and successful-write path in both store formats.
  - `tests/test_settings_load.py` drives fixed timers through production Settings reads, Retry and saved-state reloads, including fetch/body stalls, HTTP/network/JSON failures and contended orchestration reads.

  - Safety tests run identically on a pinned base and implementation; server import and low-level persistence remain covered.
  - Card schema regressions cover missing gateways, missing control databases and unexpected settings read failures.

  - Gate regressions let unexpected capacity exceptions fail directly and avoid vacuous retry-report checks; the legacy fixture documents its shared-schema coverage boundary.

  - The pinned handoff baseline adapts live tool availability and workspace schemas while retaining strict HTTP and audit equality; visibility mutations must fail.
  - Card task-type tests cover base/head acquisition, owner API boundaries, runner admission and fail-closed eligibility with legacy fixtures.
  - Integration tests combine typed owner assignment, configured starts, blocker filtering and existing-holder claim replay.

  - Card task-type tests cover owner status changes for assigned holders, non-object JSON rejection, base/head acquisition, owner API boundaries, runner admission and fail-closed eligibility with legacy fixtures.
  - Driven-clock heartbeat tests cover standby takeover after release and expiry; a subprocess SIGTERM test covers immediate restart after real serve teardown.
  - Driven-clock heartbeat tests cover standby takeover after release and expiry; a subprocess SIGTERM test covers immediate restart after stubbed-HTTP serve teardown. Shutdown regressions cover server and worker entry points, one lease release and duplicate closes after SQLite closes.
  - Driven-clock heartbeat tests cover standby takeover after release and expiry; a subprocess SIGTERM test covers immediate restart after stubbed-HTTP serve teardown. Shutdown regressions cover server and worker entry points, one lease release and duplicate closes after SQLite closes. Unreadable-grant and confirmation regressions require local disconnect and unconfirmed revocation.
  - The same contract selection runs on pinned base and implementation head; transport regressions execute separately.
  - Space profile/tool snapshots are captured from the development base, retaining its split and inbox visibility and card-schema additions.

  - The revoked-token socket guard observes the completed server-side 401, no body reads and no route dispatch, including a forced close before upload finishes.
  - The seat webhook replay checks actual worker completion with orchestration off and on, retains card-created notifications when enabled, and verifies message filtering and unchanged subscriptions on reconnect.
  - Reassignment regressions keep doing-card handoffs unclaimed and avoid queued replacement starts until the receiver takes the card.

  - Manual-assignment regressions cover the statechart claim replay, configured worker starts, visible blocked-start reasons, project admission, busy agents and restricted-origin release.
  - Off-mode capacity regressions preserve legacy refusal text and card state, including a coded admission refusal.

  - Card contract tests cover paired RED assertions, off-path guards, atomic key races, bounded retries and route busy responses with unchanged state.

  - All card requirement tests, including validation and error paths, have assertion-level RED evidence on the declared stack base; existing-database launch-role migration is checked directly.

  - Claim text assertions avoid feature imports; duplicate-key label edits preserve labels, version and audit history on refusal.

  - Pure-module guards check generic path/URL patterns and outbound imports without embedding deployment identifiers.
  - Deferred refresh regressions execute the production shell and Settings renderers in both completion orders, retain plan and account controls, and guard navigation away and background updates during rendering.
  - Coordinator settings regressions cover owner authority, current PAT eligibility, workspace isolation and missing-coordinator release notices.
  - Coordinator settings regressions cover owner authority, current PAT eligibility, workspace isolation and missing-coordinator release notices; the waiting bucket remains present for owners and absent for members.

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

  - **Browser GitHub connection:** Browser GitHub grant tests cover ownership migration, generation fencing, consent, encryption, scope evidence and safe Needs links using synthetic transports. Separate temporary roots on one database preserve in-flight state; crash leases exclude restart until expiry, then permit recovery.

  - **Connection lifecycle proof:** Adds independent-database ownership arbitration, held revocation overlap, subprocess crash recovery, revocation matrices, callback variants, Node controller render-cache checks and direct transport bounds.

  - **Connection teardown boundary:** Adds own-person grant isolation and assertions that refresh material is discarded before encryption.

  - **Mutation sensitivity:** The hidden-card assertion runs with no pending flow, so removing visibility cannot be masked by the one-flow guard.

  - **Connection recovery:** Adds expired-grant reconnect, repeated scope-header, independent visibility/confirm fences, cleanup-failure, AAD swap and key-owner/location proofs; the scope-evidence tests assert before importing the new client.

  - **Independent scope evidence:** Missing token-response scope is tested without an optional header, so header rejection cannot mask the mandatory-evidence guard.

- **How it fails:**

  - Swallowing an authority fault must fail the fixed-503 assertion rather than silently reduce counters.

  - Guard-only tests do not substitute for base failures; metadata, authority, state and unit mutations must change public assertions.

  - Configuration refusal can hide a later guard unless that boundary is also exercised directly.
  - Feature assertions fail before DTO inspection on the unregistered base route.

  - A plan fixture that names a missing test file fails G7; a shared fixture change can break unrelated suites; slow or flaky tests waste every run.

  - Email cookie-scope regressions use an HTTPS cookie jar over loopback, including an older host-only cookie; sign-in rendering checks cover the request confirmation.


- **Tests:**

  - `tests/test_card253_s12.py` covers strict store/private-reader failures, ordinary deny-on-fault compatibility and second-pass enumeration failure.

  - `tests/test_card253_s12.py` covers owner progress isolation, current read authority, quota causality, incomplete proof and bounded metadata polling.

  - `tests/test_card199_safety.py` and the sync test family retain transcript-byte/cursor and server-import compatibility checks.
  - Transcript selection tests observe the actual symlink/empty rejection; memory selections separately prove refusal before selection.
  - Token tests distinguish missing selected capture credentials during initialization from import-only feed credential rejection.
  - GitHub lifecycle rendering runs in Node with deferred replies and synthetic roots, including logout during an outstanding status read.
  - `tests/test_spaces_contract.py`, `tests/spaces_surfaces.json` and the checked REST/public surface regressions.
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
  - Thread-search tests pin rank-before-fetch and score/thread/index ordering at the limit boundary, including shuffled rowids and ID batches.


## hub-core

- **Paths:** `commonplace/__init__.py`, `commonplace/__main__.py`, `commonplace/gateway.py`, `commonplace/control_db.py`, `commonplace/auth.py`, `commonplace/policy.py`, `commonplace/tokens.py`, `commonplace/extensions.py`, `commonplace/_http.py`, `commonplace/http_util.py`, `commonplace/urls.py`, `commonplace/ratelimit.py`, `commonplace/workers.py`
- **Owns:**

  - Canonical Space REST aliases mapped to existing board tool authority.

  - The gateway (the only way into a workspace), the control database, principals, token policy, the extension seam and one memory worker per workspace.

  - Extension installation registers email shutdown before GitHub shutdown; HTTP server teardown closes both in that order before SQLite closes.

- **Promises:**

  - KG provenance writes report the offending field and citation character limit before storage; hidden-reference refusals keep their existing replies and refused writes leave existing facts untouched.

  - Supported Space routes pass the existing non-person tool visibility gate.
  - Gateway mutation scopes and provenance lookups follow the principal workspace; hidden targets use absent replies, and client-supplied scopes cannot widen access.

  - Imported-history consent: Imported reader results bypass wing restrictions only after exact-view proof; ordinary scope, tool, wing and mutation gates remain in force.

  - Local extension schemas may derive workspace-gated properties per request without mutating shared schemas.

  - Every request resolves to one principal or none; scopes, wings and trust are checked before any tool runs; tokens are stored hashed; writes are audited.

  - Existing workspace directory lookups pass a backup freeze, so a writer holding a database transaction can finish; new directories remain gated.

  - **Browser GitHub connection:** Workspace creation accepts the caller transaction so GitHub ownership, user creation and personal workspace provisioning roll back together.

- **How it fails:**

  - Rejected methods have no tool alias; authentication and body transport refusals remain shared.

  - A worker that does not start returns an error, never another workspace's data; the control database is the single point of failure for sign-in.

- **Tests:**

  - `tests/test_spaces_contract.py` and the installed public surface inventory.

  - `tests/test_commonplace_gateway.py`, `tests/test_commonplace_control_db.py`, `tests/test_commonplace_policy.py`, `tests/test_hub_` files.


## sessions-and-sign-in

- **Paths:** `commonplace/sessions.py`, `commonplace/session.py`, `commonplace/email_auth.py`, `commonplace/mailer.py`, `commonplace/oauth/`, `commonplace/social/`
- **Owns:**

  - Web sign-in links and cookies, startup context reads, email challenges, outgoing mail, OAuth for MCP clients and provider sign-in.

- **Promises:**

  - Imported-history consent: OAuth reader consent is separate and unticked; defaults and person policies exclude the reader, while refresh descendants cannot regain a dropped scope.

  - Enabled coordinator startup context includes unsplit inbox counts, without migrating boards.

  - Accounts exist only after verification; sessions are cookie-bound and revocable; mail is configured only through the environment.

  - Successful email verification expires an older host-only session before setting a configured shared-domain session, so the older cookie cannot shadow the new one.

  - **Browser GitHub connection:** GitHub login reserves subject ownership, and connect-marked callbacks dispatch before login flow lookup without provisioning or replacing sessions.

  - **Connection lifecycle proof:** Connect-marked POST callbacks are refused before login dispatch; a normal login remains independent of an outstanding connect flow.

- **How it fails:**

  - Mail outages block new sign-ins but not existing sessions; a misconfigured provider fails closed.

- **Tests:**

  - `tests/test_commonplace_email_auth.py`, `tests/test_commonplace_oauth.py`, `tests/test_commonplace_social.py`, `tests/test_commonplace_session.py`.


## cli-and-client

- **Paths:** `commonplace/cli.py`, `commonplace/client.py`, `commonplace/client_hooks.py`, `commonplace/client_install.py`, `commonplace/bridge.py`, `commonplace/onboarding.py`, `commonplace/seed.py`, `commonplace/README.md`, `commonplace/SETUP.md`, `commonplace/agent-skill/`
- **Owns:**

  - The `commonplace` command line, the credential-scoped client, native hooks and installers, the stdio bridge and the agent skill.

- **Promises:**

  - Local search-index status and run pass an explicit unscoped principal and retain their printed count keys and capture enrolment behavior.

  - Serve always closes its control database on exit, including SIGTERM unwind, releasing owned resources through pre-close callbacks.

  - Imported-history consent: CLI token creation rejects the imported-history reader scope before issuing a credential; browser consent is required.

  - The agent skill stays below 120 lines and links to the full opt-in splitting guide for delivery details.

  - The agent playbook requires inbox reads at session start, on wakes and every 10 minutes, stable keys, and completion markers.

  - CLI commands act through the same services as the network routes; installers never change trust policy; hooks fail open.

- **How it fails:**

  - Local data-directory access remains operator authority; authenticated progress never inherits it.

  - The CLI runs with data-dir access, so anyone with the service's OS user can use it (residual in `SYSTEM.md`).

- **Tests:**

  - `tests/test_search_index_progress.py` covers local status, run and explicit capture enrolment.

  - `tests/test_commonplace_cli.py`, `tests/test_commonplace_client.py`, `tests/test_commonplace_client_hooks.py`, `tests/test_commonplace_client_install.py`, `tests/test_commonplace_onboarding.py`.


## boards

- **Paths:** `commonplace/boards/`, `commonplace/events.py`, `commonplace/webhooks.py`, `commonplace/holds.py`, `commonplace/callstack.py`
- **Owns:**

  - Canonical Space REST response projection and typed creation refusals.

  - Boards, cards, the append-only `task_events` log, approvals and decisions, the hub event log, signed webhooks and delayed person approvals.

- **Promises:**

  - Owner assignments to active agent connections claim the card with a lease and a claim event.

  - Immediate assignment claims apply to todo cards; handoffs of doing cards return to todo without starting the replacement worker.

  - After commit they request the configured worker start, or persist a visible reason that it could not start; concurrent card updates are preserved.

  - Explicit takes of an already held card return that claim without renewing it.
  - Card schemas default to orchestration off when the control database is absent or settings reads fail.

  - Disabled orchestration preserves legacy capacity refusals and never rewrites coded limit errors through the enabled waiting projection.

  - Enabled blocker updates are versioned and audited; pure waiting reasons drive views and claims, and SQL skips unresolved edges.

  - Card lists share a runner read and per-board usage; a capacity refusal retains its original error when no display reason is available.

  - Board REST maps only database lock timeouts to busy; waiting summaries reuse one runner read and two board usage counts; split-key edits preserve child identity.
  - Card task types are owner-person edits; stable-ID grants in the workspace store guard new typed acquisition while existing holders retain their claims.
  - Owner todo assignments validate the target’s typed grant before claiming; repeat takes preserve an existing holder’s claim after grant removal.

  - Owner status changes preserve typed-card assignees and validate their grants; card task types are owner-person edits; stable-ID grants in the workspace store guard new typed acquisition while existing holders retain their claims.
  - Owner coordinator settings list eligible active PAT connections; needs-me reports restricted cards waiting on a missing coordinator once per board.
  - Owner coordinator settings list eligible active PAT connections; needs-me reports restricted cards waiting on a missing coordinator once per board, with the waiting bucket omitted for non-owners.
  - Metadata uses an exact active slug on a plain store connection; list retains expiry and create retains rules enforcement.

  - Every card change is one transaction with its `task_events` row; event emission happens after commit and a failed emission never undoes the change; on a configured board only `task_approve` removes `plan` and plan-check gates plan approval.

  - Trusted agents and authorized board coordinators may edit only the title/body of unassigned restricted-origin todo cards without releasing the origin or changing claim authority.

  - With workspace orchestration enabled, claims hold runner admission through board commit and person assignments or handoffs cannot reserve a busy agent.

  - Subagent and restricted-agent submissions remain inactive until coordinator release, with owner fallback only when no coordinator is live.

  - Holders may annotate an inactive status line without activation.

  - Subagent asks on released cards reach the live board coordinator, which may answer or escalate with a reason; absent a live coordinator the owner route remains, including asks already routed before a coordinator becomes unavailable or is replaced.

- **How it fails:**

  - Hidden metadata is absent; hidden repo collisions are forbidden; unsupported shapes execute no domain method.

  - A swallowed emission loses a notification but not the change (the trigger sweep reads `task_events`); webhook delivery retries and records failures.

  - Each unexpected dispatcher-pass exception is contained and logged without its private message, then waits on the stop event for 5 s, doubling after consecutive errors up to 300 s, before continuing.

  - A successful pass resets the backoff; stopping interrupts the wait, and a closed database still ends the dispatcher.

- **Tests:**

  - `tests/test_spaces_contract.py` covers DTOs, isolation, authority, collisions and side effects.

  - `tests/test_commonplace_boards.py`, the `tests/test_commonplace_board_` files, `tests/test_commonplace_decide.py`, `tests/test_orch_plan_gate.py`, `tests/test_commonplace_webhooks.py`.

  - `tests/test_commonplace_subagent_asks.py` checks question routing and recipient authority.


## runner

- **Paths:** `commonplace/runner/`, `docs/runner.md`
- **Owns:**

  - Machine enrollment and grants, runner jobs in `runner.sqlite3`, the requester and machine routes, the local launch client and chat answers.
  - Machine enrollment, binding epoch migrations and immutable job epoch snapshots, grants, runner jobs in `runner.sqlite3`, the requester and machine routes, the local launch client and chat answers.

- **Promises:**

  - Reset-before-start is refused in both orchestration modes, including retry; runner admission precedes control authority held through commit. Owner resolution audits under runner-first ordering.
  - A machine may start a todo card reserved for its own worker or a doing card already held by that worker.

  - Other assignees, waiting-person gates and unreleased origins remain refused.
  - Enabled starts reject unresolved same-board blockers through read-only board probes, without constructing a board store inside a runner transaction.
  - Runner admission refuses typed cards without the worker connection’s exact workspace grant, including unreadable or corrupt mappings.

  - Started jobs retain machine in-flight capacity and per-machine project exclusion while their cards remain doing with the machine worker; a terminal job state or a changed card state or assignee releases capacity, using read-only boards access.

  - A start needs a person owner or a listed trusted PAT and a card the worker could claim; one active start per card; an uncertain launch is never re-queued; on a configured board `card_start` is person-owner only, starts are fenced and capped, and the lease payload carries the pinned message and model.

  - Opt-in orchestration requires the active coordinator for unconfigured PAT requests, reserves project capacity when starts are queued, and admits leases using active slots under the shared runner lock.

  - Owner/local step void cancels only queued or leased jobs atomically through cancellation logic; standalone cancel authority and launch-state handling stay unchanged.


- **How it fails:**

  - A reset invalidates queued/leased snapshots; previously starting work becomes uncertain and retains capacity. A bound provider change requires explicit reset before reattestation.

  - A lost launch result becomes `uncertain` and waits for a person; a revoked machine cancels queued jobs on the next sweep; a busy queue refuses with `busy`.

- **Tests:**

  - The `tests/test_runner_` files, `tests/test_orch_runner.py`, `tests/test_orch_acceptance.py`.
  - The `tests/test_runner_` files (including binding migration, reset races, direct fences and lock-order instrumentation), `tests/test_orch_runner.py`, `tests/test_orch_acceptance.py`.


## orch

- **Paths:** `commonplace/orch/`, `docs/orchestrator.md`, `tests/_orch_helpers.py`, `tests/test_orch_acceptance.py`, `tests/test_orch_schema.py`, `tests/test_orch_areas.py`, `tests/test_orch_store.py`, `tests/test_orch_durable.py`, `tests/test_orch_runner.py`, `tests/test_orch_surfaces.py`, `tests/test_orch_checks.py`, `tests/test_orch_plan_gate.py`, `tests/test_docs_obligations.py`
- **Owns:**

  - Inert area declarations and topology validation, their render/diff representation and test-only accepted-area fixtures.

  - The planner grammar, including leading numeric labels and repository-relative existing test-file mappings; the orchestrator config format and its versions and experiments, config-driven starts, triggers, leases, the journal, resume, the restore flag and acknowledgement, `plan-check` and `docs-check`.

- **Promises:**

  - Up to eight distinct area sub-orchestrators sit directly under one parentless orchestrator; areas remain preview-only across live and experiment accept/write paths. No-area graph rules and main/worker model slots remain unchanged.

  - Owner-assigned runner work uses the existing configured person-start fence; agent-authored todo reservations still produce an assigned-card trigger miss for a different worker.
  - Enabled root triggers pin main-role launches; reconciliation uses existing miss history and an in-lock eligibility recheck for at most three attempts.

  - Empty disabled sweeps avoid config resolution, checkpoint inbox context is best effort, and failed inbox snapshots keep committed leases launchable using pinned instructions.

  - Lease snapshot failures log sanitized diagnostics without exception messages.

  - Only owner-accepted versions run; every config-driven start is one fenced, capped transaction; a retry returns the stored job; external effects are never re-issued by the hub; after a restore nothing config-driven runs until the owner acknowledges.

  - Configured starts share project admission with claims and plain jobs when workspace orchestration is enabled; boards without a `[runner]` config retain earlier behavior with the workspace switch off.

  - The stored-job retry above applies only to unvoided steps.

  - Voided runner-start keys are retired with `step_voided`; history and linked job statuses remain visible, and a new key needs normal admission.

  - First void and queued/leased cancellation share one transaction; repeats preserve actor/time and one journal event while tools emit per-call feed notifications.


- **How it fails:**

  - Area topology returns stable field paths and schema codes; acceptance returns area_not_supported before effects, preserving proposals, accepted rows, cursors, leases, jobs and events without echoing config content.

  - Refusals become coded misses shown on resume; an unproven external effect becomes `needs_person`; infrastructure errors leave the trigger cursor in place for the next sweep.

  - Void cannot stop launched threads or prove completion; existing active/recent-start and per-switch capacity guards still apply, and a pending todo trigger may run on a later sweep.


- **Tests:**

  - `tests/test_orch_areas.py` covers area roundtrips, ordered topology errors, frozen experiments, owner boundaries and complete refusal snapshots through store, MCP, REST and local CLI.

  - The `tests/test_orch_` files listed in Paths and `tests/test_docs_obligations.py`; R22 in `tests/test_orch_checks.py` covers misplaced labels, missing files, accepted label forms and repository-root resolution.

  - R1, R1b, R1d, R1e, R8b, R10a and R11e map to `tests/test_orch_acceptance.py`.


## projects

- **Paths:** `commonplace/projects/`, `commonplace/plans.py`
- **Owns:**

  - Project orchestrator seats, project model choices, project limits, project wings and optional hosted plans.

- **Promises:**

  - Orchestration settings reads wait at most one second for the shared control lock, then return the existing retryable `settings_unavailable` refusal without reading an unlocked connection.

  - Connect recommends a board-scoped subscription to `space_message` and `card.moved`, retaining `card.created` with orchestration enabled, without creating or modifying webhooks.
  - The opt-in inbox excludes children and coordinator roots, exposes keyed children, and accepts first-wins split markers only from the coordinator or signed-in owner.

  - Split marker task references are range-checked before SQLite binding and settings-read failures remain retryable.

  - Only a signed-in owner changes models or limits; `max_agents` counts distinct connection assignees with orchestration off; opt-in orchestration shares card/job admission accounting, default cap 3, owner-only settings and atomic seat trust; model resolution goes project, then workspace, then the runner default.

  - **Browser GitHub connection:** Signup plan assignment accepts the provisioning transaction to preserve atomic GitHub ownership reservation.

- **How it fails:**

  - An unresolved model refuses a config-driven start before insert; a limit change does not stop work already started.

- **Tests:**

  - The `tests/test_projects_` files, `tests/test_commonplace_plans.py`.


## rules-and-guards

- **Paths:** `commonplace/rules/`, `commonplace/guards/`, `commonplace/privacy.py`, `commonplace/redact.py`
- **Owns:**

  - Canonical Space create in the checked REST write inventory.

  - Owner-reviewed rules and their freshness gate, guard building blocks, sensitive-text flags and redaction.

- **Promises:**

  - Split completion markers are checked writes on MCP and REST.
  - Canonical create uses header freshness while legacy routes retain body compatibility.

  - Rules take effect only after owner review; writes from a client with stale rules get 428; flags are deterministic.

- **How it fails:**

  - Missing and stale served sequences retain the 428 refresh payload.

  - Redaction can miss a sensitive detail; flags are hints, not blocks.

- **Tests:**

  - Route-aware checked REST harness and Space freshness contract tests.

  - `tests/test_commonplace_rules.py`, `tests/test_commonplace_rules_enforce.py`, `tests/test_commonplace_privacy.py`, the `tests/test_guards_` files.


## handoff-and-continuity

- **Paths:** `commonplace/handoff/`, `commonplace/continuity.py`, `commonplace/continuity_read.py`, `commonplace/continuity_schema.py`
- **Owns:**

  - Agent context handoff to a fresh agent and append-only native session continuity.

- **Promises:**

  - A dedicated strict Recall callback propagates owner-progress lookup faults; extraction retains the ordinary deny-on-fault callback.

  - Search-index polls authorize current metadata once per thread, revalidate descriptors in one snapshot and project per-thread successful units, activity and quota refusals.
  - Complete protected-reference shadows join ordinary authorization tokens; namespace/source case normalizes only in the authorization copy, and full identity/hash suffixes remain literal.
  - Citation parsing optionally raises a specific decoded/NFKC length error for writes while retaining fail-closed read projection; token collection deduplicates thread ids on insertion in first-occurrence order.

  - Decoded and normalized reference scans are each capped at 16,384 characters and fail closed without partial proof; non-boundary run starts retain ordinary authorization, and only earlier runs admit overlapping shadows.

  - Complete protected-reference shadows join ordinary authorization tokens; namespace/source case normalizes only in the authorization copy, and full capture/import identity/hash suffixes remain literal.

  - Fixed-length fork shadows prove the digest with arbitrary trailing characters, replacing only its own ordinary trailing context while every overlapping protected spelling still requires proof; omitted separators distinguish ids from prose actors.

  - Provenance uses one bounded decoder and response-local access caches; extraction by a non-reader leaves the source cursor untouched.

  - Imported-history consent: Imported readers receive bounded immutable views, source-range proof and view-specific metadata; capture tails never inherit import authority.
  - Combined read boundaries: One response access checker retains normalized provenance and private-source authorization alongside import range proof, including deadline-bound startup reads.
  - Ordinary transcript compatibility: Recall checks import identity fields only for imported threads; ordinary policy-only callers retain their read path.
  - Legacy import compatibility: Ordinary reads preserve three-part filer identities without rewriting stored data; reader views still require four-part identities.

  - A handoff never loses the source thread; continuity is append-only and resumable.

  - Verified thread FTS identities provide scope and tie keys before limiting rowids; only selected rows fetch canonical chunks.

  - Scores, thread IDs and chunk indices determine stable ordering; index-drift repair and logical fallback remain in force.


- **How it fails:**

  - Store or private-reader exceptions invalidate the entire owner progress proof without an unscoped fallback.

  - Incomplete progress proof fails unavailable without global counters; process restart drops unit attribution, and shared storage may block readable work.

  - A failed launch of the successor leaves the source agent running; an interrupted archive adoption resumes from its last committed step.

- **Tests:**

  - `tests/test_card253_s12.py::test_card253_s12_authority_fault_fixed_unavailable` exercises real Recall faults in both store formats.

  - `tests/test_card253_s12.py` and the search-index backlog and sweep suites cover attribution, revocation, snapshots, quota causality and deadlines.

  - The `tests/test_handoff_` files, `tests/test_commonplace_continuity_unit.py`.


## chat-and-voice

- **Paths:** `commonplace/chat/`, `commonplace/voice.py`, `commonplace/phone_push/`
- **Owns:**

  - The workspace chat front agent, server-local dictation and opt-in phone notifications.

- **Promises:**

  - Chat status counts cards in needs-me while ignoring per-board coordinator notices.

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

  - Owner HTTP progress always forwards the authenticated principal, returns only readable progress and disables response caching.

  - Reads and writes respect the caller's wings and scopes; store cutovers are backup-gated and restartable; capture is explicit and append-only.

- **How it fails:**

  - Incomplete progress proof returns the fixed 503 error without partial totals or private failure details.

  - An interrupted cutover keeps the previous store live; an index rebuild degrades search speed, not stored text.

- **Tests:**

  - `tests/test_card253_s12.py` covers authenticated HTTP isolation and failures; `tests/test_search_index_progress.py` covers the route contract.

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

  - Transcript-only entry consent, legacy read/rewrite guards and filtered active-operation scheduling.

  - Opt-in client synchronization of transcripts and memory, its local state and the content-free live feed.

- **Promises:**

  - Explicit exact-endpoint consent precedes token/state access. Disabled legacy rows and schema survive unchanged; memory roots are never inspected. Caught-up transcripts finish backfill without legacy allowance use.

  - Sync never opens a server database and never reads credentials; local state holds metadata only.

- **How it fails:**

  - Endpoint consent does not bind workspace/principal identity; a different token at the same hub remains possible. Old services retry rejected configs until explicit reinstall or uninstall.

  - A lost connection keeps the outbox and resumes from the cursor; a corrupt local cache is rebuilt from the server.

- **Tests:**

  - `tests/test_card199_safety.py` covers entry order, each discriminator, direct scanner/attempt/rearm guards, retained rows, backfill, status and private preview output.

  - The `tests/test_commonplace_sync_` files.


## backup-and-encryption

- **Paths:** `commonplace/backup.py`, `commonplace/backup_online.py`, `commonplace/backup_supervisor.py`, `commonplace/backup_pause.py`, `commonplace/admission.py`, `commonplace/workspace_encryption.py`
- **Owns:**

  - Offline and online verified backups and restores of the data directory, the gateway pause socket and run fence, the admission gate for store/file creators and the successor registry, the restore marker, and opt-in workspace SQLite encryption.

- **Promises:**

  - A backup copies every SQLite file with a manifest; an online backup runs beside the serving gateway without stopping or replacing it and publishes only after the run is committed.

  - Its enumeration tolerates SQLite sidecars disappearing during commit or connection cleanup, while missing user files and unsafe paths still fail closed.

  - Online writer scans exempt the gateway's ancestor chain from the same `/proc` tree, alongside the backup's own ancestry; unrelated writers remain refused even with identical supervisor arguments.

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

  - Background prerequisite failures keep rendered Settings sections and form drafts mounted, surfacing the named error separately; initial and foreground failures still render Retry.

  - Settings and its prerequisite reads use ten-second per-request deadlines through JSON decoding; each failed section is named with Retry while successful sections retain saved values.

  - Imported-history consent: The owner form offers independent unticked import and reader checkboxes and displays reader authority beside Revoke.
  - The Connect checklist renders the supplied webhook event recommendation, including card moves and optional card creation, with escaped values.

  - Enabled waiting tags show dependency titles or occupied-slot counts and suppress the available tag.
  - A current refresh failure clears that responsibility, so later background reads still preserve busy Rules forms.

  - The latest refresh carries a pending foreground render for its screen, so background events cannot leave Settings or Console on Loading; completed Settings views keep their usual background-update policy.

  - The person-owner task-type API rejects non-object JSON with 400 and replaces a connection’s sorted allowlist atomically; GET never creates a boards store.

  - The web app calls the same services as REST; it shows only what the signed-in person may read.

  - **Browser GitHub connection:** Needs shows local GitHub connection state and validated PR links; selection and session teardown discard connection state.

  - **Connection lifecycle proof:** Versioned fixture cards preserve the loading assertion; stopped Needs controllers clear the detail DOM as well as cached connection state.

  - **Connection teardown boundary:** Stopping clears rendered GitHub details while preserving the established teardown contract for other Needs details.

  - **Connection recovery:** Callbacks reopen the selected Needs item; GitHub-only status failures preserve card actions, and connections in another workspace show an explicit disconnect control.

- **How it fails:**

  - Rendering errors surface in the browser; the server state is unaffected.

- **Tests:**

  - The `tests/test_workbench_` files, the `tests/test_console_` files and the `tests/test_commonplace_console_` files.

## browser-github-grants

- **Paths:** `commonplace/github/`
- **Owns:**

  - Explicit per-person GitHub browser grants, one-use connect flows, encrypted pending consent and direct bounded transport.
- **Promises:**

  - Never-owner workers retry claim and recovery on heartbeat ticks; former owners stay fenced. Serialized, idempotent shutdown deletes only the matching owner lease once, before server teardown returns or SQLite closes; standby and lost-lease logs explain readiness.

  - Disconnect purges grants and confirmations even when ciphertext or the wrapping key is unreadable, marking remote revocation unconfirmed without retrying unavailable credentials.

  - Browser sessions use exact Origin and session-derived CSRF; scope evidence and expiry fail closed; GitHub login and connect share subject ownership.
  - **Browser GitHub connection:** Default-off consent requests an empty scope, reads only the GitHub id/login and never creates login identities.
  - **Connection lifecycle proof:** Durable remote-work accounting holds the reconnect fence until revocation and older token cleanup both settle; only HTTPS or loopback development may start consent.
  - **Connection recovery:** Known expiry purges locally and reconnects without revocation acknowledgement; duplicate scope headers are all checked, and cleanup settles exchange counters even on failure. Own-person disconnect remains available across workspaces and while consent is disabled.

- **How it fails:**

  - Unknown expiry, scope conflict or missing keys refuse connection. Startup recovery requires an absent or expired shared-database lease; a competing serve cannot write GitHub state until its periodic retry claims after release/expiry; live OAuth behavior and production ingress logging remain unproven.
- **Tests:**

  - `tests/test_github_grants.py`, `tests/test_github_ui.py`, `tests/github_render.mjs`.

  - `tests/github_lifecycle.mjs` checks connected rendering, card switches and logout; a shared-volume lease fences recovery across private temporary directories without any lifetime lock file. Heartbeats renew the 30-second lease every 5 seconds, and expired owners fail closed on state writes.
