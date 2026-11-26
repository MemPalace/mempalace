# Hook write routing

> [!IMPORTANT]
> This page documents unreleased hook write-routing behavior on `develop`.
> MemPalace v3.6.0 does not consume `write_routing.hooks`; see the
> [v3.6.0 write-routing policy](https://github.com/MemPalace/mempalace/blob/v3.6.0/docs/write-routing-policy.md#backward-compatibility)
> for behavior available in that release.

Hook-triggered writes use the shared write-routing policy introduced for the
Tier 3 rollout tracked in #1963.

## Scope

This applies to every routine write initiated by the Python hook layer:

- Stop-hook diary checkpoints;
- transcript/conversation ingest;
- project auto-ingest;
- SessionEnd final flushes;
- PreCompact transcript ingest;
- PreCompact synchronous project mining.

It does not change CLI write routing. CLI adoption is a separate follow-up.

## Policy behavior

### `direct`

Hooks use the existing direct in-process or subprocess paths.

The daemon is not probed.

### `prefer`

Hooks use the daemon when it is already healthy.

If the daemon is unavailable, hooks retain the historical direct fallback.

### `require`

Hooks use the daemon when it is already healthy.

If the daemon is unavailable:

- no in-process ChromaDB write runs;
- no direct `mempalace mine` subprocess is started;
- no daemon is cold-started from the hook;
- the hook log records the skipped operation;
- the hook returns a visible `systemMessage`;
- the Stop save marker is not advanced, allowing a later retry.

## Why hooks do not start the daemon

Hooks operate under strict latency budgets. Starting a Python daemon and its
storage dependencies from a Stop or SessionEnd hook can exceed that budget.

A supervised installation using `require` must start the daemon earlier, for
example at login, plugin initialization, or session setup:

    mempalace daemon start

SessionStart performs a fast health probe in `require` mode and warns early if
the required daemon is unavailable.

## How hooks reach the daemon

A hook never waits on the daemon and never imports the storage stack:

1. It probes `/health` with a 150 ms timeout, using only the standard library.
2. It writes the job straight into the daemon's durable SQLite queue
   (`mempalace/daemon_queue.py`, which imports neither chromadb nor numpy).
   If another writer holds the queue, it retries for at most 150 ms of wall
   time, then reports the failure and returns.

Diary checkpoints are queued the same way. The daemon files the drawer and
then writes the checkpoint ack and the session's save marker, so the marker
still advances only once the entry is really stored.

## One decision per hook event

A Stop or SessionEnd event may perform several writes:

1. diary checkpoint;
2. transcript ingest;
3. project auto-ingest.

The route is resolved once and stored in a context-local value for the whole
write burst. This avoids repeated health probes and prevents different writes
from selecting inconsistent routes during the same event.

## Coalescing and debounce

Daemon mine jobs are keyed by source (`hook:mine:<mode>:<path>`). The queue
keeps at most one *queued* job per key, next to at most one *running* job:

- an event for a source that is already queued replaces that job's payload
  (latest wins) and keeps its place in line;
- an event for a source that is currently running queues one pending rerun,
  so content that arrived mid-run is mined right after it, not at the next
  hook.

Each trigger has a settle window and a maximum wait. The job runs at
`MIN(first_enqueued + max_wait, last_enqueued + settle)`; an immediate trigger
is never delayed by a later debounced one.

| Trigger      | Settle | Max wait |
|--------------|--------|----------|
| Stop         | 120 s  | 15 min   |
| PreCompact   | 0      | 0        |
| SessionEnd   | 0      | 0        |

Override per trigger with `MEMPALACE_DAEMON_<TRIGGER>_SETTLE_SECONDS` /
`MEMPALACE_DAEMON_<TRIGGER>_MAX_WAIT_SECONDS` (`STOP`, `PRECOMPACT`,
`SESSION_END`), or in `config.json`:

    {
      "daemon": {
        "debounce": {
          "stop": {"settle_seconds": 120, "max_wait_seconds": 900}
        }
      }
    }

Diary checkpoints are never debounced.

## Hallways off the mine path

A mine used to finish by recomputing the hallways of the whole wing it filed
into. That pass is exact but costs tens of seconds on a wing of ~100k drawers,
and it cannot be made incremental: entity spellings are resolved across the
whole wing, so one new drawer can change how earlier drawers' entities
resolve. A hook that re-mines a transcript every few minutes paid it every
time.

Background mines now defer it:

1. The mine files its drawers, then marks the wing dirty in the hallway store
   (`hallways.sqlite3`, table `dirty_wings`) and queues one `hallways` job for
   the wing (dedupe key `hallways:<wing>`).
2. Jobs for one wing coalesce like mine jobs: the recompute runs 10 minutes
   after the wing's last mine, and never later than 60 minutes after its first
   dirty mark. A burst of mines costs one recompute.
3. The daemon's worker runs the job under the palace write lock, with the
   same whole-wing algorithm as before. Project mines also refresh the
   cross-wing entity tunnels derived from the wing's hallways there.

A mine defers only when a daemon will run the job: inside the daemon, or with
a daemon answering for the palace. Otherwise (the daemon is opt-in) it
recomputes inline, whatever `--defer-hallways` or `hallways.defer` says.

The dirty mark is what guarantees the work, not the queue row:

- A recompute clears the mark in the transaction that saves the new records,
  and only if no mine marked the wing again after it started reading drawers.
- When a `hallways` job ends with the mark still present, the daemon queues
  it again. A mine that marked the wing during the run restarts the wait at
  that run's drawer read; a failed run is retried after `settle_seconds` or
  60 seconds, whichever is longer.
- A daemon that starts queues every mark it finds, so marks left by a crash
  are processed. The window still counts from the first mark.
- With no daemon, a direct `mempalace mine` (any wing) first checks for marks
  older than `max_wait` and recomputes them, so a mark a stopped daemon left
  behind is not stale forever.
- `mempalace hallways --dirty` lists the marked wings;
  `mempalace hallways --rebuild --dirty` recomputes them now.

Which mines defer:

| Mine | Default |
|------|---------|
| Hook mine through the daemon (`prefer` / `require`) | deferred |
| Hook mine on the direct path (spawns `mempalace mine`) | inline |
| `mempalace mine`, direct or routed through the daemon | inline |
| MCP `mempalace_mine` | inline |

`mempalace mine --defer-hallways` / `--inline-hallways` choose per run.
`hallways.defer` in `config.json` (env `MEMPALACE_HALLWAYS_DEFER`) changes the
default: `auto` (the table above), `always` (every mine defers unless it says
`--inline-hallways`, as long as a daemon serves the palace), or `never` (every
mine recomputes inline, the behaviour before deferral). The window is
`hallways.settle_seconds` / `hallways.max_wait_seconds` (env
`MEMPALACE_HALLWAYS_SETTLE_SECONDS` / `MEMPALACE_HALLWAYS_MAX_WAIT_SECONDS`):

    {
      "hallways": {"defer": "auto", "settle_seconds": 600, "max_wait_seconds": 3600}
    }

### Staleness while a wing is dirty

Readers (`mempalace hallways`, `mempalace_list_hallways`, tunnel and graph
tools) see the wing's previous hallway records until the recompute commits.
They are stale, never missing or partial: the recompute replaces the wing's
records in one SQLite transaction. Search reads drawers, not hallways, and is
unaffected. With a daemon running, a wing's hallways lag its drawers by at
most `max_wait` (60 minutes) after the first unprocessed mine, plus the
recompute's own run time and any wait for the palace lock. Only hook mines
routed through the daemon defer by default; every other route recomputes
inline and has no lag. A mark the daemon did not get to before it stopped
waits for the next daemon start, the next mine of that wing, the next direct
mine once the mark is past `max_wait`, or `mempalace hallways --rebuild
--dirty`.

## Submission ambiguity

Once a daemon submission is attempted, an error never triggers direct
fallback. The daemon may have accepted the job before the client observed the
failure; retrying directly could duplicate content.

## Invalid configuration

An explicitly invalid routing policy fails closed: hook writes are blocked and
no direct ChromaDB fallback is attempted.

An unrelated configuration read/runtime failure preserves the historical
direct-save behavior so a final checkpoint is not lost because of an
independent configuration failure.

## Backward compatibility

The default policy remains `direct`.

Legacy settings remain supported through the shared policy resolver:

- `MEMPALACE_HOOKS_DAEMON=true` maps to `prefer`;
- `hooks.daemon: true` maps to `prefer`;
- false values map to `direct`.

## Configuration examples

Prefer the daemon but permit direct fallback:

    MEMPALACE_HOOK_WRITE_ROUTING=prefer

Require the daemon and prohibit direct writers:

    MEMPALACE_HOOK_WRITE_ROUTING=require

Configuration file:

    {
      "write_routing": {
        "hooks": "require"
      }
    }
