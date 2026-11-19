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


_CAP_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
_UNSET = dict.fromkeys(_CAP_VARS)
_CAPPED = dict.fromkeys(_CAP_VARS, "1")

# Loaded through PYTHONPATH as ``sitecustomize`` in each probed interpreter:
# records the thread variables at the moment numpy is first imported (the
# point OpenBLAS reads them) and again at exit.
_PROBE_SITECUSTOMIZE = """
import atexit, json, os, sys
_OUT = os.environ["MEMPALACE_BLAS_PROBE_OUT"]
_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
_STATE = {"at_numpy": None}


class _NumpyImportProbe:
    def find_spec(self, name, path=None, target=None):
        if name == "numpy" and _STATE["at_numpy"] is None:
            _STATE["at_numpy"] = {v: os.environ.get(v) for v in _VARS}
        return None


sys.meta_path.insert(0, _NumpyImportProbe())


def _dump():
    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump(
            {"at_numpy": _STATE["at_numpy"], "at_exit": {v: os.environ.get(v) for v in _VARS}},
            fh,
        )


atexit.register(_dump)
"""


def _probe_thread_env(tmp_path, argv, *, preset=None, stdin=subprocess.DEVNULL):
    """Run ``argv`` with the probe loaded; return its recorded thread variables."""
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir(exist_ok=True)
    (probe_dir / "sitecustomize.py").write_text(_PROBE_SITECUSTOMIZE, encoding="utf-8")
    out = tmp_path / "probe.json"
    env = os.environ.copy()
    for var in _CAP_VARS:
        env.pop(var, None)
    env.update(preset or {})
    env["PYTHONPATH"] = os.pathsep.join([str(probe_dir), str(REPO_ROOT)])
    env["MEMPALACE_BLAS_PROBE_OUT"] = str(out)
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
        env=env,
        stdin=stdin,
    )
    assert out.exists(), f"probe did not run (rc={proc.returncode}): {proc.stderr[-2000:]}"
    return json.loads(out.read_text(encoding="utf-8")), proc


def _entry(code: str) -> list[str]:
    return [sys.executable, "-c", "import sys\n" + code]


def test_importing_mempalace_leaves_blas_and_omp_threads_alone(tmp_path) -> None:
    """A library import must not cap the host application's thread pools."""
    data, proc = _probe_thread_env(
        tmp_path,
        _entry("import mempalace, mempalace.searcher, mempalace.mcp_server, numpy"),
    )
    assert proc.returncode == 0, proc.stderr
    assert data == {"at_numpy": _UNSET, "at_exit": _UNSET}


