# Spec: `mempalace/daemon.py`

A long-lived, local, per-palace background daemon that accepts queued MemPalace
write jobs over an authenticated HTTP/1.1 endpoint bound to loopback, persists
those jobs in a SQLite queue, and executes them serially on a worker thread.
Daemon mode is strictly opt-in: default CLI, hooks, and MCP paths keep their
direct execution behavior unless a caller explicitly requests daemon-backed
execution (mempalace/daemon.py:L1-L6, L1109-L1111).

## Constants and configuration contract

- The server always binds to `127.0.0.1` (loopback only) (mempalace/daemon.py:L32, L786).
- State directory root: env var `MEMPALACE_DAEMON_STATE_ROOT` if set (expanded for `~`); otherwise `~/.mempalace/daemon` (mempalace/daemon.py:L33, L88-L92).
- Default job-wait timeout is 3600 seconds (mempalace/daemon.py:L34).
- Hook liveness-probe timeout is 0.5 seconds, kept under the hook budget so a wedged daemon cannot stall a hook (mempalace/daemon.py:L35-L39).
- Terminal job states are exactly `succeeded`, `failed`, `cancelled` (mempalace/daemon.py:L40).
- A job may be claimed at most `MAX_ATTEMPTS = 3` times before being dead-lettered (mempalace/daemon.py:L41, L315-L349).
- Request bodies are capped at 1 MiB (`1 << 20` bytes); larger bodies are rejected (mempalace/daemon.py:L42, L678-L679).
- Shutdown drains the active job for up to 10 seconds (mempalace/daemon.py:L43, L823).
- Terminal-job retention window in days comes from env `MEMPALACE_DAEMON_RETENTION_DAYS` (default 7); blank/zero falls back to 7 (mempalace/daemon.py:L44-L47).

## Path derivation contract

- `canonical_palace_path(path)` resolves the palace path (or `MempalaceConfig().palace_path` if none) to an absolute, symlink-resolved, `~`-expanded path (mempalace/daemon.py:L76-L78).
- `palace_key(palace_path)` returns the first 24 hex characters of the SHA-256 of the case-normalized canonical path. This key namespaces all daemon state per palace (mempalace/daemon.py:L81-L85).
- Per-palace state directory is `state_root()/<palace_key>` (mempalace/daemon.py:L95-L96).
- Within that directory: token file `token`, endpoint descriptor `endpoint.json`, pid file `pid`, queue DB `queue.sqlite3`, start lock `start.lock`, log file `daemon.log` (mempalace/daemon.py:L107, L126, L130, L134, L1013, L1045).

## File permissions and privacy contract

- Token, endpoint, pid, and any file written by `_write_private` are created with mode `0o600` (owner read/write only), parent directories created as needed (mempalace/daemon.py:L99-L104).
- The state directory is chmod'd `0o700` when created by the server and by `start_daemon` (mempalace/daemon.py:L61-L65, L788-L790, L1005-L1007).
- The queue DB and its `-wal`/`-shm` sidecars are chmod'd `0o600` whenever a `QueueStore` initializes, because they hold verbatim payloads (mempalace/daemon.py:L279-L288).
- The daemon log is chmod'd `0o600` (it may capture verbatim content in tracebacks) (mempalace/daemon.py:L966-L969).
- The server process sets umask `0o077` before creating the queue DB so all created files (DB, WAL/SHM sidecars) are owner-only, restoring the prior umask on exit (mempalace/daemon.py:L647-L653, L805-L806).
- chmod failures are swallowed (best-effort) (mempalace/daemon.py:L54-L58, L61-L65).

## Token management

- `ensure_token(palace_path)`: returns an existing non-empty token from the token file, otherwise generates a URL-safe 32-byte random token, writes it (with trailing newline) privately, and returns it (mempalace/daemon.py:L106-L114).
- `read_token(palace_path)`: returns the stripped token file contents; raises `DaemonError` if the token file is unreadable/absent (mempalace/daemon.py:L117-L122).

## Liveness probing

- `_pid_alive(pid)`: pids `<= 0` are not alive. On non-Windows, `signal 0` probe: process-not-found → not alive, permission-denied → alive, other OS error → not alive, success → alive (mempalace/daemon.py:L184-L202).
- On Windows, liveness is probed via the Win32 process-handle API (never `os.kill`, which would deliver a Ctrl-C). A live process returns wait-timeout (alive); an access-denied open means the process exists (alive); other open failures mean gone; if the Win32 probe itself errors, assume alive rather than discard a healthy endpoint (mempalace/daemon.py:L145-L193).

## On-disk endpoint descriptor (`endpoint.json`)

