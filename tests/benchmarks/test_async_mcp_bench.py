"""Production latency budgets for async MCP mining and migration search paths."""

from __future__ import annotations

import math
import statistics
import threading
import time
from pathlib import Path

import pytest

from mempalace import daemon, mcp_jobs, service
from mempalace.backends import ChromaBackend
from mempalace.migration_bundle import rebuild_drawers_vector_index
from mempalace.reorganize import MigrationAction, exact_hash, inventory_palace
from mempalace.repair import _close_chroma_handles
from mempalace.searcher import search_memories
from tests.benchmarks.data_generator import PalaceDataGenerator
from tests.benchmarks.report import record_metric


MINE_SUBMISSION_P95_BUDGET_MS = 500.0
JOB_READ_P95_BUDGET_MS = 100.0
MAINTENANCE_BM25_P95_BUDGET_MS = 1_000.0
POST_MAINTENANCE_REGRESSION_BUDGET = 1.10


def _percentile(values: list[float], percentile: float) -> float:
    """Return a nearest-rank percentile without a NumPy dependency."""
    if not values:
        raise ValueError("at least one latency is required")
    ordered = sorted(values)
    rank = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[rank]


def _record_latencies(category: str, values: list[float]) -> tuple[float, float]:
    p50_ms = _percentile(values, 0.50)
    p95_ms = _percentile(values, 0.95)
    record_metric(category, "p50_ms", round(p50_ms, 3))
    record_metric(category, "p95_ms", round(p95_ms, 3))
    return p50_ms, p95_ms


