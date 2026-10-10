"""mempalace_reconnect must not run inside a hub mine.

A hub mine hands the request lock to queued requests between files
(``_RWLock.yield_write``) but keeps the collection handles it opened when it
started. ``mempalace_reconnect`` closes every backend handle and clears the
Chroma system cache, so letting it into that handoff left the mine purging
through a closed handle for every remaining file. Each of those files was
counted as "skipped" and the mine still answered ``success: true``.

These tests run the production HTTP server, the real ``mempalace_mine`` and
``mempalace_reconnect`` handlers, the real miner and a real Chroma palace. The
only patch holds the mine at one file boundary so the reconnect is queued
while the mine is in progress, which is what a user's reconnect a few seconds
into a mine does.
"""

import http.client
import json
import threading
import time

import pytest

from mempalace import mcp_server as mcp

N_FILES = 6


@pytest.fixture
def http_server():
    httpd = mcp._build_http_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _call(port, name, arguments, req_id, timeout=120):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(
            "POST",
            "/mcp",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        payload = json.loads(resp.read())
    finally:
        conn.close()
    assert resp.status == 200, payload
    return json.loads(payload["result"]["content"][0]["text"])


def _project(tmp_path):
    src = tmp_path / "project"
    src.mkdir()
    for i in range(N_FILES):
        body = " ".join(f"word{i}_{j} palace drawer reconnect" for j in range(80))
        (src / f"note_{i}.md").write_text(f"# Note {i}\n\n{body}\n", encoding="utf-8")
    return src


def _filed_sources(palace_path):
    from mempalace.palace import get_collection

    col = get_collection(palace_path, create=False)
    metas = col.get(include=["metadatas"])["metadatas"]
    return {m["source_file"] for m in metas}


def _hold_mine_at_second_file(monkeypatch):
    """Pause the real miner at its second file; everything else stays real."""
    from mempalace import miner

    real_process_file = miner.process_file
    calls = {"n": 0}
    held = threading.Event()
    release = threading.Event()

    def process_file(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            held.set()
            release.wait(timeout=5)
        return real_process_file(**kwargs)

    monkeypatch.setattr(miner, "process_file", process_file)
    return held, release


def _wait_until_reconnect_is_queued(timeout=1.0):
    # Before the fix the reconnect queues on the request lock as a writer, so
    # the next yield_write hands it the lock. After it, it waits on the mine
    # lock and never shows up here; give it the same time either way.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if mcp._HTTP_REQUEST_LOCK._waiting_writers:
            return
        time.sleep(0.01)


def test_reconnect_queued_during_a_hub_mine_waits_and_the_mine_files_everything(
    http_server, monkeypatch, config, tmp_path, capfd
):
    port = http_server
    monkeypatch.setattr(mcp, "_config", config)
    monkeypatch.setattr(mcp, "_HTTP_RECONNECT_MINE_WAIT_S", 60.0, raising=False)
    src = _project(tmp_path)
    held, release = _hold_mine_at_second_file(monkeypatch)
    results = {}
    # Server-side: when the mine handler returned, when reconnect's started.
    stamps = {}
    real_mine = mcp.TOOLS["mempalace_mine"]["handler"]
    real_reconnect = mcp.TOOLS["mempalace_reconnect"]["handler"]

    def mine_handler(**kwargs):
        try:
            return real_mine(**kwargs)
        finally:
            stamps["mine_done"] = time.monotonic()

    def reconnect_handler(**kwargs):
        stamps["reconnect_start"] = time.monotonic()
        return real_reconnect(**kwargs)

    monkeypatch.setitem(mcp.TOOLS["mempalace_mine"], "handler", mine_handler)
    monkeypatch.setitem(mcp.TOOLS["mempalace_reconnect"], "handler", reconnect_handler)

    def run(key, name, arguments, req_id):
        results[key] = _call(port, name, arguments, req_id)

    mine = threading.Thread(
        target=run,
        args=("mine", "mempalace_mine", {"source": str(src), "mode": "projects", "wing": "w"}, 1),
    )
    mine.start()
    assert held.wait(timeout=30), "mine never reached its second file"
    reconnect = threading.Thread(target=run, args=("reconnect", "mempalace_reconnect", {}, 2))
    reconnect.start()
    _wait_until_reconnect_is_queued()
    release.set()
    mine.join(timeout=120)
    reconnect.join(timeout=120)
    assert not mine.is_alive() and not reconnect.is_alive()
    stderr = capfd.readouterr().err

    assert "purge failed" not in stderr, stderr
    assert "purge failed" not in results["mine"].get("output", "")
    assert results["mine"]["success"] is True, results["mine"]
    assert results["reconnect"]["success"] is True, results["reconnect"]
    assert stamps["reconnect_start"] >= stamps["mine_done"], "reconnect ran inside the mine"
    expected = {str(p) for p in src.iterdir()}
    assert _filed_sources(config.palace_path) == expected


def test_reconnect_refuses_clearly_when_a_mine_outlasts_its_wait(
    http_server, monkeypatch, config, tmp_path, capfd
):
    port = http_server
    monkeypatch.setattr(mcp, "_config", config)
    monkeypatch.setattr(mcp, "_HTTP_RECONNECT_MINE_WAIT_S", 0.2, raising=False)
    src = _project(tmp_path)
    held, release = _hold_mine_at_second_file(monkeypatch)
    results = {}

    def run_mine():
        results["mine"] = _call(
            port, "mempalace_mine", {"source": str(src), "mode": "projects", "wing": "w"}, 1
        )

    mine = threading.Thread(target=run_mine)
    mine.start()
    assert held.wait(timeout=30), "mine never reached its second file"
    reconnect_box = {}
    reconnect = threading.Thread(
        target=lambda: reconnect_box.update(r=_call(port, "mempalace_reconnect", {}, 2, timeout=10))
    )
    reconnect.start()
    reconnect.join(timeout=3)
    release.set()
    mine.join(timeout=120)
    reconnect.join(timeout=30)
    stderr = capfd.readouterr().err

    refused = reconnect_box.get("r")
    assert refused is not None, "reconnect neither ran nor refused while the mine held"
    assert refused["success"] is False, refused
    assert refused["error_class"] == "MineInProgress"
    assert "mine is running" in refused["error"]
    assert "purge failed" not in stderr, stderr
    assert results["mine"]["success"] is True, results["mine"]
    assert _filed_sources(config.palace_path) == {str(p) for p in src.iterdir()}


def test_a_search_cache_reset_between_files_does_not_strand_the_mine(
    http_server, monkeypatch, config, tmp_path, capfd
):
    """Readers run in the same handoff, and one of them closes the backend too.

    ``mempalace_search`` answers a transient index error (#1315) by resetting
    the Chroma caches, which closes the shared backend the mine writes
    through. Holding reconnect back does not cover that, so the miner reopens
    its collections whenever the handoff let anything run.
    """
    port = http_server
    monkeypatch.setattr(mcp, "_config", config)
    src = _project(tmp_path)
    held, release = _hold_mine_at_second_file(monkeypatch)
    calls = {"n": 0}

    def search_memories(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"error": "Internal error: Error finding id"}
        return {"query": "x", "results": []}

    monkeypatch.setattr(mcp, "search_memories", search_memories)
    monkeypatch.setattr(mcp.time, "sleep", lambda _s: None)
    results = {}
    mine = threading.Thread(
        target=lambda: results.update(
            mine=_call(
                port, "mempalace_mine", {"source": str(src), "mode": "projects", "wing": "w"}, 1
            )
        )
    )
    mine.start()
    assert held.wait(timeout=30), "mine never reached its second file"
    search = threading.Thread(
        target=lambda: results.update(search=_call(port, "mempalace_search", {"query": "x"}, 2))
    )
    search.start()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not mcp._HTTP_REQUEST_LOCK._waiting_readers:
        time.sleep(0.01)
    release.set()
    mine.join(timeout=120)
    search.join(timeout=120)
    stderr = capfd.readouterr().err

    assert calls["n"] == 2, "the search never took its cache-reset retry"
    assert "purge failed" not in stderr, stderr
    assert results["mine"]["success"] is True, results["mine"]
    assert _filed_sources(config.palace_path) == {str(p) for p in src.iterdir()}
