"""Structured CLI search preserves provenance and safe backend/hub routing."""

import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mempalace import cli, searcher, server_registry


@pytest.fixture
def json_cli(monkeypatch, tmp_path):
    palace = str(tmp_path / "palace")
    config = SimpleNamespace(
        palace_path=palace,
        collection_name="custom_drawers",
        search_config_fingerprint="fixture-config",
    )
    monkeypatch.setattr(cli, "MempalaceConfig", lambda **kwargs: config)
    for name in cli._SEARCH_OVERRIDE_ENV_VARS:
        # main() writes backend overrides directly; record even initially absent
        # keys so its changes are undone before the next test.
        monkeypatch.setenv(name, "")
    monkeypatch.delenv("MEMPALACE_MCP_HTTP_TOKEN", raising=False)
    monkeypatch.setenv("MEMPALACE_HUB_FORWARD", "0")
    monkeypatch.setattr(searcher, "resolve_backend_name", Mock(return_value="chroma"))
    monkeypatch.setattr(searcher, "_hnsw_capacity_diverged", Mock(return_value=False))
    return config


def _run_search(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["mempalace", "search", "needle", *arguments])
    cli.main()


def test_json_preserves_unicode_on_cp1252_stdout(monkeypatch):
    response = {
        "results": [{"text": "café, 日本語, 🙂", "added_by": "研究者", "origin": "agent_note"}]
    }
    buffer = io.BytesIO()
    with io.TextIOWrapper(buffer, encoding="cp1252") as stream:
        with monkeypatch.context() as patched:
            patched.setattr(sys, "stdout", stream)
            assert cli._print_search_json_result(response) is True
            stream.flush()
        assert json.loads(buffer.getvalue().decode("ascii")) == response


def test_json_parser_and_command_preserve_fields_and_forward_filters(json_cli, monkeypatch, capsys):
    response = {
        "query": "needle",
        "results": [
            {
                "drawer_id": "logical-note",
                "text": "  café, 日本語, 🙂\nunchanged.  ",
                "added_by": "custom-agent\nlabel",
                "origin": "agent_note",
            }
        ],
    }

    def search_with_diagnostic(**kwargs):
        print("backend diagnostic")
        return response

    api = Mock(side_effect=search_with_diagnostic)
    monkeypatch.setattr(searcher, "search_memories", api)
    monkeypatch.setattr(
        searcher, "search", Mock(side_effect=AssertionError("text renderer called"))
    )

    _run_search(
        monkeypatch,
        "--json",
        "--wing",
        "project",
        "--room",
        "decisions",
        "--results",
        "8",
        "--since",
        "2026-08-01",
        "--before",
        "2026-09-01",
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out) == response
    assert captured.err == "backend diagnostic\n"
    api.assert_called_once_with(
        query="needle",
        palace_path=json_cli.palace_path,
        wing="project",
        room="decisions",
        n_results=8,
        since="2026-08-01",
        before="2026-09-01",
        collection_name="custom_drawers",
        vector_disabled=False,
    )


def test_json_diverged_chroma_uses_sqlite_before_opening_collection(json_cli, monkeypatch, capsys):
    monkeypatch.setattr(searcher, "_hnsw_capacity_diverged", Mock(return_value=True))
    opener = Mock(side_effect=AssertionError("diverged Chroma opened"))
    monkeypatch.setattr(searcher, "get_collection", opener)
    response = {
        "results": [{"text": "lexical evidence", "added_by": "mcp", "origin": "agent_note"}],
        "fallback": "bm25_only_via_sqlite",
    }
    sqlite_search = Mock(return_value=response)
    monkeypatch.setattr(searcher, "_bm25_only_via_sqlite", sqlite_search)

    _run_search(monkeypatch, "--json")

    assert json.loads(capsys.readouterr().out) == response
    opener.assert_not_called()
    searcher._hnsw_capacity_diverged.assert_called_once_with(json_cli.palace_path)
    assert sqlite_search.call_args.kwargs["collection_name"] == "custom_drawers"


def test_json_explicit_non_chroma_backend_never_probes_hnsw(json_cli, monkeypatch, capsys):
    monkeypatch.setattr(searcher, "resolve_backend_name", Mock(return_value="sqlite_exact"))
    api = Mock(return_value={"results": []})
    monkeypatch.setattr(searcher, "search_memories", api)

    _run_search(monkeypatch, "--json", "--backend", "sqlite_exact")

    assert json.loads(capsys.readouterr().out) == {"results": []}
    assert os.environ["MEMPALACE_BACKEND_EXPLICIT"] == "sqlite_exact"
    searcher._hnsw_capacity_diverged.assert_not_called()
    assert api.call_args.kwargs["vector_disabled"] is False


@pytest.mark.parametrize(
    "arguments", [["--since", "invalid"], ["--since", "2026-09-01", "--before", "2026-08-01"]]
)
def test_json_invalid_date_is_structured_and_does_not_open_collection(
    json_cli, monkeypatch, capsys, arguments
):
    opener = Mock(side_effect=AssertionError("invalid window opened backend"))
    monkeypatch.setattr(searcher, "get_collection", opener)

    with pytest.raises(SystemExit) as exc:
        _run_search(monkeypatch, "--json", *arguments)

    assert exc.value.code == 1
    response = json.loads(capsys.readouterr().out)
    assert response["error"]
    assert response["results"] == []
    opener.assert_not_called()
    searcher._hnsw_capacity_diverged.assert_not_called()


