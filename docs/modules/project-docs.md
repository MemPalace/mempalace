## project-docs

- **Paths:** `AGENTS.md`, `CHANGELOG.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `HUB-DESIGN.md`, `INSPIRATION-REPOS.md`, `LICENSE`, `MISSION.md`, `PRODUCT-DESIGN.md`, `README.md`, `REPORT.md`, `ROADMAP.md`, `SECURITY.md`, `openarena-claim.txt`, `design/`, `docs/`
- **Owns:**

  - The agent guide states the batch merge lane: builders merge on a gate PASS plus a quick PR check subset PASS on the exact head; the full suite runs per develop batch, never per PR.
  - The project connection guide describes persistent conflict reasons and retry choices after consent dialogs close.
  - Memory sync documentation states the restricted-search refill bound, suppressed candidate counters and the restricted CLI-compatible route.
  - The project and orchestrator guides state that a role without a model raises one owner Needs me item and that Agent models are picked from the workspace's model list.
  - `docs/design/RULES.md` places the Rules entry in the account menu rather than the sidebar and phone tabs.

  - The OAuth contract records the displayed callback origin, numeric-host tightening and required pre-deploy registration inventory.

  - The rules write-check documentation lists the goals writes as checked (`docs/design/RULES.md`).

  - The operational Admin API and opt-in backup receipts, with operator provisioning and unknown-history limits.

  - `docs/chat.md` and `docs/bots.md` state that bots are the main way to talk to a project, what the project tools do for the person, the your-words rule, which actions wait for a tap, and that a post a bot forwards gets no front receipt or fallback card.

  - The card API reference documents the after-Done routes (archive, restore, carry on, Pocket), their person-only authorization and what archiving hides.

  - The Admin runtime/limits/stuck API, independent owner queue and project/runner guide links.

  - The Admin machine requester list, its addable agents and the owner-only edit, in the Admin and runner guides.


  - `docs/admin.md` is the authoritative current Admin API and partial-rollout contract, linked from the existing board API and agent connection guide.

  - `docs/admin.md` also describes the owner runner block, which manages machines through `/api/runners`, not the GET-only Admin dispatcher.

  - `docs/projects.md` names the split-start role: `main`, or the `[runner]` role when the config has no `main` role.

  - `docs/chat.md` lists the live delivery states of a chat message, their reasons and timing, and who sees the agent's read time.

  - `docs/design/RULES.md` describes the reviewer rules block and its verdict line, the session hook's missing-rules notice, and a table that maps each rule enforcement point to the tests that fail when it is skipped.


  - Live-sync instructions describe transcript-only reinstall, explicit destination and retained disabled memory history.

  - Live-sync and capture instructions document the generic `agent-log` source for agents without a native transcript.
  - Public Space endpoint and error documentation alongside legacy board routes.
  - `docs/design/RULES.md` documents the `rules_feedback` tool, its limits and its evidence routes.

  - `docs/boards/API.md` describes structured decision blocks: the `{label, detail, preview}` option shape, its bounds, and the `ask_blocks` card field; `docs/boards/schema.sql` lists the column.

  - Contributor and agent instructions, the mission and design notes, user and operator documentation, release history, the licence and the security policy.

  - `docs/transcript-capture.md` documents the capture hook's offline spool: bounds, drop log, backoff and the `hook_spool` status and drain commands.

  - `CONTRIBUTING.md` documents the hook install, the pre-push quick check and its budget, and the per-worktree environment.

  - `docs/projects.md` and `docs/orchestrator.md` state who may hold a lease on a board without `[runner]`: its own trusted project seat.

  - `docs/project-panels.md` documents the project panel file format, limits and routes.

- **Promises:**

  - `docs/agent-connections.md` explains granting, using and withdrawing `cards:decide` for an owner's own app.

  - `docs/agent-connections.md` also explains granting and withdrawing `bots:chat` and `bots:confirm` for a member's own app.

  - Admin documentation covers all seven sections, bootstrap capability fallback, canonical receipt timestamps and parent/mode refresh ordering.

  - The Admin API guide documents legacy-column defaults and enabled, unrevoked grant eligibility.
  - Unreleased notes record KG argument compatibility, wing aliases and intentional search response changes.
  - One Unreleased Changed section records KG argument compatibility, wing aliases and intentional search response changes.
  - One consistently spaced Unreleased Changed section records KG argument compatibility, wing aliases and intentional search response changes.

  - Admin docs pin current-member connection listing, matching inventory counts, exact-route card navigation and cursor-preserving Retry.

  - Documents accepted-only accounting, current subscription eligibility, machine/worker contact and per-list opaque cursors.

  - Admin docs describe active-workspace-only personal data, false instance capability, typed metadata and explicit unknown/error/empty distinctions without implying future sections are shipped.


  - Live-sync setup makes no active memory-upload or completed-coverage claim.
  - The three canonical endpoints retain the existing authority and verbatim chat contracts.

  - Imported-history consent: Imported-history documentation distinguishes write-only import from the independent reader scope and explains exclusions and revocation.
  - Project setup documentation also explains the recommended card-move webhook and explicit updates for existing subscriptions.

  - Documents enabled blockers, waiting payloads, stable split keys and bounded runner retry delivery.

  - The board API guide describes the app's Blocked by section and the plain refusal sentences for blocker edits.

  - The board API guide lists every waiting reason in precedence order, which of them refuse a claim, and the board `waiting_notes` sentences.

  - Documentation describes shipped behaviour; design notes are marked as proposals.

  - Project documentation explains opt-in start authority, shared admission, seat consent and the card-webhook delivery residual.

  - Public text uses synthetic names only and carries no secrets or private hosts.

  - Board design also documents coordinator release of restricted-origin cards.

  - Project documentation explains the coordinator status block, the owner-only End lease and what ending a lease leaves in place.

  - Board API and design documentation state default coordinator routing, audited person reasons, owner recovery and coordinator-only withdrawal of its own asks; existing asks retain their stored data.

  - Contributor test instructions state that the default run excludes the `timing` lane and that timing results are informational, never a merge gate.

  - Board API and design prose describe deliberate origin release, fail-closed provenance and signed-in owner recovery separately from proposal/plan review. They also describe creator-only reservation cancellation without activation authority.
  - Board API and design prose describe releases that outlive appointment changes, the owner's explicit Release, reservation takeover and the orchestrator and pool handoff aliases.

- **How it fails:**

  - Safe errors and missing optional sources remain distinct from measured zeros; missing operational records remain unknown.


  - Operational sections use the exact server admin predicate; public docs contain no operator configuration or credential material.


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
