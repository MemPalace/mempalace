# Behavior Spec — `tests/test_daemon.py`

This is a test suite that asserts the externally observable contract of two
modules: the background **daemon** (a local HTTP job queue server) and the
**service** layer (job-execution dispatch). The behaviors below are the
ground-truth contract any reimplementation of `daemon` / `service` must satisfy.

## Environment & process-global contracts

A state-root override environment key (`daemon.STATE_ROOT_ENV`) redirects all
daemon on-disk state (queue DB, token, endpoint file) to a caller-chosen
directory; every test sets it before constructing daemon objects
(`tests/test_daemon.py:L60`, `tests/test_daemon.py:L233`).

The daemon mutates three process-global environment keys —
`MEMPALACE_PALACE_PATH`, `MEMPALACE_BACKEND`, `MEMPALACE_BACKEND_EXPLICIT` — and
the process umask from its background thread; these must be restored to their
pre-existing values so a leaked server thread cannot poison later operations
(`tests/test_daemon.py:L19-L48`).

POSIX file-mode bits `0600`/`0700` are part of the privacy contract on POSIX
systems only; on Windows privacy is provided by user-profile directory ACLs and
the owner-only mode assertions do not apply (`tests/test_daemon.py:L10-L17`).

## QueueStore — durable job queue (SQLite-backed)

`daemon.queue_path(palace_path)` derives the queue DB path from a palace path;
`daemon.QueueStore(path)` opens/creates the durable queue at that path
(`tests/test_daemon.py:L62-L63`).

### Enqueue, dedupe, and job shape

`enqueue(kind, payload, dedupe_key=None, priority=0)` inserts a job and returns a
job object exposing at least `id`, `state`, and `attempts`
(`tests/test_daemon.py:L65`, `tests/test_daemon.py:L350`,
`tests/test_daemon.py:L362`). A newly enqueued job has state `"queued"`
(`tests/test_daemon.py:L86`).

When two enqueues share the same non-null `dedupe_key`, the second returns the
same job id as the first — no duplicate job is created
(`tests/test_daemon.py:L95-L98`).

### Claiming jobs

`claim_next()` atomically transitions one `queued` job to `running` and returns
it; the returned job's `state` is `"running"` (`tests/test_daemon.py:L100-L102`).
The claim is a conditional update gated on `state='queued'`, so a job already
`running` can never be claimed twice — the cross-process double-execution guard
(`tests/test_daemon.py:L365-L375`). When no `queued` job exists, `claim_next()`
returns `None` (`tests/test_daemon.py:L379-L380`).

### Finishing jobs

`finish(id, state=..., result=...)` marks a job terminal with the given state
and stored result; e.g. `state="succeeded"` with a result object
(`tests/test_daemon.py:L66`, `tests/test_daemon.py:L379`). `get(id)` returns the
stored job; a finished job reports the terminal state and its stored result
(`tests/test_daemon.py:L85`, `tests/test_daemon.py:L125-L126`).

### Recovery of interrupted jobs

`recover_running()` reconciles jobs left in `running` state after an interrupted
run and returns the count of jobs it re-queued (`tests/test_daemon.py:L104-L106`).
A recovered running job is returned to state `"queued"`
(`tests/test_daemon.py:L106`).

A job whose `attempts` has reached `daemon.MAX_ATTEMPTS` is NOT re-queued by
`recover_running()`; it is dead-lettered to terminal `"failed"` (its attempt
count preserved) and counts as 0 recovered. This prevents non-idempotent kinds
(e.g. `diary_write`) from duplicating verbatim content on every restart
(`tests/test_daemon.py:L342-L362`).

A job already in a terminal `"cancelled"` state is never re-queued by
`recover_running()` (returns 0) (`tests/test_daemon.py:L334-L336`).

### Pruning terminal jobs