def _capture_http_server(monkeypatch) -> list:
    """Capture the actual daemon HTTP server so teardown cannot leak a socket."""
    servers: list = []
    base = daemon.ThreadingHTTPServer

    class CapturingServer(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(daemon, "ThreadingHTTPServer", CapturingServer)
    return servers


def _start_daemon(
    tmp_path: Path,
    monkeypatch,
    *,
    execute_fn=None,
    palace: Path | None = None,
):
    """Start the real loopback daemon with a controlled worker."""
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "daemon-state"))
    palace = palace or (tmp_path / "daemon-palace")
    palace.mkdir(exist_ok=True)
    monkeypatch.setattr(
        service,
        "execute_job",
        execute_fn
        or (lambda _kind, _payload, *_args, **_kwargs: {"success": True, "exit_code": 0}),
    )
    servers = _capture_http_server(monkeypatch)
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            daemon.run_server(str(palace), port=0)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            errors.append(exc)

    thread = threading.Thread(target=serve, name="benchmark-daemon", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    client = None
    while time.monotonic() < deadline:
        if errors:
            raise AssertionError(f"benchmark daemon failed to start: {errors[0]!r}")
        client = daemon.get_client_if_running(str(palace), health_timeout=0.2)
        if client is not None:
            return client, thread, palace, servers
        time.sleep(0.02)
    raise AssertionError("benchmark daemon did not become ready within 15 seconds")


def _stop_daemon(client, thread: threading.Thread, servers: list) -> None:
    try:
        client.shutdown()
    except Exception:  # noqa: BLE001 - forced shutdown below is the fallback
        pass
    thread.join(timeout=5.0)
    if thread.is_alive() and servers:
        servers[-1].shutdown()
        servers[-1].server_close()
        thread.join(timeout=5.0)
    assert not thread.is_alive(), "benchmark daemon thread leaked"


def _measure_search_rounds(
    palace_path: str,
    queries: list[str],
    *,
    vector_disabled: bool,
    rounds: int = 3,
) -> tuple[list[float], float]:
    all_latencies: list[float] = []
    round_p95: list[float] = []
    for _ in range(rounds):
        current: list[float] = []
        for query in queries:
            started = time.perf_counter()
            result = search_memories(
                query,
                palace_path=palace_path,
                n_results=5,
                vector_disabled=vector_disabled,
            )
            current.append((time.perf_counter() - started) * 1_000)
            assert "error" not in result
        all_latencies.extend(current)
        round_p95.append(_percentile(current, 0.95))
    return all_latencies, statistics.median(round_p95)


@pytest.mark.benchmark
def test_async_mcp_submission_and_job_reads_meet_latency_budgets(tmp_path, monkeypatch):
    """Exercise validation, loopback HTTP, durable SQLite enqueue, and MCP job reads."""
    job_started = threading.Event()
    release_job = threading.Event()

    def execute_job(_kind, payload, *_args, **_kwargs):
        if payload.get("wing") == "blocked-maintenance":
            job_started.set()
            assert release_job.wait(timeout=30.0), "benchmark blocked job was never released"
        return {"success": True, "exit_code": 0}

    client, thread, palace, servers = _start_daemon(
        tmp_path,
        monkeypatch,
        execute_fn=execute_job,
    )
    try:
        sources = tmp_path / "sources"
        sources.mkdir()
        for index in range(41):
            (sources / f"project-{index:03d}").mkdir()

        # Warm token/endpoint/HTTP connection paths before measuring steady-state p95.
        warm = mcp_jobs.submit_mine(
            str(palace),
            {"source": str(sources / "project-000"), "mode": "projects", "wing": "warm"},
        )
        assert warm.get("accepted") is True
        deadline = time.monotonic() + 5.0
        while client.get_job(warm["job_id"])["state"] not in {"succeeded", "failed"}:
            assert time.monotonic() < deadline, "warm benchmark job did not finish"
            time.sleep(0.01)

        blocked = mcp_jobs.submit_mine(
            str(palace),
            {
                "source": str(sources / "project-000"),
                "mode": "projects",
                "wing": "blocked-maintenance",
            },
        )
        assert blocked.get("accepted") is True
        assert job_started.wait(timeout=5.0), "blocked maintenance job did not start"
        assert client.get_job(blocked["job_id"])["state"] == "running"

        submission_latencies: list[float] = []
        job_ids: list[str] = []
        for index in range(1, 41):
            started = time.perf_counter()
            result = mcp_jobs.submit_mine(
                str(palace),
                {
                    "source": str(sources / f"project-{index:03d}"),
                    "mode": "projects",
                    "wing": f"bench-{index:03d}",
                },
            )
            submission_latencies.append((time.perf_counter() - started) * 1_000)
            assert result.get("accepted") is True
            job_ids.append(result["job_id"])
            # A successful response means the row is already durable and readable.
            assert client.get_job(result["job_id"])["id"] == result["job_id"]

        _, submission_p95 = _record_latencies("async_mcp_mine_submission", submission_latencies)
        assert submission_p95 < MINE_SUBMISSION_P95_BUDGET_MS

        # Warm the health + read paths independently from submission.
        assert mcp_jobs.tool_job_status(job_ids[-1], palace_path=str(palace))["success"] is True
        assert mcp_jobs.tool_list_jobs(limit=20, palace_path=str(palace))["success"] is True

        status_latencies: list[float] = []
        list_latencies: list[float] = []
        for index in range(40):
            started = time.perf_counter()
            status = mcp_jobs.tool_job_status(
                job_ids[index % len(job_ids)], palace_path=str(palace)
            )
            status_latencies.append((time.perf_counter() - started) * 1_000)
            assert status["success"] is True

            started = time.perf_counter()
            page = mcp_jobs.tool_list_jobs(limit=20, palace_path=str(palace))
            list_latencies.append((time.perf_counter() - started) * 1_000)
            assert page["success"] is True

        _, status_p95 = _record_latencies("async_mcp_job_status", status_latencies)
        _, list_p95 = _record_latencies("async_mcp_job_list", list_latencies)
        assert status_p95 < JOB_READ_P95_BUDGET_MS
        assert list_p95 < JOB_READ_P95_BUDGET_MS
        assert client.get_job(blocked["job_id"])["state"] == "running"
    finally:
        release_job.set()
        _stop_daemon(client, thread, servers)


@pytest.mark.benchmark
def test_maintenance_search_and_post_rebuild_hybrid_meet_budgets(tmp_path, monkeypatch):
    """Bound BM25 fallback latency and hybrid regression after vector maintenance."""
    palace_path = str(tmp_path / "search-palace")
    generator = PalaceDataGenerator(seed=42, scale="small")
    generator.populate_palace_directly(
        palace_path,
        n_drawers=1_000,
        include_needles=False,
    )
    _close_chroma_handles(palace_path)

    queries = [
        "authentication middleware",
        "database migration",
        "deployment pipeline",
        "error handling",
        "caching strategy",
        "message queue",
        "monitoring health check",
        "query optimization",
        "token refresh",
        "batch processing",
        "rate limiting",
        "connection pooling",
    ]
    workload = queries * 4

    # Warm both routes, then use the median of three round-level p95 values to
    # avoid treating one scheduler pause as a search regression.
    for query in queries:
        search_memories(query, palace_path=palace_path, n_results=5)
        search_memories(query, palace_path=palace_path, n_results=5, vector_disabled=True)

    job_started = threading.Event()
    release_job = threading.Event()

    def execute_blocked_mine(_kind, _payload, *_args, **_kwargs):
        job_started.set()
        assert release_job.wait(timeout=30.0), "benchmark blocked job was never released"
        return {"success": True, "exit_code": 0}

    client, thread, _palace, servers = _start_daemon(
        tmp_path / "maintenance-daemon",
        monkeypatch,
        execute_fn=execute_blocked_mine,
        palace=Path(palace_path),
    )
    source = tmp_path / "maintenance-source"
    source.mkdir()
    blocked = mcp_jobs.submit_mine(
        palace_path,
        {"source": str(source), "mode": "projects", "wing": "blocked-maintenance"},
    )
    assert blocked.get("accepted") is True
    assert job_started.wait(timeout=5.0), "blocked maintenance job did not start"
    try:
        bm25_latencies, bm25_p95 = _measure_search_rounds(
            palace_path,
            workload,
            vector_disabled=True,
        )
        assert client.get_job(blocked["job_id"])["state"] == "running"
    finally:
        release_job.set()
        _stop_daemon(client, thread, servers)
    _record_latencies("maintenance_bm25_search", bm25_latencies)
    record_metric("maintenance_bm25_search", "median_round_p95_ms", round(bm25_p95, 3))
    assert bm25_p95 < MAINTENANCE_BM25_P95_BUDGET_MS

    baseline_latencies, baseline_p95 = _measure_search_rounds(
        palace_path,
        workload,
        vector_disabled=False,
    )
    _record_latencies("pre_maintenance_hybrid_search", baseline_latencies)
    record_metric(
        "pre_maintenance_hybrid_search",
        "median_round_p95_ms",
        round(baseline_p95, 3),
    )

    _close_chroma_handles(palace_path)
    inventory = inventory_palace(
        palace_path,
        canonical_root=palace_path,
        worktree_roots=(),
        session_roots=(),
    )
    actions = [
        MigrationAction(
            drawer_id=record.drawer_id,
            action="retain_benchmark",
            destination_wing=str(record.metadata.get("wing") or "benchmark"),
            reason="benchmark vector maintenance",
            content_sha256=exact_hash(record.content),
            metadata=dict(record.metadata),
        )
        for record in inventory
    ]
    backend = ChromaBackend()
    maintenance_started = time.perf_counter()
    rebuilt, reembedded = rebuild_drawers_vector_index(
        backend=backend,
        palace_path=palace_path,
        inventory=inventory,
        actions=actions,
        evidence=(),
        batch_size=250,
    )
    maintenance_ms = (time.perf_counter() - maintenance_started) * 1_000
    assert rebuilt.count() == 1_000
    assert reembedded == 0
    record_metric("vector_maintenance", "elapsed_ms", round(maintenance_ms, 3))
    record_metric("vector_maintenance", "vectors_reused", 1_000)
    record_metric("vector_maintenance", "vectors_reembedded", reembedded)
    backend.close_palace(palace_path)
    del rebuilt
    _close_chroma_handles(palace_path)

    for query in queries:
        search_memories(query, palace_path=palace_path, n_results=5)
    post_latencies, post_p95 = _measure_search_rounds(
        palace_path,
        workload,
        vector_disabled=False,
    )
    _record_latencies("post_maintenance_hybrid_search", post_latencies)
    record_metric(
        "post_maintenance_hybrid_search",
        "median_round_p95_ms",
        round(post_p95, 3),
    )
    ratio = post_p95 / max(baseline_p95, 0.001)
    record_metric("post_maintenance_hybrid_search", "p95_ratio", round(ratio, 4))
    assert ratio <= POST_MAINTENANCE_REGRESSION_BUDGET
