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

  - Admin card navigation restores a regular base, inventory counts match joined rows, and Retry retains the failed page mode and cursor.

  - The Admin runtime, effective-cap and attention read contracts, bounded RO sources and current partial-rollout residuals.

  - The Admin person-owner/workspace boundary, partial-rollout residuals and independent source/client deadlines.


  - The canonical Space REST foundation reuses board rows as Spaces, with Board, Memory and Chat views and Board as the default.

  - The system-wide decisions, numbers, stores, residuals and waiver list (`SYSTEM.md`) and this ownership map.

  - The system-wide decisions, numbers, stores, residuals and waiver list (`SYSTEM.md`) and this ownership map, including thread-ranking invariants.


- **Promises:**

  - Graph documentation describes SQLite affinity, native Compact values and read-only legacy schema ordering.

  - Documents backend graph order, numeric locus text parity and explicit tunnel compatibility residuals.

  - Consented private-chat drawer listings validate page arguments with the worker before listing imports, forwarding the normalized limit and offset unchanged.

  - The private-chat list_rooms branch also uses list_drawers validation and reports list_drawers errors; its own public schema declares only wing, and legacy extra filters remain accepted.

  - Literal-wing drawer readers retain proved visible totals after import-aware metadata scrubbing; page arguments share worker schema defaults and error forms.

  - Documents readable legacy graph parity and the distinction between caller errors and projection faults.

  - Graph inputs are authorized before traversal, ranking and statistics; explicit totals reuse endpoint and label proof.

  - Documents citation omission and backend proof limits separately from fact eligibility.

  - Records current typed citation proof and raw path authorization before display conversion.
  - Tunnel concurrency and computed imported-drawer qualifications are documented separately from stored-association proof.

  - Explicit tunnel read proof documents current drawer associations and proof before preview hydration or traversal recording.

  - Logical drawer pages authorize physical identities before grouping, dates, totals and slicing; only visible page text is hydrated.

  - Hallway documentation preserves dynamics, lists numeric types and documents a public shared scan cap.

  - Hallway documentation names the reviewed public key set and the pre-scan label character cap.

  - Hallway documentation distinguishes plural-room final validation from drawer scrubbing.

  - Hallway read documentation distinguishes display-label token proof from citation parsing, visible counts from global storage and empty-on-incomplete behavior.

  - Documents field-specific citation length errors, atomic whole-write refusal, surface-specific citation exceptions and informational performance measurements.

  - Documents both 16,384-character authorization caps, O(n + C²) bounds, over-limit readable citation refusal and verbatim fact/storage preservation.

  - Documents bounded authorization-only reference spellings, exact producer domains and citation compatibility.

  - Fork proof uses the fixed digest before trailing context and independently proves overlapping embedded references; omitted namespace separators require an actor id marker, and capture/import punctuation remains literal.


  - Records the strict owner-progress authority probe separately from ordinary Recall denial semantics.

  - Records authenticated owner progress, process-lifetime units, fixed unavailable responses and the accepted global-quota causality.
  - Admin reads tolerate missing migration columns, accept full-length slugs and distinguish active grants from historical assignments.

  - Records workspace-scoped operational facts without inventing a workspace cap, runner liveness, queue count or backup success.


  - Records read-only SQLite behavior and the injected UTC/monotonic seam without claiming atomic observations or runtime liveness.


  - Architecture describes exact-slug, workspace-local reads without implying lifecycle support.

  - Cold-start readability, imported-room count compatibility and remaining search totals are documented separately from content range checks.

  - Standalone mediator compatibility distinguishes policy-only local helpers from workspace-bound callers without thread authority.

  - Workspace-less overview and fan-out callers fail closed; the previous legacy-listing exception is removed.

  - Visible overview and search fan-out boundaries are documented separately from the remaining hallway, tunnel, graph, provenance and progress residuals.

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

  - Documented graph ordering depends on actual snapshot schema rather than backend name alone.

  - Distinguishes accepted non-string-label and existing follow-argument residuals from fixed graph regressions.

  - Private-chat pagination preserves invalid-params and filter errors; incomplete validation or listing faults fail closed without raw exceptions.

  - Invalid filters retain worker error results, invalid pagination retains JSON-RPC invalid-params, and incomplete membership proof remains an empty page.

  - Schema lookup or projection failure remains an empty graph envelope; caller mistakes retain RPC errors.

  - Incomplete graph metadata or tunnel proof returns the operation’s empty public envelope without global counts.

  - Keeps unavailable citation links absent without changing visible facts.

  - Unproved citations disappear without removing readable fact text; plain-file proof remains a separate decision.

  - Incomplete drawer membership proof returns an empty pagination envelope; changed metadata cannot authorize a sibling chunk.

  - Every hallway string is proved; malformed types exclude rows and incomplete acquisition discards projected totals.

  - Every returned hallway string uses the same capped raw-token proof; malformed numeric fields and unreviewed keys cannot reach public rows.

  - Hallway read failures expose an empty page, never global counts.

  - An authority fault makes the complete progress response unavailable, even when other threads are readable.

  - A progress count is not a new thread read grant; other thread-boundary residuals remain documented.
  - The reused needs-me service can initialize legacy metadata; new Admin GETs have no store initialization or logical writes.


  - Legacy needs-me can initialize stores; new Admin no-write claims exclude only SQLite sidecars from file comparisons and no logical tables.


  - Later object migration requires an explicit compatibility decision.

  - Overview failure documentation promises keyed empty responses and no latency guarantee.


  - A stale block passes the mechanical check while describing old behaviour; reviewers judge whether the text is true.

  - A path added without a block fails `docs-check`.