`prune_terminal(older_than_days=N)` deletes terminal jobs whose `finished_at`
timestamp is older than N days and returns the count pruned
(`tests/test_daemon.py:L80-L81`). Jobs whose `finished_at` is within the
retention window, and any non-terminal (`queued`/`running`) jobs, are kept
untouched; a pruned job is fully removed so `get(id)` on it raises
`daemon.DaemonError` (`tests/test_daemon.py:L82-L86`). `finished_at` is stored as
an ISO-8601 UTC timestamp string (`tests/test_daemon.py:L71-L78`).

### Owner-only DB permissions (POSIX)

The queue DB file (`store.path`) holds verbatim payloads and MUST be created with
mode `0600`, not the SQLite default `0644`
(`tests/test_daemon.py:L383-L395`).

## Token & endpoint files

`daemon.ensure_token(palace_path)` creates an authentication token under the
daemon state directory; `daemon.state_dir(palace_path)` returns that directory
and the token lives at `state_dir/token` (`tests/test_daemon.py:L405-L406`). On
POSIX the token file MUST be mode `0600` (`tests/test_daemon.py:L398-L407`).

The endpoint descriptor is a JSON file at `state_dir/endpoint.json` whose object
contains at least `host`, `port`, and `pid`
(`tests/test_daemon.py:L499-L501`). `daemon.endpoint_path(palace_path)` returns
its path (`tests/test_daemon.py:L265`). Constructing a `DaemonClient` against an
`endpoint.json` that is missing the `port` field MUST raise `daemon.DaemonError`
(not a bare key/lookup error) (`tests/test_daemon.py:L489-L504`).

## Palace path canonicalization

`daemon.canonical_palace_path(palace_path)` returns the canonical (resolved)
absolute form of a palace path (`tests/test_daemon.py:L120`,
`tests/test_daemon.py:L166`). The health endpoint reports this canonical path,
and it equals the resolved absolute path of the palace directory
(`tests/test_daemon.py:L120`, `tests/test_daemon.py:L127`).

## HTTP daemon server (`run_server`) and client

`daemon.run_server(palace_path, port=0)` binds an HTTP server (port 0 = OS-chosen
ephemeral port) and serves until shut down; it runs the job worker in the
background (`tests/test_daemon.py:L246`). The server is multi-threaded
(`ThreadingHTTPServer` base) (`tests/test_daemon.py:L192-L199`).

`daemon.get_client_if_running(palace_path, health_timeout=...)` returns a
connected client if the daemon is up and healthy, else `None`; it probes by
calling the client's `health(timeout=...)` (`tests/test_daemon.py:L257`,
`tests/test_daemon.py:L838-L856`). The hook precheck path uses a short probe
timeout `daemon.HOOK_PROBE_TIMEOUT` which is `<= 0.5` seconds, and that value is
passed through verbatim as the health timeout (`tests/test_daemon.py:L853-L856`).

### Client surface

A daemon client exposes `host`, `port`, and `token` attributes
(`tests/test_daemon.py:L796`, `tests/test_daemon.py:L802`,
`tests/test_daemon.py:L418`). Methods:

- `health()` → object with `ok: bool`, `palace_path: str`, and `worker_alive: bool`
  (`tests/test_daemon.py:L118-L120`, `tests/test_daemon.py:L293`).
- `submit(kind, payload, dedupe_key=None, priority=0)` → enqueues a job, returns
  an object with at least `id` and `state` (`tests/test_daemon.py:L122`,
  `tests/test_daemon.py:L140-L142`).
- `wait(job_id, timeout=DEFAULT_WAIT_TIMEOUT)` → blocks until the job is terminal
  and returns its final state object (`tests/test_daemon.py:L123`,
  `tests/test_daemon.py:L144-L150`).
- `get_job(job_id)` → returns the current job state object
  (`tests/test_daemon.py:L324`).
- `shutdown()` → requests server shutdown (POST `/shutdown`)
  (`tests/test_daemon.py:L214`).

### Job lifecycle & result shape

On submit, the daemon executes the job via `service.execute_job(kind, payload)`
on its worker thread (`tests/test_daemon.py:L236`, `tests/test_daemon.py:L112`).
On success the final job object has `state == "succeeded"` and a `result` object
equal to what `execute_job` returned (e.g. `{"success": True, "exit_code": 0,
"stdout": "done\n"}`) (`tests/test_daemon.py:L113-L126`).