def test_json_unknown_explicit_backend_is_structured_before_dispatch(json_cli, monkeypatch, capsys):
    api = Mock(side_effect=AssertionError("unknown backend dispatched"))
    monkeypatch.setattr(searcher, "search_memories", api)

    with pytest.raises(SystemExit) as exc:
        _run_search(monkeypatch, "--json", "--backend", "nonexistent-json-test-backend")

    assert exc.value.code == 1
    response = json.loads(capsys.readouterr().out)
    assert response["error"] == "Unknown backend"
    assert response["results"] == []
    api.assert_not_called()


def test_json_backend_resolution_error_defers_to_api_diagnostic(json_cli, monkeypatch, capsys):
    monkeypatch.setattr(searcher, "resolve_backend_name", Mock(side_effect=KeyError("bad backend")))
    response = {"error": "Unknown backend", "results": [], "details": "bad backend"}
    monkeypatch.setattr(searcher, "search_memories", Mock(return_value=response))

    with pytest.raises(SystemExit) as exc:
        _run_search(monkeypatch, "--json")

    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out) == response
    searcher._hnsw_capacity_diverged.assert_not_called()


def test_json_unexpected_search_failure_emits_one_error(json_cli, monkeypatch, capsys):
    monkeypatch.setattr(
        searcher, "search_memories", Mock(side_effect=RuntimeError("backend offline"))
    )

    with pytest.raises(SystemExit) as exc:
        _run_search(monkeypatch, "--json")

    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out) == {"error": "backend offline", "results": []}


def test_default_text_search_keeps_existing_renderer_and_arguments(json_cli, monkeypatch, capsys):
    renderer = Mock(side_effect=lambda **kwargs: print("existing CLI text"))
    monkeypatch.setattr(searcher, "search", renderer)
    monkeypatch.setattr(
        searcher, "search_memories", Mock(side_effect=AssertionError("JSON API called"))
    )

    _run_search(monkeypatch)

    assert capsys.readouterr().out == "existing CLI text\n"
    renderer.assert_called_once_with(
        query="needle",
        palace_path=json_cli.palace_path,
        wing=None,
        room=None,
        n_results=5,
        since=None,
        before=None,
    )


@pytest.fixture
def json_search_hub(json_cli, monkeypatch):
    state = {"result": {"results": []}, "rpc_error": None, "status": 200}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if state["status"] != 200:
                self.send_error(state["status"])
                return
            payload = {"jsonrpc": "2.0", "id": request["id"]}
            if state["rpc_error"] is not None:
                payload["error"] = state["rpc_error"]
            else:
                payload["result"] = {
                    "content": [{"type": "text", "text": json.dumps(state["result"])}]
                }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    info = {
        "pid": os.getpid(),
        "host": "127.0.0.1",
        "port": server.server_port,
        "scheme": "http",
        "capabilities": ["search_cli_compatible"],
        "search_config_fingerprint": json_cli.search_config_fingerprint,
    }
    monkeypatch.setattr(server_registry, "read_live_serverinfo", lambda palace_path: info)
    monkeypatch.setenv("MEMPALACE_HUB_FORWARD", "1")
    local_search = Mock(side_effect=AssertionError("accepted hub search retried locally"))
    monkeypatch.setattr(searcher, "search_memories", local_search)
    try:
        yield state, requests, local_search
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_json_hub_preserves_structured_response_and_request_settings(
    json_search_hub, monkeypatch, capsys
):
    state, requests, local_search = json_search_hub
    response = {
        "results": [
            {
                "drawer_id": "note",
                "text": "café\nexact",
                "added_by": "reviewer",
                "origin": "agent_note",
            }
        ],
        "vector_disabled": True,
    }
    state["result"] = response

    _run_search(
        monkeypatch, "--json", "--wing", "project", "--results", "8", "--since", "2026-08-01"
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out) == response
    assert "forwarding search to palace hub" in captured.err
    assert requests[0]["params"]["arguments"] == {
        "query": "needle",
        "limit": 8,
        "cli_compatible": False,
        "max_distance": 0.0,
        "wing": "project",
        "since": "2026-08-01",
    }
    local_search.assert_not_called()


@pytest.mark.parametrize("failure", ["tool", "rpc", "http", "text-only"])
def test_json_hub_failures_emit_error_without_local_retry(
    json_search_hub, monkeypatch, capsys, failure
):
    state, requests, local_search = json_search_hub
    if failure == "tool":
        state["result"] = {"error": "Backend mismatch"}
    elif failure == "rpc":
        state["rpc_error"] = {"code": -32000, "message": "search refused"}
    elif failure == "http":
        state["status"] = 500
    else:
        state["result"] = {"cli_output": "legacy text"}

    with pytest.raises(SystemExit) as exc:
        _run_search(monkeypatch, "--json")

    assert exc.value.code == 1
    response = json.loads(capsys.readouterr().out)
    assert response["error"]
    assert response["results"] == []
    assert len(requests) == 1
    local_search.assert_not_called()
