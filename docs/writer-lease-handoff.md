# Writer-lease handoff

One palace, several MemPalace processes: two Claude Code sessions, a hook-driven
mine, an editor plugin. Exactly one of them may write — local backends keep
in-memory index state (Chroma HNSW, SQLite WAL/FTS) that a second long-lived
writer would silently invalidate. The palace lock enforces that, and a
long-lived MCP server takes it as a **lease**: acquired on first use, held for
the lifetime of the process.

That is safe but sticky. The first server to touch the palace keeps it even when
it has been idle for hours, and every other session stays read-only for its own
whole lifetime — the peer has to *exit* before anyone else can write.

The handoff turns the lease into a **baton**: an idle holder gives the lock to a
peer that asks for it, and takes it back the next time it needs to write.

## The protocol

Two OS lock files per palace, in `~/.mempalace/locks/`:

| File | Meaning |
| --- | --- |
| `mine_palace_<key>.lock` | **Ownership.** Whoever holds it may write. |
| `mine_palace_<key>.want` | **Demand.** A contender holds it while queued. |

Both are kernel locks, so a crashed or killed process releases both instantly
and there is no stale state to garbage-collect. Neither file's *contents* decide
anything — the body records `PID + argv` purely so error messages and
`mempalace_status` can name a human-readable holder.

**Contender** (`mine_palace_lock(palace, wait=N)`):

1. Try ownership without blocking. Uncontended → done, nothing else happens.
2. Otherwise take the demand lock. Only the head of the queue proceeds, so N
   idle servers cannot stampede one holder into N handoffs.
3. Poll ownership until it lands or `wait` expires. On expiry: the same
   `MineAlreadyRunning` as before the handoff existed.

