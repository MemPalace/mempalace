# Orchestrator setup as code

A board can carry one owner-approved orchestrator config: the roles that work
on it, their prompts, workflows, topics and models, the agent limit, the runner
settings and the `todo` trigger. Commonplace's own runner (see
[runner.md](runner.md)) launches the configured runner role with the pinned
prompt, workflow and model and records the config version and model on every
job. For other harnesses the rendered config is advisory.

Nothing changes for a board until its owner accepts a live config with a
`[runner]` table. Decisions, numbers and residuals are in `SYSTEM.md`.

## The config

The store is the source of truth. `.commonplace/orchestrator.toml` in a
repository is a proposal: `commonplace orch submit FILE` proposes it (the
source ref is recorded, not verified) and the owner approves its sha256.
`commonplace orch render` prints a version; `commonplace orch diff vA vB`
compares two (also over REST); accepting shows the diff first. TOML is read
with the standard library from Python 3.11 and with `tomli` on 3.10.

```toml
schema = "commonplace.orchestrator/1"
project = "example-shop"
[limits]
max_agents = 3                 # config-driven starts; min with the project limit
[roles.main]
kind = "orchestrator"
harness = "t3"
topic = "orchestrate"
model_role = "main"            # resolved once per job, then pinned
workflow = "card-pipeline"
prompt = '''Coordinate example-shop. {{include:resume_protocol}}'''
skills = [{ id = "skl_example", version = 3 }]
mcps = ["hub"]
[roles.planner]
kind = "planner"
parent = "main"
topic = "plan"
connections = ["planner-example"]   # connection names; matched as conn:<name>
prompt = '''{{include:design_before_code}}'''
[roles.builder]
kind = "builder"
parent = "main"
harness = "t3"                 # the runner role must use t3
topic = "build"
model_role = "worker"
workflow = "build-unit"
prompt = '''Implement the card's plan unit; run fast checks.'''
[workflows.card-pipeline]
steps = ["planner", "builder"]
[workflows.build-unit]
steps = ["read plan", "write RED test", "implement", "fast checks", "report"]
[mcps.hub]
url = "https://hub.example.invalid/mcp"
auth = { secret_ref = "env:EXAMPLE_HUB_TOKEN" }
[runner]
role = "builder"
machine = "mach_0123456789abcdef"
project = "example"
runtime_mode = "approval-required"
[[triggers]]
on = "card.todo"               # live channel only
```

Role kinds are `orchestrator`, `sub-orchestrator`, `planner`, `builder` and
`reviewer`; `parent` links form an acyclic graph; role names match
`[a-z0-9-]{1,32}`. A planner's prompt must include `design_before_code`.
Secrets are only references of the form `env:[A-Z][A-Z0-9_]{1,63}`; an inline
value is refused and never echoed. Limits: a config is at most 128 KiB with at
most 16 roles, and a prompt is at most 32 KiB.

| Field | Slice 1 |
|---|---|
| runner role `prompt`, `workflow` | **Enforced.** The launch message is `SPAWN_MESSAGE`, then the prompt, then the workflow steps, at most 120,000 characters. Step order is instruction text only. |
| runner role `harness`, `topic`, `model_role` | **Enforced.** `harness` must be `t3`; the topic passes the machine's grant checks; `model_role` is resolved once when the job is inserted, and the provider and model are stored on the job and sent as `modelSelection {instanceId: provider, model}`. An unresolved role refuses the start before insert. |
| `runner.machine`, `runner.project`, `runner.runtime_mode` | **Enforced** through the machine's existing grant checks. |
| `triggers` (`card.todo`, live), `limits.max_agents`, planner `connections` | **Enforced** (below). |
| other roles, `skills`, `mcps` | **Advisory.** Rendered for sessions to read. |

Model routing has two slots, `main` and `worker`, set per project by the owner
(`PUT /api/projects/<board>/models`).

## Versions and experiments

Each board has versions `v1`, `v2`, … with a parent version, a channel, a
source ref and a sha256. Status is `proposed`, `accepted`, `rejected` or
`superseded`. Anyone who can write the board's wing may propose (at most 5 open
proposals each, at most 20 per board); the proposer and their trust are stamped.
Only the owner accepts or rejects, or writes an accepted version directly.

