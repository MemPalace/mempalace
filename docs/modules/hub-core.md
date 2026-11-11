## hub-core

- **Paths:** `commonplace/__init__.py`, `commonplace/__main__.py`, `commonplace/gateway.py`, `commonplace/control_db.py`, `commonplace/auth.py`, `commonplace/policy.py`, `commonplace/decide_grant.py`, `commonplace/tokens.py`, `commonplace/extensions.py`, `commonplace/_http.py`, `commonplace/http_util.py`, `commonplace/urls.py`, `commonplace/ratelimit.py`, `commonplace/backfill_allowance.py`, `commonplace/workers.py`
- **Owns:**

  - Canonical Space REST aliases mapped to existing board tool authority.

  - The gateway (the only way into a workspace), the control database, principals, token policy, the extension seam and one memory worker per workspace.

  - The extension seam lists `commonplace.agent_schedules`, installed after holds.

  - The extension list ends with `commonplace.bots`, which installs the person-only `/api/bots` routes.

  - The extension list also loads `commonplace.routines`, which starts its worker with the other background workers and stops it before the control database closes.

  - Extension installation registers email shutdown before GitHub shutdown; HTTP server teardown closes both in that order before SQLite closes.

  - The untrusted write limit and the first-backfill allowance are environment configuration (`COMMONPLACE_WRITE_LIMIT*`, `COMMONPLACE_BACKFILL_*`); the allowance is counted durably per connection and per workspace.

- **Promises:**

  - Restricted CLI-compatible search acquires structured hits and renders only after the final drawer gate, on literal-wing and fan-out paths. Its closed response vocabulary never includes structured hits; flat rendering ignores unrelated import metadata, the pure canonical formatter is shared with the owner worker path, and CLI argument restrictions remain.

  - Owner-granted `cards:decide`: only the signed-in owner's principal can grant it, to their own top-level, non-agent, non-seat connection; loaded policies drop it with ownership; the stored grant is removed on demotion (a trigger on `UPDATE` of `memberships.role` from owner), on coordinator assignment and on every coordinator removal or replacement (`board_set`, auto-unbind; a separate control-DB commit ordered before the board COMMIT, not atomic with it) and when an expired PAT is rotated; OAuth issue and refresh return only live scopes. Holders answer only asks addressed to their own person, each answer audited as `card.decide.delegated` with the connection id; `LocalTool.untrusted_scope` admits untrusted holders to `decide` only.

  - Tokens cannot be minted with private read for non-owner holders; PAT and durable-connection authentication cap loaded private/admin scopes to current ownership. Tunnel writes require readable endpoints and scrub outgoing results.

  - Wildcard readers without private scope use existing input-authorized projections for overview, graph, KG, tunnels, hallways and drawer pages; unprojected whole-palace operations remain refused.

  - Only the explicit worker-memory observation opt-out is added to the worker environment allowlist; diagnostic data stays inside the existing per-workspace config directory.

  - Consented private-chat drawer listings validate page arguments with the worker before listing imports, forwarding the normalized limit and offset unchanged.

  - The private-chat list_rooms branch also uses list_drawers validation and reports list_drawers errors; its own public schema declares only wing, and legacy extra filters remain accepted.

  - Literal-wing drawer readers retain proved visible totals after import-aware metadata scrubbing; page arguments share worker schema defaults and error forms.

  - Graph callers validate names against worker schemas before metadata probes, retaining RPC error and coercion conventions.

  - Graph inputs are authorized before traversal, ranking and statistics; explicit totals reuse endpoint and label proof.

  - Only KG query enforces strict search booleans; other tools retain normal unknown-key handling. KG reads preserve protocol errors for unknown keys and malformed pages; non-owner shorthand filters resolve to stored names after raw policy checks. Sees-all string wing names retain worker validation and literal filtering; per-fact policy and final allow-lists still enforce visibility. Fact enrichment and KG search share projection, worker expiry and fixed empty failures.

  - Only list_rooms/status respond payloads bypass the final generic gate after existing authorization proof; provenance-named count labels remain visible.

  - Private association requests carry the authenticated workspace and are unavailable as public operations.
  - Authenticated explicit tunnel reads use request-local endpoint and drawer proof for owners and connections without altering mutation authorization.

  - Logical drawer pages authorize physical identities before grouping, dates, totals and slicing; only visible page text is hydrated.

  - Hallway reads project authorized metadata before strength sorting and pagination.
  - Authenticated room/status sums use authorized pairs. Status suppresses global capacity and raw probe reasons, reports a fixed disabled-index reason, and omits transport diagnostics and palace/database paths. Status, list_rooms (including consented private_chats imports), list_wings and taxonomy propagate backup-pause retry errors; consented private_chats drawer listings do too. Pause tests cover RPC and internal calls on those routes.

  - KG provenance writes report the offending field and citation character limit before storage; hidden-reference refusals keep their existing replies and refused writes leave existing facts untouched.

  - Supported Space routes pass the existing non-person tool visibility gate.
  - Gateway installation registers project reports without resolving workspace directories; the webhook worker recovers existing sources on its first scan and drives independent monotonic deadlines without adding authority or holding database locks during report HTTP.

  - Authenticated wing lists and search fan-out share the complete authorized pair projection. Hidden-only wings affect neither wings_searched nor the fan-out denial count; internal consumers inherit the same listing.

  - Restricted structured drawer searches with an attached handoff context gate before limiting and double ranked prefixes up to 100 candidates in at most eight worker calls on one deadline. Responses omit candidate/filter totals and count only returned hits; every-wing and standalone paths retain existing behavior, and restricted CLI-compatible search refills before its final gate and rendering. Pinned searches preserve original protocol errors and opaque worker replies before structured refill.

  - Direct mediator helpers default to no gateway; workspace-bound callers still project metadata.

  - Policy-only callers without workspace context receive empty overview and fan-out results through the shared projection boundary.

  - Gateway mutation scopes and provenance lookups follow the principal workspace; hidden targets use absent replies, and client-supplied scopes cannot widen access.

  - Imported-history consent: Imported reader results bypass wing restrictions only after exact-view proof; ordinary scope, tool, wing and mutation gates remain in force.

  - Local extension schemas may derive workspace-gated properties per request without mutating shared schemas.

  - Every request resolves to one principal or none; scopes, wings and trust are checked before any tool runs; tokens are stored hashed; writes are audited.

  - Existing workspace directory lookups pass a backup freeze, so a writer holding a database transaction can finish; new directories remain gated.

  - **Browser GitHub connection:** Workspace creation accepts the caller transaction so GitHub ownership, user creation and personal workspace provisioning roll back together.

