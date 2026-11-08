"""Regression witnesses for observation isolation and lazy activation."""

import io
import json
from types import SimpleNamespace

from tests.test_worker_observation import serving as _serving

serving = _serving


def test_rss_exception_preserves_request_and_active_count(serving, monkeypatch):
    server, invoke, _, instances = serving
    invoke()
    observer = instances[0]
    before = observer.lost

    def broken():
        raise RuntimeError("synthetic observation failure")

    monkeypatch.setattr(observer, "rss", broken)
    try:
        response = invoke()
    except Exception:
        response = None
    assert response is not None, "observation sampling must not fail the request"
    assert "result" in response
    assert observer.active == 0
    assert observer.lost == before + 1
    # Slot and nested-token cleanup must also permit the next normal call.
    monkeypatch.setattr(observer, "rss", lambda: ((1, 1, 0), "linux_proc", "ok"))
    invoke()
    assert observer.active == 0
    assert server._worker_observer is observer


def test_end_rss_status_and_source_describe_same_read(serving, monkeypatch):
    _, invoke, records, instances = serving
    invoke()
    samples = iter(
        [
            ((None, None, None), "unavailable", "error"),
            ((1024, 1024, 0), "linux_proc", "ok"),
        ]
    )
    monkeypatch.setattr(instances[0], "rss", lambda: next(samples))
    invoke()
    end = records()[-1]
    assert (end["rss_source"], end["rss_status"]) == ("linux_proc", "ok")
    assert end["rss_before_bytes"] is None
    assert end["rss_after_bytes"] == 1024


def test_forwarding_stdio_startup_does_not_activate(serving, monkeypatch):
    server, _, records, instances = serving
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": {}},
    }
    forwarded = []

    def post(value):
        forwarded.append(value)
        return {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}

    monkeypatch.setattr(server, "_hub_proxy_target", lambda: SimpleNamespace(post=post))
    monkeypatch.setattr(server, "_restore_stdout", lambda: None)
    monkeypatch.setattr(server.sys, "stdin", io.StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr(server.sys, "stdout", io.StringIO())
    for name in (
        "_startup_preflight",
        "_maybe_eager_warmup_embedder",
        "_start_idle_exit_watchdog",
        "_start_write_stall_watchdog",
    ):
        monkeypatch.setattr(server, name, lambda: None)
    monkeypatch.setattr(
        server.threading, "Thread", lambda target, **kwargs: SimpleNamespace(start=target)
    )
    server._run_stdio_loop()
    assert forwarded == [request]
    assert instances == [], "forward-only stdio must not construct an observer"
    assert records() == []


def test_constructor_failure_is_latched_and_counted(serving, monkeypatch):
    server, invoke, records, _ = serving
    attempts = []
    monkeypatch.setattr(server, "_worker_observer_failed", False, raising=False)
    monkeypatch.setattr(server, "_worker_observation_lost", 0, raising=False)

    def broken(directory):
        attempts.append(True)
        raise RuntimeError("synthetic constructor failure")

    monkeypatch.setattr(server, "_worker_observer_factory", broken)
    assert "result" in invoke()
    assert "result" in invoke()
    assert attempts == [True], "a failed constructor must not be retried per call"
    assert server._worker_observation_lost == 1
    assert records() == []