A JSON object written with 2-space indent and trailing newline, containing keys: `host` (always `127.0.0.1`), `port` (the actual bound TCP port), `pid` (server process id), `palace_path` (canonical path), `started_at` (UTC ISO-8601 timestamp) (mempalace/daemon.py:L791-L798). All timestamps in this module are UTC ISO-8601 (mempalace/daemon.py:L72-L73). `_read_endpoint` raises `DaemonError("daemon endpoint not found")` if the file is missing or malformed JSON (mempalace/daemon.py:L137-L142).

## Queue store: on-disk format and invariants

The queue is a SQLite database in WAL journal mode with a single table `jobs` (mempalace/daemon.py:L249-L278):

- Columns: `id` TEXT PRIMARY KEY, `kind` TEXT NOT NULL, `payload_json` TEXT NOT NULL, `state` TEXT NOT NULL, `priority` INTEGER DEFAULT 0, `dedupe_key` TEXT (nullable), `created_at` TEXT NOT NULL, `started_at` TEXT (nullable), `finished_at` TEXT (nullable), `result_json` TEXT (nullable), `error_json` TEXT (nullable), `attempts` INTEGER DEFAULT 0 (mempalace/daemon.py:L254-L267).
- Index `idx_jobs_state` on `(state, priority)`; index `idx_jobs_dedupe` on `(dedupe_key, state)` (mempalace/daemon.py:L270-L271).
- A UNIQUE partial index `idx_jobs_dedupe_active` on `dedupe_key` restricted to `state IN ('queued','running')` enforces at most one active job per dedupe key across processes; finished jobs drop out so a later identical enqueue is allowed (mempalace/daemon.py:L272-L278).
- `payload_json` is serialized with sorted keys and non-ASCII preserved (mempalace/daemon.py:L359).
- Each connection is short-lived and closed after use; on exception it rolls back, otherwise commits (mempalace/daemon.py:L228-L247).

### Job record (in-memory and serialized shape)

A `Job` has fields: `id`, `kind`, `payload` (object), `state`, `priority` (int), `dedupe_key` (string|null), `created_at`, `started_at` (string|null), `finished_at` (string|null), `result` (object|null), `error` (object|null), `attempts` (int) (mempalace/daemon.py:L205-L219). `_row_to_job` decodes JSON columns, treating empty/invalid JSON as null; payload defaults to `{}` (mempalace/daemon.py:L500-L523).

`job_to_dict(job, include_payload=True)` produces the wire/JSON shape with all the above scalar fields plus `result` and `error`; `payload` is included only when `include_payload` is true (verbatim content is withheld by default) (mempalace/daemon.py:L526-L542).

### Queue operations and ordering guarantees

- `enqueue(kind, payload, dedupe_key=None, priority=0)`: if a dedupe key is given and an active (queued/running) job already has it, that existing job is returned (no new row) (mempalace/daemon.py:L351-L372). Otherwise inserts a new `queued` job with a fresh 32-hex-char `id` and `attempts=0` (mempalace/daemon.py:L374-L383). On a cross-process unique-index collision: without a dedupe key the error propagates; with one, the winning active job is returned, or the insert is retried if the colliding row has since disappeared (mempalace/daemon.py:L384-L412).
- `claim_next()`: selects the highest-priority, oldest-created `queued` job (`ORDER BY priority DESC, created_at ASC`) and atomically flips it to `running`, sets `started_at`, and increments `attempts`, but only if it is still `queued`. Returns null if no queued job or if it lost the claim race to another process (mempalace/daemon.py:L414-L444).
- `finish(job_id, state, result=None, error=None, only_if_running=False)`: sets `state`, `finished_at`, `result_json` (defaulting to `{}`), and `error_json` (null if no error). When `only_if_running` is set, the update applies only while the job is still `running`, so a late worker finish cannot overwrite a shutdown-`cancelled` job (mempalace/daemon.py:L446-L478).
- `get(job_id)`: returns the job; raises `DaemonError("unknown job id: ...")` if absent (mempalace/daemon.py:L480-L485).
- `list(limit=20)`: returns jobs ordered `created_at DESC`, limit floored to at least 1 (mempalace/daemon.py:L487-L493).
- `counts()`: returns a map of state name → count (mempalace/daemon.py:L495-L498).
- `prune_terminal(older_than_days=retention)`: deletes terminal jobs whose `finished_at` is non-null and older than the cutoff; returns count deleted; no-ops returning 0 when the window is `<= 0`. Queued/running jobs are never touched (mempalace/daemon.py:L290-L313).
- `recover_running()`: on startup, jobs left `running` with `attempts >= MAX_ATTEMPTS` are dead-lettered to `failed` (with finish time and a `MaxAttemptsExceeded` error if none present); those with `attempts < MAX_ATTEMPTS` are re-queued (`state='queued'`, `started_at=NULL`). Returns the count re-queued. This prevents infinite re-execution of non-idempotent jobs (mempalace/daemon.py:L315-L349).