- **How it fails:**

  - `http_util.log_failure` logs the exception type and stack locations only; for sqlite3/sqlcipher3 errors it adds `sqlite_errorname`/`sqlite_errorcode` and the message only when it is one of SQLite's fixed texts (`SQLITE_MESSAGES`), never other exception text.

  - Private-chat pagination preserves invalid-params and filter errors; incomplete validation or listing faults fail closed without raw exceptions.

  - Invalid filters retain worker error results, invalid pagination retains JSON-RPC invalid-params, and incomplete membership proof remains an empty page.

  - Malformed or unavailable graph schemas yield empty operation envelopes within the request deadline.

  - Incomplete graph metadata or tunnel proof returns the operation’s empty public envelope without global counts.


  - Incomplete pair proof still returns zero counts. Tunnel/drawer/other replies retain the gate and malformed scalar provenance remains omitted.

  - Worker failures remove only the affected citation, with no other-workspace fallback.

  - Incomplete drawer membership proof returns an empty pagination envelope; changed metadata cannot authorize a sibling chunk.

  - Incomplete hallway acquisition returns the normal empty pagination envelope.
  - Incomplete room/status acquisition returns zero visible sums with a count-free disabled-index reason; integrity failure flags remain, without raw failure details or shared principal caches.

  - Rejected methods have no tool alias; authentication and body transport refusals remain shared.

  - Malformed, partial or timed-out overview acquisition yields the same empty successful shape as no visible data, before any sees-all shortcut. Client scopes are discarded and absent from discovery.


  - A worker that does not start returns an error, never another workspace's data; the control database is the single point of failure for sign-in.

- **Tests:**

  - Private-chat null-limit, invalid-offset and invalid-date regressions assert real import-reader eligibility and pass-through listing arguments; plain literal readers compare hidden-present and hidden-absent pages.

  - Drawer-page regressions cover import-reader totals, literal wing authority, date errors, invalid offsets, null limits and exact denial messages.

  - Unknown/missing graph arguments and traversal types exercise full gateway RPC responses.

  - Readable graph controls, hidden bridges/top-N candidates, both statistics paths and bounded projection fault guards exercise the read boundary.


  - `tests/test_card253_s02.py` covers nine provenance-key count collisions, hidden-pair exclusion and real tunnel/drawer gate reachability; the same typed-association and fact-provenance suites remain guards.

  - Public typed citation tests cover trusted members, owner sessions and workspace isolation.

  - Logical page SQLite/SDK equality, chunk visibility, owner authority, changed associations and incomplete acquisition are covered in the focused drawer-page tests.

  - `tests/test_card253_s03.py` verifies visible totals, hidden client scope arguments and both persisted formats.
  - `tests/test_card253_s02.py` covers room/status branches, real capacity refresh with hidden/control equality, transport diagnostic suppression, invalid wing types, no-palace/local guards and projection faults.

  - `tests/test_spaces_contract.py` and the installed public surface inventory.

  - Public presence witnesses cover wing counts, every fan-out entry and denial, malformed payloads, deadlines and forged scopes.


  - `tests/test_commonplace_gateway.py`, `tests/test_commonplace_control_db.py`, `tests/test_commonplace_policy.py`, `tests/test_hub_` files.

  - `tests/test_restricted_search_fill.py` covers bounded refill and response privacy, with a synthetic latency fixture.
  - `tests/test_cards_decide_scope.py` covers granting, refusal, other people's asks, revocation and audit; `tests/test_cards_decide_scope_browser.py` covers the consent and Agents UI (needs Playwright; skipped without it, except that `CP_BROWSER_E2E=1` makes a missing Playwright fail).