Channels are `live` and `exp:<name>` (at most 8 experiments per board;
`orch exp retire` closes one). An experiment may change only `prompt`,
`workflow`/`workflows`, `topic`, `model_role` and `skills`; any other
difference is refused. Experiments run through `orch_card_start(config_ref=
"exp:<name>")` or an owner's `card_start(config_ref=…)`; triggers always use
live. Only accepted versions run.

## Who may do what

| Action | Who |
|---|---|
| read, render, diff | anyone who can read the board's wing |
| propose | anyone who can write the board's wing |
| accept, reject, retire an experiment, force a lease, void a step, acknowledge a restore | the owner: a signed-in person with role owner, or the local CLI |
| claim a lease, write the journal, `orch_card_start` | a principal that passes `requester_ok` for `runner.machine` (a listed trusted PAT or the person owner), with the lease epoch and token |
| `card_start` and cancel on a configured board | the person owner |
| trigger start | the actor of the board event, if it passes `requester_ok` |

Agent tokens with an owner role are not the owner. The project's Connect seat
(standard trust) cannot hold a lease.

## Starting work on a configured board

| Path | Who | Lease | Cap |
|---|---|---|---|
| `orch_card_start(board, card, step_key, epoch, token, config_ref?)` | the lease holder | yes | yes |
| `card_start(…, config_ref?)` over MCP or `POST /api/card-starts` | the person owner only; anyone else gets `no: use orch_card_start` | no | yes |
| trigger | the event's actor | no | yes |
| `card_start_cancel` of a job that belongs to a step | the person owner only | – | – |

Every path runs one fenced transaction on the workspace's `runner.sqlite3`.
The control snapshot (principal, grants, model settings) is read before it.
Inside it, in this order:

1. **Resolve** `model_role` and compute the request hash: sha256 of card, role,
   config version and sha256, the resolved provider and model, machine,
   project, topic and runtime mode. A resolution failure refuses.
2. **Refuse** while the restore flag is set, or when the card carries the
   `plan` label (`plan_pending`). These win even over an exact retry; read the
   job with `card_start_status`. `orch_card_start` also checks the lease epoch,
   token and expiry.
3. **Look up** the existing step `(board, role, step_key)`; a person call with a
   `request_key` looks up the runner's own `(card, requester, request_key)`
   instead. If the card and hash match, the stored job is returned; any mismatch
   is a 409 with the stored step or job.
4. **Cap.** Count distinct cards with a queued, leased, starting or uncertain
   job, with a job started within the last hour, or in `doing` with a
   connection assignee. Refuse at `min(max_agents, project limit)`.
5. **Insert** the step row and the runner job, with the existing guards: one
   active start per card, and a non-person start within 24 h is refused
   `started_recently`. The request key is `orch:<role>:<step_key>`;
   `step_key` matches `[A-Za-z0-9._:-]{1,80}`.

The runner lease payload of a config-driven job carries `message` (the launch
message above) and `modelSelection`, and the runner sends both to T3.

## Triggers

Only boards with an accepted live `[runner]` config are swept, on each wake-up
and on each runner lease. The sweep reads committed `task_events` after the
board's cursor and fires when a card moves into `todo` from another status
(`add`, `move`, `decide`) and on the plan-approval row. An event emission that
failed after commit is still found.

- **Cursor.** Accepting a config that adds `[runner]` commits the board's
  cursor as unset with a new generation; arming then sets it to the current
  highest event id if the generation is unchanged. Events before arming never
  fire. Removing `[runner]` deletes the cursor. A restore acknowledgement
  re-arms every configured board. The CLI, MCP and REST accept and
  acknowledgement arm right away; the next sweep arms any cursor left unset.
- **Actor.** Rebuilt from the event's stamps; an unknown or revoked actor is a
  miss `requester_unknown`, one that fails `requester_ok` is
  `requester_not_allowed`. Members' moves do not start work.
- **Misses.** Every refusal (`assigned`, `not_todo`, `started_recently`,
  `busy`, `plan_pending`, …) is recorded as a coded miss and shown on resume;
  the cursor moves past it. Only infrastructure errors leave the cursor for the
  next sweep. A refused card is not retried automatically, even when the
  refusal was transient (`busy`, `at_cap`, `started_recently`): it stays in the
  checkpoint's unstarted list and as a miss. Move it to `todo` again to retry.