def test_cli_compute_command_leaves_blas_and_omp_threads_alone(tmp_path) -> None:
    """``mempalace mine`` keeps every core for embedding and exact scans."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.md").write_text("# Notes\n\nThread caps belong to servers.\n")
    data, proc = _probe_thread_env(
        tmp_path,
        _entry(
            "sys.argv = ['mempalace', '--palace', sys.argv[1], 'mine', sys.argv[2], '--dry-run']\n"
            "from mempalace.cli import main\n"
            "main()"
        )
        + [str(tmp_path / "palace"), str(project)],
    )
    assert proc.returncode == 0, proc.stderr
    assert data["at_exit"] == _UNSET
    assert data["at_numpy"] in (None, _UNSET)


_SERVER_ENTRY_POINTS = {
    # mempalace-light-mcp console script
    "light-console": _entry(
        "sys.argv = ['mempalace-light-mcp', '--help']\n"
        "from mempalace.mcp_light_server import main\n"
        "main()"
    ),
    "light-module": [sys.executable, "-m", "mempalace.mcp_light_server", "--help"],
    # mempalace-mcp console script; --help falls through to the full server
    "mcp-console": _entry(
        "sys.argv = ['mempalace-mcp', '--help']\nfrom mempalace.mcp_proxy import main\nmain()"
    ),
    "mcp-module": [sys.executable, "-m", "mempalace.mcp_server", "--help"],
}


@pytest.mark.parametrize("name", sorted(_SERVER_ENTRY_POINTS))
def test_server_entry_points_cap_blas_threads_before_numpy(tmp_path, name) -> None:
    data, proc = _probe_thread_env(tmp_path, _SERVER_ENTRY_POINTS[name])
    assert proc.returncode == 0, proc.stderr
    assert data["at_exit"] == _CAPPED
    if name.startswith("light"):
        # The light server stays light: numpy never loads for --help.
        assert data["at_numpy"] is None
    else:
        assert data["at_numpy"] == _CAPPED


def test_hub_launcher_caps_blas_threads_before_numpy(tmp_path) -> None:
    """``mempalace serve`` starts the hub through the cwd-safe module launcher."""
    from mempalace._proc import safe_module_argv

    data, proc = _probe_thread_env(tmp_path, safe_module_argv("mempalace.mcp_server", "--help"))
    assert proc.returncode == 0, proc.stderr
    assert data == {"at_numpy": _CAPPED, "at_exit": _CAPPED}


def test_sidecar_bridge_caps_blas_threads(tmp_path) -> None:
    bridge = REPO_ROOT / "plugins" / "mempalace" / "sidecars" / "palace" / "bridge.py"
    data, proc = _probe_thread_env(tmp_path, [sys.executable, "-u", str(bridge)])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.splitlines()[0])["type"] == "ready"
    assert data["at_exit"] == _CAPPED
    assert data["at_numpy"] is None


@pytest.mark.parametrize("name", ["light-console", "mcp-console", "mcp-module"])
def test_server_entry_points_preserve_explicit_thread_env_vars(tmp_path, name) -> None:
    """Operator-configured thread counts win over the server default."""
    preset = {"OPENBLAS_NUM_THREADS": "4", "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "3"}
    data, proc = _probe_thread_env(tmp_path, _SERVER_ENTRY_POINTS[name], preset=preset)
    assert proc.returncode == 0, proc.stderr
    assert data["at_exit"] == preset
    assert data["at_numpy"] in (None, preset)


def test_serve_passes_blas_cap_to_hub_child(tmp_path, monkeypatch) -> None:
    import argparse
    import subprocess as _subprocess

    from mempalace import cli

    captured = {}

    class _Exec(Exception):
        pass

    def _capture(argv, env):
        captured["env"] = env
        raise _Exec

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    monkeypatch.setenv("OMP_NUM_THREADS", "6")
    monkeypatch.delenv("MKL_NUM_THREADS", raising=False)
    monkeypatch.setattr(cli.os, "execve", lambda path, argv, env: _capture(argv, env))
    monkeypatch.setattr(_subprocess, "run", lambda argv, env=None, **kw: _capture(argv, env))
    args = argparse.Namespace(
        host="127.0.0.1",
        port=8765,
        backend=None,
        global_backend=None,
        palace=str(tmp_path / "palace"),
        token=None,
        tls_cert=None,
        tls_key=None,
        read_only=False,
        allow_insecure=False,
        no_token=False,
    )
    with pytest.raises(_Exec):
        cli.cmd_serve(args)
    env = captured["env"]
    assert (env["OPENBLAS_NUM_THREADS"], env["OMP_NUM_THREADS"], env["MKL_NUM_THREADS"]) == (
        "1",
        "6",
        "1",
    )
    # The serving CLI process itself is not capped.
    assert "OPENBLAS_NUM_THREADS" not in os.environ


@pytest.mark.parametrize(
    ("orig_argv", "expected"),
    [
        (["python", "-m", "mempalace.mcp_server", "--transport", "http"], True),
        (["python", "-u", "-X", "utf8", "-m", "mempalace.mcp_server"], True),
        (["python", "-mmempalace.mcp_server"], True),
        (["python", "-m", "mempalace.mcp_server.runtime"], False),
        (["python", "-m", "pytest", "-m", "mempalace.mcp_server"], False),
        (["python", "-c", "import mempalace.mcp_server"], False),
        (["python", "script.py", "-m", "mempalace.mcp_server"], False),
        (["mempalace"], False),
        ([], False),
    ],
)
def test_launched_as_module_reads_interpreter_argv(monkeypatch, orig_argv, expected) -> None:
    from mempalace import _blas_threads

    monkeypatch.setattr(sys, "orig_argv", orig_argv, raising=False)
    assert _blas_threads.launched_as_module("mempalace.mcp_server") is expected


def test_launched_as_module_recognises_the_safe_launcher(monkeypatch) -> None:
    from mempalace import _blas_threads
    from mempalace._proc import safe_module_argv

    monkeypatch.setattr(
        sys, "orig_argv", safe_module_argv("mempalace.mcp_server", "--port", "1"), raising=False
    )
    assert _blas_threads.launched_as_module("mempalace.mcp_server") is True
    assert _blas_threads.launched_as_module("mempalace.daemon") is False


def _torch_threads(code: str) -> int:
    env = os.environ.copy()
    for var in _CAP_VARS:
        env.pop(var, None)
    proc = subprocess.run(
        [sys.executable, "-c", code + "; print(torch.get_num_threads())"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return int(proc.stdout.strip().splitlines()[-1])


def test_importing_mempalace_keeps_torch_threads() -> None:
    """CPU embedding (EmbeddingGemma via torch) keeps the default intra-op pool."""
    pytest.importorskip("torch")
    assert _torch_threads("import mempalace, torch") == _torch_threads("import torch")


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