- **Tests:**

  - The documentation obligation checks the updated graph parity contract.

  - Backend sibling, suggestion, cap and numeric-locus witnesses substantiate the compatibility topics.

  - Private-chat null-limit, invalid-offset and invalid-date regressions assert real import-reader eligibility and pass-through listing arguments; plain literal readers compare hidden-present and hidden-absent pages.

  - Drawer-page regressions cover import-reader totals, literal wing authority, date errors, invalid offsets, null limits and exact denial messages.

  - Legacy graph, default-backend and caller-error regressions accompany the documented contract.

  - Readable graph controls, hidden bridges/top-N candidates, both statistics paths and bounded projection fault guards exercise the read boundary.

  - Memory-map and capped-backend regressions verify the documented failure policy.

  - Typed association regressions and documentation obligations.

  - Logical page SQLite/SDK equality, chunk visibility, owner authority, changed associations and incomplete acquisition are covered in the focused drawer-page tests.

  - Dynamics equality, embedded-token and numeric-type guards hold the documented public shape.

  - Hallway extra-key and scan-bound cases pin the documented failure policy.

  - Readable mirror pages, exact page boundaries and private-label guards pin the documented behavior.

  - The documentation obligation covers hallway worker descriptors, public mediation and thread-reader ownership.

  - Owner-route store/private-reader fault regressions and the documentation obligation.

  - Authenticated progress regressions and documentation obligations.
  - Runtime fixed-clock, lock/write sentinel, absent/corrupt source, pagination and documentation checks.


  - Admin boundary, fixed-clock, lifecycle and documentation obligation checks.


  - Space REST contract tests and documentation obligations.

  - Presence, private read-scope and docs-obligation checks cover the scoped overview contract.


  - `tests/test_docs_obligations.py` runs `docs-check` against the merge base; `tests/test_orch_checks.py` covers unowned paths, missing obligations, empty blocks and waivers.


## project-docs

