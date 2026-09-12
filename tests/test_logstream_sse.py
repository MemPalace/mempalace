"""
Tests for RFC 003 phase 5: the /logstream/stream SSE endpoint and the
lock-free HTTP dispatch of logstream tools.

Follows test_mcp_http_transport.py's harness: the production server from
_build_http_server on an ephemeral loopback port, pure stdlib clients.
"""

import http.client
import json
import threading
import time

import pytest

from mempalace import mcp_server as mcp


@pytest.fixture
def patched_palace(monkeypatch, config, palace_path):
    """Point the server's logstream at a temp palace with a fresh cache."""
    monkeypatch.setattr(mcp, "_config", config)
    monkeypatch.setattr(mcp, "_logstream_by_path", {})
    yield palace_path
    for ls in mcp._logstream_by_path.values():
        ls.close()


@pytest.fixture
def http_server(patched_palace):
    httpd = mcp._build_http_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield port, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _append(body="hello", correlation_id="task_sse", type="task.request", topic=None):
    result = mcp.tool_event_append(
        type=type,
        stream="project/mempalace",
        room="delegation",
        topic=topic,
        from_agent="mac-claude",
        to_agent="windows-codex",
        correlation_id=correlation_id,
        body=body,
    )
    assert result.get("success"), result
    return result["event"]


def _open_stream(port, query="", headers=None, timeout=8):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", f"/logstream/stream{query}", headers=headers or {})
    resp = conn.getresponse()
    return conn, resp


def _read_frames(resp, count, deadline_s=8):
    """Read SSE frames until `count` data frames arrive or the deadline hits."""
    frames = []
    current = {}
    deadline = time.monotonic() + deadline_s
    while len(frames) < count and time.monotonic() < deadline:
        line = resp.readline().decode("utf-8").rstrip("\n")
        if line.startswith("id: "):
            current["id"] = line[4:]
        elif line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[6:])
        elif line == "" and current.get("data") is not None:
            frames.append(current)
            current = {}
    return frames


class TestSSEStream:
    def test_live_tail_delivers_only_post_connect_events(self, http_server):
        port, _ = http_server
        pre = _append(body="before connect")

        conn, resp = _open_stream(port)
        assert resp.status == 200
        assert resp.getheader("Content-Type").startswith("text/event-stream")
        try:
            first = _append(body="after connect 1")
            second = _append(body="after connect 2")
            frames = _read_frames(resp, 2)
            assert [f["id"] for f in frames] == [first["id"], second["id"]]
            assert all(f["event"] == "logstream" for f in frames)
            assert frames[0]["data"]["body"] == "after connect 1"
            assert frames[0]["data"]["seq"] == first["seq"]
            assert pre["id"] not in {f["id"] for f in frames}
        finally:
            conn.close()

    def test_cursor_replays_events_after_it(self, http_server):
        port, _ = http_server
        first = _append(body="one")
        second = _append(body="two")

        conn, resp = _open_stream(port, query=f"?since_event_id={first['id']}")
        try:
            frames = _read_frames(resp, 1)
            assert frames[0]["id"] == second["id"]
            assert frames[0]["data"]["body"] == "two"
        finally:
            conn.close()

    def test_last_event_id_header_acts_as_cursor(self, http_server):
        port, _ = http_server
        first = _append(body="one")
        second = _append(body="two")

        conn, resp = _open_stream(port, headers={"Last-Event-ID": first["id"]})
        try:
            frames = _read_frames(resp, 1)
            assert frames[0]["id"] == second["id"]
        finally:
            conn.close()

    def test_filters_scope_the_stream(self, http_server):
        port, _ = http_server
        conn, resp = _open_stream(port, query="?correlation_id=task_wanted")
        try:
            _append(body="noise", correlation_id="task_noise")
            wanted = _append(body="signal", correlation_id="task_wanted")
            frames = _read_frames(resp, 1)
            assert [f["id"] for f in frames] == [wanted["id"]]
        finally:
            conn.close()

    def test_topic_filter_scopes_the_stream(self, http_server):
        port, _ = http_server
        conn, resp = _open_stream(port, query="?topic=topic_wanted")
        try:
            _append(body="noise", topic="topic_noise")
            wanted = _append(body="signal", topic="topic_wanted")
            frames = _read_frames(resp, 1)
            assert [f["id"] for f in frames] == [wanted["id"]]
            assert frames[0]["data"]["topic"] == "topic_wanted"
        finally:
            conn.close()

    def test_invalid_filter_returns_400(self, http_server):
        port, _ = http_server
        conn, resp = _open_stream(port, query="?type=Not%20A%20Type!")
        try:
            assert resp.status == 400
            assert "type" in json.loads(resp.read())["error"]
        finally:
            conn.close()

    def test_unknown_cursor_returns_400(self, http_server):
        port, _ = http_server
        conn, resp = _open_stream(port, query="?since_event_id=evt_nope")
        try:
            assert resp.status == 400
            assert "not found" in json.loads(resp.read())["error"]
        finally:
            conn.close()

    def test_client_cap_returns_503(self, patched_palace, monkeypatch):
        monkeypatch.setenv(mcp._SSE_MAX_CLIENTS_ENV, "0")
        httpd = mcp._build_http_server("127.0.0.1", 0)
        port = httpd.server_address[1]
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            conn, resp = _open_stream(port)
            try:
                assert resp.status == 503
                assert resp.getheader("Retry-After") == "5"
            finally:
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_stream_requires_token_when_configured(self, patched_palace, monkeypatch):
        monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "s3cret")
        httpd = mcp._build_http_server("127.0.0.1", 0)
        port = httpd.server_address[1]
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            conn, resp = _open_stream(port)
            try:
                assert resp.status == 401
            finally:
                conn.close()
            _append(body="post-connect")  # ensure the stream has data to emit
            conn, resp = _open_stream(
                port,
                query="?stream=project/mempalace",
                headers={"Authorization": "Bearer s3cret"},
            )
            try:
                assert resp.status == 200
            finally:
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


class TestLockFreeDispatch:
    def _post(self, port, body):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(
                "POST",
                "/mcp",
                json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def _call(self, port, name, arguments, req_id=1):
        status, payload = self._post(
            port,
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert status == 200, payload
        return json.loads(payload["result"]["content"][0]["text"])

    def test_event_wait_does_not_block_concurrent_append(self, http_server):
        """Regression for the hub-starvation hazard: with logstream tools
        behind _HTTP_REQUEST_LOCK, a waiting event_wait holds the lock, the
        append queues behind it, and the wait can only ever time out."""
        port, _ = http_server
        results = {}

        def waiter():
            results["wait"] = self._call(
                port,
                "mempalace_event_wait",
                {"correlation_id": "task_lockfree", "timeout_ms": 8000},
                req_id=2,
            )

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.4)  # let the wait begin polling
        appended = self._call(
            port,
            "mempalace_event_append",
            {
                "type": "task.request",
                "stream": "project/mempalace",
                "room": "delegation",
                "from_agent": "mac-claude",
                "correlation_id": "task_lockfree",
                "body": "unblock the waiter",
            },
        )
        assert appended["success"] is True
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert results["wait"]["timed_out"] is False
        assert results["wait"]["events"][0]["id"] == appended["event"]["id"]
