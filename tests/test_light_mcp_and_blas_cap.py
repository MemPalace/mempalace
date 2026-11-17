"""Tests for issue #3020: BLAS/OMP thread-pool cap and lazy light MCP server."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path

import pytest

from mempalace import mcp_light_server
from mempalace.hub_client import HubClient

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_importing_mempalace_caps_blas_and_omp_threads_by_default() -> None:
    """Importing mempalace sets OPENBLAS/OMP/MKL_NUM_THREADS=1 before numpy loads."""
    env = os.environ.copy()
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        env.pop(var, None)

    probe = (
        "import json, os, sys; "
        "import mempalace; "
        "print(json.dumps({"
        "'openblas': os.environ.get('OPENBLAS_NUM_THREADS'), "
        "'omp': os.environ.get('OMP_NUM_THREADS'), "
        "'mkl': os.environ.get('MKL_NUM_THREADS'), "
        "'numpy_loaded': 'numpy' in sys.modules"
        "}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip())
    assert data == {"openblas": "1", "omp": "1", "mkl": "1", "numpy_loaded": False}


def test_importing_mempalace_preserves_explicit_thread_env_vars() -> None:
    """User-configured OPENBLAS/OMP/MKL thread counts are preserved via setdefault."""
    env = os.environ.copy()
    env["OPENBLAS_NUM_THREADS"] = "4"
    env["OMP_NUM_THREADS"] = "2"
    env["MKL_NUM_THREADS"] = "3"

    probe = (
        "import json, os, mempalace; "
        "print(json.dumps({"
        "'openblas': os.environ.get('OPENBLAS_NUM_THREADS'), "
        "'omp': os.environ.get('OMP_NUM_THREADS'), "
        "'mkl': os.environ.get('MKL_NUM_THREADS')"
        "}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip())
    assert data == {"openblas": "4", "omp": "2", "mkl": "3"}


def test_importing_mcp_light_server_keeps_chromadb_and_numpy_unloaded() -> None:
    """Importing mcp_light_server and running handshake/classify must not load mcp_server/chromadb/numpy."""
    probe = (
        "import json, sys; "
        "from mempalace import mcp_light_server as l; "
        "init = l.handle_light_request({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}}); "
        "listed = l.handle_light_request({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}); "
        "cls = l._classify_underlying_tool('palace_query', 'FIND auth IN proj LIMIT 5'); "
        "heavy = [m for m in ('mempalace.mcp_server', 'chromadb', 'numpy') if m in sys.modules]; "
        "l._restore_stdout(); "
        "print(json.dumps({'heavy': heavy, 'server': init['result']['serverInfo']['name'], 'tools': [t['name'] for t in listed['result']['tools']], 'cls': cls, 'loaded': l._mcp_server_loaded()}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout.strip())
    assert info["heavy"] == []
    assert info["loaded"] is False
    assert info["server"] == "mempalace-light"
    assert info["tools"] == ["palace_query", "palace_exec", "palace_coordinate"]
    assert info["cls"] == "mempalace_search"


def test_flagged_light_session_skips_hub_discovery(monkeypatch) -> None:
    monkeypatch.setattr(
        HubClient,
        "discover",
        classmethod(lambda cls, palace: pytest.fail("flags must skip hub discovery")),
    )

    class Args:
        palace = "/palace"
        read_only = True
        backend = None
        collection = None

    assert mcp_light_server._hub_for_light_session(Args()) is None


def test_light_hub_proxy_rewrites_pql_and_enriches_tunnels_without_loading_server(
    tmp_path, monkeypatch
) -> None:
    from mempalace.config import MempalaceConfig

    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    tunnel_path = Path(MempalaceConfig(palace_path=str(palace_dir)).tunnel_file)
    assert tunnel_path == tmp_path / "tunnels.json"
    tunnel_path.write_text(
        json.dumps(
            [
                {
                    "id": "t1",
                    "source": {"wing": "patient_1042", "room": "labs"},
                    "target": {"wing": "guidelines", "room": "rx", "drawer_id": "d1"},
                    "label": "protocol",
                }
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        mcp_light_server,
        "_mcp_server_loaded",
        lambda: False,
    )
    monkeypatch.setattr(
        mcp_light_server,
        "_get_mcp_server",
        lambda: pytest.fail("hub proxy must not load mcp_server"),
    )

    hub = HubClient(str(palace_dir), {"host": "127.0.0.1", "port": 8765, "pid": 9999})
    proxy = mcp_light_server._LightHubProxy(hub, str(palace_dir))
    forwarded = []

    def fake_forward(request):
        forwarded.append(request)
        if request.get("method") == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "tools": [
                        {
                            "name": "mempalace_search",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"query": {}, "wing": {}, "limit": {}},
                            },
                        }
                    ]
                },
            }
        name = (request.get("params") or {}).get("name")
        if name == "mempalace_list_wings":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"wings": {"patient_1042": 5}})}
                    ]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "results": [
                                    {
                                        "text": "potassium 5.4",
                                        "distance": 0.12,
                                        "wing": "patient_1042",
                                        "room": "labs",
                                        "created_at": "2026-01-01T00:00:00",
                                    }
                                ]
                            }
                        ),
                    }
                ]
            },
        }

    proxy._forward = fake_forward

    req = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "palace_query",
            "arguments": {"query": 'FIND "potassium" IN 1042 LIMIT 5'},
        },
    }
    resp = proxy.handle(req)
    proxy.handle(req)

    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["relevance_confidence"] == "high"
    assert payload["connected_tunnels"] == [
        {
            "direction": "outgoing",
            "connected_wing": "guidelines",
            "connected_room": "rx",
            "label": "protocol",
            "drawer_id": "d1",
            "tunnel_id": "t1",
        }
    ]
    search_reqs = [
        r for r in forwarded if (r.get("params") or {}).get("name") == "mempalace_search"
    ]
    wing_reqs = [
        r for r in forwarded if (r.get("params") or {}).get("name") == "mempalace_list_wings"
    ]
    assert len(search_reqs) == 2
    assert search_reqs[0]["params"]["arguments"]["wing"] == "patient_1042"
    assert len(wing_reqs) == 1


def test_light_hub_proxy_does_not_replay_mutating_errors() -> None:
    hub = HubClient("/palace", {"host": "127.0.0.1", "port": 8765, "pid": 9999})
    proxy = mcp_light_server._LightHubProxy(hub, "/palace")
    proxy._forward = lambda _req: (_ for _ in ()).throw(urllib.error.URLError("hub down"))

    resp = proxy.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "palace_exec",
                "arguments": {"action": "add_drawer", "wing": "w", "room": "r", "content": "hi"},
            },
        }
    )
    assert resp["error"]["code"] == -32000
    assert "hub down" in resp["error"]["message"]


def test_light_server_subprocess_hub_proxy_stays_light(tmp_path) -> None:
    """Running mempalace.mcp_light_server against a live hub never imports chromadb/numpy."""
    from mempalace import server_registry
    from mempalace.config import MempalaceConfig

    home_dir = tmp_path / "home"
    palace_dir = tmp_path / "palace"
    home_dir.mkdir()
    palace_dir.mkdir()

    class _Hub(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            method = req.get("method")
            if method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "mempalace_kg_stats",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                }
            else:
                result = {
                    "content": [
                        {"type": "text", "text": json.dumps({"current_facts": 9, "entities": 4})}
                    ]
                }
            body = json.dumps({"jsonrpc": "2.0", "id": req.get("id", 1), "result": result}).encode(
                "utf-8"
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hub)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    old_home = os.environ.get("HOME")
    old_userprofile = os.environ.get("USERPROFILE")
    old_palace = os.environ.get("MEMPALACE_PALACE_PATH")
    try:
        os.environ["HOME"] = str(home_dir)
        os.environ["USERPROFILE"] = str(home_dir)
        os.environ["MEMPALACE_PALACE_PATH"] = str(palace_dir)
        fp = MempalaceConfig(palace_path=str(palace_dir)).search_config_fingerprint
        server_registry.write_serverinfo(
            str(palace_dir),
            host="127.0.0.1",
            port=port,
            scheme="http",
            read_only=False,
            capabilities=[],
            search_config_fingerprint=fp,
        )

        env = os.environ.copy()
        stdin_data = (
            "\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {"protocolVersion": "2025-11-25"},
                        }
                    ),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {"name": "palace_query", "arguments": {"target": "kg_stats"}},
                        }
                    ),
                ]
            )
            + "\n"
        )
        proc = subprocess.run(
            [sys.executable, "-m", "mempalace.mcp_light_server"],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(REPO_ROOT),
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        assert [r["id"] for r in replies] == [1, 2, 3]
        assert replies[0]["result"]["serverInfo"]["name"] == "mempalace-light"
        assert [t["name"] for t in replies[1]["result"]["tools"]] == [
            "palace_query",
            "palace_exec",
            "palace_coordinate",
        ]
        kg_payload = json.loads(replies[2]["result"]["content"][0]["text"])
        assert kg_payload["current_facts"] == 9
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_userprofile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_userprofile
        if old_palace is None:
            os.environ.pop("MEMPALACE_PALACE_PATH", None)
        else:
            os.environ["MEMPALACE_PALACE_PATH"] = old_palace
        httpd.shutdown()
        httpd.server_close()


def test_mcp_light_server_exposes_real_module_for_pytest_static_monkeypatch() -> None:
    """Accessing mcp_light_server.mcp_server returns the real ModuleType so pytest 9.2+ keeps attributes."""
    import inspect
    import types

    resolved = mcp_light_server.mcp_server
    assert isinstance(resolved, types.ModuleType)
    sentinel = object()
    assert inspect.getattr_static(resolved, "_mcp_tool_preflight_refusal", sentinel) is not sentinel

    mp_ctx = pytest.MonkeyPatch()
    mp_ctx.setattr(mcp_light_server.mcp_server, "_mcp_tool_preflight_refusal", lambda *a, **k: None)
    assert mcp_light_server.mcp_server._mcp_tool_preflight_refusal(1, "x") is None
    mp_ctx.undo()
    assert hasattr(resolved, "_mcp_tool_preflight_refusal")
    assert callable(resolved._mcp_tool_preflight_refusal)


def test_light_hub_proxy_degrades_once_and_reconnects_when_hub_restarts(
    tmp_path, monkeypatch
) -> None:
    """Dead hub fails once (not twice on wing lookup), annotates degraded mode, and reconnects on restart."""
    palace_dir = str(tmp_path / "palace")
    hub1 = HubClient(palace_dir, {"host": "127.0.0.1", "port": 8765, "pid": 9999})
    proxy = mcp_light_server._LightHubProxy(hub1, palace_dir)

    attempts = []

    def dead_forward(_req):
        attempts.append("dead")
        raise urllib.error.URLError("connection refused")

    proxy._forward = dead_forward
    discovered = {"hub": None}
    monkeypatch.setattr(HubClient, "discover", classmethod(lambda cls, _p: discovered["hub"]))
    local_calls = []
    monkeypatch.setattr(
        proxy,
        "_fallback_local",
        lambda req, *, degraded=False: (
            local_calls.append(degraded)
            or {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {
                    "content": [
                        {"type": "text", "text": "[mempalace] degraded" if degraded else "ok"}
                    ]
                },
            }
        ),
    )

    req = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {"name": "palace_query", "arguments": {"query": 'FIND "x" IN proj LIMIT 3'}},
    }
    first = proxy.handle(req)
    assert len(attempts) == 1
    assert proxy.hub is None
    assert local_calls == [True]
    assert "degraded" in first["result"]["content"][0]["text"]

    # Second call while hub is still down must not touch dead socket again
    proxy.handle(req)
    assert len(attempts) == 1
    assert local_calls == [True, True]

    # When a restarted hub appears on a new port, proxy reconnects automatically
    hub2 = HubClient(palace_dir, {"host": "127.0.0.1", "port": 8766, "pid": 10001})
    discovered["hub"] = hub2
    live_calls = []

    def live_forward(r):
        live_calls.append(r)
        if r.get("method") == "tools/list":
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        return {
            "jsonrpc": "2.0",
            "id": r["id"],
            "result": {"content": [{"type": "text", "text": json.dumps({"results": []})}]},
        }

    proxy._forward = live_forward
    resumed = proxy.handle(req)
    assert proxy.hub is hub2
    assert len(live_calls) >= 2
    assert "error" not in resumed