### Palace-path override (security invariant)

Before executing, the daemon overwrites `payload["palace_path"]` with its own
canonical palace path; it is NEVER trusted from the client-supplied payload. A
payload submitting `"palace_path": "/tmp/other-palace"` is observed by the
executor as the daemon's canonical palace path instead
(`tests/test_daemon.py:L127`, `tests/test_daemon.py:L432-L450`).
`daemon.submit_job(...)` likewise overrides (not appends) `palace_path` with the
canonical form before handing the payload to the client
(`tests/test_daemon.py:L155-L167`).

### Worker crash resilience (`SystemExit`)

If `execute_job` raises `SystemExit` (a non-`Exception` base exception), the
daemon MUST catch it, mark the job `"failed"` with an `error` object whose
`error_class == "SystemExit"`, and keep the worker alive
(`tests/test_daemon.py:L273-L290`). After such a crash, `health()["worker_alive"]`
is `True` and a subsequent submitted job runs to `"succeeded"`
(`tests/test_daemon.py:L292-L296`).

### Shutdown cancels in-flight work

On `/shutdown`, the worker is drained for a bounded window
(`daemon.SHUTDOWN_DRAIN_SECONDS`); a job still in flight after the drain is
marked terminal `"cancelled"` (not left `running`), so that `recover_running()`
will not re-run it on the next start (`tests/test_daemon.py:L301-L336`).

### Request validation / hardening

The server is token-authenticated: a request to `/health` with no
`Authorization` header, or with a wrong bearer token, returns HTTP 401
(`tests/test_daemon.py:L410-L427`). The expected header form is
`Authorization: Bearer <token>` (`tests/test_daemon.py:L425`,
`tests/test_daemon.py:L802`).

A POST to `/jobs` with `Content-Length: -1` (negative) MUST return a prompt HTTP
400 and not block/hang the worker (must not read until socket close, must not
bypass the `MAX_BODY_BYTES` cap) (`tests/test_daemon.py:L783-L808`).

On POSIX, `run_server` MUST tighten the process umask to `0o077` BEFORE
constructing `DaemonRuntime`/`QueueStore`, so SQLite WAL/SHM sidecar files
holding un-checkpointed verbatim payloads are not created world-readable
(`tests/test_daemon.py:L756-L780`).

## Daemon spawn lifecycle (`start_daemon`)

`daemon.start_daemon(palace_path, timeout=...)` spawns the daemon subprocess
(via `subprocess.Popen`) and waits for readiness. If the spawned daemon never
becomes ready within `timeout`, `start_daemon` MUST kill and reap the orphaned
subprocess (call its `kill()`) and raise `daemon.DaemonError`, rather than
leaking the process with its bound port/token
(`tests/test_daemon.py:L541-L577`).

## PID liveness probe (`_pid_alive`)

`daemon._pid_alive(pid)` is a pure, signal-free liveness probe. It returns `True`
for the current process pid, and `False` for pid `0`, pid `-1`, and a pid that is
almost certainly not running (e.g. `2_000_000_000`)
(`tests/test_daemon.py:L520-L524`). It MUST NOT emit any signal/console control
event — even when called repeatedly it never delivers `SIGINT`/`CTRL_C_EVENT`
(no spurious `KeyboardInterrupt`) (`tests/test_daemon.py:L526-L538`).

## `submit_job` convenience entry point

`daemon.submit_job(kind, payload, palace_path=..., dedupe_key=..., wait=...)`
obtains a client via `daemon.ensure_client(...)`, calls `client.submit(...)` with
the canonicalized `palace_path` injected into the payload and the given
`dedupe_key`, and — when `wait=True` — returns the result of `client.wait(...)`
on the new job id (`tests/test_daemon.py:L132-L167`).

## service.classify_tool — tool category mapping

`service.classify_tool(name)` maps an MCP tool name to a category string:
`"mempalace_search"` → `"read"`; `"mempalace_add_drawer"` → `"write"`;
`"mempalace_mine"` → `"maintenance"`; an unrecognized name → `"unknown"`
(`tests/test_daemon.py:L170-L174`, `tests/test_daemon.py:L469`).

