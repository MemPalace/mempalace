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

Hooks use the daemon when it is healthy. With auto-start on, they also start it
(detached) when none is registered. If it cannot be used -- auto-start is off
(the default), another writer holds the palace lock, or a registered daemon is
alive but does not answer -- hooks retain the historical direct fallback.

### `require`

Hooks use the daemon when it is healthy. With auto-start on, they also start it
(detached) when none is registered.

If the daemon cannot be used (auto-start off, another writer holding the palace
lock, or a registered daemon that does not answer):

- no in-process ChromaDB write runs;
- no direct `mempalace mine` subprocess is started;
- the hook log records the skipped operation;
- the hook returns a visible `systemMessage`;
- the Stop save marker is not advanced, allowing a later retry.

## How hooks reach the daemon

A hook never waits on the daemon and never imports the storage stack:

1. It probes `/health` with a 150 ms timeout, using only the standard library.
2. It writes the job straight into the daemon's durable SQLite queue
   (`mempalace/daemon_queue.py`, which imports neither chromadb nor numpy).
3. With auto-start on and no daemon registered, it spawns
   `mempalace.daemon serve` detached (its own session on POSIX; a hidden
   console and its own process group on Windows) and returns. The daemon's own
   start-up runs outside the hook budget. SessionStart does the same, so the
   daemon is usually warm by the first Stop.

Before it queues work for a daemon it would start, the hook probes the palace
writer lock without waiting. If another process holds it (a writable MCP
server, a manual `mempalace mine`), a daemon could not take it either, so the
hook takes the policy's no-daemon path instead of queuing a job nothing runs.

Two hooks may spawn at the same moment. Every daemon takes a per-palace
lifetime lock (`daemon.lock`) before touching the palace; the loser exits with
status 0 and the winner drains the queue. Hooks also skip the spawn for
10 seconds after one, while the first daemon comes up.

Diary checkpoints are queued the same way. The daemon files the drawer and
then writes the checkpoint ack and the session's save marker, so the marker
still advances only once the entry is really stored.

A daemon a hook started exits after 30 minutes with nothing running or queued
(a debounced job not yet due counts as queued), and the next hook starts it
again. `mempalace daemon start` keeps running unless configured:

    MEMPALACE_DAEMON_IDLE_EXIT_MINUTES=30  # or daemon.idle_exit_minutes; 0 = never

### Where a daemon mine runs

The daemon is the palace's only writer. With `daemon.mine_child` on (it is off
by default for now), a mine job's compute (reading files, chunking, embedding)
runs in a child process at background priority: Linux
`SCHED_IDLE` (fallback `nice 19`), macOS `taskpolicy -b`, Windows background
mode (fallback idle priority class). The child never opens the storage backend;
each collection call it makes travels over a pipe and runs in the daemon, with
vectors the child already computed. A persistent Chroma database therefore
stays open in one process only.

The daemon serves those calls one at a time under a write lock that diary
checkpoints also take, and diary checkpoints have their own worker lane, so a
checkpoint lands between a long mine's batches instead of after it.

The child uses 2 ONNX Runtime intra-op threads with thread spinning off:

    MEMPALACE_DAEMON_MINE_THREADS=2        # or daemon.mine_threads; 0 = embedding_threads
    MEMPALACE_DAEMON_MINE_CHILD=1          # or daemon.mine_child: true; default off mines on the daemon thread

The child sends a heartbeat every 10 s, and the daemon's reaper kills a child
that has sent nothing for 60 s (time the daemon spends running one of its
calls does not count), or one still running 30 s after it reported done. The
job then fails and the daemon keeps going; writes already made stay, and the
next mine resumes from them.

On Linux a `SCHED_IDLE` child that is silent because busier processes take
every CPU (runnable, its CPU time flat) is not killed. Elsewhere the daemon
cannot tell a starved child from a wedged one, so a background-priority child
it cannot sample (macOS, Windows, Linux `nice 19`) gets ten times the stall
limit, 10 minutes by default, before it is killed.

`/health` reports the running child under `mine_progress` (pid, calls
served, seconds since its last message).

    MEMPALACE_DAEMON_MINE_STALL_SECONDS=60  # or daemon.mine_stall_seconds; 0 = no reaper

### Turning auto-start on

Auto-start is off by default. A running daemon holds the palace writer lock,
the same lock a writable MCP server and `mempalace mine` take, so while a
hook-started daemon idles (up to 30 minutes after the last job) those writers
are refused. Auto-start only acts under `prefer` or `require`; the default
`direct` policy never probes or starts a daemon. To let hooks start it:

    MEMPALACE_HOOK_DAEMON_AUTOSTART=1

or in `config.json`:

    {"hooks": {"daemon_autostart": true}}

The legacy `hooks.daemon: true` setting does not turn it on. With auto-start
off, start the daemon yourself before the first hook:

    mempalace daemon start

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
