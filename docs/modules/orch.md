## orch

- **Paths:** `commonplace/orch/`, `docs/orchestrator.md`, `tests/_orch_helpers.py`, `tests/test_orch_acceptance.py`, `tests/test_orch_schema.py`, `tests/test_orch_areas.py`, `tests/test_orch_store.py`, `tests/test_orch_durable.py`, `tests/test_orch_checkpoint_threads.py`, `tests/test_orch_runner.py`, `tests/test_orch_surfaces.py`, `tests/test_orch_checks.py`, `tests/test_orch_plan_gate.py`, `tests/test_orch_trigger_stall.py`, `tests/test_orch_start_retry.py`, `tests/test_orch_stall_wake.py`, `tests/test_docs_obligations.py`, `tests/test_docs_fragments.py`
- **Owns:**

  - `durable.coordinator_holder_in` and `coordinator_holders_in` name the actors holding live coordinator leases, read-only and fail closed, for the board coordinator checks.

  - A `model_unresolved` start refusal names the role and board and where an owner sets the model (`details.role`, `details.config_role`, `details.board`); `Config.start_roles` and `split_role` give the roles a start can run to `prepare()` and to Needs me.

  - Inert area declarations and topology validation, their render/diff representation and test-only accepted-area fixtures.

  - The planner grammar, including leading numeric labels and repository-relative existing test-file mappings; the orchestrator config format and its versions and experiments, config-driven starts, triggers, leases, the journal, resume, the restore flag and acknowledgement, `plan-check` and `docs-check`.

  - The read-only mapping from journaled thread starts and doing-card claims to checkpoint jobs.

- **Promises:**
  - A start resolves its model from the project slots and, when the project's escalation rule applies (card failed jobs counted in the start transaction, or a card label), from the rule; the job records `model_reason` and the reply carries it. With no rule the model is unchanged.

  - `docs-check` reads the module map from `docs/modules/<module>.md` (plus any block left in `MODULES.md`) and accepts `docs/system/` sections as changes to `SYSTEM.md`; editing those files is not itself a module change. A module many changes touch (`tests`) also takes field fragments in `docs/modules/<module>.d/<name>.md` (`- **Owns|Promises|How it fails|Tests:**` bullets, no `Paths`): they are appended to the module's fields, and editing one is an edit of that module's block, so concurrent changes add their own file instead of editing the same lines.

  - Up to eight distinct area sub-orchestrators sit directly under one parentless orchestrator; areas remain preview-only across live and experiment accept/write paths. No-area graph rules and main/worker model slots remain unchanged.

  - Owner-assigned runner work uses the existing configured person-start fence; agent-authored todo reservations still produce an assigned-card trigger miss for a different worker.
  - Enabled root triggers pin main-role launches; reconciliation uses existing miss history and an in-lock eligibility recheck for at most three attempts.

  - A split start uses the config's `main` role, or the `[runner]` role when no role is named `main`. A trigger start that cannot be planned (`StartRefused`, `ConfigError`) is a coded miss that advances the cursor, so one card never blocks the board; an origin release is a firing event.

  - Empty disabled sweeps avoid config resolution, checkpoint inbox context is best effort, and failed inbox snapshots keep committed leases launchable using pinned instructions.

  - Lease snapshot failures log sanitized diagnostics without exception messages.

  - Only owner-accepted versions run; every config-driven start is one fenced, capped transaction; a retry returns the stored job; external effects are never re-issued by the hub; after a restore nothing config-driven runs until the owner acknowledges.

  - A runner start is refused with `path_overlap` while a running card of the board (a queued, leased, starting, uncertain or started job of any age whatever the card's status, or doing with a connection assignee) declares an overlapping path contract; the check reads jobs, doing cards and contracts in the write lock for configured and unconfigured boards alike, treats an unreadable contract as `**`, refuses with `path_contract_too_large` past its work-unit budget (running cards' stored size per glob, wildcard compiles by length, stars and classes, comparisons), and never refuses cards without a contract. A started job has no end state, so only the workspace owner releases its card, by clearing its paths. Manual claims are not checked at the `doing` transition: their holder may clear or narrow its own contract (globs it adds are checked), and a person's claim does not hold a start.

  - Configured starts share project admission with claims and plain jobs when workspace orchestration is enabled; boards without a `[runner]` config retain earlier behavior with the workspace switch off.

  - The stored-job retry above applies only to unvoided steps.

  - Voided runner-start keys are retired with `step_voided`; history and linked job statuses remain visible, and a new key needs normal admission.

  - First void and queued/leased cancellation share one transaction; repeats preserve actor/time and one journal event while tools emit per-call feed notifications.

  - Checkpoint `jobs` also lists builders started outside the runner (`source=journal`): an unvoided, unfinished `thread` step whose start was recorded for a card, while that card is in `doing` on the same board under a live claim. Runner jobs carry `source=runner`. The view is derived on read and never stored.