## Worker runtime

`DaemonRuntime` canonicalizes the palace path, opens the queue store, and tracks shutdown/wake events and the active job id (mempalace/daemon.py:L545-L553).

- `start_worker()` first runs `recover_running()`, then best-effort `prune_terminal()` (a prune failure must not block startup), then starts a background worker thread (mempalace/daemon.py:L555-L569).
- `worker_alive()` reports whether the worker thread exists and is alive (mempalace/daemon.py:L571-L572).
- The worker loop runs until shutdown is signaled. It claims the next job; on a queue/disk error it waits ~1s and retries without dying; with no job it waits up to 0.5s for a wake signal then re-loops (mempalace/daemon.py:L582-L594).
- For each claimed job the worker overrides `payload["palace_path"]` to the daemon's own canonical palace (a client cannot retarget the daemon at another palace) and overrides `payload["backend"]` when the daemon was started with one (mempalace/daemon.py:L595-L602).
- Execution is delegated to `execute_job(kind, payload)`; success is `result.get("success", True)` truthy → state `succeeded`, otherwise `failed` with error message from `result.get("error", "job failed")` (mempalace/daemon.py:L583, L603-L607).
- Any `Exception` or `SystemExit` during execution marks the job `failed` with `exit_code: 1` and an error object carrying the exception class name and message; the worker never dies from a job error (mempalace/daemon.py:L608-L622).
- All worker finishes go through `_safe_finish`, which calls `finish(..., only_if_running=True)` and swallows finish failures so a finish error cannot kill the worker or resurrect a cancelled job (mempalace/daemon.py:L574-L580).

## HTTP server contract

The server is an HTTP/1.1 threading server on `127.0.0.1` at the requested port (`0` = OS-assigned). It overrides bind to avoid a reverse-DNS lookup that could block startup ~30s (mempalace/daemon.py:L636-L637, L657-L660, L764-L781). All JSON responses are `application/json; charset=utf-8` with `Connection: close` (mempalace/daemon.py:L625-L633).

### Authentication

Every request must carry header `Authorization: Bearer <token>` matching the palace's token via constant-time comparison; otherwise the response is `401` with body `{"error":"unauthorized"}` and the handler returns without dispatching (mempalace/daemon.py:L664-L669, L684-L685, L738-L740).

### Body reading

`Content-Length` is parsed; a negative length raises `invalid Content-Length`; a length exceeding 1 MiB raises `request body too large`; an empty body yields `{}` (mempalace/daemon.py:L671-L681).

### Endpoints

- `GET /health` → `200` with `{ok: true, worker_alive, pid, palace_path, backend, active_job_id, counts}` (mempalace/daemon.py:L691-L707).
- `GET /jobs?limit=N` → `200` with `{jobs: [...]}` where each job omits its payload (default limit 20) (mempalace/daemon.py:L708-L715).
- `GET /jobs/<id>` → `200` with `{job: {...}}`; payload is included only when query `include_payload` is one of `1/true/yes/on` (case-insensitive), else withheld. Unknown id → `404` with `{"error": ...}` (mempalace/daemon.py:L716-L735).
- Any other GET path → `404` `{"error":"not found"}` (mempalace/daemon.py:L736).
- Other GET errors (malformed query / DB error) → `400` with the error string (mempalace/daemon.py:L686-L689).
- `POST /jobs` with JSON body `{kind, payload, dedupe_key, priority}` enqueues the job, wakes the worker, and returns `202` with `{job: {...}}` (payload included). `kind` defaults to empty string, `payload` to `{}`, `priority` to 0. Any error → `400` with `{"error": ...}` (mempalace/daemon.py:L742-L756).
- `POST /shutdown` → responds `200 {"ok": true}`, then sets the shutdown event and asynchronously stops the server (mempalace/daemon.py:L757-L761).
- Any other POST path → `404` `{"error":"not found"}` (mempalace/daemon.py:L762).

### Server lifecycle and side effects

`run_server` sets env vars `MEMPALACE_PALACE_PATH`, and (when a backend is given) `MEMPALACE_BACKEND_EXPLICIT` and `MEMPALACE_BACKEND`; the prior values are captured and restored on exit (mempalace/daemon.py:L636-L646, L805-L806, L837-L841). After binding, it creates the state dir, writes `endpoint.json` and `pid`, starts the worker, and serves with a 0.5s poll interval (mempalace/daemon.py:L785-L802).

