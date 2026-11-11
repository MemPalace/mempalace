"""The tunnel tools read and write the palace the server was configured with.

Before #401 ``tool_create_tunnel`` checked rooms in the ambient default palace
(``MempalaceConfig()``), not the server's. When that palace held other drawers
the check refused rooms that exist in the server's palace; when it held none
the check passed rooms that exist nowhere.
"""

import json
import os

import chromadb
import pytest

from tests._mcp_server_helpers import _patch_mcp_server


def _drawer(palace, wing, room):
    client = chromadb.PersistentClient(path=str(palace))
    try:
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=[f"drawer_{wing}_{room}"],
            documents=[f"synthetic words in {wing}/{room}"],
            metadatas=[{"wing": wing, "room": room, "source_file": "s.md", "chunk_index": 0}],
        )
    finally:
        client.close()


@pytest.fixture
def server(monkeypatch, config, kg, tmp_path):
    from mempalace import mcp_server

    # Pin the server's palace before the ambient one moves, as ``--palace``
    # pins it for the life of a real server.
    monkeypatch.setattr(config, "_palace_path_override", config.palace_path)
    _patch_mcp_server(monkeypatch, config, kg)
    _drawer(config.palace_path, "shared", "source")
    _drawer(config.palace_path, "shared", "target")
    # A different, populated palace is the ambient default, as on a test
    # worker where an earlier test filed drawers through the default config.
    ambient = tmp_path / "ambient" / "palace"
    ambient.mkdir(parents=True)
    _drawer(ambient, "other", "elsewhere")
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(ambient))
    return mcp_server, ambient


def test_tunnel_tools_use_the_server_palace_not_the_ambient_default(server, config):
    mcp_server, ambient = server

    created = mcp_server.tool_create_tunnel("shared", "source", "shared", "target")
    assert "error" not in created, created
    missing = mcp_server.tool_create_tunnel("shared", "no-such-room", "shared", "target")
    assert missing == {"error": "Source room 'no-such-room' does not exist in wing 'shared'"}

    with open(config.tunnel_file, encoding="utf-8") as f:
        assert [t["id"] for t in json.load(f)] == [created["id"]]
    assert not os.path.exists(ambient.parent / "tunnels.json")
    assert [t["id"] for t in mcp_server.tool_list_tunnels("shared")] == [created["id"]]
    followed = mcp_server.tool_follow_tunnels("shared", "source", record=False)
    assert [c["tunnel_id"] for c in followed] == [created["id"]]

    assert mcp_server.tool_delete_tunnel(created["id"]) == {"deleted": created["id"]}
    assert mcp_server.tool_list_tunnels() == []


def test_graph_stats_fallback_counts_the_server_palace_tunnels(server, monkeypatch):
    mcp_server, ambient = server
    created = mcp_server.tool_create_tunnel("shared", "source", "shared", "target")
    assert "error" not in created, created
    # The grouped sqlite read is unavailable on other backends (qdrant,
    # pgvector, milvus) or after a read error; the client path answers then.
    monkeypatch.setattr(mcp_server, "_sqlite_graph_stats", lambda: None)
    assert mcp_server.tool_graph_stats()["explicit_tunnels"] == 1
