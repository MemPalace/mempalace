## web-and-console

- **Paths:** `commonplace/web/`, `commonplace/console/`
- **Owns:**

  - The card panel offers Release with a required reason on cards awaiting release, the handoff form names the orchestrator and pool recipients, and refused card actions appear inside the open panel.

  - Deck bots (`deck-bots.css`, `bots.js`): the opt-in roster becomes responsive tiles and an open conversation becomes a bot-chip strip over a centred thread. Project bots offer a read-only work glance using the existing person-scoped project read route. The `cmmn.deck.draft` session key fills the named bot’s composer exactly once in either look, preserving other bots’ drafts. Classic additions stay hidden and every new CSS selector is scoped to Deck.

  - The ledger lines of the card panel's Runs list in `cardruns.js`: end status, tokens, cost and log pointer of a started run.

  - Bots-first navigation (`routes.js`, `rail.js`, `app.js`): a bare visit resolves to `#/bots` for a person who has a project (the first-run screen stays for one who has none); the rail and the phone tabs list Bots and Memory only, with the Bots count taken from the roster (`waitingTotal` in `bots.js`); Projects, Needs me and Console sit in a "Workspace" group of the account menu (desktop rail and phone top bar), beside Rules, Workspace settings and Admin. `boardRoute` keeps the old landing board for shared card links and the exit from Admin; every earlier deep link resolves as before.

  - Deck settings (`deck.css`, `toolsHTML` in `deck.js`): in the Deck look Settings is one column of cards, the shell dialog and the shared `.opt` / `.blockbtn` buttons take the Deck tokens, and on a phone, which has no account menu, Settings opens with a Workspace section linking Needs me, Projects, Memory, Console, Rules and (for admins) Admin.

  - The opt-in Deck look (`deck.js`, `deck.css`, `fonts/inter-latin.woff2`): one per-browser switch (`localStorage` key `cmmn.ui`, the Look section in Settings, or `?ui=deck` / `?ui=classic` once). On, the shell draws the Deck top bar and phone tabs instead of the rail, with the workspace tools in the account menu, and `deck.css` re-tokens every screen; every rule in it is scoped to `:root[data-ui="deck"]`, so the file is inert while the switch is off. Deck-only screens register in `SCREENS` and are reached through `state.deck` in the shell.

  - The bots screens (`bots.js`, `botstream.js`, `bots.css`, routes `#/bots` and `#/bots/<id>`): a roster of named bots with one state dot each and one streamed thread per bot, drawn from the `/api/bots*` contract. `botstream.js` is the DOM-free SSE frame parser and the message sender; `bots.js` renders blocks (actions on one line, work chip, decision buttons and reply field through the existing decide route, confirm chip, setup block, a "Waiting on you" strip) and owns polling, the unsent-text retry and the memory/rename/archive sheet. See "Using bots in the web app" in `docs/bots.md`.

  - The Admin runner block (`adminrunners.js`): owner-only machine list, enroll, attest and wings edit over `/api/runners`, with one-time token display kept outside the Admin sections' redraws.

  - The Needs me Set up row for a missing model role, and the project settings model picker with its stored id, add-a-model form and role focus.

  - The instance-admin sign-up waitlist routes (`GET`/`POST /api/admin/instance/waitlist`, mounted from `commonplace/capacity.py`) and the plain `waitlist` sign-in message in `signin.js`.

  - The Admin runner block (`adminrunners.js`): owner-only machine list, enroll, attest, wings edit and two-step revoke over `/api/runners`, with one-time token display (and a token-free setup note) kept outside the Admin sections' redraws; revoking a machine clears its token panel.

  - Runtime switch/orchestrator/machine read projectors, effective limits, stuck facts and separate legacy needs-me rendering in the existing Admin lifecycle.


  - The GET-only Admin dispatcher, scoped users/connections projections, account/palette navigation and independently settling page controller.

  - The card panel shows a card's path contract (`Owns paths`).


  - The card panel's Runs section (`cardruns.js`) lists the card's start jobs with Cancel and the owner's Resolve choice, and offers the owner a Start agent control fed by the options route, with plain words for each refusal.

  - The cookie-authenticated web app and the live console, including the owner orchestration switch, project seat-consent controls and default agent-limit display.

  - The chat pane's live delivery block (`chatlive.js`, mounted by `chat.js`): one line per server state with owner or member wording, shown in place of the timed "Working on it…" guess whenever the chat page carries `live`.

  - Owner REST for the workspace goals tree (`web/goals.py`: GET/POST `/api/goals`, POST `/api/goals/card`), a thin layer over `BoardService`.

  - Settings mounts the owner-only Scheduled prompts section (`agent-schedules.js`) next to Agents; members see nothing there.