**Holder** (the MCP server's `mcp-writer-handoff` watchdog thread):

1. Every `MEMPALACE_WRITER_HANDOFF_POLL_SECONDS`, probe demand — a non-blocking
   attempt on the demand file. Nobody queued → do nothing.
2. Refuse if the lease is younger than `MEMPALACE_WRITER_MIN_HOLD_SECONDS`.
3. Take the dispatch lock without blocking; a request in flight means try again
   next tick.
4. Open a handoff window (`begin_palace_lock_handoff`). It refuses while any
   write frame is active and parks writers that arrive mid-release.
5. Close storage handles, release ownership, close the window. This server is
   read-only again until its next write, which reacquires through step 1 above.

### Why a watchdog and not a check on the write path

The case that hurts is a server that took the lease, went idle, and now blocks
everyone else while doing nothing. A demand check that only runs when *this*
server writes would never fire for exactly that server.

### What makes it safe

Handing the lock away mid-write would put two processes into one palace — the
corruption the lock exists to prevent, and silent when it happens. Two things
prevent it:

* **Write-frame depth.** Every executing `mine_palace_lock` body counts, including
  the re-entrant pass-through frames that a write takes under an existing lease
  (they hold no OS lock of their own and trust the lease to still be there).
  `lease=True` deliberately does not count: a parked lease *is* the idle state.
* **The handoff window.** Depth is checked and the window opened under one lock,
  and a writer arriving mid-release parks on that same condition instead of
  taking pass-through credit. Without it, a thread that checked ownership
  microseconds before the release would write with no lock at all.

The dispatch lock covers the other direction: reads serve from cached storage
handles *without* taking the palace lock, so closing those handles under a
running request would be a use-after-close.

One class of request is deliberately outside that lock, on both transports:
the logstream tools in `_HTTP_LOCK_FREE_TOOLS` reach only `logstream.sqlite3`,
never Chroma or the KG (they are exempt from the writer lease for the same
reason). Serializing them would put a five-minute `mempalace_event_wait`
long-poll in front of every handoff — the watchdog would find the dispatch lock
held for minutes by a call that touches no storage at all. `_dispatch_locally`
is the single place that policy lives; if the lock is narrowed further (#1984),
whatever replaces it must still exclude the watchdog for every request that
*does* touch storage.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `MEMPALACE_WRITER_HANDOFF_WAIT_SECONDS` | `15` | How long a mutating tool waits for the baton before falling back to a read-only refusal. |
| `MEMPALACE_WRITER_MIN_HOLD_SECONDS` | `5` | Minimum ownership time before this server will give the lease away. |
| `MEMPALACE_WRITER_HANDOFF_POLL_SECONDS` | `1` | How often the holder probes for demand. |
| `MEMPALACE_WRITER_HANDOFF_DISABLED` | unset | Opt out entirely; restores pre-handoff behavior. |
| `MEMPALACE_WRITER_HANDOFF_ENABLED` | unset | Opt *in* for the HTTP transport (off there by default). |
| `MEMPALACE_PALACE_LOCK_WAIT_SECONDS` | `0` | Makes non-MCP callers (`mempalace mine`, hooks) wait for the baton too. |

Keep `WAIT > MIN_HOLD + POLL`: a contender must outlast the holder's floor, or a
lease taken moments earlier can never change hands inside the contender's window.

`MIN_HOLD` is the anti-thrash knob. Handing off means closing and reopening
storage, so two chatty sessions with a floor of zero would ping-pong the lease on
every write and be slower than the read-only refusal they replaced. Raise it when
sessions write in bursts; lower it when they trade writes turn by turn.

`MEMPALACE_PALACE_LOCK_WAIT_SECONDS` stays `0` by default because hook-spawned
`mempalace mine` copies are *supposed* to collapse: N of them firing at once must
exit immediately rather than queue as waiters that then drive parallel HNSW
inserts. Set it only where a mine losing its turn is worse than a mine waiting.

The HTTP transport is opt-in because a writable HTTP server is a deliberate
single-writer service — it refuses to start without ownership — so giving the
palace to a random peer mid-flight is an operator's decision.

## Observing it

`mempalace_status` reports a `writer_lease` block:

```json
"writer_lease": {
  "held_by_this_server": false,
  "handoff_enabled": true,
  "handoffs_granted": 1,
  "handoffs_taken": 0,
  "palace_locked": true,
  "holder": "PID 3770678 (…/mcp_server.py --palace /srv/palace)",
  "last_reason": "writer lease handed to PID 3770678; this server reacquires it on its next write"
}
```

`palace_locked` comes from a kernel probe, `holder` from the lock-file body —
and the body names whoever took the lock *last*, which may be a process that has
since exited. When the probe says the palace is free, `holder` is `null` rather
than a dead PID: a stale name here would send someone debugging a stuck write
after a process that no longer exists.

The server logs every transition:

```
Writer lease for /srv/palace handed off to PID 3770678 (…) (held 3.0s, 1 total)
Took the writer lease for /srv/palace after 3.0s in the queue
```

Rising `handoffs_granted` with no useful work between them means the lease is
thrashing — raise `MEMPALACE_WRITER_MIN_HOLD_SECONDS`.

## When one server is the better answer

The handoff makes several direct writers on one palace workable; it does not
make them the best shape. `mempalace serve` (HTTP) is one process owning the
storage with several clients talking to it: there is no cross-process lease at
all, concurrent writes from different sessions simply serialize inside the
server, and none of the knobs on this page apply. The hub proxy makes that the
default experience for stdio clients too — `_dispatch_stdio_request` forwards to
a live hub when one is configured, and the local path below only runs when there
is none.

Prefer the single server where you control the deployment. The handoff is for
everywhere you do not: stdio sessions started by an editor or an agent harness,
a hook-driven mine on someone's laptop, mixed versions across a fleet.

## Interaction with the daemon

The daemon (`docs/write-routing-policy.md`, #1963) solves the same contention a
different way: one writer process, everyone else enqueues jobs. Where write
routing is set to `require`, this handoff is redundant — the daemon is the sole
owner and no peer queues for the lock. The two do not conflict: the handoff is
what keeps direct-writing peers usable where the daemon is not deployed, and it
is the daemon's own lease that keeps it authoritative where it is.
