## projects

- **Paths:** `commonplace/projects/`, `commonplace/plans.py`
- **Owns:**

  - Source outboxes and observed stuck episodes, internal seat bindings, publication receipts and reserved report dispatch.
  - Report installation touches only control state; dispatcher passes resolve existing board databases without creating workspace directories, migrate sources lazily by path, inode and SQLite schema version, and isolate workspace failures, including publication ProgrammingError, with diagnostics that omit exception text and local values. Ordinary data writes retain the initialization cache; a new inode or a changed schema version triggers initialization again.
  - Recovery skips zero-byte sources before connecting and uses a read-only base-schema probe for nonempty sources. Missing `boards` or `tasks` defers all report writes and migrations until a later pass, leaving creator initialization unblocked.
  - Report source initialization installs its reset trigger only when every watched task column exists and removes an existing incomplete trigger otherwise. For sources missing only `heartbeat_at`, raw status writes remain usable until BoardStore adds that column and installs the trigger; other watched columns belong to the base schema and are not added by this migration. Until migration, no episodes are recorded, while reporting is enabled, a pass that reaches a coordinated task logs an error, and watched writes do not bump `report_reset_rev`. Product writers open BoardStore before writing. `tests/test_project_report_legacy_source.py` covers pump recovery, stale-trigger removal and subsequent migration.
  - The optional read-only runner input to shared usage accounting.


  - Project orchestrator seats, project model choices with each board's model list (`models.catalog`, `catalog_add`; board-scoped, a choice outside the list is refused with `unknown_model`), the owner-only Needs me `model_roles` items for the start roles of the accepted config with no model (`models.missing_roles`), project limits, project wings and optional hosted plans.

- **Promises:**

  - Orchestration settings reads wait at most one second for the shared control lock, then return the existing retryable `settings_unavailable` refusal without reading an unlocked connection.
  - The explicit Admin-only optional-input flag permits absent runner jobs/config without initialization while ordinary doing cards still count. Admission defaults retain strict runner input and shared accounting.


  - Connect recommends a board-scoped subscription to `space_message` and `card.moved`, retaining `card.created` with orchestration enabled, without creating or modifying webhooks.
  - Connect recommends a board-scoped subscription to `space_message` and `card.moved`, retaining `card.created` and adding `project.report` with orchestration enabled, without creating or modifying webhooks.

  - The opt-in inbox excludes children and coordinator roots, exposes keyed children, and accepts first-wins split markers only from the coordinator or signed-in owner.

  - Split marker task references are range-checked before SQLite binding and settings-read failures remain retryable.
  - Default-off project reports capture the current coordinator and activation/binding generations, baseline existing staleness, scan at most 100 cards and publish at most 100 rows per workspace pass, and retain receipt-backed event identity across crashes.

  - `seats.held_by` names the board and role of the seat a connection holds; the durable orchestrator uses it so a seat trusted in Project settings holds its own board's lease without a config.

  - The seat-creation listener checks its event with the shared `events.is_stored` before acting.

  - Only a signed-in owner changes models or limits; `max_agents` counts distinct connection assignees with orchestration off; opt-in orchestration shares card/job admission accounting, default cap 3, owner-only settings and atomic seat trust; model resolution goes project, then workspace, then the runner default.

  - A project panel (`projects/panel.py`, `panel_routes.py`, format `commonplace.panel/1`, documented in `docs/project-panels.md`): a few highlight lines and http(s) action links per project, stored beside its board and read by anyone who can read the project. Setting or clearing it, over `PUT /api/projects/<slug>/panel`, needs the board-write authority and the rules check; the project view payload is unchanged.

  - **Browser GitHub connection:** Signup plan assignment accepts the provisioning transaction to preserve atomic GitHub ownership reservation.

- **How it fails:**

  - A panel with an unknown field, markup, control characters, an over-limit list or string, or a link that is not absolute http(s) without credentials is refused with 400 `invalid_panel`; over 4096 bytes is 413 `panel_too_large`; a read-only or add-cards-only connection is 403 `not_allowed`; nothing is stored on refusal.

  - Snapshot failures leave observation state untouched. Missing seats yield history only; changed bindings cancel old reports. Unknown authorization errors retain fixed-code retries without exposing exception text.
  - Corrupt existing input still raises; accounting does not create an empty substitute store or clamp actual use to its cap.


  - An unresolved model refuses a config-driven start before insert; a limit change does not stop work already started.

- **Tests:**

  - `tests/test_project_reports.py::test_done_writers`, `test_seed_done_new_board_history_only`, `test_idle_stuck_and_episode_resets`, `test_scan_budget_isolation_cleanup_recovery` and `test_publication_crashes_and_lock_order`.
  - tests/test_admin_runtime_api.py::test_limits_orchestration_on and test_optional_stores_no_initialization, plus existing project limits/admission tests.


  - The `tests/test_projects_` files, `tests/test_commonplace_plans.py`.

  - `tests/workbench_g4_projects.mjs` (case `models_submit_before_loads`) holds that the project settings models form saves, and reloads with the stored values, while the page's other reads are still pending.

  - `tests/test_projects_seat_lease.py` drives the settings routes and the seat's own MCP calls: take, hold, renew and release with no config, plus refusals without trust, after demotion or switch-off, for another project's seat, for another role and outside the wing.

  - `tests/test_projects_panel.py` covers the format limits, unsafe link schemes, a stored panel read back, refusals for read-only, add-cards-only, anonymous and other-workspace callers, and a project without a panel left unchanged. `tests/test_projects_guards.py` keeps every module here, `panel.py` included, free of outside host names and local paths, in comments and docstrings too.
