## agent-schedules

- **Paths:** `commonplace/agent_schedules/`, `commonplace/schedule_time.py`, `commonplace/web/static/agent-schedules.js`, `tests/test_agent_schedules.py`, `tests/test_schedule_time.py`, `tests/agent_schedules_view.mjs`
- **Owns:**

  - Scheduled prompts per agent: an owner gives an agent (a connection) a schedule, a timezone and a prompt, in the control database (`agent_schedules`, `agent_schedule_runs`), through `/api/agent-schedules` and the Scheduled prompts section of Settings.

  - `schedule_time.py` is the one due-time calculation: validation of the two schedule forms (`{every_minutes}` or a five-field `{cron}`, with an IANA timezone), `next_due` and the stored timestamp form. It has no I/O and no clock, so other features (routines) import it instead of re-deriving due times.

  - Delivery uses the existing wake path and no second channel: an owner-authored board message (`space_post`) on the board the agent coordinates, which emits the ordinary `space_message` event that wakes the coordinator and is read with `space_read`. A schedule creates no card.

- **Promises:**

  - Owner-only: listing, creating, pausing, resuming and cancelling answer 403 to members and agent tokens and 401 to anonymous callers; ids from another workspace answer 404.

  - Bounded: at most 20 live (active or paused) schedules per workspace, intervals and cron gaps of at least 15 minutes, prompts of at most 4000 characters, names of at most 80, 30 runs kept per schedule.

  - At most once per due time, also across restarts and racing workers: a due time is claimed by one transaction that advances `next_due` and inserts the run keyed by (schedule, due time). A run left claimed by a crash becomes failed (`interrupted`), never delivered again. A due time more than ten minutes late is recorded as skipped (`missed`). Resuming counts from the resume time; cancelling is final.

  - Delivery re-checks authority at each due time: creator still a workspace owner, connection live, agent still the board's coordinator. Otherwise nothing is posted and the run fails with `owner_gone`, `agent_gone`, `not_coordinator` or `board_gone`; five failures in a row pause the schedule with that reason.

- **How it fails:**

  - A schedule for an agent that is not the board's coordinator is refused at creation (409 `not_coordinator`); other refusals carry `invalid`, `limit`, `agent_not_found`, `board_not_found` or `not_found` codes.

  - A worker pass error is logged without text or values and the next pass retries; a stopped hub delivers nothing and the missed due times are skipped on the next start.

- **Tests:**

  - `tests/test_agent_schedules.py` covers firing once with a fake clock, cron in a timezone, restart, crash between claim and delivery, missed due times, racing workers, pause/resume/cancel, owner-only refusals, workspace isolation, limits, validation refusals and re-checked delivery.

  - `tests/test_schedule_time.py` covers interval and cron due times, daylight-saving gaps and repeats, and every validation refusal.

  - `tests/agent_schedules_view.mjs` (run by `tests/test_agent_schedules.py`) covers row rendering and escaping in the app section.
