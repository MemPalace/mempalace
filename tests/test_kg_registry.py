import pytest
from mempalace.kg_store import get_kg_store, register_kg_backend, BaseKGStore
from mempalace.knowledge_graph import KnowledgeGraph


def test_default_is_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPALACE_KG_BACKEND", raising=False)
    s = get_kg_store(db_path=str(tmp_path / "kg.sqlite3"))
    assert isinstance(s, KnowledgeGraph)
    s.close()


def test_register_rejects_non_subclass():
    with pytest.raises(TypeError):
        register_kg_backend("x", object)


def test_env_selects_backend(tmp_path, monkeypatch):
    class FakeKG(BaseKGStore):
        name = "fake"

        def __init__(self, **kw):
            pass

        def add_entity(self, *a, **k):
            return "e"

        def add_triple(self, *a, **k):
            return "t"

        def query_entity(self, *a, **k):
            return []

        def query_relationship(self, *a, **k):
            return []

        def invalidate(self, *a, **k):
            return None

        def seed_from_entity_facts(self, *a, **k):
            return None

        def timeline(self, *a, **k):
            return []

        def stats(self, *a, **k):
            return {}

        def close(self):
            return None

    register_kg_backend("fake", FakeKG)
    monkeypatch.setenv("MEMPALACE_KG_BACKEND", "fake")
    assert isinstance(get_kg_store(db_path=str(tmp_path / "x")), FakeKG)