## Durable orchestrator

- **Lease.** One per `(board, role)`. `orch_resume` claims it when none is
  held, after release or expiry, or when the owner forces it. The token is
  returned once. TTL 1800 s, renewed by fenced writes and `orch_heartbeat`;
  waits are sliced to at most 10 minutes with heartbeats. Expiry only fences.
  A lost token means waiting for expiry or an owner force.
- **Journal.** `intent → started → waiting → done | failed | in_doubt`;
  `in_doubt` moves on to `done`, `failed` or `needs_person`. A job maps to its
  step as: queued, leased or starting → `waiting`; started → `done`;
  uncertain → `in_doubt`; failed or cancelled → `failed`.
- **Evidence.** `runner_job` (the hub checks the job belongs to this step,
  card and board), `card_state` (the hub reads the card) and `holder_attested`
  (stored, never acted on).
- **Gates.** `gate_pass`, `gate_fail`, `gate_error`, `gate_budget`; one gate
  per `(board, repo, PR, head)` across roles unless the owner voids it.
- **External effects** are never re-issued by the hub. Before any re-issue the
  resume protocol checks the truth source: merge → PR state and head; gate →
  the PR's `Reviewed head:` and `Verdict:` lines, else `needs_person`; runner
  start → T3 threads with `[commonplace-job:<id>]` in the title; card → card
  state. An empty thread list is no proof (D3), so the step goes to
  `needs_person`.
- **Resume.** `orch_resume` claims the lease (never cards) and returns a
  checkpoint: open steps, `in_doubt`, `needs_person`, holds, next, jobs, misses,
  and the unstarted `todo` cards of each configured board (a `todo` card with no
  active job and no job started after it last entered `todo`; a start in the
  same second as that entry cannot be ordered, so the card is listed). Cards are
  classified read-only as `held`, `expired` or `not_held_by_caller`; only the
  caller's own held cards get a heartbeat.

## Restore

A restore made with `commonplace restore` leaves a marker. The first open of
the orchestrator store by a surface, the CLI, a lease call or a start turns it
into a flag in `runner.sqlite3` with a new incarnation, and every lease is
dropped. The trigger sweep never converts the marker; it skips the workspace
while one is pending. While the flag is set, lease claims, config-driven starts and the
sweep are refused; unconfigured boards are unaffected. Only the owner clears it,
with the exact text:

```sh
commonplace orch restore-ack --text "effects after <T> may be unknown"
```

Check the truth sources first; effects after the backup time T are unknown.
The full runbook is in `SYSTEM.md`.

## Design before code

On a board with an accepted live config:

- Approving a `plan` card runs plan-check on its body; rejecting and escalating
  are unchanged.
- `task_update` cannot remove the `plan` label; only `task_approve` clears it.
- A card created by a configured planner connection must pass plan-check
  before it is created into or moved to `todo` or `done`, and before approval.

`commonplace orch plan-check FILE` exits 1 at the first violation:

- **G0** text inside fenced code blocks is ignored;
- **G1** headings `## 0.` to `## 7.` are present, in order;
- **G2** section 0 has `**a.` to `**f.`, in order, each with at least 40
  non-space characters;
- **G3** every (a) line with a digit is labelled `M`, `T`, `G`, MEASURED,
  TARGET or GUESS;
- **G4** (e) has a table with the header `Dependency | Timeout | Bounded retry |
  Idempotency key | User sees | RED`, six non-empty cells per row, and RED
  cells like `R1, R2b`;
- **G5** (f) has a table whose header starts `ID | Fault injected | Assertion`,
  with unique ids like `R1` or `R2b`;
- **G6** every (e) id appears in (f);
- **G7** a section 6 line `R-test files:` maps every (f) id to exactly one file.

## Not yet

Agent approval of config changes; seat tokens as lease holders; leases on
boards without `[runner]`; per-child provenance (D1); runner children in the
project limit; model or harness routing for non-runner roles and more than two
model slots; non-T3 runner harnesses, other triggers and experiment triggers;
lease-fenced card moves and early handoff transfer; an owner UI; evaluations and
replay of recorded runs.