`_drain_and_cleanup` (always run on server exit): signals shutdown, joins the worker for up to 10s, marks any still-active job `cancelled` (`exit_code: 1`, message "cancelled by daemon shutdown") so recovery will not re-run it, deletes the `endpoint.json` and `pid` files, and restores the mutated env vars (mempalace/daemon.py:L803-L841).

## DaemonClient

`DaemonClient(palace_path)` reads `endpoint.json`; raises `DaemonError` if it lacks a port (mempalace/daemon.py:L844-L850). If the endpoint's pid is present and not alive, it raises `DaemonError("daemon endpoint pid is not alive")` and never reads/sends the token — guarding against a stale endpoint whose port was reused by an unrelated process (mempalace/daemon.py:L851-L858). It builds a no-proxy HTTP opener so loopback requests bypass proxy discovery (mempalace/daemon.py:L859-L869).

- `base_url` is `http://<host>:<port>` (mempalace/daemon.py:L871-L873).
- `request(method, path, body=None, timeout=5.0)`: sends JSON with the bearer token; on HTTP error raises `DaemonError` carrying the server's `error` field; on other OS errors raises `DaemonError`; empty body returns `{}`; a non-JSON 2xx body raises `DaemonError("daemon returned non-JSON response: ...")` (mempalace/daemon.py:L875-L913).
- `health(timeout=5.0)` GETs `/health` (mempalace/daemon.py:L915-L916).
- `submit(kind, payload, dedupe_key=None, priority=0)` POSTs `/jobs` and returns the `job` object (mempalace/daemon.py:L918-L930).
- `get_job(id)` and `list_jobs(limit=20)` GET the respective endpoints (mempalace/daemon.py:L932-L936).
- `wait(job_id, timeout=3600)` polls `get_job` every 0.2s until the job reaches a terminal state and returns it; raises `DaemonError` on timeout (mempalace/daemon.py:L938-L946).
- `shutdown()` POSTs `/shutdown` (mempalace/daemon.py:L948-L949).

## Lifecycle helpers

- `get_client_if_running(palace_path, health_timeout=5.0)`: constructs a client and probes `/health`; returns the client if healthy, else `None` (any `DaemonError` is swallowed). Hook callers pass the short 0.5s timeout (mempalace/daemon.py:L952-L962).
- `start_daemon(palace_path, backend=None, foreground=False, timeout=15.0)`: ensures a token; returns an already-running daemon's client if present. In `foreground` mode it runs the server in-process (blocking) and returns `None` on clean stop (mempalace/daemon.py:L987-L1003). Otherwise it spawns a detached child process `python -m mempalace.daemon serve --palace <path> [--backend <b>]`, propagating `MEMPALACE_DAEMON_STATE_ROOT`, and polls `/health` until ready or until `timeout`; if the child exits during startup it raises `DaemonError`; on any readiness failure it kills and reaps the orphaned child before raising (mempalace/daemon.py:L1032-L1076).
- Spawn mutual exclusion uses a non-blocking exclusive `flock` on `start.lock` (POSIX only). A second concurrent starter blocks on the lock, then reuses the winner's daemon if it came up, else spawns itself (mempalace/daemon.py:L1009-L1025). Before spawning, stale `endpoint.json`/`pid` files are removed (mempalace/daemon.py:L1027-L1031).
- The detached child is launched with stdin from devnull, stdout/stderr appended to `daemon.log`, closed fds, and a new session (POSIX) or detached process-group creation flags (Windows) (mempalace/daemon.py:L965-L984).
- `ensure_client(palace_path, backend=None, auto_start=True)`: returns a running client; if none and `auto_start` is false, raises `DaemonError("daemon is not running")`; otherwise starts a daemon (mempalace/daemon.py:L1085-L1094).
- `submit_job(...)`: resolves the canonical palace (from arg or `payload["palace_path"]`), overrides `payload["palace_path"]` (never trusting client input) and optionally `payload["backend"]`, obtains a client (`auto_start` defaults to false — strictly opt-in), submits the job, and either returns it immediately (`wait=False`) or waits for terminal state (mempalace/daemon.py:L1097-L1121).
- `stop_daemon(palace_path)`: if a daemon is running, sends shutdown and returns `True`; otherwise returns `False` (mempalace/daemon.py:L1124-L1129).

## CLI entry point

Invoked as `python -m mempalace.daemon`. It requires a subcommand; the only subcommand is `serve` with options `--palace` (required), `--backend` (default none), `--port` (int, default 0). `serve` runs the blocking HTTP server (mempalace/daemon.py:L1132-L1149).

## Error type

All client/operation failures raise `DaemonError`, a runtime error subtype (mempalace/daemon.py:L68-L69).
