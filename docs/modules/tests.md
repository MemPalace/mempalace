## tests

- **Paths:** `tests/`
- **Owns:**

  - #451 wing editor regressions: `tests/test_runner_owner.py` (W1-W3: picker and wing edit with the memory listing failing, fallback, bookkeeping-only and private wings), case 4g of `tests/admin_runners_ui.mjs`, and two Chromium cases in `tests/workbench_e2e/test_admin_runners.py` that add a wing while `/api/connections` hangs or the wing read fails.

  - `tests/test_commonplace_deck_bots.py` (`commonplace_deck_bots.mjs`) covers draft hand-off in both looks, Deck tile and strip markup, project work status mapping, week-bounded completion groups, escaped text and the read-only project sheet lifecycle.

  - The navigation tests follow the bots-first rail: `tests/workbench_shell_wiring.mjs`, `tests/workbench_chat_wiring.mjs`, `tests/commonplace_home_render.mjs`, `tests/commonplace_board_defaults_render.mjs`, `tests/card_log_links.mjs`, `tests/admin_ui.mjs` and the phone scenarios of `tests/workbench_e2e/test_keys.py` no longer expect project rows, a Needs me or Console rail entry or a Chat tab (the phone Chat button is the dock toggle in the header).

  - `tests/conftest.py` refuses a raw pytest run (exit 4 at configure) on a shared test host that opts in with `~/.config/commonplace/require-test-runner` in the real account home; the marker names the environment variable the host's test runner sets for admitted runs. xdist workers pass; `COMMONPLACE_ALLOW_RAW_PYTEST=1` overrides one run. `tests/test_raw_pytest_guard.py` pins the cases.

  - `tests/test_commonplace_agent_log.py` and `tests/test_commonplace_agent_log_sync.py` cover agent-log capture, provenance, idempotent retry and a non-reader of the private chat; the protected-reference producer matrix includes the agent-log source.

  - `tests/test_chat_model_stream.py` runs the streaming model transport against loopback fake Anthropic and OpenAI-compatible servers; `tests/test_bots_turn.py` drives bot turns over real HTTP with a scripted model and one loopback fake provider.

  - `tests/test_run_ledger.py`: run ledger acceptance through the public HTTP routes and the runner CLI.

  - `tests/commonplace_deck.mjs` also covers the Workspace section that Deck adds to Settings (Deck only, Admin for admins only).

  - `tests/test_commonplace_deck.py` (`commonplace_deck.mjs`): the Deck switch, bar, phone tabs, screen registry, Look section and shell wiring.

  - `tests/bots_harness.html` and `tests/bots_shots.sh` are the screenshot harness for the bots screens: the real `bots.js` and stylesheets with a scripted fake API and stream, captured through headless Chromium.

  - `tests/test_rules_index.py` pins the compact rules index (completeness, size bound), keyed `rules_get` with unknown keys, partial reads not counting as served, and unchanged defaults; `tests/spaces_surfaces.json` carries the new `rules_get` and `session_start` arguments.

  - Model-role regressions: the one-per-role Needs me item and its clearing and owner-only scope, the start refusal naming the role, the API and UI refusal of an unknown model id, and the Needs me row and settings focus.

  - `tests/test_commonplace_capacity.py` covers the unlimited default, under/at the cap, concurrent sign-ups on one and on separate connections, waitlist admit, invited and disabled accounts, and the email, provider, REST and CLI paths.

  - The encrypted-store budget regression proves a budgeted thread write and its refusal through SQLCipher, whose `page_size` is text.

  - Import-reader CLI regressions retain allowed row locations and verbatim text; union fixtures acquire a protected-room hit in the readable wing before testing the drawer gate. Source-path controls pin control/format-character refusal alongside ordinary-path preservation.

  - Whole-response source-path and hidden-candidate regressions cover both search routes and wing selections; canonical output goldens and the three-backend union test distinguish raw owner rendering from the member's final gated render.

  - Protected shared-room CLI regressions cover literal-wing and fan-out search with independently readable controls; owner renderer controls, both room-list paths, cited and uncited cross-workspace mutations, and all nine projected-read literal refusals retain their distinct boundaries.

  - `tests/test_commonplace_hook_spool.py` covers the offline spool, ordered upload, backoff, retry after failure, a crash between upload and delete against the real gateway, and the hook time budget; the existing capture hook test spools into a temporary home.



  - Chromium project-connect regressions verify visible shared-memory and other conflict statuses, dialog dismissal and unchanged explicit retry bodies.
  - Restricted-search refill regressions cover hidden ranked prefixes, counter suppression, exhaustion, work bounds, unchanged every-wing calls, restricted CLI-compatible search, filters and deadline failures; the same fixture reports information-only latency.
  - `tests/test_runner_start_context.py` is the measurement and the contract of a start prompt's card context; `tests/test_runner_start.py` allows `card_context` beside the identifier keys of a lease.

  - `tests/test_chat_live.py` and `tests/test_chat_live_view.py` (with `tests/chat_live_view.mjs`) pin the live chat delivery states on the server and in the pane.

  - OAuth CSP header controls and real Chromium form submissions cover normal consent, login retry and re-rendered error consent.

  - OAuth CSP and destination-display controls cover normal consent, retry, canonical numeric hosts, explicit default ports and actionable validation errors.

  - Legacy report-source tests quiesce hold, chat and phone workers and verify removal of a pre-existing invalid reset trigger before raw status writes.

  - Real-browser refresh cases (`workbench_e2e/test_refresh_forms.py`) hold an open hand-off form or comment across background refreshes, including a read in flight and a changed Needs me queue, and check that Cancel closes the form, that J and K leave an open form alone, and that closing the panel after a failed read keeps it closed.

  - `tests/cardruns.mjs` (run by `tests/test_card_runs_ui.py`) covers the card panel's Runs section including the Start agent control: the words of every refusal, the configured and free forms, what a refresh keeps and when no control shows.

  - Skill-bundle contracts: the Claude marketplace copy of every skill matches the canonical one file for file (helpers included), and `tests/test_untangle_skill.py` checks the generic untangle skill's front matter, required sections and absence of local paths, and runs its read-only measuring helper on a synthetic tree.

  - `tests/test_routines.py` pins routine creation per due time on a fake clock, restart recovery and owner-only access.

  - `tests/test_projects_seat_lease.py` creates a seat through the Project settings routes and has it take, hold, renew and release its lease over MCP.
  - `tests/test_commonplace_rules_evidence.py` and the `rules_feedback` entries of `tests/spaces_surfaces.json` pin evidence writes, refusals, workspace isolation, bounded growth and old-database migration.

  - `tests/test_commonplace_goals.py` covers the goals tree, its owner-only writes, the additive schema and the card-context chain budget.

  - `tests/projectpanel.mjs` (run by `tests/test_project_panel_ui.py`) covers the panel draw: escaped text, dropped unsafe links, nothing for a project without a panel, cached redraws and failed reads, and that the rules come from one adopted sheet (no inline style element, which the app's CSP refuses).

  - `tests/test_rules_enforcement_points.py` checks the four rule enforcement points; hook tests that compare the startup context now feed it a complete rules section, and the import-reader hook check expects the missing-rules notice for a memory-only context.

  - `tests/test_guards_prepush.py` and `tests/test_guards_hooks.py` build throwaway git repos and linked worktrees with stand-in tools; they run no host tool and need no network.

  - `tests/test_statechart_card_rules.py` walks the shortest breaking path of each card-lifecycle statechart rule (R1, R3, R4, R5, R6, R6b, R7) and asserts the rule holds.

  - Overview witnesses separate old-head behavioral regressions from the original-base fail-closed contract, with exact facet-call sequences and both lazy SQLite error classes.

  - Assertion-only review witnesses pin public facet compatibility, real-wrapper projected retry, refusal envelopes and negative-cache invalidation.

  - Deterministic overview memory regressions exercise bounded projected acquisition and unchanged counts on synthetic fixtures.

  - Sign-out regression covers null and mounted Admin controllers; slice fixtures supply the module binding. The installed route inventory includes the authenticated Admin workspace prefix. Sign-in and voice boot fixtures load the real bounded request helper.
  - Sign-out regression covers null and mounted Admin controllers; slice fixtures supply the module binding. The installed route inventory includes the authenticated Admin workspace and instance prefixes. Sign-in and voice boot fixtures load the real bounded request helper.
  - The twelve runtime/limits/stuck/owner-queue RED rows, separate pagination/source/authority controls and updated earlier Admin lifecycle acceptance.


  - Synthetic Admin HTTP inventories and served-asset Node contracts, with assertion-only pinned-base RED evidence and identical selected head GREEN nodes.

  - Runner block checks run the served `adminrunners.js` against a fake DOM: enroll, attest, wings edit and the two-step revoke call the owner routes with exact bodies, and enrollment tokens never reach storage, logs, list redraws or a dismissed panel.


  - Synthetic legacy safety-stop cases and explicitly classified transcript-only fixture updates.
  - `tests/test_backup_online_processes.py` reproduces separate backup/gateway ancestry and preserves foreign-writer and paused-worker refusals using a synthetic `/proc`.
  - The canonical Space REST matrix and base-captured profile/tool schemas.

  - Card339 regressions exercise MCP and HTTP routing, owner fallback, person queue visibility, withdrawal history and authorization refusals. The pinned Space tool contract records the optional person_reason parameter and matching ask/review descriptions without changing tool names or profile visibility.

  - `tests/test_commonplace_subagent_asks.py` covers released-card questions and coordinator routing, answers, escalation and recovery.

  - `tests/test_commonplace_decision_blocks.py` and its Node harness `tests/commonplace_decision_blocks.mjs` cover structured asks from the MCP call through `needs_me` and the decide route to the Needs me blocks, with refusal controls for options that were not offered and for malformed blocks.

  - Shared fixtures and harnesses and every test file not listed in another block.
  - Shared fixtures and harnesses and every test file not listed in another block, including binding-epoch migration, direct fences, lock-entry instrumentation and bounded reset races.

  - Subagent question routing, proposal/release prerequisites and recipient authority regressions.

  - Coordinator status regressions: `tests/orchstatus.mjs` (run by `tests/test_orchstatus.py`) and `tests/test_orch_lease_status.py` pin the status states, the End lease confirm and refusals, and that ending a live lease leaves unfinished steps listed.

  - `tests/test_fact_readers_import_graph.py` runs a fresh interpreter over the fact read and search path and asserts `commonplace.rules` is never loaded, scans the three fact readers for any `commonplace` import, and pins the shared read deadline.

- **Promises:**

  - The pinned handoff baseline in `tests/test_handoff_adversarial_off_perf.py` overlays only leaf or board modules the current board code imports (now including `commonplace/decide_grant.py` and `mempalace/deadline.py`); the pinned gateway, policy and handoff sources stay at the baseline commit.

  - Browser tests that need Playwright skip at module level when it is missing, so collecting `tests/` never errors; `CP_BROWSER_E2E=1` turns a missing Playwright into a failure (`tests/test_cards_decide_scope_browser.py`, as in `tests/workbench_e2e`).

  - Tests that read the module map read every file in `docs/modules/`, not `MODULES.md`; `tests/test_docs_fragments.py` holds the merge-without-conflict proof for the documentation layout.

  - `tests/spaces_surfaces.json` pins the `decide` description naming owner connections holding `cards:decide`.

  - Restricted event regressions cover omitted webhook numbers, keyed opaque ids, authority changes, readable-set resets, cursor recovery and saved-but-unlinked notices. Restricted recovery checks retain exact event identity and internal deduplication without exposing public sequence fields. Authority lookup failures pin one redacted diagnostic and no enqueue. Shared fixtures also retain real member wildcard and literal-wing holders alongside owner-only private readers.

  - `tests/test_person_private_scope_r1.py` covers event cursor equality, single/chunked unreadable writes, tunnel side effects and scrubbing, token mint/load role caps and private-save notices. Broad private-read fixtures use owner-held tokens so independent filer guards remain exercised.

  - `tests/test_person_private_scope.py` checks authenticated owner/private and member/shared controls across thirty read variants, absent/hidden count parity, materialized hallways, isolated estate counts and unproven roles.

  - Graph parity fixtures include generated and absent locus schemas, Compact native values and large integers.

  - Graph compatibility witnesses distinguish required RED regressions from Chroma and old-base parity guards.
  - Outgoing alias tests cover token-free retry compatibility, short/flat alias proof, hidden storage equality, plain/chunked endpoints and orphan ranges. The focused set includes gateway thread scope and unchanged fact-save compatibility; drawer-search guards claim text preservation only.

  - Consented private-chat drawer listings validate page arguments with the worker before listing imports, forwarding the normalized limit and offset unchanged.

  - The private-chat list_rooms branch also uses list_drawers validation and reports list_drawers errors; its own public schema declares only wing, and legacy extra filters remain accepted.

  - Literal-wing drawer readers retain proved visible totals after import-aware metadata scrubbing; page arguments share worker schema defaults and error forms.

  - Graph regressions compare scoped responses with local readable outputs on exact and default Chroma backends.

  - Graph inputs are authorized before traversal, ranking and statistics; explicit totals reuse endpoint and label proof.


  - Only list_rooms/status respond payloads bypass the final generic gate after existing authorization proof; provenance-named count labels remain visible.

  - Citation failure cases compare complete surviving facts with readable controls.

  - Typed citation fixtures use native stored threads and real public reads, with metadata and Recall spies.
  - Tunnel concurrency witnesses, effective hydration spies and four authorization mutants cover the explicit-read contract alongside retained mirror and import suites.

  - Explicit tunnel regression witnesses and private protocol tests cover moved drawers, room-only compatibility, bounded pages and authorization races.

  - Logical drawer pages authorize physical identities before grouping, dates, totals and slicing; only visible page text is hydrated.

  - Hallway fixtures exercise stored dynamics initialized by the real production helper in both stores.

  - Six public hallway extra-key cases fail by hidden-id AssertionError on the preceding head.

  - Readable mirror hallway regressions fail on the prior head with count/row disagreement in both stores.

  - Hallway regression witnesses exercise public tools with real stored thread authority.
  - Authenticated room/status sums use authorized pairs. Status suppresses global capacity and raw probe reasons, reports a fixed disabled-index reason, and omits transport diagnostics and palace/database paths. Status, list_rooms (including consented private_chats imports), list_wings and taxonomy propagate backup-pause retry errors; consented private_chats drawer listings do too. Pause tests cover RPC and internal calls on those routes.

  - Default provenance regressions assert zero candidate work above either cap and accurate write errors with unchanged verbatim storage. Cap-sized and 1 MiB scan measurements run separately under `timing`, reporting resource budgets without gating on them.

  - Public read/write cases cover full-field refusal, long readable controls and non-boundary fork prefixes.

  - Public KG spelling regressions retain ordinary citations and all canonical producer formats on read/write, with separate grammar, owner and exact-hash isolation checks.

  - Trailing fork regressions cover hidden read scrubbing/write refusal, readable spelling preservation, every printable ASCII suffix and ordinary URL/path/prose collision guards; concatenated references have public read/write refusal and develop byte-preservation guards.

  - Background Settings prerequisite regressions preserve rendered sections, mounted webhook input identity and unsaved values through errors, deadlines and recovery.

  - Console activity schema and query checks freeze the store clock so dated fixtures stay inside the retention window.
  - Owner progress fault regressions traverse the real wired Recall authority in paired hidden/absent worlds.

  - Progress witnesses compare authenticated hidden-present and hidden-absent worlds through the real scheduler and successful-write path in both store formats.
  - `tests/test_settings_load.py` drives fixed timers through production Settings reads, Retry and saved-state reloads, including fetch/body stalls, HTTP/network/JSON failures and contended orchestration reads.
  - Admin and receipt controls observe operational calls and open flags outside production exception handlers; explicit alias-rejection and stale retry regressions fail by assertion.

  - Admin cleanup regressions exercise 100-character cursor boundaries, missing columns, inactive grants, typed metadata and next-page source errors.

  - Baseline-pass Admin controls remain distinct from RED regressions, and routing, disposal, escaping and redaction mutations must fail by assertion.

  - Pinned parent receives only new test/support files; all RED rows fail by assertion and the identical head selection passes. Synthetic clock and fake timers replace wall-clock assertions.

  - Admin tests use injected UTC and explicitly advanced JS timers, no sleep/elapsed-time assertions; harness errors are distinct from assertion failures, and credential fixtures never enter diagnostics.


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
  - `tests/test_projects_phase_b.py::test_head_connect_hint_and_trust_failures` asserts the Connect list contract: base message and move events, plus creation and report events when enabled.

  - `tests/test_project_reports.py::test_gateway_install_does_not_resolve_or_create_workspaces` covers missing, forbidden and real supervisor lookups; `test_dispatch_recovers_existing_source_without_creating_workspaces` proves lazy migration, delivery, workspace isolation and one diagnostic per failed workspace pass without private workspace names, identifiers or content in formatted logs. `test_dispatch_reinitializes_changed_source` checks schema recovery through the catalog after an in-process table drop; normal-pass initialization counts have a separate regression. `test_dispatch_isolates_publish_programming_error` proves that a non-closure publication error is logged once while the same pass publishes the next workspace's report for delivery.

  - `tests/test_project_reports.py` uses the same public completion witnesses on the base and changed code; deterministic scans cover episode keys, publication recovery, routing isolation, retry inspection and transaction faults.
  - Source initialization regressions hold the real report write transaction before the creator's WAL setup, require a successful BoardStore open, and verify next-pass recovery. Nonempty files missing either base table must also defer all report writes until initialization finishes.

  - Cold and warm public overview/search responses retain readable native rooms; explicitly consenting import readers retain readable grant-room counts and fan-out membership.

  - Standalone overview/search regressions retain ordinary rows while unresolved thread presence matches the absent-thread world, with missing, null or incomplete gateway context.

  - Workspace-less direct tool calls return empty overview shapes; fan-out tests hide unreadable thread-only wings even at the visible-wing limit. Union privacy and joint-ranking fixtures use workspace-bound principals and metadata probe/revalidation replies.

  - Public presence witnesses reach real worker acquisition branches and real search fan-out before comparing hidden and absent worlds. Private protocol tests cover descriptor revalidation, bounded batches and metadata-only reads.

  - Space profile/tool snapshots retain split and inbox visibility and card-schema additions, with current origin-release descriptions. Legacy NULL-origin and untrusted-policy controls isolate each predicate independently.

  - Blocker app tests drive the real `blockers.js` and `details.js` modules through Node and the REST add/clear path through the hub, so UI and API stay in step.

  - Waiting-reason app tests drive the real `board.js`, `details.js` and `waiting.js` modules through Node against the server sentences.

  - Task-type app tests drive the real `tasktypes.js` and `details.js` modules through Node; refusal-reason tests run a matching agent, a non-matching agent and untyped cards through take, status start and take-next.
  - Card reviewer tests (`test_commonplace_card_reviewer.py`) run the owner-only choice, member validation, completion routing, decide enforcement over service, MCP and REST, self-review, re-addressing and needs-me through the hub; `card_reviewer.mjs` drives `card-reviewer.js`, `details.js` and `board.js` in Node.

  - Origin overflow stays out of ordinary proposal buckets when the bounded approvals projection truncates; list and digest retain their truncation signals.
  - Stranded-reservation tests exercise coordinator release of its own reservation, creator cancellation after a real handoff with current or lapsed release, and atomic refusals for unrelated/descendant release, stored descendant markers, other reservations, bundled updates and pending reviews.
  - Release-loop tests replay the real release, creator claim and handoff through the service, then cover coordinator replacement, idle seats, owner Release, handoff aliases, reservation takeover and drop, and refusals for lost grants, creator markers, unrelated and descendant connections. Member tests cover take-over, drop and handoff through REST and the service for released and ordinary reservations; lapse tests cover revocation, creator descendants and a same-named agent in another workspace. Browser tests cover the card Release control, handoff recipients and visible refusals after the page settles, without retries.
  - Origin-holder regressions cover assertion-level RED evidence on a pinned base for same-holder and child-held release, lease preservation, exact agent/owner queues, MCP discovery, REST, notices, digest and direct web actions. The web action harness keeps required reasons and configuration navigation separate from delayed proposal review.
  - Owner rejection of person-targeted restricted proposals with a healthy coordinator preserves origin state; owner fallback still refuses rejection. Existing release, connection, default-client and delayed-approval contracts supply explicit recovery reasons and separate required proposal review.
  - Person proposal/plan queues assert reject-only actions before an independent coordinator releases origin and full actions afterwards; coordinator and fallback-owner approval controls remain positive. Routed-decision claims preserve choice and reply across a later origin release, including reply-only decisions. Owners can directly reject pending proposals during creator, descendant, unsafe-lineage and invalid-stamp configuration states without releasing origin. Coordinator-settings coverage counts the owner recovery card once, excluding its notice. Button-click regressions exercise reject-only proposals and plans through both the direct rejection request and the shell callback, with no approval control. Reserved-choice regressions refuse the exact origin sentinel in free-text decisions and new/existing ask options without changing task/event state, then verify delivery of a replacement decision; case and whitespace variants remain ordinary choices.
  - Origin security controls preserve creator, descendant, token, person-decision, terminal, hidden-tool and standard-member boundaries. Named mutations exercise unsafe lineage, unverifiable stamps, grant fallback, stored-holder revalidation and review/ask queue separation; controls are distinct from base RED evidence.

  - The revoked-token socket guard observes the completed server-side 401, no body reads and no route dispatch, including a forced close before upload finishes.
  - The seat webhook replay checks actual worker completion with orchestration off and on, retains card-created notifications when enabled, and verifies message filtering and unchanged subscriptions on reconnect.
  - Reassignment regressions keep doing-card handoffs unclaimed and avoid queued replacement starts until the receiver takes the card.

  - Manual-assignment regressions cover the statechart claim replay, configured worker starts, visible blocked-start reasons, project admission, busy agents and restricted-origin release.
  - Off-mode capacity regressions preserve legacy refusal text and card state, including a coded admission refusal.

  - Card contract tests cover paired RED assertions, off-path guards, atomic key races, bounded retries and route busy responses with unchanged state.

  - All card requirement tests, including validation and error paths, have assertion-level RED evidence on the declared stack base; existing-database launch-role migration is checked directly.

  - Claim text assertions avoid feature imports; duplicate-key label edits preserve labels, version and audit history on refusal.

  - Pure-module guards check generic path/URL patterns and outbound imports without embedding deployment identifiers.
  - Deferred refresh regressions execute the production shell and Settings renderers in both completion orders, retain plan and account controls, and guard navigation away and background updates during rendering. The harness (`tests/commonplace_refresh_render.mjs`) also answers the bots roster read that `refresh()` makes for the Bots badge and checks the badge total on a completed view.
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

  - Regression assertions compare complete scoped payloads with local outputs, including ranking and result caps.

  - Base-only regressions must be AssertionErrors, with identical test bytes and explicit rebase provenance.

  - Private-chat pagination preserves invalid-params and filter errors; incomplete validation or listing faults fail closed without raw exceptions.

  - Invalid filters retain worker error results, invalid pagination retains JSON-RPC invalid-params, and incomplete membership proof remains an empty page.

  - Regression evidence distinguishes client RPC errors from empty projection results and proves descriptor compatibility.

  - Incomplete graph metadata or tunnel proof returns the operation’s empty public envelope without global counts.


  - Incomplete pair proof still returns zero counts. Tunnel/drawer/other replies retain the gate and malformed scalar provenance remains omitted.

  - Deadline tests assert no worker calls; closet probes assert invocation and a readable positive result.

  - Only named public AssertionErrors count as base regressions; readable citations and plain-file display remain controls.

  - Incomplete drawer membership proof returns an empty pagination envelope; changed metadata cannot authorize a sibling chunk.

  - Embedded hidden references in ids, rooms, timestamps and entity fields must fail public JSON assertions on the prior head.

  - A final gate that silently drops a readable mirror row fails the count equality and zero-drop assertions.

  - Hallway fault cases discard incomplete results and preserve the pagination envelope.
  - Incomplete room/status acquisition returns zero visible sums with a count-free disabled-index reason; integrity failure flags remain, without raw failure details or shared principal caches.

  - Swallowing an authority fault must fail the fixed-503 assertion rather than silently reduce counters.

  - Guard-only tests do not substitute for base failures; metadata, authority, state and unit mutations must change public assertions.
  - Setup/runtime errors, skips, timeouts and hangs never count as RED or mutation kills; source/lock mutants must fail explicit assertions.


  - An Admin asset missing on the base fails an HTTP status assertion before module loading; independent mutations must fail security/deadline assertions, not hang.


  - Configuration refusal can hide a later guard unless that boundary is also exercised directly.
  - Feature assertions fail before DTO inspection on the unregistered base route.

  - Fixture reachability and setup failures are distinct from expected output AssertionErrors; malformed acquisition must never become an authorization grant.


  - A plan fixture that names a missing test file fails G7; a shared fixture change can break unrelated suites; slow or flaky tests waste every run.

  - Email cookie-scope regressions use an HTTPS cookie jar over loopback, including an older host-only cookie; sign-in rendering checks cover the request confirmation.


- **Tests:**

  - Large signed integers, booleans and Compact numerics cover wing, room and hall; locus-less cases cover siblings, suggestions and the 50-result cap.

  - Numeric wing/room/hall cases and three ordering surfaces run on exact and default backends; Compact additionally covers native numerics and booleans.
  - `tests/test_worker_observation_fix.py` pins request survival, active-count cleanup, consistent RSS status, forward-only startup and counted constructor latching; pairing oracles also exercise unsupported RSS platforms.

  - `tests/test_worker_observation.py` independently enumerates the diagnostic schema and injects synchronous serving delivery, synthetic canaries and bounded-writer failures.

  - Private-chat null-limit, invalid-offset and invalid-date regressions assert real import-reader eligibility and pass-through listing arguments; plain literal readers compare hidden-present and hidden-absent pages.

  - Drawer-page regressions cover import-reader totals, literal wing authority, date errors, invalid offsets, null limits and exact denial messages.

  - Question-mark placements, registry rows, null labels, unknown names, argument coercion and Chroma fallback ties have dedicated witnesses.

  - Readable graph controls, hidden bridges/top-N candidates, both statistics paths and bounded projection fault guards exercise the read boundary.

  - `tests/test_card253_s07.py` covers local/scoped parity, stop-word names, candidate caps, refusal scope, cooperative deadline cleanup and lock release alongside KG visibility and source-snapshot guards; hidden-present parity guards and missing-snapshot/unknown-key regressions cover defensive reads; literal/shorthand readers and malformed-argument protocol/handler regressions and owner/wildcard blank and canonical-empty wing cases cover compatibility, legacy hidden facts and error-shape equality.

  - `tests/test_card253_s02.py` covers nine provenance-key count collisions, hidden-pair exclusion and real tunnel/drawer gate reachability; the same typed-association and fact-provenance suites remain guards.

  - Regression selections cover owner maps and capped non-Chroma logical citation proof.

  - Typed association read and protocol suites cover public results and private snapshot failures.

  - Logical page SQLite/SDK equality, chunk visibility, owner authority, changed associations and incomplete acquisition are covered in the focused drawer-page tests.

  - Both-store RED/GREEN probes retain hidden-label controls; type checks and gate projection get head-only guards.

  - Pass-through worker calls prove acquisition; over-limit label guards reject any scan above the character cap.


  - Hallway tests verify reviewed-key output, unchanged stored rows, visible counts and exact label-limit behavior.

  - Hallway regressions additionally pin malformed-numeric row exclusion, private-label bytes and exact metadata page boundaries.

  - `tests/test_card253_s03.py` covers labels, rooms, totals, ties, complete enumeration and forged scopes.
  - `tests/test_card253_s02.py` covers room/status branches, real capacity refresh with hidden/control equality, transport diagnostic suppression, invalid wing types, no-palace/local guards and projection faults.

  - `tests/test_card253_s12.py` covers strict store/private-reader failures, ordinary deny-on-fault compatibility and second-pass enumeration failure.

  - `tests/test_card253_s12.py` covers owner progress isolation, current read authority, quota causality, incomplete proof and bounded metadata polling.
  - test_admin_instance_api.py, test_admin_instance_ui.py, admin_instance_ui.mjs and test_backup_status.py provide nine operational regression rows using fixed clocks, explicit fake timers, hostile files and synthetic CLI/hook fixtures. test_admin_instance_controls.py checks no writes, deadlines and build propagation; real-browser Admin acceptance retains owner/mobile boundaries.

  - Baseline-pass gateway and route-disposal controls accompany count/list, card-navigation and cursor-retry regressions; individual mutations prove sensitivity without counting setup failures.

  - test_admin_runtime_api.py and test_admin_runtime_ui.py are the twelve-row proof; test_admin_runtime_controls.py covers independent list cursors, absent tables, workspace filtering and completed-source survival.

  - test_runner_requesters_admin.py, the requesters case of admin_runtime_ui.mjs and the models_submit_before_loads case of workbench_g4_projects.mjs are the proof for the Admin requester edit and the early models-form binding; tests/workbench_e2e/test_project_models.py is the browser check of Save models with slow reads.

  - `test_admin_api.py` and `test_admin_ui.py` provide fourteen foundation rows; `admin_ui.mjs` exercises served navigation, production route/refresh wiring, request generations, expiry and body-decoding deadlines. `test_admin_controls.py` covers cursor binding, pagination, source budgets and gateway controls; `workbench_e2e/test_admin.py` covers real-browser owner/member navigation at three widths.


  - `tests/test_card199_safety.py` and the sync test family retain transcript-byte/cursor and server-import compatibility checks.
  - Transcript selection tests observe the actual symlink/empty rejection; memory selections separately prove refusal before selection.
  - Token tests distinguish missing selected capture credentials during initialization from import-only feed credential rejection.
  - GitHub lifecycle rendering runs in Node with deferred replies and synthetic roots, including logout during an outstanding status read.
  - `tests/test_spaces_contract.py`, `tests/spaces_surfaces.json` and the checked REST/public surface regressions.
  - Surface snapshots include only the newly projected restricted reads; disabled-handoff comparisons preserve every other legacy field.
  - Memory REST freshness doubles return the save result including its privacy notice; unreadable retry tests retain the fresh-write envelope and source redaction.

  - Focused presence and read-scope selections run alongside existing gateway, mirror, imported-history and mutation-scope regressions.

  - Thread-boundary integration tests exercise real separate workspace workers; focused scope tests cover stale snapshots, optional collections, malformed probes, decoding and response caches.

  - Release helper regressions simulate the old app disappearing before the signed-in board arrives and verify useful exception messages without credentials in JSON or Markdown reports.
  - Marked backup classification and snapshots use live synthetic B2 controls; direct-provider and version guards have observed base AssertionError regressions.

  - Synthetic token-slot proofs bind every envelope context field, reject operator/recovery wraps and test marked-workspace connection and backup refusal with an unmarked SQLCipher control.
  - Import boundary fixtures use the real Recall authorization dependency while retaining frozen continuity doubles and control-count assertions without holder reads.
  - Imported-history consent: Synthetic reader journeys check unticked consent, unchanged old credentials, bounded shared holders, not-found refusals and source-range proof.

  - `tests/test_commonplace_webhooks.py::test_dispatcher_recovers_from_programming_error` covers recovery and subsequent delivery at each pass stage; `test_dispatcher_closed_control_database_exits_silently` guards silent shutdown.
  - The suite itself under the coverage gate; `tests/test_commonplace_webhooks.py::test_dispatcher_thread_survives_an_unexpected_pass_error` checks that an unexpected delivery-pass error is logged without its private message and the dispatcher retries while staying alive.

  - `tests/test_projects_phase_b.py` and `tests/test_projects_admission.py` keep paired pin/head assertions and independent runner-lock coverage.

  - `tests/test_backup_online_enumeration.py` forces SQLite sidecar cleanup and the writer/workspace-lookup freeze interleaving, and checks that missing user files, unsafe paths and new directory creation remain guarded.

  - `tests/_mirror_drain.py` (`drain_service`) waits on the real drain condition (no sweep active, no job pending, 120 s stuck-job limit) instead of one fixed `wait_for_mirrors` budget, which returns silently when it runs out; `tests/test_search_index_sweep.py`, `tests/test_card253_s12.py`, `tests/test_handoff_adversarial_idempotency.py`, `tests/test_handoff_adversarial_security.py`, `tests/test_handoff_fact_integration.py`, `tests/test_commonplace_import.py`, `tests/test_commonplace_import_reader.py` and `tests/test_imported_history_search.py` use it, so host load cannot leave a job half-finished before the assertions. `tests/test_search_index_sweep_slow_host.py` with `tests/_slow_sweep_plugin.py` re-runs a selection of them with late-starting jobs and a wait that returns at once. The 20-thread and unit bounds are unchanged. `tests/test_card253_s12_teardown.py` with `tests/_card253_teardown_probe.py` checks that a failed teardown drain still shuts down every world's server thread, so the process exits and the drain error is reported.

  - `tests/test_commonplace_rules_inject.py` runs its functional tests on a frozen deadline clock, so a loaded host cannot spend the 80 ms injection budget and turn the injected text empty; `tests/test_commonplace_rules_inject_slow_host.py` loads `tests/_slow_host_plugin.py` (a clock that burns the whole budget on every read) and checks that the functional selection still passes because it keeps its own frozen clock; it does not exercise injection against a slow clock. The locked-store setup retries until background listener threads release the store.

  - `tests/test_commonplace_agent_defaults.py` runs every test that asserts startup content (fresh connections, warm startup, composed grants, import flow, native startup hook) on a frozen deadline clock (`frozen_deadline_clock`, opt-in per test; the late-initialize test drives its own clock) with the functional worker transport, and runs the installed startup hook command with only `HUB_TIMEOUT_S` lifted to a hang guard (the lift rewrites every installed form; the encoded PowerShell form is unit-tested only, never executed here); `tests/test_commonplace_agent_defaults_slow_host.py` re-runs the acceptance tests through `tests/_slow_host.py` with `tests/_slow_hub_plugin.py`, so every gateway POST takes longer than the production hook allowance; the hook runs with that allowance lifted, so this covers hub latency, not the 0.4 s budget. It does not starve the deadline clock, which those tests freeze. The test HTTP clients in `tests/test_commonplace_email_auth.py` and the acceptance fixture use a named hang guard instead of a 3 s timeout. `observe_sql()` in that file also stops the answer-hold worker before it opens a SQL boundary window. `test_sql_boundary_window_gets_no_background_hold_tick` checks that the helper leaves no live worker, and with a helper that does not stop it, it forces a live tick inside the window and fails on the late `SELECT`.

  - `tests/test_mcp_tunnel_server_palace.py` checks the tunnel tools and the `mempalace_graph_stats` client path against the server's palace while a different, populated palace is the ambient default.

  - `tests/test_commonplace_rules_inject.py::test_locked_store_uses_remaining_deadline` checks locked-store fallback and remaining connection/statement waits with a fake clock; the separate timing test retains the informational wall-clock budget.

  - `tests/test_chroma_client_lifecycle.py::test_session_evicts_dropped_system_before_opening_missing_database` checks eviction before client construction without depending on SQLite pool scheduling; the external-rebuild probe checks the replacement's exact drawer ids.
  - Thread-search tests pin rank-before-fetch and score/thread/index ordering at the limit boundary, including shuffled rowids and ID batches.
  - `tests/test_commonplace_owner_403_message.py` and `tests/owner_403_message.mjs` (base red: 6 failures on `origin/develop`) cover the reason and next step of a refused Done move or handoff, and that 404, 409 and other 403 answers keep their status.

  - `tests/lifecycle_ui.mjs` (run by `tests/test_card_lifecycle_ui.py`) drives the Done and Pocket card-details module alone; the shell harnesses stub its `placeLifecycle` hook.
