# Async MCP Daemon Writes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MCP mining and writes durable daemon jobs so long mines never hold an MCP request or block job status and maintenance-time search.

**Architecture:** Add progress-aware daemon jobs and a focused `mcp_jobs` adapter. In daemon-write mode, MCP submits mutations to the existing single daemon worker, exposes safe job reads, and routes search to SQLite/BM25 while maintenance is active. Legacy direct mode remains behind the disabled feature flag during the compatibility window.

**Tech Stack:** Python 3.9+, stdlib SQLite/HTTP/threading, ChromaDB, pytest.

## Global Constraints

- Preserve verbatim content and local-only operation.
- `MEMPALACE_MCP_DAEMON_WRITES=1` enables the new writer lane; no direct-write fallback is allowed while enabled.
- Warm mine submission p95 must stay below 500 ms; job reads below 100 ms.
- CLI `mempalace mine` remains synchronous.
- Daemon jobs remain serialized; do not add a second palace writer.
- Queue payloads and job-list responses must not expose verbatim write content.

---

### Task 1: Progress-aware durable queue

**Files:**
- Modify: `mempalace/daemon.py:205-542`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Produces: `QueueStore.update_progress(job_id: str, progress: dict[str, Any]) -> Job`
- Produces: `QueueStore.heartbeat(job_id: str) -> Job`
- Produces: `QueueStore.active(kind: str | None = None) -> list[Job]`
- Produces: `QueueStore.list(limit: int = 20, state: str | None = None, kind: str | None = None) -> list[Job]`
- Produces: `QueueStore.enqueue_with_status(kind, payload, dedupe_key=None, priority=0) -> tuple[Job, bool]`, where the boolean reports active-job deduplication without changing legacy `enqueue()` callers.
- Produces: `job_to_dict(job, include_payload=False, redact_payload=True)` with progress/timestamps.

- [ ] **Step 1: Write failing queue schema/progress/filter/redaction tests**

```python
def test_queue_adds_progress_columns_to_existing_database(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    job = store.enqueue("mine", {"source": "/repo"})
    updated = store.update_progress(job.id, {"phase": "scanning", "files_processed": 2})
    assert updated.progress == {"phase": "scanning", "files_processed": 2}
    assert updated.updated_at is not None

def test_job_to_dict_redacts_write_payload(tmp_path):
    store = QueueStore(tmp_path / "queue.sqlite3")
    job = store.enqueue("mcp_tool", {"name": "mempalace_add_drawer", "arguments": {"content": "secret"}})
    payload = job_to_dict(job, include_payload=True, redact_payload=True)
    assert payload["payload"] == {"name": "mempalace_add_drawer", "arguments": "<redacted>"}
```

- [ ] **Step 2: Run tests and confirm the missing fields/methods fail**

Run: `.venv/bin/pytest tests/test_daemon.py -k 'progress or filters or redact' -q`

Expected: FAIL because `Job.progress`, `update_progress`, filtering, and redaction are absent.

- [ ] **Step 3: Implement additive queue migration and methods**

```python
@dataclass
class Job:
    # existing fields stay unchanged
    updated_at: str | None
    heartbeat_at: str | None
    progress: dict[str, Any] | None

def _ensure_job_columns(conn: sqlite3.Connection) -> None:
    present = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for name, ddl in (
        ("updated_at", "TEXT"),
        ("heartbeat_at", "TEXT"),
        ("progress_json", "TEXT"),
    ):
        if name not in present:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")

def update_progress(self, job_id: str, progress: dict[str, Any]) -> Job:
    now = _now()
    with self._lock, self._connect() as conn:
        conn.execute(
            "UPDATE jobs SET progress_json = ?, updated_at = ?, heartbeat_at = ? WHERE id = ?",
            (json.dumps(progress, ensure_ascii=False), now, now, job_id),
        )
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row)
```

- [ ] **Step 4: Run daemon unit tests**

Run: `.venv/bin/pytest tests/test_daemon.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mempalace/daemon.py tests/test_daemon.py
git commit -m "feat: add daemon job progress and safe listing"
```

### Task 2: Heartbeat and progress execution context

**Files:**
- Modify: `mempalace/daemon.py:545-635`
- Modify: `mempalace/service.py:104-231`
- Modify: `mempalace/miner.py:1402-1900`
- Modify: `mempalace/convo_miner.py:644-820`
- Modify: `mempalace/format_miner.py:693-930`
- Test: `tests/test_daemon.py`
- Test: `tests/test_miner.py`

