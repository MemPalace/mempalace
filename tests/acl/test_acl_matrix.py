"""Per-tool identity matrix (M13, M14), trusted-doors docs (S12), INTERNAL scope (S22)."""

import io
import json
import threading
import types
from pathlib import Path

import pytest
from _acl_golden import post, run_flow
from _acl_helpers import LEGACY, key

from mempalace import acl, mcp_server as mcp
from mempalace.tool_capabilities import TOOL_CAPABILITIES

pytestmark = pytest.mark.acl_integration
KEYS = [
    key("g1", "tok-g1", wings=["alpha"]),
    key("gone", "tok-gone", wings=["alpha"], revoked=True),
]


def call_body(tool, rid=1):
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {"bogus": 1}},
    }


@pytest.mark.parametrize(
    "token", ["wrong-token", "tok-gone", None], ids=["unknown", "revoked", "missing"]
)
@pytest.mark.parametrize("tool", sorted(TOOL_CAPABILITIES))
def test_m13_every_tool_bad_identity_is_401_with_one_body(serve_with, monkeypatch, tool, token):
    reached = []
    for name, entry in mcp.TOOLS.items():  # spy on the real handlers, leave them in place
        real = entry["handler"]
        monkeypatch.setitem(
            entry, "handler", lambda *a, _r=real, _n=name, **k: reached.append(_n) or _r(*a, **k)
        )
    port, _ = serve_with(KEYS)
    status, body = post(port, call_body(tool), token=token)
    assert status == 401
    reference = post(port, call_body("mempalace_no_such_tool"), token="wrong-token")
    assert (status, body) == reference
    assert "gone" not in body and "g1" not in body and tool not in body
    assert reached == []  # I2: an unknown or revoked token never reaches a handler


@pytest.mark.parametrize("auth", [None, "Bearer ", "Bearer    ", "Bearer\t", "Basic x"])
def test_empty_credentials_are_401_on_every_route(serve_with, auth):
    import http.client

    port, _ = serve_with(KEYS)
    for method, path in (
        ("POST", "/mcp"),
        ("POST", "/mcp/light"),
        ("GET", "/mcp/light"),
        ("GET", "/statusz"),
        ("GET", "/sync/version_vector"),
        ("GET", "/logstream/stream"),
    ):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if auth is not None:
            headers["Authorization"] = auth
        body = json.dumps(call_body("mempalace_get_aaak_spec")) if method == "POST" else None
        conn.request(method, path, body=body, headers=headers)
        assert conn.getresponse().status == 401, (method, path, auth)
        conn.close()


def _off_server(monkeypatch, config, kg):
    from _mcp_server_helpers import _patch_mcp_server

    _patch_mcp_server(monkeypatch, config, kg)
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", LEGACY)
    monkeypatch.delenv("MEMPALACE_MCP_GRANTS_FILE", raising=False)
    httpd = mcp._build_http_server("127.0.0.1", 0)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    return httpd


def test_m14_legacy_admin_with_acl_on_equals_acl_off(monkeypatch, config, kg, serve_with):
    tools = sorted(TOOL_CAPABILITIES)
    off = _off_server(monkeypatch, config, kg)
    try:
        want = {t: post(off.server_address[1], call_body(t), token=LEGACY) for t in tools}
    finally:
        off.shutdown()
        off.server_close()
    port, _ = serve_with(KEYS)
    assert acl.current_scope() is acl.DENIED  # the flag really is on
    for t in tools:
        assert post(port, call_body(t), token=LEGACY) == want[t], t


def test_m14_flow_with_legacy_token_and_acl_on_matches_golden(serve_with):
    port, _ = serve_with(KEYS)
    want = json.loads((Path(__file__).parent / "golden_flow.json").read_text())
    assert run_flow(port, token=LEGACY) == want


def test_s12_docs_name_the_trusted_doors():
    text = (Path(__file__).parents[2] / "docs" / "acl.md").read_text(encoding="utf-8")
    assert (
        "stdio, the light server, the daemon and hub clients are owner-trusted and run as admin"
        in " ".join(text.split())
    )
    assert "not encryption" in text.lower()


def _run_target(target, stop=()):
    """Run a captured Thread target on a real thread; only ``stop`` may end it early."""
    errors = []

    def run():
        try:
            target()
        except stop:
            pass
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive()
    assert errors == []