- **How it fails:**

  - Area topology returns stable field paths and schema codes; acceptance returns area_not_supported before effects, preserving proposals, accepted rows, cursors, leases, jobs and events without echoing config content.

  - Refusals become coded misses shown on resume; an unproven external effect becomes `needs_person`; infrastructure errors leave the trigger cursor in place for the next sweep.

  - No-progress wake (`stall.py`): after a clean sweep of a runner-configured board with actionable open work (not blocked, not waiting on a person), a free slot, an unstarted card or an expired claim, and no card done for `minutes` (default 20, per board, 0 = off), the hub emits one `board.stalled` event listing doing, unstarted and expired cards; the next nudge waits another `minutes`, a done card resets the streak, and three nudges in a row list the board in the owner's `needs_me` (`orchestrator_stalled`). Errors are logged and never break the sweep.

  - Two failed sweeps of a board in a row emit one `orch.trigger.failing` event and list the board in the owner's `needs_me` (`orchestrator_failing`) until a sweep succeeds; `orch_sweep_health` stores only the exception type.

  - On boards without workspace orchestration a trigger start refused as `at_cap`, `project_limit`, `busy`, `settings_unavailable`, `config_changed`, `worker_unavailable` or `started_recently` is retried by the sweep (`start_retry`, `orch_start_retries`) after 1, 5, 15, 30 and 60 minutes under the event's own request key, each refusal adds a miss, and the fifth ends as `retry_exhausted`; any other refusal, including one on a retry, is final.

  - Void cannot stop launched threads or prove completion; existing active/recent-start and per-switch capacity guards still apply, and a pending todo trigger may run on a later sweep.


- **Tests:**

  - `tests/test_docs_fragments.py` proves that two branches adding module blocks, promise or test bullets or system sections merge in both orders without a conflict while the single-file layout conflicts, and covers fragment validation, obligations and `--print`.

  - `tests/test_orch_areas.py` covers area roundtrips, ordered topology errors, frozen experiments, owner boundaries and complete refusal snapshots through store, MCP, REST and local CLI.

  - Lease authority without `[runner]` belongs only to the board's own trusted project seat for its seat role (`not_configured` for everyone else, `not_allowed` with the Project settings hint for that seat before trust); a seat is refused `not_allowed` on other boards. Covered by `tests/test_projects_seat_lease.py`.

  - `tests/test_orch_runner.py` pins the lease keys of a plain start: the identifiers plus `card_context`; the pinned message of a configured start stays unchanged, the card context travels beside it.

  - The `tests/test_orch_` files listed in Paths and `tests/test_docs_obligations.py`; R22 in `tests/test_orch_checks.py` covers misplaced labels, missing files, accepted label forms and repository-root resolution.

  - R1, R1b, R1d, R1e, R8b, R10a and R11e map to `tests/test_orch_acceptance.py`.

  - `tests/test_orch_checkpoint_threads.py` covers journaled thread starts as checkpoint jobs: the recorded start plus a live doing claim, board and role scoping, in-doubt visibility after a new epoch and the absence of writes.
  - `tests/test_orch_stall_wake.py` covers exactly one wake for an idle board with open work, backoff, a done card restarting the timer, no wake for blocked or person-held cards, owner escalation after three nudges and the off switch.

  - `tests/test_orch_trigger_stall.py` covers a split start without a `main` role (including the live v3 config shape), unplannable starts as misses, a released todo card starting, and the failing-sweep owner alert.

  - `tests/test_orch_start_retry.py` covers a retried `at_cap` start with its exact backoff, repeated refusals recorded as misses, the bounded `retry_exhausted` end, a permanent refusal never retried, a retry that is refused permanently, and a settled retry not counted twice.