**Interfaces:**
- Produces: `ProgressCallback = Callable[[dict[str, Any]], None]`
- Produces: `execute_job(kind, payload, progress_callback=None)` and `run_mine(payload, progress_callback=None)`.
- Consumes: Task 1 `QueueStore.update_progress()` and `heartbeat()`.
- Consumes: existing process-reentrant `mine_palace_lock()` so every daemon job owns the cross-process writer lease while nested miner/Chroma writes pass through safely.

- [ ] **Step 1: Write failing heartbeat and miner callback tests**

```python
def test_runtime_heartbeats_while_job_is_blocked(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(service, "execute_job", lambda kind, payload, progress_callback=None: (started.set(), release.wait(5), {"success": True})[-1])
    runtime = DaemonRuntime(str(tmp_path / "palace"))
    runtime.store = QueueStore(tmp_path / "queue.sqlite3")
    job = runtime.store.enqueue("mine", {"source": str(tmp_path)})
    runtime.start_worker()
    assert started.wait(1)
    first = runtime.store.get(job.id).heartbeat_at
    assert first is not None
    release.set()

def test_mine_reports_scanning_and_post_processing(tmp_path, monkeypatch):
    events = []
    mine(str(tmp_path), str(tmp_path / "palace"), dry_run=True, progress_callback=events.append)
    assert events[0]["phase"] == "scanning"
    assert events[-1]["phase"] == "verifying"
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `.venv/bin/pytest tests/test_daemon.py tests/test_miner.py -k 'heartbeat or reports_scanning' -q`

Expected: FAIL on unsupported callback and missing heartbeat updates.

- [ ] **Step 3: Add a throttled callback and daemon heartbeat thread**

```python
ProgressCallback = Callable[[dict[str, Any]], None]

def _emit_progress(callback: ProgressCallback | None, **values: Any) -> None:
    if callback is not None:
        callback(values)

def _heartbeat_until_stopped(store: QueueStore, job_id: str, stop: threading.Event) -> None:
    while not stop.wait(5.0):
        store.heartbeat(job_id)