def _fake_threading(captured):
    def fake(target, **kw):
        return types.SimpleNamespace(start=lambda: captured.append(target), is_alive=lambda: False)

    return types.SimpleNamespace(**{**vars(threading), "Thread": fake})


def test_s22_real_start_sites_run_their_threads_as_internal(monkeypatch):
    """Drive the real start sites; their Thread targets must carry the INTERNAL scope."""
    acl.enable()
    scopes = {}
    captured = []
    monkeypatch.setattr(mcp, "threading", _fake_threading(captured))

    def record(label):
        def fn(*a, **k):
            scopes[label] = acl.current_scope().kind

        return fn

    # stdio start: the startup preflight thread
    monkeypatch.setattr(mcp, "_restore_stdout", lambda: None)
    monkeypatch.setattr(mcp.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(mcp, "_maybe_eager_warmup_embedder", lambda: None)
    monkeypatch.setattr(mcp, "_start_idle_exit_watchdog", lambda: None)
    monkeypatch.setattr(mcp, "_start_write_stall_watchdog", lambda: None)
    monkeypatch.setattr(mcp, "_ensure_sqlite_integrity_status", record("preflight"))
    monkeypatch.setattr(mcp, "_refresh_vector_disabled_flag", lambda: None)
    mcp._run_stdio_loop()
    # http start: the eager warmup thread
    monkeypatch.setattr(mcp, "_READ_ONLY", True)
    monkeypatch.setattr(mcp, "_serve_http", lambda host, port: None)
    monkeypatch.setattr(mcp, "_maybe_eager_warmup_embedder", record("warmup"))
    monkeypatch.setenv("MEMPALACE_EAGER_WARMUP", "1")
    mcp._run_http_loop()
    assert len(captured) == 2
    for target in captured:
        _run_target(target)
    assert scopes == {"preflight": "internal", "warmup": "internal"}


def test_s22_watchdog_threads_and_bare_request_thread(monkeypatch):
    acl.enable()

    class Stop(Exception):
        pass

    seen = []

    def sleep(_):
        seen.append(acl.current_scope().kind)
        raise Stop

    captured = []
    monkeypatch.setenv("MEMPALACE_MCP_IDLE_HOURS", "1")
    monkeypatch.setenv("MEMPALACE_MCP_WRITE_STALL_WARN_SECS", "60")
    monkeypatch.setattr(mcp, "threading", _fake_threading(captured))
    mcp._start_idle_exit_watchdog()
    mcp._start_write_stall_watchdog()
    monkeypatch.setattr(mcp, "time", types.SimpleNamespace(sleep=sleep, monotonic=lambda: 0.0))
    assert len(captured) == 2
    for target in captured:
        _run_target(target, (Stop,))
    assert seen == ["internal", "internal"]

    # a request thread that never set a scope is denied
    out = {}

    def request_thread():
        out["kind"] = acl.current_scope().kind
        out["reply"] = mcp.handle_request(call_body("mempalace_get_aaak_spec"))

    t = threading.Thread(target=request_thread)
    t.start()
    t.join()
    assert out["kind"] == "denied"
    assert out["reply"]["error"]["code"] == -32601


def test_s22_peer_sync_thread_runs_as_internal(monkeypatch, config, kg):
    from _mcp_server_helpers import _patch_mcp_server

    _patch_mcp_server(monkeypatch, config, kg)
    acl.enable()
    seen = []
    captured = []

    class OneRound:
        def wait(self, _interval):
            seen.append(acl.current_scope().kind)
            return True  # stop after the first look

        def is_set(self):
            return True

        def set(self):
            pass

    monkeypatch.setenv("MEMPALACE_SYNC_INTERVAL", "1")
    monkeypatch.setattr(mcp, "threading", _fake_threading(captured))
    mcp._start_peer_sync_thread(stop_event=OneRound())
    assert len(captured) == 1
    _run_target(captured[0])
    assert seen == ["internal"]
    assert mcp._stop_peer_sync_thread()  # leave no fake thread behind for later tests


def test_docs_say_grants_load_once_and_revocation_needs_a_restart():
    text = " ".join((Path(__file__).parents[2] / "docs" / "acl.md").read_text().split())
    assert "read once, at startup" in text
    assert "needs a server restart; SIGHUP does not reload it" in text
