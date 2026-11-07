"""A palace that exists but cannot be read is not an empty graph."""

import pytest

import mempalace.palace_graph as palace_graph
from mempalace.config import MempalaceConfig
from mempalace.palace_graph import GraphUnavailable


def _config(path):
    return MempalaceConfig(config_dir=str(path.parent / "cfg"), palace_path=str(path))


def test_a_missing_palace_is_an_empty_graph(tmp_path):
    assert palace_graph._get_collection(_config(tmp_path / "missing")) is None


def test_a_palace_without_drawers_is_an_empty_graph(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    assert palace_graph._get_collection(_config(palace)) is None
    # A graph read creates nothing, not even an empty chroma.sqlite3.
    assert list(palace.iterdir()) == []


def test_any_other_open_failure_raises_graph_unavailable(tmp_path, monkeypatch):
    def locked(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(palace_graph, "_get_palace_collection", locked)
    with pytest.raises(GraphUnavailable, match="could not be read") as info:
        palace_graph._get_collection(_config(tmp_path))
    # The client-facing message names the palace, not the backend's error text.
    assert "locked" not in str(info.value)


def _drawer_rows(palace):
    import sqlite3

    db = sqlite3.connect(f"file:{palace / 'chroma.sqlite3'}?mode=ro", uri=True)
    try:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("embeddings", "embedding_metadata")
        }
    finally:
        db.close()


def test_graph_reads_change_no_drawer_rows(tmp_path, monkeypatch):
    # chromadb's own client rewrites bookkeeping pages and the HNSW files on
    # every open, and the pre-open safety fixes may stamp collection config,
    # so the file bytes can't be compared. The graph read must not pin HNSW
    # threads, and no drawer or drawer-metadata row may move.
    from mempalace.backends import chroma as chroma_backend, get_backend
    from mempalace.palace import get_collection

    palace = tmp_path / "palace"
    col = get_collection(str(palace), create=True, backend="chroma")
    col.upsert(
        ids=["d1", "d2"],
        documents=["alpha note", "beta note"],
        metadatas=[{"wing": "w1", "room": "r1"}, {"wing": "w2", "room": "r1"}],
    )
    get_backend("chroma").close_palace(str(palace))
    before = _drawer_rows(palace)

    pinned = []
    monkeypatch.setattr(chroma_backend, "_pin_hnsw_threads", pinned.append)
    config = _config(palace)
    for _ in range(2):
        graph_col = palace_graph._get_collection(config)
        palace_graph.invalidate_graph_cache()
        nodes, _ = palace_graph.build_graph(col=graph_col, config=config)
        assert nodes["r1"]["wings"] == ["w1", "w2"]
    get_backend("chroma").close_palace(str(palace))

    assert pinned == []
    assert _drawer_rows(palace) == before


def test_a_failed_drawer_read_raises_graph_unavailable(tmp_path):
    class Broken:
        def count(self):
            return 1

        def get(self, **kwargs):
            raise RuntimeError("Error loading hnsw index")

    palace_graph.invalidate_graph_cache()
    with pytest.raises(GraphUnavailable) as info:
        palace_graph.build_graph(col=Broken(), config=_config(tmp_path))
    assert "hnsw" not in str(info.value)


def test_build_graph_does_not_report_an_unreadable_palace_as_empty(tmp_path, monkeypatch):
    palace_graph.invalidate_graph_cache()
    monkeypatch.setattr(palace_graph, "_try_sqlite_nodes_edges", lambda config: None)

    def locked(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(palace_graph, "_get_palace_collection", locked)
    with pytest.raises(GraphUnavailable):
        palace_graph.build_graph(config=_config(tmp_path))


@pytest.mark.parametrize("tool", ["traverse", "find_tunnels"])
def test_mcp_graph_tools_answer_an_unreadable_palace_with_an_error(monkeypatch, tool):
    import mempalace.mcp_server as mcp_server

    def unavailable(*args, **kwargs):
        raise GraphUnavailable("/some/palace")

    monkeypatch.setattr(mcp_server, "_graph_sqlite_reader", lambda: object())
    monkeypatch.setattr(mcp_server, tool, unavailable)
    if tool == "traverse":
        result = mcp_server.tool_traverse_graph("room")
    else:
        result = mcp_server.tool_find_tunnels("wing_a", "wing_b")
    assert result == {"error": "palace at /some/palace could not be read"}


def test_mcp_create_tunnel_answers_an_unreadable_palace_with_an_error(monkeypatch):
    import mempalace.mcp_server as mcp_server

    def unavailable(*args, **kwargs):
        raise GraphUnavailable()

    monkeypatch.setattr(mcp_server, "create_tunnel", unavailable)
    result = mcp_server.tool_create_tunnel("wing_a", "room_a", "wing_b", "room_b")
    assert result == {"error": "palace could not be read"}