```

Pass `progress_callback` through `execute_job` and `run_mine` into all three miners. Emit scanning, mining counters, post-processing, and verifying phases; throttle file-loop emission to one update per second. Wrap each daemon `execute_job` call in `with mine_palace_lock(self.palace_path):`; the existing process-wide re-entrant lock behavior permits nested miner and Chroma write acquisition while excluding direct writers in other processes.

- [ ] **Step 4: Run miner and daemon tests**

Run: `.venv/bin/pytest tests/test_daemon.py tests/test_miner.py tests/test_convo_miner.py tests/test_format_miner.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mempalace/daemon.py mempalace/service.py mempalace/miner.py mempalace/convo_miner.py mempalace/format_miner.py tests/
git commit -m "feat: report mining progress through daemon jobs"
```

### Task 3: Async mine submission and worktree protection

**Files:**
- Create: `mempalace/mcp_jobs.py`
- Modify: `mempalace/mcp_server.py:2741-2887`
- Modify: `mempalace/mcp_server.py:4354-4406`
- Test: `tests/test_mcp_jobs.py`
- Modify: `tests/test_mcp_mine.py`

**Interfaces:**
- Produces: `daemon_writes_enabled() -> bool`
- Produces: `detect_linked_worktree(source: str) -> tuple[bool, str | None]`
- Produces: `mine_dedupe_key(palace_path: str, payload: dict[str, Any]) -> str`
- Produces: `submit_mine(palace_path: str, payload: dict[str, Any]) -> dict[str, Any]`

- [ ] **Step 1: Write failing canonicalization/dedupe/worktree/submit tests**

```python
def test_mine_dedupe_key_is_stable_for_symlinked_source(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    assert mine_dedupe_key("/palace", {"source": str(source), "mode": "projects"}) == mine_dedupe_key("/palace", {"source": str(alias), "mode": "projects"})

def test_detect_linked_worktree_rejects_git_worktrees(tmp_path):
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    subprocess.run(["git", "init", str(primary)], check=True)
    subprocess.run(["git", "-C", str(primary), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(primary), "config", "user.name", "Test"], check=True)
    (primary / "README.md").write_text("fixture\n")
    subprocess.run(["git", "-C", str(primary), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(primary), "commit", "-m", "fixture"], check=True)
    subprocess.run(["git", "-C", str(primary), "worktree", "add", str(linked)], check=True)
    is_linked, canonical = detect_linked_worktree(str(linked))
    assert is_linked is True
    assert canonical == str(primary.resolve())

def test_tool_mine_daemon_mode_returns_accepted_job(monkeypatch, config, tmp_dir):
    monkeypatch.setenv("MEMPALACE_MCP_DAEMON_WRITES", "1")
    monkeypatch.setattr(mcp_jobs, "submit_job", lambda *a, **k: {"id": "job-1", "state": "queued", "created_at": "now"})
    result = tool_mine(source=tmp_dir)
    assert result == {"success": True, "accepted": True, "job_id": "job-1", "state": "queued", "deduplicated": False, "source": os.path.realpath(tmp_dir), "mode": "projects", "wing": None, "submitted_at": "now"}
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `.venv/bin/pytest tests/test_mcp_jobs.py tests/test_mcp_mine.py -q`

Expected: FAIL because `mcp_jobs` and async branch do not exist.

- [ ] **Step 3: Implement focused adapter and feature-flagged branch**

```python
DAEMON_WRITES_ENV = "MEMPALACE_MCP_DAEMON_WRITES"

def daemon_writes_enabled() -> bool:
    return os.environ.get(DAEMON_WRITES_ENV, "").strip().lower() in {"1", "true", "yes", "on"}

def mine_dedupe_key(palace_path: str, payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    normalized["source"] = os.path.realpath(os.path.expanduser(payload["source"]))
    config_path = os.path.join(normalized["source"], "mempalace.yaml")
    normalized["config_sha256"] = _file_sha256(config_path)
    raw = json.dumps({"palace": canonical_palace_path(palace_path), "payload": normalized}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

`submit_mine()` must call existing `daemon.submit_job("mine", payload, wait=False, auto_start=False, dedupe_key=mine_dedupe_key(palace_path, payload))`, compare the returned active job with a newly generated request marker to set `deduplicated`, and translate `DaemonError` to `DaemonUnavailable` without direct fallback.

- [ ] **Step 4: Run MCP mine tests**

Run: `.venv/bin/pytest tests/test_mcp_jobs.py tests/test_mcp_mine.py -q`

Expected: PASS for both legacy and daemon-backed modes.

- [ ] **Step 5: Commit**

```bash
git add mempalace/mcp_jobs.py mempalace/mcp_server.py tests/test_mcp_jobs.py tests/test_mcp_mine.py
git commit -m "feat: submit MCP mining as durable daemon jobs"
```

### Task 4: Job tools and daemon-backed short writes

**Files:**
- Modify: `mempalace/mcp_jobs.py`
- Modify: `mempalace/mcp_server.py:4630-4820`
- Modify: `mempalace/service.py:40-90`
- Test: `tests/test_mcp_jobs.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Produces: `tool_job_status(job_id: str) -> dict[str, Any]`
- Produces: `tool_list_jobs(limit=20, state=None, kind=None) -> dict[str, Any]`
- Produces: `dispatch_daemon_write(tool_name, arguments, fast_wait_ms=250) -> dict[str, Any]`

- [ ] **Step 1: Write failing job-read, redaction, fast-path, and queued-path tests**

```python
def test_daemon_write_returns_accepted_when_not_done(monkeypatch):
    fake = FakeClient(submitted={"id": "j1", "state": "queued"}, after_wait={"id": "j1", "state": "queued"})
    monkeypatch.setattr(mcp_jobs, "get_client_if_running", lambda *a, **k: fake)
    result = dispatch_daemon_write("mempalace_checkpoint", {"items": []}, fast_wait_ms=1)
    assert result["delivery"] == "durable_queue"
    assert result["job_id"] == "j1"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `.venv/bin/pytest tests/test_mcp_jobs.py tests/test_mcp_server.py -k 'job_status or list_jobs or daemon_write' -q`

Expected: FAIL on missing tools/dispatcher.

- [ ] **Step 3: Implement read tools and dispatch routing**

```python
def dispatch_daemon_write(tool_name: str, arguments: dict[str, Any], fast_wait_ms: int = 250) -> dict[str, Any]:
    client = require_running_client()
    job = client.submit("mcp_tool", {"name": tool_name, "arguments": arguments}, dedupe_key=None, priority=0)
    deadline = time.monotonic() + fast_wait_ms / 1000
    while time.monotonic() < deadline:
        current = client.get_job(job["id"])
        if current["state"] in TERMINAL_STATES:
            result = dict(current.get("result") or {})
            result["delivery"] = "completed"
            return result
        time.sleep(0.02)
    return {"success": True, "accepted": True, "job_id": job["id"], "state": current["state"], "delivery": "durable_queue"}
```

At MCP dispatch, keep read-only and integrity gates, skip lifetime peer-writer acquisition in daemon mode, and route write-classified tools other than the job-read tools through `dispatch_daemon_write`. The daemon's direct `run_mcp_tool()` continues to call handlers without entering MCP request dispatch, preventing recursion.

- [ ] **Step 4: Run MCP and daemon suites**

Run: `.venv/bin/pytest tests/test_mcp_jobs.py tests/test_mcp_server.py tests/test_daemon.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mempalace/mcp_jobs.py mempalace/mcp_server.py mempalace/service.py tests/
git commit -m "feat: route MCP mutations through daemon writer"
```

### Task 5: Responsive maintenance search and HTTP lock scope

**Files:**
- Modify: `mempalace/mcp_jobs.py`
- Modify: `mempalace/mcp_server.py:2011-2075`
- Modify: `mempalace/mcp_server.py:5077-5435`
- Test: `tests/test_mcp_http_transport.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Produces: `active_maintenance_job() -> dict[str, Any] | None`
- Consumes: existing `search_memories(query, palace_path=palace_path, vector_disabled=True)`.

- [ ] **Step 1: Write blocked-mine concurrency tests**

```python
def test_job_status_and_search_respond_while_mine_request_is_blocked(http_server, monkeypatch):
    # Block a fake maintenance job, issue concurrent JSON-RPC calls, and assert
    # job status returns promptly and search calls search_memories(vector_disabled=True).
    assert status_elapsed < 0.5
    assert search_payload["index_state"] == "updating"
    assert search_payload["retrieval_mode"] == "bm25_sqlite"
```

- [ ] **Step 2: Run the concurrency test and verify timeout/failure**

Run: `.venv/bin/pytest tests/test_mcp_http_transport.py -k 'while_mine' -q`

Expected: FAIL because the HTTP handler serializes the full request.

- [ ] **Step 3: Move serialization to backend operations and mark fallback results**

Remove `with _HTTP_REQUEST_LOCK: response = handle_request(request)` from HTTP/SSE framing. Introduce `_PALACE_READ_LOCK` only around direct Chroma handler execution. Job reads/submissions bypass it. If `active_maintenance_job()` returns a mine/sync job, call search with `vector_disabled=True` and add:

```python
result.update({
    "index_state": "updating",
    "active_job_id": active["id"],
    "retrieval_mode": "bm25_sqlite",
})
```

After terminal maintenance, call `_force_chroma_cache_reset()` before the next hybrid read and retain existing readiness/error fallback behavior.

- [ ] **Step 4: Run focused and regression suites**

Run: `.venv/bin/pytest tests/test_mcp_http_transport.py tests/test_mcp_server.py tests/test_mcp_mine.py tests/test_daemon.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mempalace/mcp_jobs.py mempalace/mcp_server.py tests/
git commit -m "fix: keep MCP reads responsive during mining"
```

### Task 6: Runtime documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-14-async-mcp-mining-memory-tiers-design.md`
- Test: full project suite

**Interfaces:**
- Documents: enabling/supervising daemon writes, async mine response, polling, failure behavior, and rollback flag.

- [ ] **Step 1: Document exact operator flow**

```bash
mempalace daemon start
export MEMPALACE_MCP_DAEMON_WRITES=1
# call mempalace_mine, then mempalace_job_status(job_id)
```

- [ ] **Step 2: Run formatting and lint**

Run: `.venv/bin/ruff format . && .venv/bin/ruff check .`

Expected: exit 0.

- [ ] **Step 3: Run complete test suite**

Run: `.venv/bin/pytest tests/ -v --ignore=tests/benchmarks`

Expected: PASS with zero failures.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/ mempalace/ tests/
git commit -m "docs: explain daemon-backed MCP mining"
```
