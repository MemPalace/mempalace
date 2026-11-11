## runner

- **Paths:** `commonplace/runner/`, `docs/runner.md`
- **Owns:**

  - The run ledger: `run_ledger` in `runner.sqlite3` (`ledger.py`), the machine usage route `POST /api/runner/usage` (`ledger_api.py`) and the `runner report` client command (`report.py`).

  - The runner guide links the owner Admin contact/grant projection and documents the Admin runner block that enrolls, attests and edits machines through the owner routes.


  - Machine enrollment and grants, runner jobs in `runner.sqlite3`, the requester and machine routes, the local launch client and chat answers.
  - Machine enrollment, binding epoch migrations and immutable job epoch snapshots, grants, runner jobs in `runner.sqlite3`, the requester and machine routes, the local launch client and chat answers.

  - `commonplace/runner/start_context.py`: the card context of a start. `assemble` is the one function that turns a card into launch text; its `SECTIONS` list is the priority order and the place to add a section. A card that points at a goal gets its own goal as an always-present line beside the title and the goals above it (nearest first) as the section after decisions.

- **Promises:**

  - `GET /api/runners/wings` (owner only) returns the wings grant validation accepts; enroll and wing edits validate against the same fast index read, so neither waits on a drawer-by-drawer listing.

  - Every start has one ledger row, written in the job's own transaction and following its state; tokens, cost, log pointer and the run's end are only what the run's own enabled machine reported, and an unreported value stays NULL. The end status is set once; jobs from before the ledger are given a row when the store opens.

  - A machine's worker policy reads and writes exactly its granted wings (first is the default write wing); starts on any other board wing are refused with a message naming the board wing and the granted wings, and the worker cannot write to an ungranted wing through any tool.

  - Admin keeps machine and worker contact separate and always reports owner attestation as unverified; expired job inspection never sweeps or renews leases.

  - Admin lists each machine's requesters and the active trusted agents an owner may add, and edits them only through owner-only per-requester add and remove routes that act on the stored row under the registry lock and require the grant version the caller read (409 when stale); a grant edit vets added requesters and keeps ones the machine already holds.


  - Reset-before-start is refused in both orchestration modes, including retry; runner admission precedes control authority held through commit. Owner resolution audits under runner-first ordering.
  - A machine may start a todo card reserved for its own worker or a doing card already held by that worker.

  - Other assignees, waiting-person gates and unreleased origins remain refused.
  - Enabled starts reject unresolved same-board blockers through read-only board probes, without constructing a board store inside a runner transaction.
  - Runner admission refuses typed cards without the worker connection’s exact workspace grant, including unreadable or corrupt mappings.

  - Started jobs retain machine in-flight capacity and per-machine project exclusion while their cards remain doing with the machine worker; a terminal job state or a changed card state or assignee releases capacity, using read-only boards access.

  - A start needs a person owner or a listed trusted PAT and a card the worker could claim; one active start per card; an uncertain launch is never re-queued; on a configured board `card_start` is person-owner only, starts are fenced and capped, and the lease payload carries the pinned message and model.

  - Opt-in orchestration requires the active coordinator for unconfigured PAT requests, reserves project capacity when starts are queued, and admits leases using active slots under the shared runner lock.

  - Owner/local step void cancels only queued or leased jobs atomically through cancellation logic; standalone cancel authority and launch-state handling stay unchanged.

  - Every lease carries `card_context`, read as the machine's worker: fields, title and body always and verbatim, then asks/decisions/handoffs, comments, blockers, linked evidence and linked memory text in that order inside `COMMONPLACE_START_CONTEXT_TOKENS` (default 6000, 0 is off). Items are kept or cut whole, never shortened; a cut section ends with a marker that counts the cut items and names the call that returns them.


- **How it fails:**

  - A usage report for a run that has not started, or a second, different end status, answers 409; a job of another machine or any caller but an enabled machine answers 404; an invalid report answers 400 and changes nothing.

  - Absent optional runner stores remain absent for Admin; existing unreadable sources produce safe errors rather than fabricated usage.


  - A reset invalidates queued/leased snapshots; previously starting work becomes uncertain and retains capacity. A bound provider change requires explicit reset before reattestation.

  - A lost launch result becomes `uncertain` and waits for a person; a revoked machine cancels queued jobs on the next sweep; a busy queue refuses with `busy`.

  - A card that cannot be read at lease time never fails the lease: the context becomes one line telling the agent to read the card. A context that would push the launch message over its limit loses the body whole, with a marker.

  - The card panel's "Start agent" asks `GET /api/card-starts/options?task=<n>` (signed-in owner only, read-only, `commonplace/runner/start_options.py`): on a configured board the config's machine, project and topic, otherwise the attested machines the owner may use with the projects and topics each allows; a start the card or setup cannot take comes back as a `refusal` code, never as a choice. On a configured board the owner's `card_start` may send the card alone; a refused start answers its stable `code` next to `error`.

  - Resolving a start that is no longer `uncertain` answers 409, not 403, so a settled start reads differently from a refused caller; `tests/test_card_runs_contract.py` pins it with the member and other-workspace refusals.

- **Tests:**

  - `tests/test_run_ledger.py` covers the ledger row per start and its states, partial and final reports, refusals by caller and by body, the backfill of older jobs and the `runner report` command.

  - tests/test_admin_runtime_api.py covers machine timestamps, unverified binding, expired lease omission and absent/corrupt runner sources; existing runner owner tests retain authority contracts.

  - tests/test_runner_requesters_admin.py and tests/test_admin_runtime_ui.py (requesters case) cover the Admin requester list, candidates, the owner-only add and remove, lapsed-peer removal and the refusal of non-owner callers.


  - The `tests/test_runner_` files, `tests/test_orch_runner.py`, `tests/test_orch_acceptance.py`.

  - `tests/test_card_start_defaults.py` covers the owner's card-only start, equal and differing explicit values, the options route, and the refusal of a member, an untrusted agent and a coordinator token with no job created; `tests/cardruns.mjs` the panel's Start control.
  - `tests/test_runner_wings.py` covers multi-wing starts, the ungranted-wing refusal, worker write bounds after enrollment and PATCH, and owner-only listing.

  - `tests/test_runner_start_context.py` covers the fields a start prompt must carry on plain and configured boards, the budget on a very large card, cut markers, whole-item cuts, the unchanged prompt of a small card, worker-only visibility and the runner's handling of the field.

  - `tests/test_runner_revoke_scope.py` covers a second workspace's owner failing to list or revoke a machine (404, tokens still valid) and revoke/list never returning a token.
  - The `tests/test_runner_` files (including binding migration, reset races, direct fences and lock-order instrumentation), `tests/test_orch_runner.py`, `tests/test_orch_acceptance.py`.