- **Promises:**

  - The Admin runner wing editor loads its picker from `GET /api/runners/wings` under a client deadline; on failure it keeps granted wings ticked and accepts typed wing names. `GET /api/connections` gets its wing names from the same index read (`workspace_wing_names` in `web/agents.py`).

  - The Runs list shows a run's reported tokens, cost, end and log pointer; an unreported value reads "not reported", never zero, and a log pointer is a link only when it is an http(s) address.

  - Settings → Agents offers an unticked `cards:decide` consent on create, shows the grant on PAT and OAuth rows with **Stop answering**, and offers **Allow answering Needs-me** only on rows the server marks `decide_grantable` (the viewing owner's own eligible, unexpired PATs; never agents, runners, seats, board coordinators or other members' connections; the grant route refuses coordinators too); the consent copy comes from `decide_consent` in `GET /api/connections`; `PATCH /api/connections/<id>` with `{"decide": bool}` grants (PAT only) or withdraws (PAT or OAuth).

  - Event polling sends the readable-set epoch and refetches the open screen, including card details, before adopting a reset cursor. The owner feed resets non-empty member-era epochs; pre-deploy member tabs require a reload to resume live events.

  - Non-owner person event feeds project payloads and cursors into visible events; private-only insertions do not change member responses.

  - Admin callback eligibility reconstructs person policies with the queried membership role rather than assuming private read authority.

  - Project Connect keeps every 409 reason in its status, including shared-memory and replacement guidance after cancelling a dialog. Accepted choices clear that conflict before retrying; the memory-access rules remain unchanged.

  - Sign-out and expiry stop an existing Admin controller; it is null until mounted.

  - Background prerequisite failures keep rendered Settings sections and form drafts mounted, surfacing the named error separately; initial and foreground failures still render Retry.

  - The Admin runtime panel lists each machine's requesters and the active trusted agents an owner may add; its one write is the owner-only add or remove of a single requester on the stored list, and the project settings models form binds its save handler before the page's slow reads.

  - Settings and its prerequisite reads use ten-second per-request deadlines through JSON decoding; each failed section is named with Retry while successful sections retain saved values.
  - The navigation-only instance flag falls back to false on read faults without breaking /api/me; receipt parent refreshes invalidate older mode generations.

  - The runner block never stores, logs or redraws enrollment tokens: they live only in a dismissible panel that sign-out, session expiry and leaving Admin also clear.

  - Admin validates runtime metadata, probes optional columns, filters inactive grants and preserves paginated rows with source-specific retry errors.

  - Instance deploy/backups and /api/me share the exact per-request RO users.is_admin flag predicate after person-owner checks. Operational denial text is exact; deployed SHA is package data, health is independently validated, and receipt mode retries preserve siblings. test_admin_instance_api.py and test_admin_instance_ui.py cover authority, schemas, independent deadlines and disposal.

  - Only the exact Admin route bypasses shared refresh; leaving it disposes requests, card hashes load their requested card, joined counts match rows and Retry repeats the failed page.

  - Uses generation-aware encrypted RO board/runner reads and workspace-scoped control SELECTs only; injected UTC/monotonic clocks, query-only connections, short busy bounds and progress interrupts apply to all new sources. No store, seat or subscription initialization or application lock; pagination is list-bound and deduplicated.

  - Admin requires a signed-in current-workspace owner before read-only SELECTs; explicit credential-free allowlists, display redaction, 100-row cursors, injected clocks and 2 second source/5,000 ms client deadlines preserve private state boundaries.


  - The project panel (`projectpanel.js`): mounted on the board with one line in `app.js`, draws the stored panel as escaped text with http(s)-only links, and nothing for a project without one; its rules are adopted as a constructed sheet (no inline style element); covered by `tests/projectpanel.mjs` via `tests/test_project_panel_ui.py`.
  - Imported-history consent: The owner form offers independent unticked import and reader checkboxes and displays reader authority beside Revoke.
  - The Connect checklist renders the supplied webhook event recommendation, including card moves and optional card creation, with escaped values.

  - Enabled waiting tags show dependency titles or occupied-slot counts and suppress the available tag.

  - `blockers.js` renders the card-details Blocked by section (blocker title, Waiting or Done, Clear, Add blocker by card number) only when card views carry `blocked_by`, and edits the list through the card `PATCH`.

  - `lifecycle.js` renders the card-details Done and Pocket section (Archive, Restore, Carry on, pinned facts) from the server's `can_*` flags and builds its REST requests; `details.js` only exposes an `afterDraw` hook, so `tests/lifecycle_ui.mjs` (with `tests/test_card_lifecycle_ui.py`) drives the module alone.

  - `waiting.js` shows the server's waiting sentence as a board tag and in the card details, and a strip of `waiting_notes` above the board; it adds no sentences of its own.

  - `card-reviewer.js` renders the card panel's Reviewer row: a member picker for the owner (server-enforced, default says the workspace owner reviews) and a read-only name for everyone else; the board card carries a Reviewer tag.

  - A current refresh failure clears that responsibility, so later background reads still preserve busy Rules forms.

  - A background refresh never replaces an edit, hand-off or ask form open in the card panel, including one opened while the refresh's card read was in flight. On Needs me (including a hash the router does not recognise, which draws it too) the item's detail waits (paused) while a hand-off form is open; Cancel, a saved hand-off, choosing another item or a filter that drops the item gives the panel back and focuses the item heading, and a read that failed meanwhile is repeated. J and K do not move the selection while a form is open, wherever focus is. Closing the panel does not read a card, so the close itself does not reopen it; a later change to the Needs me payload re-selects the item and opens the panel again, as before. When a filter leaves no items, focus goes to the filter button. On a card route the panel redraws from a fresh card read.

  - The latest refresh carries a pending foreground render for its screen, so background events cannot leave Settings or Console on Loading; completed Settings views keep their usual background-update policy.

  - The person-owner task-type API rejects non-object JSON with 400 and replaces a connection’s sorted allowlist atomically; GET never creates a boards store.

  - `tasktypes.js` lets the owner set a card’s task type in the card details (empty clears it; others see it read-only) and edit each agent’s allowed types in Settings > Agents, loaded when the section is first opened. Both call the existing REST routes; the server stays the authority and refuses a pickup with the type named and the Settings > Agents next step.

  - The web app calls the same services as REST; it shows only what the signed-in person may read.

  - **Coordinator status:** Project settings > Orchestrator reads the read-only checkpoint (`roles/main/checkpoint`) and shows who holds the lease, when it runs out, the accepted config version and counts of open, in-doubt and unstarted work; only a signed-in owner sees End lease, behind a confirm, and ending it leaves unfinished steps listed. A board without orchestration, or one the person cannot read, shows nothing.

  - **Browser GitHub connection:** Needs shows local GitHub connection state and validated PR links; selection and session teardown discard connection state.

  - **Connection lifecycle proof:** Versioned fixture cards preserve the loading assertion; stopped Needs controllers clear the detail DOM as well as cached connection state.

  - **Connection teardown boundary:** Stopping clears rendered GitHub details while preserving the established teardown contract for other Needs details.

  - **Connection recovery:** Callbacks reopen the selected Needs item; GitHub-only status failures preserve card actions, and connections in another workspace show an explicit disconnect control.

  - Needs me renders origin/configuration as approvals, requires a release reason and posts directly; coordinator configuration navigates to existing settings without mutating a card. Proposal Decline and plan Reject controls follow reject permission independently of approve permission and use the existing rejection handler.

  - Needs me shows a structured ask (`ask_blocks`, `web/static/decision-blocks.js`) as side-by-side option blocks under the question: optional plain-text preview, the answer button, optional detail. The button answers with the option's label exactly like a plain option; plain asks keep their footer buttons, and blocks that do not match the offered labels are not drawn.
  - Shell needItems includes approvals when counting each project and retaining selected details on refresh; release ranks between person decisions and plan review.