## service.run_mcp_tool — write-only allowlist dispatch

`service.run_mcp_tool({"name": ..., "arguments": ...})` only accepts **write**
tools onto the durable queue. Read tools, maintenance tools (which have their own
kinds `mine`/`sync`), and unknown tools are rejected with `success: False` and an
error message containing `"only accepts write tools"`
(`tests/test_daemon.py:L453-L466`).

`arguments` must be an object/map; a non-object `arguments` is rejected with
`success: False`, error containing `"must be an object"`, and `exit_code == 2`
(`tests/test_daemon.py:L639-L645`).

For a valid write tool, the handler is invoked with `arguments` spread as keyword
parameters; the handler's returned object is passed through with `exit_code == 0`
added on success (`tests/test_daemon.py:L648-L663`).

Result interpretation: a handler result containing `{"error": ...}` with no
explicit success flag is recorded as a **failed** job — `success: False`,
`exit_code == 1`, and `error` set to that message
(`tests/test_daemon.py:L813-L827`). A result with neither an explicit success
flag nor an `error` key is treated as **success** — `success: True`,
`exit_code == 0` (`tests/test_daemon.py:L829-L835`).

## service.execute_job — kind dispatch & env isolation

`service.execute_job(kind, payload)` dispatches by `kind`:
- `"mine"` → mining path (`tests/test_daemon.py:L485`).
- `"diary_write"` → diary write tool (`tests/test_daemon.py:L707`).
- `"mcp_tool"` → `run_mcp_tool` with `name`/`arguments`
  (`tests/test_daemon.py:L708-L713`).
- any unknown kind → structured failure: `success: False`, `exit_code == 2`
  (`tests/test_daemon.py:L714-L716`).

Per-job environment isolation: if a job mutates the process env (e.g. sets
`MEMPALACE_BACKEND`), that mutation MUST NOT leak past the job — after
`execute_job` returns, `MEMPALACE_BACKEND` is back to unset
(`tests/test_daemon.py:L472-L486`).

## service.run_mine

`service.run_mine(payload)` validates the `mode` field. An invalid mode (e.g.
`"bogus"`) returns a structured error: `success: False`, error containing
`"invalid mine mode"`, `exit_code == 2`
(`tests/test_daemon.py:L628-L636`). Backend selection (`backend` key) is applied
— env set plus backend-class validation — BEFORE mode validation, and the
invalid mode short-circuits before any mining runs
(`tests/test_daemon.py:L685-L694`).

## service.run_sync

`service.run_sync(payload)` returns success when there is nothing to sync: a
missing palace directory yields `success: True`, `exit_code == 0`
(`tests/test_daemon.py:L610-L615`); a palace directory present but with no
backend artifact also yields `success: True`, `exit_code == 0`
(`tests/test_daemon.py:L618-L625`).

When `sync_palace` fails, `run_sync` returns a structured error rather than
propagating: a "mine already running" lock condition →
`error_class == "LockHeldByOtherProcess"`; a `ValueError` (e.g. bad scope) →
`exit_code == 2`; any other exception → error message containing `"sync failed"`
(`tests/test_daemon.py:L719-L750`).

## service.run_diary_write

`service.run_diary_write(payload)` forwards `agent_name`, `entry`, `topic`, and
`wing` to the diary-write tool and, on success, returns `success: True` with
`exit_code == 0` (`tests/test_daemon.py:L666-L682`).

## service.print_job_result — stdout/stderr replay & exit code

`service.print_job_result(result)` replays the job's captured `stdout` to this
process's stdout and `stderr` to this process's stderr, and returns the job's
`exit_code` as its return value (`tests/test_daemon.py:L589-L598`). When the
result has no `stderr` but has an `error`, the error is written to stderr in the
form `"mempalace: <error>"`, and the result's `exit_code` is returned
(`tests/test_daemon.py:L601-L607`).

<promise>SPEC_WRITTEN path=specs/tests/test_daemon.md citations=58</promise>
