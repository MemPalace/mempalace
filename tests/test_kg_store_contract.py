from mempalace.kg_store import BaseKGStore
from mempalace.knowledge_graph import KnowledgeGraph


def test_sqlite_backend_name():
    assert KnowledgeGraph.name == "sqlite"
    assert issubclass(
        KnowledgeGraph, __import__("mempalace.kg_store", fromlist=["BaseKGStore"]).BaseKGStore
    )


def test_knowledge_graph_is_a_kg_store(tmp_path):
    kg = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))
    assert isinstance(kg, BaseKGStore)
    for m in (
        "add_entity",
        "add_triple",
        "query_entity",
        "query_relationship",
        "invalidate",
        "seed_from_entity_facts",
        "timeline",
        "stats",
        "close",
    ):
        assert callable(getattr(kg, m))
    kg.close()