- **How it fails:**

  - A hub that sends no ledger with a start shows the Runs list as before, without ledger lines.

  - Runtime suberrors preserve completed sibling facts; needs-me and stuck settle independently. Deployment/health and backup receipts have independent subpanels. Missing optional data stays absent and corrupt sources return safe errors.


  - Safe source errors remain local to one section; session expiry/disposal clears data and pending generations. Ineligible operational sections send no requests; token contact is not liveness.


  - Rendering errors surface in the browser; the server state is unaffected.

- **Tests:**

  - `tests/test_card_runs_ui.py` (ledger case) covers reported and unreported usage, end words, cost formatting and log pointer escaping.

  - tests/test_admin_runtime_api.py, tests/test_admin_runtime_controls.py, tests/test_admin_runtime_ui.py and tests/admin_runtime_ui.mjs cover fixed-time accounting, authority, wildcard subscription eligibility, no-write/lock sentinels, partial failures and GET-only pagination.

  - tests/test_runner_requesters_admin.py and the requesters case of tests/admin_runtime_ui.mjs hold the Admin requester list, candidates, the per-requester owner-only edit with its version precondition and the other-workspace refusal.


  - `tests/test_admin_api.py` covers person/role/workspace refusals, fixed lifecycle time, pagination, redaction and no writes/locks; `tests/test_admin_ui.py` and `tests/admin_ui.mjs` cover cold routing, independent deadlines, retries and disposal.


  - The `tests/test_workbench_` files, the `tests/test_console_` files and the `tests/test_commonplace_console_` files.

  - `tests/workbench_e2e/test_refresh_forms.py` drives a real browser: an open hand-off form (also one opened mid-refresh) and a comment in progress survive a background refresh, the panel updates once the form is cancelled, and on Needs me a queue change leaves an open hand-off form alone and Cancel closes it.

  - `tests/commonplace_decision_blocks.mjs` (run by `tests/test_commonplace_decision_blocks.py`) checks that a structured ask renders one block per option in the Needs me body, escapes option text, answers with the option's label through the decide route, and falls back to plain buttons for plain or mismatched blocks.

  - `tests/orchstatus.mjs` (run by `tests/test_orchstatus.py`) covers the status block states, the End lease confirm, refusals and escaping; `tests/test_orch_lease_status.py` covers the checkpoint fields it reads and the owner-only force-release contract.

  - `api.js` joins a refusal's `error` and `next_step` and words a 403 without a JSON body; `tests/test_commonplace_owner_403_message.py` and `tests/owner_403_message.mjs` cover it.

  - `tests/commonplace_bots_nav.mjs` (run by `tests/test_commonplace_bots_nav.py`) covers the landing route, every old deep link still being a screen, the Bots count and the exact rail, menu and phone-tab entries; `tests/workbench_rail.mjs` and `tests/workbench_routes.mjs` hold the rail markup and route resolution cases.

  - `tests/commonplace_bots_view.mjs` (run by `tests/test_commonplace_bots_view.py`) covers the stream parser cut at every byte, the sender's pre-stream refusals and events, the roster, every block type, the optimistic echo and streamed fill, Stop, retry under one request id, escaping of hostile text and the route and rail glue; `tests/bots_harness.html` with `tests/bots_shots.sh` renders the screens for desktop and phone screenshots.
