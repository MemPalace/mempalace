import mempalace.mcp_server as mcp


def test_get_kg_uses_factory(monkeypatch, tmp_path):
    called = {}

    def fake_factory(db_path=None, **kw):
        called["db_path"] = db_path
        from mempalace.knowledge_graph import KnowledgeGraph

        return KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))

    monkeypatch.setattr("mempalace.kg_store.get_kg_store", fake_factory)
    mcp._kg_by_path.clear()
    kg = mcp._get_kg()
    assert "db_path" in called
    kg.close()
