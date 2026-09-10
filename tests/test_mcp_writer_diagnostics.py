"""Writer setup failures must not impersonate observed lease contention."""

import json

import pytest

from mempalace import mcp_server as mcp, palace


@pytest.fixture
def isolated_writer(monkeypatch, tmp_path):
    from mempalace.config import MempalaceConfig

    monkeypatch.setattr(mcp, "_config", MempalaceConfig(palace_path=tmp_path / "palace"))
    for name, value in (
        ("_MCP_WRITER_LOCK_CM", None),
        ("_MCP_WRITER_READ_ONLY", False),
        ("_MCP_WRITER_LOCK_FAILED", False),
        ("_MCP_WRITER_LOCK_ERROR", ""),
    ):
        monkeypatch.setattr(mcp, name, value)
    monkeypatch.setattr(mcp, "_discard_mcp_storage_handles", lambda: None)
    monkeypatch.delenv(mcp._MCP_ALLOW_PEER_WRITER_ENV, raising=False)
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    monkeypatch.delenv(palace._EXPLICIT_BACKEND_ENV, raising=False)
    yield tmp_path
    mcp._release_mcp_writer_lock()


def test_unavailable_backend_refusal_and_recovery(isolated_writer, monkeypatch):
    monkeypatch.setenv("MEMPALACE_BACKEND", "unregistered_test_backend")
    result = mcp._mcp_peer_writer_refusal(1, "mempalace_add_drawer")
    error = result["error"]
    assert "Peer MCP writer active" not in error["message"]
    assert "unregistered_test_backend" in error["data"]["reason"]
    assert "sqlite_exact" in error["data"]["reason"]
    assert "restart" in error["data"]["reason"]
    assert not (isolated_writer / "palace").exists()
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    assert mcp._mcp_peer_writer_refusal(2, "mempalace_add_drawer") is None
    assert mcp._MCP_WRITER_LOCK_CM is not None


def test_contention_then_setup_failure_has_fresh_diagnostic(isolated_writer, monkeypatch):
    def busy(*args, **kwargs):
        raise palace.MineAlreadyRunning("synthetic peer")

    monkeypatch.setattr(palace, "mine_palace_lock", busy)
    result = mcp._mcp_peer_writer_refusal(1, "mempalace_add_drawer")
    assert "Peer MCP writer active" in result["error"]["message"]
    monkeypatch.setenv("MEMPALACE_BACKEND", "unregistered_test_backend")
    result = mcp._mcp_peer_writer_refusal(2, "mempalace_add_drawer")
    assert "Peer MCP writer active" not in result["error"]["message"]
    assert not mcp._MCP_WRITER_READ_ONLY

    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    result = mcp._mcp_peer_writer_refusal(3, "mempalace_add_drawer")
    assert result["error"]["data"]["failure_kind"] == "peer_contention"
    assert not mcp._MCP_WRITER_LOCK_FAILED


def test_lock_setup_error_does_not_claim_contention(isolated_writer, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("synthetic permission failure")

    monkeypatch.setattr(palace, "mine_palace_lock", denied)
    result = mcp._mcp_peer_writer_refusal(1, "mempalace_add_drawer")
    assert result["error"]["data"]["failure_kind"] == "initialization_failed"
    assert "synthetic permission failure" in result["error"]["data"]["reason"]
    assert "Peer MCP writer active" not in result["error"]["message"]


def test_light_dispatch_writes_and_reads_after_config_repair(isolated_writer, monkeypatch, kg):
    from mempalace import mcp_light_server
    from mempalace.backends import embedding_wrapper

    monkeypatch.setattr(mcp, "_get_kg", lambda *a, **kw: kg)
    monkeypatch.setattr(mcp, "_READ_ONLY", False)
    monkeypatch.setattr(mcp, "_vector_disabled", False)
    monkeypatch.setattr(mcp, "_hub_proxy_target", lambda: None)
    for name in (
        "_collection_cache",
        "_client_cache",
        "_collection_cache_backend",
        "_collection_cache_palace",
        "_collection_open_error",
    ):
        monkeypatch.setattr(mcp, name, None)
    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts: [[1.0, 0.0] for _ in texts]
    )

    def call(name, arguments):
        return mcp_light_server.dispatch_light_stdio_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    args = {
        "action": "add_drawer",
        "wing": "synthetic",
        "room": "verification",
        "content": "Synthetic durable write verification.",
    }
    monkeypatch.setenv("MEMPALACE_BACKEND", "unregistered_test_backend")
    refused = call("palace_exec", args)
    assert refused["error"]["data"]["failure_kind"] == "initialization_failed"
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    written = call("palace_exec", args)
    result = json.loads(written["result"]["content"][0]["text"])
    assert result["success"], result
    read = call("palace_query", {"target": "drawer", "drawer_id": result["drawer_id"]})
    drawer = json.loads(read["result"]["content"][0]["text"])
    assert drawer["content"] == args["content"]


def test_light_hub_forward_does_not_acquire_local_writer(isolated_writer, monkeypatch):
    from mempalace import mcp_light_server

    monkeypatch.setattr(mcp, "_hub_proxy_target", lambda: ("http://127.0.0.1:9", {}))
    monkeypatch.setattr(
        mcp,
        "_acquire_mcp_writer_lock",
        lambda: pytest.fail("Client must not acquire the hub's writer lease"),
    )
    monkeypatch.setattr(
        mcp,
        "_forward_request_to_hub",
        lambda *a: {"jsonrpc": "2.0", "id": 1, "result": {"content": []}},
    )
    result = mcp_light_server.dispatch_light_stdio_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "palace_exec",
                "arguments": {
                    "action": "add_drawer",
                    "wing": "synthetic",
                    "room": "verification",
                    "content": "Synthetic",
                },
            },
        }
    )
    assert "result" in result