- **Paths:** `AGENTS.md`, `CHANGELOG.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `HUB-DESIGN.md`, `INSPIRATION-REPOS.md`, `LICENSE`, `MISSION.md`, `PRODUCT-DESIGN.md`, `README.md`, `REPORT.md`, `ROADMAP.md`, `SECURITY.md`, `openarena-claim.txt`, `design/`, `docs/`
- **Owns:**

  - The Admin runtime/limits/stuck API, independent owner queue and project/runner guide links.


  - `docs/admin.md` is the authoritative current Admin API and partial-rollout contract, linked from the existing board API and agent connection guide.


  - Live-sync instructions describe transcript-only reinstall, explicit destination and retained disabled memory history.
  - Public Space endpoint and error documentation alongside legacy board routes.

  - Contributor and agent instructions, the mission and design notes, user and operator documentation, release history, the licence and the security policy.

- **Promises:**

  - The Admin API guide documents legacy-column defaults and enabled, unrevoked grant eligibility.

  - Admin docs pin current-member connection listing, matching inventory counts, exact-route card navigation and cursor-preserving Retry.

  - Documents accepted-only accounting, current subscription eligibility, machine/worker contact and per-list opaque cursors.

  - Admin docs describe active-workspace-only personal data, false instance capability, typed metadata and explicit unknown/error/empty distinctions without implying future sections are shipped.


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

  - Board API and design prose describe deliberate origin release, fail-closed provenance and signed-in owner recovery separately from proposal/plan review.

- **How it fails:**

  - Safe errors and missing optional sources remain distinct from measured zeros; deployment/backups remain deferred.


  - Later operational sections remain unavailable until their authority and status producers ship; public docs contain no operator configuration or credential material.


  - Updating installed files alone leaves an old running process unaffected.
  - Rename, archive, retries, deleted queries and desktop synchronization are deferred.

  - Text drifts from code silently; nothing executes most of it.

  - Broken commands in docs surface only when someone follows them.

- **Tests:**

  - Admin API and lifecycle suites plus docs-check against the development merge base.


  - Admin API/UI contracts and `tests/test_docs_obligations.py` keep the documented boundary aligned with the shipped foundation.


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

  - Graph snapshots use local SQL locus expressions, including native booleans and integers outside int64; descriptors also bind stored sort values.

  - Graph page descriptors carry their source order key; grouped Chroma and exact numeric loci retain SQLite text casts.

  - Graph descriptors preserve literal placeholders and backend-specific hall/date values; page source determines readable tie ordering.

  - Graph inputs are authorized before traversal, ranking and statistics; explicit totals reuse endpoint and label proof.
  - Allocation observation activates only on first local dispatch; sampling construction failures release active state, construction failures latch with counted loss, and RSS status/source use the same read.

  - Storage-serving processes emit only a closed primitive allocation schema to a private two-file local diagnostic ring; forwarding and daemon jobs do not activate it. Contexts, queue, line size and retained files are bounded; observation failure never changes storage limits or restart behavior.

  - Tunnel keysets use stable ids; final lookup ignores traversal statistics and unrelated metadata while rechecking current endpoint associations.

  - Explicit tunnel probes page metadata without content, bind both endpoints and optional current drawer descriptors, and revalidate before follow hydration or recording.
  - Logical drawer pages authorize physical identities before grouping, dates, totals and slicing; only visible page text is hydrated.

  - Display-label token scans deduplicate identities without rewriting labels or changing citation parsing.

  - Hallway probes return bounded metadata pages in storage order; final reads revalidate exact descriptors.

  - Scoped wing and taxonomy counts use bounded metadata-only identity pages, normalize absent/null/question-mark loci to unknown, exclude bookkeeping, and revalidate concrete descriptors in fresh snapshots. Local unscoped readers retain their conventions.
  - Exact non-Chroma physical citation ids remain supported without document hydration.

  - A private read-only metadata capability resolves physical and logical citation ids, including every legacy chunk, without document hydration or optional collection creation.

  - Scoped KG writes recheck provenance snapshots in-lock; source/id deletes use authorized rooms and explicit ids, and missing optional closets count as zero.

  - Direct B2 providers reject malformed metadata with encryption errors; marked-workspace refusals retain an explicit locked diagnostic.

  - Operator-blind token-slot primitives accept strict versioned token/grant envelopes only; opt-in B3 metadata stays locked before any B2 key provider or database open.

  - User content is stored verbatim; ingest is append-only; search returns original words; nothing leaves the machine unless the user configured a provider.

- **How it fails:**

  - Changed locus values or sort values invalidate authorization; unavailable snapshots expose no partial graph.

  - Unknown order keys fail closed; unrelated overview bookkeeping and tunnel compatibility exceptions remain unchanged.
  - Dropped, rotated and unmatched allocation records are incomplete coverage; they cannot exclude a memory cause. Unsupported current-RSS sampling and untouched counters remain explicitly unknown.

  - Incomplete drawer membership proof returns an empty pagination envelope; changed metadata cannot authorize a sibling chunk.

  - Capped raw-token scans retain the existing grammar; checkpoints between string scans and token lookups enforce request deadline failure.

  - Changed hallway associations are excluded without rewriting stored rows; unverified internal scope calls are refused, while local unscoped pages remain compatible.
  - Unavailable metadata still fails closed without applying overview bookkeeping normalization to readable graph rows.

  - Incomplete graph metadata or tunnel proof returns the operation’s empty public envelope without global counts.

  - Incomplete acquisition or invalid descriptors discard all projected output. New or changed metadata cannot inherit an earlier allow descriptor.
  - Non-Chroma logical citations fail closed because the collection API cannot guarantee ordered snapshot enumeration.

  - Missing metadata, changed descriptors and incomplete room proof return no association; read probes never invoke mutation handlers.

  - Backend lock or index damage degrades recall; repair tools rebuild indexes without touching drawers.

  - When a session's cached database disappears during a rebuild, the session resets the palace's shared Chroma System as well as its client so dropped owners cannot leave a pool pointing at the renamed database.


- **Tests:**

  - Exact and Compact numeric/boolean witnesses and locus-less ordering cases compare with unchanged local readers.

  - SQLite, Chroma and Compact ordering guards plus numeric zero/exponent cases compare with local graph readers.
  - `tests/test_worker_observation.py` covers generated call pairs, private-value rejection, actual leaf counters, opt-out, transport lifetime, bounded contexts and cross-process ring rotation.

  - Logical page SQLite/SDK equality, chunk visibility, owner authority, changed associations and incomplete acquisition are covered in the focused drawer-page tests.

  - Hallway guards cover exact 500/501 metadata page boundaries and readable private-token labels.

  - Hallway snapshot tests cover SQLite and legacy JSON ordering and changed descriptors.
  - Default Chroma and exact-backend witnesses cover placeholders, registry rows, numeric halls and collection fallback.

  - Readable graph controls, hidden bridges/top-N candidates, both statistics paths and bounded projection fault guards exercise the read boundary.

  - Presence and private read-scope tests exercise SQLite, compact, Chroma, facet and generic metadata paths, unknown values, batching and races.
  - Capped-backend tests place a hidden chunk beyond the first short metadata page.

  - Typed association protocol tests cover optional collection absence, metadata-only paging and snapshot revalidation.

  - The `tests/test_` files for mining, search, palace, knowledge graph, backends and the MCP server.


## tests

- **Paths:** `tests/`
- **Owns:**

  - Sign-out regression covers null and mounted Admin controllers; slice fixtures supply the module binding. The installed route inventory includes the authenticated Admin workspace prefix. Sign-in and voice boot fixtures load the real bounded request helper.
  - The twelve runtime/limits/stuck/owner-queue RED rows, separate pagination/source/authority controls and updated earlier Admin lifecycle acceptance.


  - Synthetic Admin HTTP inventories and served-asset Node contracts, with assertion-only pinned-base RED evidence and identical selected head GREEN nodes.


  - Synthetic legacy safety-stop cases and explicitly classified transcript-only fixture updates.
  - `tests/test_backup_online_processes.py` reproduces separate backup/gateway ancestry and preserves foreign-writer and paused-worker refusals using a synthetic `/proc`.
  - The canonical Space REST matrix and base-captured profile/tool schemas.

  - `tests/test_commonplace_subagent_asks.py` covers released-card questions and coordinator routing, answers, escalation and recovery.

  - Shared fixtures and harnesses and every test file not listed in another block.
  - Shared fixtures and harnesses and every test file not listed in another block, including binding-epoch migration, direct fences, lock-entry instrumentation and bounded reset races.

  - Subagent question routing, proposal/release prerequisites and recipient authority regressions.

- **Promises:**

  - Graph parity fixtures include generated and absent locus schemas, Compact native values and large integers.

  - Graph compatibility witnesses distinguish required RED regressions from Chroma and old-base parity guards.

  - Consented private-chat drawer listings validate page arguments with the worker before listing imports, forwarding the normalized limit and offset unchanged.

  - The private-chat list_rooms branch also uses list_drawers validation and reports list_drawers errors; its own public schema declares only wing, and legacy extra filters remain accepted.

  - Literal-wing drawer readers retain proved visible totals after import-aware metadata scrubbing; page arguments share worker schema defaults and error forms.

  - Graph regressions compare scoped responses with local readable outputs on exact and default Chroma backends.

  - Graph inputs are authorized before traversal, ranking and statistics; explicit totals reuse endpoint and label proof.

  - Citation failure cases compare complete surviving facts with readable controls.

  - Typed citation fixtures use native stored threads and real public reads, with metadata and Recall spies.
  - Tunnel concurrency witnesses, effective hydration spies and four authorization mutants cover the explicit-read contract alongside retained mirror and import suites.

  - Explicit tunnel regression witnesses and private protocol tests cover moved drawers, room-only compatibility, bounded pages and authorization races.

  - Logical drawer pages authorize physical identities before grouping, dates, totals and slicing; only visible page text is hydrated.

  - Hallway fixtures exercise stored dynamics initialized by the real production helper in both stores.

  - Six public hallway extra-key cases fail by hidden-id AssertionError on the preceding head.

  - Readable mirror hallway regressions fail on the prior head with count/row disagreement in both stores.

  - Hallway regression witnesses exercise public tools with real stored thread authority.

  - Default provenance regressions assert zero candidate work above either cap and accurate write errors with unchanged verbatim storage. Cap-sized and 1 MiB scan measurements run separately under `timing`, reporting resource budgets without gating on them.

  - Public read/write cases cover full-field refusal, long readable controls and non-boundary fork prefixes.

  - Public KG spelling regressions retain ordinary citations and all canonical producer formats on read/write, with separate grammar, owner and exact-hash isolation checks.

  - Trailing fork regressions cover hidden read scrubbing/write refusal, readable spelling preservation, every printable ASCII suffix and ordinary URL/path/prose collision guards; concatenated references have public read/write refusal and develop byte-preservation guards.

  - Background Settings prerequisite regressions preserve rendered sections, mounted webhook input identity and unsaved values through errors, deadlines and recovery.

  - Console activity schema and query checks freeze the store clock so dated fixtures stay inside the retention window.
  - Owner progress fault regressions traverse the real wired Recall authority in paired hidden/absent worlds.

  - Progress witnesses compare authenticated hidden-present and hidden-absent worlds through the real scheduler and successful-write path in both store formats.
  - `tests/test_settings_load.py` drives fixed timers through production Settings reads, Retry and saved-state reloads, including fetch/body stalls, HTTP/network/JSON failures and contended orchestration reads.
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
  - Origin overflow stays out of ordinary proposal buckets when the bounded approvals projection truncates; list and digest retain their truncation signals.
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

  - Regression assertions compare complete scoped payloads with local outputs, including ranking and result caps.

  - Base-only regressions must be AssertionErrors, with identical test bytes and explicit rebase provenance.

  - Private-chat pagination preserves invalid-params and filter errors; incomplete validation or listing faults fail closed without raw exceptions.

  - Invalid filters retain worker error results, invalid pagination retains JSON-RPC invalid-params, and incomplete membership proof remains an empty page.

  - Regression evidence distinguishes client RPC errors from empty projection results and proves descriptor compatibility.

  - Incomplete graph metadata or tunnel proof returns the operation’s empty public envelope without global counts.

  - Deadline tests assert no worker calls; closet probes assert invocation and a readable positive result.

  - Only named public AssertionErrors count as base regressions; readable citations and plain-file display remain controls.

  - Incomplete drawer membership proof returns an empty pagination envelope; changed metadata cannot authorize a sibling chunk.

  - Embedded hidden references in ids, rooms, timestamps and entity fields must fail public JSON assertions on the prior head.

  - A final gate that silently drops a readable mirror row fails the count equality and zero-drop assertions.

  - Hallway fault cases discard incomplete results and preserve the pagination envelope.

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

  - Regression selections cover owner maps and capped non-Chroma logical citation proof.

  - Typed association read and protocol suites cover public results and private snapshot failures.

  - Logical page SQLite/SDK equality, chunk visibility, owner authority, changed associations and incomplete acquisition are covered in the focused drawer-page tests.

  - Both-store RED/GREEN probes retain hidden-label controls; type checks and gate projection get head-only guards.

  - Pass-through worker calls prove acquisition; over-limit label guards reject any scan above the character cap.


  - Hallway tests verify reviewed-key output, unchanged stored rows, visible counts and exact label-limit behavior.

  - Hallway regressions additionally pin malformed-numeric row exclusion, private-label bytes and exact metadata page boundaries.

  - `tests/test_card253_s03.py` covers labels, rooms, totals, ties, complete enumeration and forged scopes.

  - `tests/test_card253_s12.py` covers strict store/private-reader failures, ordinary deny-on-fault compatibility and second-pass enumeration failure.

  - `tests/test_card253_s12.py` covers owner progress isolation, current read authority, quota causality, incomplete proof and bounded metadata polling.
  - Baseline-pass gateway and route-disposal controls accompany count/list, card-navigation and cursor-retry regressions; individual mutations prove sensitivity without counting setup failures.

  - test_admin_runtime_api.py and test_admin_runtime_ui.py are the twelve-row proof; test_admin_runtime_controls.py covers independent list cursors, absent tables, workspace filtering and completed-source survival.

  - `test_admin_api.py` and `test_admin_ui.py` provide fourteen foundation rows; `admin_ui.mjs` exercises served navigation, production route/refresh wiring, request generations, expiry and body-decoding deadlines. `test_admin_controls.py` covers cursor binding, pagination, source budgets and gateway controls; `workbench_e2e/test_admin.py` covers real-browser owner/member navigation at three widths.


  - `tests/test_card199_safety.py` and the sync test family retain transcript-byte/cursor and server-import compatibility checks.
  - Transcript selection tests observe the actual symlink/empty rejection; memory selections separately prove refusal before selection.
  - Token tests distinguish missing selected capture credentials during initialization from import-only feed credential rejection.
  - GitHub lifecycle rendering runs in Node with deferred replies and synthetic roots, including logout during an outstanding status read.
  - `tests/test_spaces_contract.py`, `tests/spaces_surfaces.json` and the checked REST/public surface regressions.

  - Focused presence and read-scope selections run alongside existing gateway, mirror, imported-history and mutation-scope regressions.

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

  - Only the explicit worker-memory observation opt-out is added to the worker environment allowlist; diagnostic data stays inside the existing per-workspace config directory.

  - Consented private-chat drawer listings validate page arguments with the worker before listing imports, forwarding the normalized limit and offset unchanged.

  - The private-chat list_rooms branch also uses list_drawers validation and reports list_drawers errors; its own public schema declares only wing, and legacy extra filters remain accepted.

  - Literal-wing drawer readers retain proved visible totals after import-aware metadata scrubbing; page arguments share worker schema defaults and error forms.

  - Graph callers validate names against worker schemas before metadata probes, retaining RPC error and coercion conventions.

  - Graph inputs are authorized before traversal, ranking and statistics; explicit totals reuse endpoint and label proof.

  - Private association requests carry the authenticated workspace and are unavailable as public operations.
  - Authenticated explicit tunnel reads use request-local endpoint and drawer proof for owners and connections without altering mutation authorization.

  - Logical drawer pages authorize physical identities before grouping, dates, totals and slicing; only visible page text is hydrated.

  - Hallway reads project authorized metadata before strength sorting and pagination.

  - KG provenance writes report the offending field and citation character limit before storage; hidden-reference refusals keep their existing replies and refused writes leave existing facts untouched.

  - Supported Space routes pass the existing non-person tool visibility gate.
  - Gateway installation registers project reports without resolving workspace directories; the webhook worker recovers existing sources on its first scan and drives independent monotonic deadlines without adding authority or holding database locks during report HTTP.

  - Authenticated wing lists and search fan-out share the complete authorized pair projection. Hidden-only wings affect neither wings_searched nor the fan-out denial count; internal consumers inherit the same listing.

  - Direct mediator helpers default to no gateway; workspace-bound callers still project metadata.

  - Policy-only callers without workspace context receive empty overview and fan-out results through the shared projection boundary.

  - Gateway mutation scopes and provenance lookups follow the principal workspace; hidden targets use absent replies, and client-supplied scopes cannot widen access.

  - Imported-history consent: Imported reader results bypass wing restrictions only after exact-view proof; ordinary scope, tool, wing and mutation gates remain in force.

  - Local extension schemas may derive workspace-gated properties per request without mutating shared schemas.

  - Every request resolves to one principal or none; scopes, wings and trust are checked before any tool runs; tokens are stored hashed; writes are audited.

  - Existing workspace directory lookups pass a backup freeze, so a writer holding a database transaction can finish; new directories remain gated.

  - **Browser GitHub connection:** Workspace creation accepts the caller transaction so GitHub ownership, user creation and personal workspace provisioning roll back together.

- **How it fails:**

  - Private-chat pagination preserves invalid-params and filter errors; incomplete validation or listing faults fail closed without raw exceptions.

  - Invalid filters retain worker error results, invalid pagination retains JSON-RPC invalid-params, and incomplete membership proof remains an empty page.

  - Malformed or unavailable graph schemas yield empty operation envelopes within the request deadline.

  - Incomplete graph metadata or tunnel proof returns the operation’s empty public envelope without global counts.

  - Worker failures remove only the affected citation, with no other-workspace fallback.

  - Incomplete drawer membership proof returns an empty pagination envelope; changed metadata cannot authorize a sibling chunk.

  - Incomplete hallway acquisition returns the normal empty pagination envelope.

  - Rejected methods have no tool alias; authentication and body transport refusals remain shared.

  - Malformed, partial or timed-out overview acquisition yields the same empty successful shape as no visible data, before any sees-all shortcut. Client scopes are discarded and absent from discovery.


  - A worker that does not start returns an error, never another workspace's data; the control database is the single point of failure for sign-in.

- **Tests:**

  - Private-chat null-limit, invalid-offset and invalid-date regressions assert real import-reader eligibility and pass-through listing arguments; plain literal readers compare hidden-present and hidden-absent pages.

  - Drawer-page regressions cover import-reader totals, literal wing authority, date errors, invalid offsets, null limits and exact denial messages.

  - Unknown/missing graph arguments and traversal types exercise full gateway RPC responses.

  - Readable graph controls, hidden bridges/top-N candidates, both statistics paths and bounded projection fault guards exercise the read boundary.

  - Public typed citation tests cover trusted members, owner sessions and workspace isolation.

  - Logical page SQLite/SDK equality, chunk visibility, owner authority, changed associations and incomplete acquisition are covered in the focused drawer-page tests.

  - `tests/test_card253_s03.py` verifies visible totals, hidden client scope arguments and both persisted formats.

  - `tests/test_spaces_contract.py` and the installed public surface inventory.

  - Public presence witnesses cover wing counts, every fan-out entry and denial, malformed payloads, deadlines and forged scopes.


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
  - Owner coordinator settings list eligible active PAT connections; signed-in owners see restricted-origin recovery once per visible board across backlog, todo and doing.
  - Owner notices count release and configuration separately; notices stay out of digest while actionable origin cards retain reasons and actions in it. Non-person callers receive no owner notice.
  - Metadata uses an exact active slug on a plain store connection; list retains expiry and create retains rules enforcement.
  - Non-done to done transitions capture metadata in the source transaction. Unavailable report settings reject before status or lesson writes; new boards without a coordinator remain history only. Raw events retain their existing matching and authorization.

  - Every card change is one transaction with its `task_events` row; event emission happens after commit and a failed emission never undoes the change; on a configured board only `task_approve` removes `plan` and plan-check gates plan approval.

  - Trusted agents retain their title/body edit alternative on unassigned restricted-origin todo cards. Coordinator-derived edits require verified independence: creators, descendants and unsafe lineage cannot use that exception. Content edits never release origin or grant claims.

  - With workspace orchestration enabled, claims hold runner admission through board commit and person assignments or handoffs cannot reserve a busy agent.

  - Subagent and restricted-agent submissions remain inactive until deliberate origin release. An appointed independent coordinator needs current memory read/write, board-wing read/write and visible space_read, space_post, task_approve and needs_me; decide is not required. Ancestors remain eligible, while creators and their descendants cannot obtain new release authority. Missing, foreign or cyclic ancestry and unverifiable connection-creator stamps fail closed on every agent release path, including stored markers.
  - The appointed independent coordinator may release origin while holding the card, preserving status, holder, every lease timestamp, provenance and pending review. Its marker is revalidated against current appointment, liveness, lineage and grants. This grants no self-review, rejection or escalation rights; origin release requires an affirmative decision and a nonempty reason of at most 500 characters.
  - Signed-in workspace owners recover origin only when the configured record is absent/non-live, or verified independent but missing a required release grant. A live creator, descendant or unsafe lineage requires coordinator configuration and blocks owner release, even with missing grants. An unverifiable creator stamp plus any live configured record also blocks release: clear the coordinator first. Clearing affects the entire board and routes its other unreleased cards to owner recovery as well. Owner-role connection tokens gain no person fallback.
  - Owner recovery preserves pending proposals and plan reviews: release origin first, then review separately. Origin/configuration items appear once in approvals with release/you needs and approve/configure_coordinator actions; healthy coordinator work creates no owner origin item. Explicit person-targeted backlog proposals and plans retain their independent review route, including owner rejection outside fallback without origin release. Origin audit events keep kind=decide with choice="release origin"; that exact validated choice is reserved and refused in user decisions and ask options.
  - Person review items advertise reject only while origin awaits release that the viewer cannot perform, with a matching explanation; proposal Decline and plan Reject controls render independently of approval permission; other review actions remain intact. Owners retain pending-proposal rejection in configuration states via the card/API approve route with approve=false; the configuration item itself offers only board settings. Claim delivery excludes origin-release decide events by choice so an earlier return-routed decision, including a reply-only decision, is delivered unchanged.

  - Holders may annotate an inactive status line without activation.

  - Subagent asks on released cards reach the live board coordinator, which may answer or escalate with a reason; absent a live coordinator the owner route remains, including asks already routed before a coordinator becomes unavailable or is replaced.

- **How it fails:**

  - Hidden metadata is absent; hidden repo collisions are forbidden; unsupported shapes execute no domain method.
  - Report routing defers one event durably before advancing the global cursor; authorization retries indefinitely with fixed inspection code and persisted count/since, independent of HTTP attempts. Control persistence failures retain unacknowledged work.

  - A swallowed emission loses a notification but not the change (the trigger sweep reads `task_events`); webhook delivery retries and records failures.

  - Each unexpected dispatcher-pass exception is contained and logged without its private message, then waits on the stop event for 5 s, doubling after consecutive errors up to 300 s, before continuing.

  - A successful pass resets the backoff; stopping interrupts the wait, and a closed database still ends the dispatcher.

- **Tests:**

  - `tests/test_spaces_contract.py` covers DTOs, isolation, authority, collisions and side effects.
  - `tests/test_project_reports.py::test_enqueue_deferral_raw_progress_and_backoff`, `test_send_retry_accounting_and_identity` and `test_auth_retry_inspection_beyond_latest_100` pin durable retries and inspection.

  - `tests/test_commonplace_boards.py`, the `tests/test_commonplace_board_` files, `tests/test_commonplace_decide.py`, `tests/test_orch_plan_gate.py`, `tests/test_commonplace_webhooks.py`.

  - `tests/test_commonplace_subagent_asks.py` checks question routing and recipient authority.


## runner

- **Paths:** `commonplace/runner/`, `docs/runner.md`
- **Owns:**

  - The runner guide links the owner Admin contact/grant projection without adding runner management actions.


  - Machine enrollment and grants, runner jobs in `runner.sqlite3`, the requester and machine routes, the local launch client and chat answers.
  - Machine enrollment, binding epoch migrations and immutable job epoch snapshots, grants, runner jobs in `runner.sqlite3`, the requester and machine routes, the local launch client and chat answers.

- **Promises:**

  - Admin keeps machine and worker contact separate and always reports owner attestation as unverified; expired job inspection never sweeps or renews leases.


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

  - Absent optional runner stores remain absent for Admin; existing unreadable sources produce safe errors rather than fabricated usage.


  - A reset invalidates queued/leased snapshots; previously starting work becomes uncertain and retains capacity. A bound provider change requires explicit reset before reattestation.

  - A lost launch result becomes `uncertain` and waits for a person; a revoked machine cancels queued jobs on the next sweep; a busy queue refuses with `busy`.

- **Tests:**

  - tests/test_admin_runtime_api.py covers machine timestamps, unverified binding, expired lease omission and absent/corrupt runner sources; existing runner owner tests retain authority contracts.


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

  - Source outboxes and observed stuck episodes, internal seat bindings, publication receipts and reserved report dispatch.
  - Report installation touches only control state; dispatcher passes resolve existing board databases without creating workspace directories, migrate sources lazily by path, inode and SQLite schema version, and isolate workspace failures, including publication ProgrammingError, with diagnostics that omit exception text and local values. Ordinary data writes retain the initialization cache; a new inode or a changed schema version triggers initialization again.
  - Recovery skips zero-byte sources before connecting and uses a read-only base-schema probe for nonempty sources. Missing `boards` or `tasks` defers all report writes and migrations until a later pass, leaving creator initialization unblocked.
  - Report source initialization installs its reset trigger only when every watched task column exists. Legacy status writes remain usable until BoardStore adds missing columns and installs the trigger; `tests/test_project_report_legacy_source.py` covers pump recovery and subsequent migration.
  - The optional read-only runner input to shared usage accounting.


  - Project orchestrator seats, project model choices, project limits, project wings and optional hosted plans.

- **Promises:**

  - Orchestration settings reads wait at most one second for the shared control lock, then return the existing retryable `settings_unavailable` refusal without reading an unlocked connection.
  - The explicit Admin-only optional-input flag permits absent runner jobs/config without initialization while ordinary doing cards still count. Admission defaults retain strict runner input and shared accounting.


  - Connect recommends a board-scoped subscription to `space_message` and `card.moved`, retaining `card.created` with orchestration enabled, without creating or modifying webhooks.
  - Connect recommends a board-scoped subscription to `space_message` and `card.moved`, retaining `card.created` and adding `project.report` with orchestration enabled, without creating or modifying webhooks.

  - The opt-in inbox excludes children and coordinator roots, exposes keyed children, and accepts first-wins split markers only from the coordinator or signed-in owner.

  - Split marker task references are range-checked before SQLite binding and settings-read failures remain retryable.
  - Default-off project reports capture the current coordinator and activation/binding generations, baseline existing staleness, scan at most 100 cards and publish at most 100 rows per workspace pass, and retain receipt-backed event identity across crashes.

  - Only a signed-in owner changes models or limits; `max_agents` counts distinct connection assignees with orchestration off; opt-in orchestration shares card/job admission accounting, default cap 3, owner-only settings and atomic seat trust; model resolution goes project, then workspace, then the runner default.

  - **Browser GitHub connection:** Signup plan assignment accepts the provisioning transaction to preserve atomic GitHub ownership reservation.

- **How it fails:**

  - Snapshot failures leave observation state untouched. Missing seats yield history only; changed bindings cancel old reports. Unknown authorization errors retain fixed-code retries without exposing exception text.
  - Corrupt existing input still raises; accounting does not create an empty substitute store or clamp actual use to its cap.


  - An unresolved model refuses a config-driven start before insert; a limit change does not stop work already started.

- **Tests:**

  - `tests/test_project_reports.py::test_done_writers`, `test_seed_done_new_board_history_only`, `test_idle_stuck_and_episode_resets`, `test_scan_budget_isolation_cleanup_recovery` and `test_publication_crashes_and_lock_order`.
  - tests/test_admin_runtime_api.py::test_limits_orchestration_on and test_optional_stores_no_initialization, plus existing project limits/admission tests.


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

  - Graph mediation validates and rechecks the descriptor-bound SQLite sort values before traversal and ranking.

  - Graph traversal and ranking preserve backend sibling order before caps; grouped stats preserve their distinct tie convention.

  - Literal-wing drawer readers retain proved visible totals after import-aware metadata scrubbing; page arguments share worker schema defaults and error forms.

  - Graph authorization binds literal projected metadata and keeps readable registry placements; null-label scans preserve stored values.

  - Graph inputs are authorized before traversal, ranking and statistics; explicit totals reuse endpoint and label proof.

  - Outgoing typed citations require current wing and thread authority plus snapshot revalidation; raw source_file tokens are authorized before basename display.
  - Tunnel projection validates names before acquisition, pages by identity and preserves live readable associations across non-association updates.

  - Tunnel visibility combines wing policy with the workspace reader, renews authority after probing, and discards incomplete proof.

  - Logical drawer pages authorize physical identities before grouping, dates, totals and slicing; only visible page text is hydrated.

  - One public key tuple projects hallway rows at both producer and gate, with all strings scanned by the same bounded mechanism.

  - A shared thirteen-field hallway projection preserves dynamics and readable mirror rows before counting and at the final gate.

  - The hallway final gate preserves readable mirror-wing rows and their visible counts after plural-room validation.

  - Every returned hallway string and room entry needs current authority for each independently recognized thread/private reference.

  - A dedicated strict Recall callback propagates owner-progress lookup faults; extraction retains the ordinary deny-on-fault callback.

  - Search-index polls authorize current metadata once per thread, revalidate descriptors in one snapshot and project per-thread successful units, activity and quota refusals.
  - Complete protected-reference shadows join ordinary authorization tokens; namespace/source case normalizes only in the authorization copy, and full identity/hash suffixes remain literal.
  - Citation parsing optionally raises a specific decoded/NFKC length error for writes while retaining fail-closed read projection; token collection deduplicates thread ids on insertion in first-occurrence order.

  - Decoded and normalized reference scans are each capped at 16,384 characters and fail closed without partial proof; non-boundary run starts retain ordinary authorization, and only earlier runs admit overlapping shadows.

  - Complete protected-reference shadows join ordinary authorization tokens; namespace/source case normalizes only in the authorization copy, and full capture/import identity/hash suffixes remain literal.

  - Fixed-length fork shadows prove the digest with arbitrary trailing characters, replacing only its own ordinary trailing context while every overlapping protected spelling still requires proof; omitted separators distinguish ids from prose actors.
  - Overview pair authorization reuses the existing response reader in the authenticated workspace, including owners, with response-only memoization and no persisted authorization. Whole-holder counts require ordinary thread authority; import range consent grants no whole-room authority.
  - Overview projection opens the authenticated workspace store before deadline-bound authorization and uses the existing room predicate without adding whole-thread authority requirements.

  - Missing gateway or handoff context does not discard ordinary readable pairs; workspace-bound projections deny unresolved thread pairs through the existing reader.

  - A principal without workspace context gets an empty overview before worker acquisition, shared by direct lists, taxonomy and fan-out.

  - Overview pair authorization reuses the existing response reader in the authenticated workspace, including owners, with response-only memoization and no persisted authorization. Readable imported rooms retain existing presence/count visibility; imported content keeps its separate range proof.

  - Provenance uses one bounded decoder and response-local access caches; extraction by a non-reader leaves the source cursor untouched.

  - Imported-history consent: Imported readers receive bounded immutable views, source-range proof and view-specific metadata; capture tails never inherit import authority.
  - Combined read boundaries: One response access checker retains normalized provenance and private-source authorization alongside import range proof, including deadline-bound startup reads.
  - Ordinary transcript compatibility: Recall checks import identity fields only for imported threads; ordinary policy-only callers retain their read path.
  - Legacy import compatibility: Ordinary reads preserve three-part filer identities without rewriting stored data; reader views still require four-part identities.

  - A handoff never loses the source thread; continuity is append-only and resumable.

  - Verified thread FTS identities provide scope and tie keys before limiting rowids; only selected rows fetch canonical chunks.

  - Scores, thread IDs and chunk indices determine stable ordering; index-drift repair and logical fallback remain in force.


- **How it fails:**

  - Malformed sort values or a changed snapshot descriptor discard projected graph output.

  - Incomplete proof still returns empty envelopes; accepted non-string tunnel-label and pre-existing follow-argument residuals remain.

  - Invalid filters retain worker error results, invalid pagination retains JSON-RPC invalid-params, and incomplete membership proof remains an empty page.

  - Projection and schema faults stay distinct from caller validation errors; non-string labels other than null remain ineligible.

  - Incomplete graph metadata or tunnel proof returns the operation’s empty public envelope without global counts.

  - Association faults or revoked authority omit citations after original-source fact eligibility; words remain verbatim.

  - Incomplete drawer membership proof returns an empty pagination envelope; changed metadata cannot authorize a sibling chunk.

  - Non-numeric dynamics/counts and unproved or oversized string values exclude the whole row before counts.

  - Every projected hallway string, including room entries and timestamps, fails closed above the public shared reference cap before token scanning.

  - Invalid hallway rows are excluded before counts/pages; only a failed final-gate recheck empties the entire page and its counts.

  - Malformed rooms and roomless mirror hallways fail closed; display labels stay verbatim.

  - Store or private-reader exceptions invalidate the entire owner progress proof without an unscoped fallback.

  - Incomplete progress proof fails unavailable without global counters; process restart drops unit attribution, and shared storage may block readable work.
  - Missing, unresolved, malformed, revoked or failed thread authorization hides the pair. Any incomplete private acquisition discards all sums.


  - A failed launch of the successor leaves the source agent running; an interrupted archive adoption resumes from its last committed step.

- **Tests:**

  - Graph protocol fault, live-reader and legacy locus-order witnesses exercise mediation.

  - Sibling ties, fuzzy suggestions, cap membership and grouped statistics are compared to local readable outputs.

  - Drawer-page regressions cover import-reader totals, literal wing authority, date errors, invalid offsets, null limits and exact denial messages.

  - Literal placeholders, registry rows, null tunnel labels and malformed schema guards cover projection compatibility.

  - Readable graph controls, hidden bridges/top-N candidates, both statistics paths and bounded projection fault guards exercise the read boundary.

  - Typed citation tests cover moved targets, legacy chunk handles, raw directories and revoked authority.

  - Logical page SQLite/SDK equality, chunk visibility, owner authority, changed associations and incomplete acquisition are covered in the focused drawer-page tests.

  - Hallway probes cover dynamics preservation, all public strings, malformed numeric values and defensive gate projection.

  - Public hallway regressions cover extra provenance, singular-room and preview fields in both stores and prove the pre-scan cap.

  - Hallway eligibility tests require zero final-gate drops and count/total agreement for readable mirror rows.

  - Hallway guards cover owner access, revocation and final-gate stability.

  - `tests/test_card253_s12.py::test_card253_s12_authority_fault_fixed_unavailable` exercises real Recall faults in both store formats.

  - `tests/test_card253_s12.py` and the search-index backlog and sweep suites cover attribution, revocation, snapshots, quota causality and deadlines.
  - Presence and private read-scope tests cover native private threads, owner/shared readable threads, workspace binding, cache isolation and later revocation.


  - The `tests/test_handoff_` files, `tests/test_commonplace_continuity_unit.py`.


## chat-and-voice

- **Paths:** `commonplace/chat/`, `commonplace/voice.py`, `commonplace/phone_push/`
- **Owns:**

  - The workspace chat front agent, server-local dictation and opt-in phone notifications.

- **Promises:**

  - Chat status counts cards in needs-me while ignoring per-board coordinator notices.
  - Opt-in phone needs-you evaluation includes actionable Approvals, so owner origin recovery still notifies after coordinator revocation.

  - Chat answers come from the configured provider or the poster's own runner, never a silent fallback; audio stays in request memory; push is opt-in.

- **How it fails:**

  - Without a provider or runner the chat shows a receipt and no answer; push delivery failures are recorded, not retried forever.

- **Tests:**

  - The `tests/test_chat_` files, `tests/test_commonplace_voice.py`, the `tests/test_commonplace_phone_push_` files.
  - Coordinator-revocation recovery verifies the origin approval item and retains notification, priority and duplicate-suppression assertions.


## memory-and-search

- **Paths:** `commonplace/memory_tools.py`, `commonplace/search_index_api.py`, `commonplace/store_embed.py`, `commonplace/store_switch.py`, `commonplace/workspace_store.py`, `commonplace/capture.py`, `commonplace/capture_client.py`
- **Owns:**

  - Memory tools and routes under the caller's policy, search index progress, workspace store generations and cutovers, and opt-in chat capture.

- **Promises:**

  - Owner memory maps retain readable person relationships when a citation cannot be proved.

  - Owner HTTP progress always forwards the authenticated principal, returns only readable progress and disables response caching.

  - Reads and writes respect the caller's wings and scopes; store cutovers are backup-gated and restartable; capture is explicit and append-only.

- **How it fails:**

  - Missing, deleted and unreadable citation targets omit memory links without failing the map.

  - Incomplete progress proof returns the fixed 503 error without partial totals or private failure details.

  - An interrupted cutover keeps the previous store live; an index rebuild degrades search speed, not stored text.

- **Tests:**

  - Memory-map regressions compare hidden and absent citation targets and retain readable fact text.

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

  - Runtime switch/orchestrator/machine read projectors, effective limits, stuck facts and separate legacy needs-me rendering in the existing Admin lifecycle.


  - The GET-only Admin dispatcher, scoped users/connections projections, account/palette navigation and independently settling page controller.


  - The cookie-authenticated web app and the live console, including the owner orchestration switch, project seat-consent controls and default agent-limit display.

- **Promises:**

  - Sign-out and expiry stop an existing Admin controller; it is null until mounted.

  - Background prerequisite failures keep rendered Settings sections and form drafts mounted, surfacing the named error separately; initial and foreground failures still render Retry.

  - Settings and its prerequisite reads use ten-second per-request deadlines through JSON decoding; each failed section is named with Retry while successful sections retain saved values.
  - Admin validates runtime metadata, probes optional columns, filters inactive grants and preserves paginated rows with source-specific retry errors.

  - Only the exact Admin route bypasses shared refresh; leaving it disposes requests, card hashes load their requested card, joined counts match rows and Retry repeats the failed page.

  - Uses generation-aware encrypted RO board/runner reads and workspace-scoped control SELECTs only; injected UTC/monotonic clocks, query-only connections, short busy bounds and progress interrupts apply to all new sources. No store, seat or subscription initialization or application lock; pagination is list-bound and deduplicated.

  - Admin requires a signed-in current-workspace owner before read-only SELECTs; explicit credential-free allowlists, display redaction, 100-row cursors, injected clocks and 2 second source/5,000 ms client deadlines preserve private state boundaries.


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

  - Needs me renders origin/configuration as approvals, requires a release reason and posts directly; coordinator configuration navigates to existing settings without mutating a card. Proposal Decline and plan Reject controls follow reject permission independently of approve permission and use the existing rejection handler.
  - Shell needItems includes approvals when counting each project and retaining selected details on refresh; release ranks between person decisions and plan review.

- **How it fails:**

  - Runtime suberrors preserve completed sibling facts; needs-me and stuck settle independently. Only deployment/health and backups remain later work. Missing optional data stays absent and corrupt sources return safe errors.


  - Safe source errors remain local to one section; session expiry/disposal clears data and pending generations. Deployment/health and backups are labeled as later work with no requests; token contact is not liveness.


  - Rendering errors surface in the browser; the server state is unaffected.

- **Tests:**

  - tests/test_admin_runtime_api.py, tests/test_admin_runtime_controls.py, tests/test_admin_runtime_ui.py and tests/admin_runtime_ui.mjs cover fixed-time accounting, authority, wildcard subscription eligibility, no-write/lock sentinels, partial failures and GET-only pagination.


  - `tests/test_admin_api.py` covers person/role/workspace refusals, fixed lifecycle time, pagination, redaction and no writes/locks; `tests/test_admin_ui.py` and `tests/admin_ui.mjs` cover cold routing, independent deadlines, retries and disposal.


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
