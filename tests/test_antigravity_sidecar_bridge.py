"""Tests for the Antigravity palace sidecar bridge (issue #3019).

Covers:
1. Hub-first forwarding for overview, search, drawers, drawer, tunnels, KG,
   events, artifact, and diary without importing ``mempalace.mcp_server``.
2. Opt-in warm-up (off by default; skipped when a hub is running) and lazy
   bridge launch in ``main.mjs``.
3. Local lazy vector loading (FTS5 lexical & overview never load vectors)
   and idle unload releasing backend and embedding caches.
4. ``candidate_strategy="lexical"`` in ``search_memories`` / ``mempalace_search``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_DIR = REPO_ROOT / "plugins" / "mempalace" / "sidecars" / "palace"
BRIDGE_PATH = SIDECAR_DIR / "bridge.py"
MAIN_MJS_PATH = SIDECAR_DIR / "main.mjs"


def _load_bridge(name: str = "test_agy_bridge_mod"):
    spec = importlib.util.spec_from_file_location(name, BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeHub:
    def __init__(self, responses: dict[str, dict], *, capabilities=None, pid: int = 4242):
        self.responses = responses
        self.capabilities = list(capabilities or [])
        self.pid = pid
        self.info = {
            "pid": pid,
            "capabilities": self.capabilities,
            "search_config_fingerprint": "fp",
        }
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments: dict, *, timeout: float = 30.0):
        self.calls.append((name, dict(arguments)))
        payload = self.responses.get(name)
        if callable(payload):
            payload = payload(arguments)
        if isinstance(payload, Exception):
            raise payload
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
        }


def test_bridge_import_does_not_import_mcp_server(monkeypatch) -> None:
    """Importing bridge.py must stay lightweight and keep mcp_server unloaded."""
    # Ensure a clean check against fresh bridge module
    mod = _load_bridge("test_agy_bridge_no_import")
    assert not mod._mp_loaded()


def test_main_mjs_defers_bridge_spawn_unless_warmup_opted_in() -> None:
    """main.mjs must not spawn bridge.py unconditionally on startup."""
    body = MAIN_MJS_PATH.read_text(encoding="utf-8")
    assert "MEMPALACE_SIDECAR_WARMUP" in body
    assert "MEMPALACE_EAGER_WARMUP" in body
    # bridge.ensureStarted() at top-level startup must be guarded by opt-in check
    tail = body.split("registerApi('/api/diary'", 1)[1]
    assert "if (isEnvTruthy(" in tail


def test_hub_first_routes_all_actions_without_loading_mcp_server(tmp_path, monkeypatch) -> None:
    """When a hub is reachable, all bridge handlers proxy to it and never import mcp_server."""
    mod = _load_bridge("test_agy_bridge_hub_all")
    monkeypatch.setattr(mod, "_disk_cache_path", lambda: str(tmp_path / "cache.json"))
    monkeypatch.setattr(mod, "_palace_path", lambda: str(tmp_path / "palace"))
    monkeypatch.setattr(mod, "_collection_name", lambda: "mempalace_drawers")
    monkeypatch.setattr(mod, "search_settings_overridden", lambda: False)
    monkeypatch.setattr(mod, "search_fingerprint_matches", lambda info, p: True)
    monkeypatch.setattr(
        mod,
        "_get_mp",
        lambda: pytest.fail("hub-proxied sidecar must not import mempalace.mcp_server"),
    )

    hub = _FakeHub(
        {
            "mempalace_reconnect": {"success": True},
            "mempalace_status": {"total_drawers": 120, "backend": "sqlite_exact"},
            "mempalace_get_taxonomy": {"taxonomy": {"mempalace": {"auth": 50, "ui": 70}}},
            "mempalace_list_tunnels": {"tunnels": [{"room": "auth", "wings": ["a", "b"]}]},
            "mempalace_kg_stats": {"current_facts": 12, "entities": 5},
            "mempalace_event_list": {
                "events": [{"event_id": "e1", "type": "status"}],
                "tip": {"seq": 1},
            },
            "mempalace_search": lambda args: {
                "query": args["query"],
                "results": [
                    {
                        "drawer_id": "d1",
                        "text": "hello",
                        "wing": "mempalace",
                        "room": "auth",
                        "bm25_score": 3.2,
                    }
                ],
                "strategy": args["candidate_strategy"],
            },
            "mempalace_list_drawers": {
                "drawers": [
                    {
                        "drawer_id": "d1",
                        "wing": "mempalace",
                        "room": "auth",
                        "content_preview": "drawer preview text",
                        "metadata": {
                            "source_file": "/repo/a.py",
                            "filed_at": "2026-11-16T10:00:00Z",
                            "hall": "hall_facts",
                        },
                    }
                ],
                "total": 40,
            },
            "mempalace_get_drawer": {"drawer_id": "d1", "content": "full drawer text"},
            "mempalace_traverse": {"nodes": ["auth"], "edges": []},
            "mempalace_kg_timeline": {
                "timeline": [{"subject": "Alice", "predicate": "works_at", "object": "Acme"}]
            },
            "mempalace_kg_query": {"facts": [{"subject": "Alice", "object": "Acme"}]},
            "mempalace_artifact_get": {"artifact": {"artifact_id": "abc123", "content": "patch"}},
            "mempalace_diary_read": {"agent": "claude", "entries": [{"content": "entry"}]},
        },
        capabilities=["search_lexical"],
    )
    monkeypatch.setattr(mod, "_discover_hub", lambda palace_path=None: hub)

    ov = mod._handle_overview({"workspacePath": "/work/mempalace", "force_refresh": True})
    assert ov["hub_connected"] is True
    assert ov["vector_warm"] is True
    assert ov["vector_warming"] is False
    assert ov["workspace_wing"] == "mempalace"
    assert ov["status"]["total_drawers"] == 120
    assert ov["graph_stats"]["explicit_tunnels"] == 1

    lex_res = mod._handle_search({"query": "hello", "candidate_strategy": "lexical", "limit": 5})
    assert lex_res["hub_connected"] is True
    assert lex_res["strategy"] == "lexical"
    assert (
        "mempalace_search",
        {"query": "hello", "limit": 5, "candidate_strategy": "lexical"},
    ) in hub.calls

    vec_res = mod._handle_search({"query": "hello", "candidate_strategy": "union", "limit": 5})
    assert vec_res["hub_connected"] is True
    assert (
        "mempalace_search",
        {"query": "hello", "limit": 5, "candidate_strategy": "union"},
    ) in hub.calls

    dr = mod._handle_drawers({"wing": "mempalace", "limit": 10, "offset": 0})
    assert dr["hub_connected"] is True
    assert dr["total"] == 40
    assert dr["has_more"] is True
    assert dr["drawers"][0]["content"] == "drawer preview text"
    assert dr["drawers"][0]["source_file"] == "/repo/a.py"
    assert dr["drawers"][0]["filed_at"] == "2026-11-16T10:00:00Z"

    one = mod._handle_drawer({"drawer_id": "d1"})
    assert one["content"] == "full drawer text"

    trav = mod._handle_tunnels({"start_room": "auth", "max_hops": 2})
    assert trav["nodes"] == ["auth"]

    kg = mod._handle_kg({"entity": "Alice"})
    assert kg["hub_connected"] is True
    assert "Alice" in kg["entities_sample"]

    ev = mod._handle_events({"room": "delegation", "limit": 10})
    assert len(ev["events"]) == 1

    art = mod._handle_artifact({"sha256": "abc123"})
    assert art["artifact"]["artifact_id"] == "abc123"
    assert ("mempalace_artifact_get", {"artifact_id": "abc123"}) in hub.calls

    diary = mod._handle_diary({"agent_name": "claude", "last_n": 5})
    assert diary["mode"] == "agent"
    assert diary["result"]["agent"] == "claude"
    assert not mod._mp_loaded()


def test_older_hub_without_search_lexical_uses_union_without_local_fallback(
    tmp_path, monkeypatch
) -> None:
    """An older hub lacking search_lexical still gets lexical-keystroke queries via union."""
    mod = _load_bridge("test_agy_bridge_old_hub")
    monkeypatch.setattr(mod, "_palace_path", lambda: str(tmp_path / "palace"))
    monkeypatch.setattr(
        mod,
        "_get_mp",
        lambda: pytest.fail("must not fall back to local mcp_server when hub is live"),
    )

    hub = _FakeHub(
        {"mempalace_search": {"query": "test", "results": [], "strategy": "union"}},
        capabilities=["search_cli_compatible"],
    )
    monkeypatch.setattr(mod, "_discover_hub", lambda palace_path=None: hub)

    res = mod._handle_search({"query": "test", "candidate_strategy": "lexical", "limit": 5})
    assert res["hub_connected"] is True
    assert (
        "mempalace_search",
        {"query": "test", "limit": 5, "candidate_strategy": "union"},
    ) in hub.calls


def test_hub_transport_error_falls_back_to_local(tmp_path, monkeypatch) -> None:
    """When the hub dies mid-session, bridge falls back cleanly to local handlers."""
    mod = _load_bridge("test_agy_bridge_fallback")
    monkeypatch.setattr(mod, "_palace_path", lambda: str(tmp_path / "palace"))
    monkeypatch.setattr(mod, "_disk_cache_path", lambda: str(tmp_path / "cache.json"))

    failing_hub = _FakeHub(
        {"mempalace_status": urllib.error.URLError("connection refused")},
        capabilities=["search_lexical"],
    )
    monkeypatch.setattr(mod, "_discover_hub", lambda palace_path=None: failing_hub)

    fake_mp = types.SimpleNamespace(
        _config=types.SimpleNamespace(
            palace_path=str(tmp_path), collection_name="mempalace_drawers", config_dir=str(tmp_path)
        ),
        tool_status=lambda: {"total_drawers": 7, "backend": "sqlite_exact"},
        tool_get_taxonomy=lambda: {"taxonomy": {"demo": {"room": 7}}},
        tool_kg_stats=lambda: {"current_facts": 0},
        tool_event_list=lambda **kw: {"events": []},
        tool_reconnect=lambda: {"success": True},
        _selected_backend_name=lambda: "sqlite_exact",
        _load_graph_tunnels=lambda cfg: [],
    )
    mod.mp = fake_mp

    ov = mod._handle_overview({})
    assert ov["hub_connected"] is False
    assert ov["vector_warm"] is False
    assert ov["status"]["total_drawers"] == 7


def test_warmup_is_opt_in_and_skipped_with_hub(monkeypatch) -> None:
    """Background warmup is off by default and skipped if a hub is alive."""
    mod = _load_bridge("test_agy_bridge_warmup")
    monkeypatch.delenv("MEMPALACE_SIDECAR_WARMUP", raising=False)
    monkeypatch.delenv("MEMPALACE_EAGER_WARMUP", raising=False)
    assert mod._warmup_enabled() is False

    called = []
    monkeypatch.setattr(mod, "_get_mp", lambda: called.append("loaded"))
    mod._background_warmup()
    assert called == []

    monkeypatch.setenv("MEMPALACE_SIDECAR_WARMUP", "1")
    assert mod._warmup_enabled() is True
    monkeypatch.setattr(mod, "_discover_hub", lambda palace_path=None: object())
    mod._background_warmup()
    assert called == []


def test_local_lazy_vector_load_and_idle_unload(monkeypatch) -> None:
    """Local vector search sets _VECTOR_WARM and idle unload drops backend & embedder caches."""
    from mempalace import embedding

    mod = _load_bridge("test_agy_bridge_unload")
    monkeypatch.setattr(mod, "_discover_hub", lambda palace_path=None: None)
    monkeypatch.setenv("MEMPALACE_SIDECAR_IDLE_UNLOAD_SECONDS", "60")

    reconnect_calls = []
    fake_mp = types.SimpleNamespace(
        _config=types.SimpleNamespace(
            palace_path="/tmp/p", collection_name="mempalace_drawers", config_dir="/tmp"
        ),
        tool_search=lambda **kw: {"query": kw["query"], "results": []},
        tool_reconnect=lambda: reconnect_calls.append("reconnected") or {"success": True},
    )
    mod.mp = fake_mp

    # Seed _EF_CACHE with a dummy EF holding a mock session/model
    dummy_ef = types.SimpleNamespace(_session="onnx_sess", _tokenizer="tok", _model="torch_model")
    dummy_ef.__dict__["model"] = "cached_minilm_model"
    with embedding._EF_CACHE_LOCK:
        embedding._EF_CACHE[("test_dummy", ("CPUExecutionProvider",))] = dummy_ef

    try:
        res = mod._handle_search({"query": "concept", "candidate_strategy": "vector", "limit": 3})
        assert res["vector_warm"] is True
        assert mod._VECTOR_WARM is True

        # Before idle timeout, non-forced unload keeps warm state
        assert mod._unload_vector_stack(force=False) is False
        assert mod._VECTOR_WARM is True

        # Simulate idle expiry
        mod._LAST_VECTOR_USE -= 120.0
        assert mod._unload_vector_stack(force=False) is True
        assert mod._VECTOR_WARM is False
        assert reconnect_calls == ["reconnected"]
        assert dummy_ef._session is None
        assert dummy_ef._tokenizer is None
        assert dummy_ef._model is None
        assert "model" not in dummy_ef.__dict__
        assert ("test_dummy", ("CPUExecutionProvider",)) not in embedding._EF_CACHE
    finally:
        mod._cancel_idle_unload_timer()
        with embedding._EF_CACHE_LOCK:
            embedding._EF_CACHE.pop(("test_dummy", ("CPUExecutionProvider",)), None)


def test_search_memories_lexical_strategy_skips_embedder(tmp_path, monkeypatch) -> None:
    """candidate_strategy='lexical' searches FTS5/BM25 without loading the embedder."""
    from mempalace import embedding
    from mempalace.palace import get_collection
    from mempalace.searcher import search_memories

    palace = str(tmp_path / "palace")
    col = get_collection(palace, create=True, backend="sqlite_exact")
    col.add(
        ids=["d1", "d2"],
        documents=[
            "Sidecar bridge hub proxy reduces memory commit charge",
            "Unrelated recipe about baking sourdough bread",
        ],
        metadatas=[
            {"wing": "mempalace", "room": "perf", "source_file": "issue3019.md"},
            {"wing": "cooking", "room": "bread", "source_file": "bread.md"},
        ],
        embeddings=[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
    )

    monkeypatch.setattr(
        embedding,
        "get_embedding_function",
        lambda *a, **kw: pytest.fail("lexical strategy must not load embedder"),
    )

    result = search_memories(
        "memory commit charge",
        palace,
        wing="mempalace",
        n_results=5,
        candidate_strategy="lexical",
    )
    assert result["strategy"] == "lexical"
    assert len(result["results"]) == 1
    assert result["results"][0]["drawer_id"] == "d1"
    assert result["results"][0]["match_type"] == "fts5"
    assert result["results"][0]["similarity"] is None


def test_mcp_search_accepts_lexical_candidate_strategy(monkeypatch, tmp_path) -> None:
    """mempalace_search MCP tool schema accepts candidate_strategy='lexical'."""
    from mempalace import mcp_server

    captured = {}

    def fake_search(query, palace_path, **kwargs):
        captured["query"] = query
        captured.update(kwargs)
        return {"query": query, "results": [], "strategy": kwargs.get("candidate_strategy")}

    monkeypatch.setattr(mcp_server, "search_memories", fake_search)
    monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(mcp_server, "_vector_disabled", False)

    resp = mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mempalace_search",
                "arguments": {"query": "fts5", "candidate_strategy": "lexical"},
            },
        }
    )
    assert "error" not in resp
    assert captured["candidate_strategy"] == "lexical"


def test_bridge_subprocess_hub_proxy_end_to_end(tmp_path) -> None:
    """Real subprocess bridge.py proxies to a live HTTP hub and keeps mcp_server unloaded."""
    import http.server
    import os
    import subprocess
    import threading

    from mempalace import server_registry
    from mempalace.config import MempalaceConfig

    home_dir = tmp_path / "home"
    palace_dir = tmp_path / "palace"
    home_dir.mkdir()
    palace_dir.mkdir()

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["USERPROFILE"] = str(home_dir)
    env["MEMPALACE_PALACE_PATH"] = str(palace_dir)
    env["MEMPALACE_REPO_ROOT"] = str(REPO_ROOT)
    env["PYTHONUTF8"] = "1"
    env.pop("MEMPALACE_SIDECAR_WARMUP", None)
    env.pop("MEMPALACE_EAGER_WARMUP", None)

    tool_calls = []

    class _HubHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_GET(self):
            if self.path == "/healthz":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            tool_calls.append((name, args))
            answers = {
                "mempalace_status": {"total_drawers": 5, "backend": "sqlite_exact"},
                "mempalace_get_taxonomy": {"taxonomy": {"proj": {"notes": 5}}},
                "mempalace_list_tunnels": {"tunnels": []},
                "mempalace_kg_stats": {"current_facts": 1, "entities": 2},
                "mempalace_event_list": {"events": [{"event_id": "e1"}], "tip": {"seq": 1}},
                "mempalace_search": {
                    "query": args.get("query", ""),
                    "results": [
                        {"drawer_id": "d1", "text": "hit", "wing": "proj", "room": "notes"}
                    ],
                    "strategy": args.get("candidate_strategy", "union"),
                },
                "mempalace_list_drawers": {
                    "drawers": [
                        {
                            "drawer_id": "d1",
                            "wing": "proj",
                            "room": "notes",
                            "content_preview": "drawer body",
                            "metadata": {"source_file": "a.md", "filed_at": "2026-11-16T00:00:00Z"},
                        }
                    ],
                    "total": 1,
                },
            }
            payload = {
                "jsonrpc": "2.0",
                "id": req.get("id", 1),
                "result": {
                    "content": [{"type": "text", "text": json.dumps(answers.get(name, {}))}]
                },
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HubHandler)
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
            capabilities=["search_lexical"],
            search_config_fingerprint=fp,
        )

        requests = (
            "\n".join(
                [
                    json.dumps({"id": 1, "action": "ping", "params": {}}),
                    json.dumps({"id": 2, "action": "overview", "params": {}}),
                    json.dumps(
                        {
                            "id": 3,
                            "action": "search",
                            "params": {"query": "hit", "candidate_strategy": "lexical"},
                        }
                    ),
                    json.dumps(
                        {
                            "id": 4,
                            "action": "search",
                            "params": {"query": "hit", "candidate_strategy": "union"},
                        }
                    ),
                    json.dumps({"id": 5, "action": "drawers", "params": {"limit": 10}}),
                ]
            )
            + "\n"
        )

        proc = subprocess.run(
            [sys.executable, "-u", str(BRIDGE_PATH)],
            input=requests,
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        frames = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        assert frames[0]["type"] == "ready"
        by_id = {f["id"]: f for f in frames[1:]}
        assert by_id[1]["ok"] is True
        assert by_id[1]["data"]["hub_connected"] is True
        assert by_id[2]["data"]["hub_connected"] is True
        assert by_id[2]["data"]["status"]["total_drawers"] == 5
        assert by_id[3]["data"]["hub_connected"] is True
        assert by_id[3]["data"]["strategy"] == "lexical"
        assert by_id[4]["data"]["hub_connected"] is True
        assert by_id[5]["data"]["drawers"][0]["content"] == "drawer body"
        assert any(
            name == "mempalace_search" and args.get("candidate_strategy") == "lexical"
            for name, args in tool_calls
        )
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


def test_bridge_subprocess_startup_without_hub_keeps_heavy_modules_unloaded(tmp_path) -> None:
    """Starting bridge.py without a hub must not import mcp_server, chromadb, or onnxruntime."""
    import os
    import subprocess

    home_dir = tmp_path / "home"
    palace_dir = tmp_path / "palace"
    home_dir.mkdir()
    palace_dir.mkdir()

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["USERPROFILE"] = str(home_dir)
    env["MEMPALACE_PALACE_PATH"] = str(palace_dir)
    env["MEMPALACE_REPO_ROOT"] = str(REPO_ROOT)
    env["PYTHONUTF8"] = "1"
    env.pop("MEMPALACE_SIDECAR_WARMUP", None)
    env.pop("MEMPALACE_EAGER_WARMUP", None)

    probe_code = (
        "import json, sys, importlib.util; "
        f"spec = importlib.util.spec_from_file_location('b', {str(BRIDGE_PATH)!r}); "
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
        "heavy = [m for m in ('mempalace.mcp_server', 'chromadb', 'onnxruntime', 'tokenizers') if m in sys.modules]; "
        "print(json.dumps({'heavy': heavy, 'mp_loaded': mod._mp_loaded(), 'vector_warm': mod._VECTOR_WARM}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe_code],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout.strip())
    assert info["heavy"] == []
    assert info["mp_loaded"] is False
    assert info["vector_warm"] is False


def test_bridge_mp_exposes_real_module_for_pytest_static_monkeypatch() -> None:
    """Accessing bridge.mp must return the real ModuleType so pytest 9.2+ getattr_static keeps attributes."""
    import inspect

    mod = _load_bridge("test_agy_bridge_real_mp_mod")
    assert not mod._mp_loaded()

    resolved_mp = mod.mp
    assert isinstance(resolved_mp, types.ModuleType)
    assert mod._mp_loaded()
    sentinel = object()
    assert inspect.getattr_static(resolved_mp, "_get_collection", sentinel) is not sentinel

    mp_ctx = pytest.MonkeyPatch()
    mp_ctx.setattr(mod.mp, "_get_collection", lambda: "patched")
    assert mod.mp._get_collection() == "patched"
    mp_ctx.undo()
    assert hasattr(resolved_mp, "_get_collection")
    assert callable(resolved_mp._get_collection)


def test_overview_force_refresh_does_not_reconnect_shared_hub_and_skips_caching_errors(
    tmp_path, monkeypatch
) -> None:
    """UI force_refresh refreshes structure without evicting hub vectors; errors are not cached."""
    mod = _load_bridge("test_agy_bridge_refresh_vs_reconnect")
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(mod, "_disk_cache_path", lambda: str(cache_path))
    monkeypatch.setattr(mod, "_palace_path", lambda: str(tmp_path / "palace"))
    monkeypatch.setattr(mod, "_collection_name", lambda: "mempalace_drawers")

    err_hub = _FakeHub(
        {
            "mempalace_reconnect": {"success": True},
            "mempalace_status": {"error": "transient failure"},
            "mempalace_get_taxonomy": {"taxonomy": {}},
            "mempalace_list_tunnels": {"tunnels": []},
            "mempalace_kg_stats": {"current_facts": 0},
            "mempalace_event_list": {"events": []},
        },
        capabilities=["search_lexical", "list_drawers_recent"],
    )
    monkeypatch.setattr(mod, "_discover_hub", lambda palace_path=None: err_hub)
    mod._handle_overview({"forceRefresh": True})
    assert ("mempalace_reconnect", {}) not in err_hub.calls
    assert not cache_path.exists()

    ok_hub = _FakeHub(
        {
            "mempalace_reconnect": {"success": True},
            "mempalace_status": {"total_drawers": 12, "backend": "sqlite_exact"},
            "mempalace_get_taxonomy": {"taxonomy": {"proj": {"notes": 12}}},
            "mempalace_list_tunnels": {"tunnels": []},
            "mempalace_kg_stats": {"current_facts": 0},
            "mempalace_event_list": {"events": []},
        },
        capabilities=["search_lexical", "list_drawers_recent"],
    )
    monkeypatch.setattr(mod, "_discover_hub", lambda palace_path=None: ok_hub)
    mod._handle_overview({"force_refresh": True})
    assert ("mempalace_reconnect", {}) not in ok_hub.calls
    assert cache_path.exists()

    mod._handle_overview({"reconnect": True})
    assert ("mempalace_reconnect", {}) in ok_hub.calls


def test_hub_drawers_and_diary_preserve_recent_sort_and_verbatim_content(
    tmp_path, monkeypatch
) -> None:
    """New hubs receive sort='recent' + include_content=True; older hubs hydrate via get_drawers."""
    mod = _load_bridge("test_agy_bridge_hub_drawers_verbatim")
    monkeypatch.setattr(mod, "_palace_path", lambda: str(tmp_path / "palace"))
    long_diary = "Verbatim diary entry line. " * 20  # > 500 chars (well past 200-char preview)

    new_hub = _FakeHub(
        {
            "mempalace_list_drawers": lambda args: {
                "drawers": [
                    {
                        "drawer_id": "d-new",
                        "wing": "proj",
                        "room": "diary",
                        "content_preview": long_diary[:200] + "...",
                        "content": long_diary,
                        "total_chars": len(long_diary),
                        "metadata": {"filed_at": "2026-11-16T12:00:00Z", "hall": "diary"},
                    }
                ],
                "total": 1,
            }
        },
        capabilities=["search_lexical", "list_drawers_recent"],
    )
    monkeypatch.setattr(mod, "_discover_hub", lambda palace_path=None: new_hub)

    res_new = mod._handle_drawers({"wing": "proj", "sort": "recent", "limit": 10, "offset": 0})
    assert res_new["drawers"][0]["content"] == long_diary
    assert res_new["drawers"][0]["is_truncated"] is False
    assert (
        "mempalace_list_drawers",
        {"limit": 10, "offset": 0, "wing": "proj", "sort": "recent", "include_content": True},
    ) in new_hub.calls

    old_hub = _FakeHub(
        {
            "mempalace_list_drawers": {
                "drawers": [
                    {
                        "drawer_id": "d-old",
                        "wing": "proj",
                        "room": "diary",
                        "content_preview": long_diary[:200] + "...",
                        "metadata": {"filed_at": "2026-11-16T12:00:00Z", "hall": "diary"},
                    }
                ],
                "total": 1,
            },
            "mempalace_get_drawers": {
                "results": [
                    {
                        "drawer_id": "d-old",
                        "content": long_diary,
                        "total_chars": len(long_diary),
                    }
                ],
                "count": 1,
                "errors": 0,
            },
        },
        capabilities=["search_cli_compatible"],
    )
    monkeypatch.setattr(mod, "_discover_hub", lambda palace_path=None: old_hub)

    diary_all = mod._handle_diary({"wing": "proj", "last_n": 5})
    assert diary_all["mode"] == "all"
    assert diary_all["result"]["entries"][0]["content"] == long_diary
    assert ("mempalace_get_drawers", {"drawer_ids": ["d-old"]}) in old_hub.calls


def test_tool_list_drawers_supports_recent_sort_and_include_content(tmp_path, monkeypatch) -> None:
    """mempalace_list_drawers supports sort='recent' and include_content=True."""
    from mempalace import mcp_server
    from mempalace.config import MempalaceConfig
    from mempalace.palace import get_collection

    palace = str(tmp_path / "palace")
    col = get_collection(palace, create=True, backend="sqlite_exact")
    full_a = "Older verbatim memory " * 15
    full_b = "Newer verbatim memory " * 15
    col.add(
        ids=["old-1", "new-2"],
        documents=[full_a, full_b],
        metadatas=[
            {"wing": "proj", "room": "notes", "filed_at": "2026-01-01T00:00:00+00:00"},
            {"wing": "proj", "room": "notes", "filed_at": "2026-06-01T00:00:00+00:00"},
        ],
        embeddings=[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
    )
    monkeypatch.setattr(mcp_server, "_config", MempalaceConfig(palace_path=palace))
    monkeypatch.setattr(mcp_server, "_is_chroma_backend", lambda: False)
    monkeypatch.setattr(mcp_server, "_get_collection", lambda: col)

    listed = mcp_server.tool_list_drawers(
        wing="proj", sort="recent", include_content=True, limit=10
    )
    assert [d["drawer_id"] for d in listed["drawers"]] == ["new-2", "old-1"]
    assert listed["drawers"][0]["content"] == full_b
    assert listed["drawers"][1]["content"] == full_a


def test_lexical_strategy_rejects_media_or_code_task(tmp_path) -> None:
    """candidate_strategy='lexical' returns a clean error when combined with media/task search."""
    from mempalace.searcher import search_memories

    err = search_memories(
        "query",
        str(tmp_path / "palace"),
        candidate_strategy="lexical",
        include_media=True,
    )
    assert "error" in err
    assert "lexical" in err["error"]
