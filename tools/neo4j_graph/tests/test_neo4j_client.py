import sys
from types import SimpleNamespace


class FakeTx:
    def __init__(self) -> None:
        self.query = ""
        self.params = {}

    def run(self, query: str, **params) -> None:
        self.query = query
        self.params = params


def test_upsert_memory_uses_cardinality_preserving_relationship_blocks() -> None:
    sys.modules.setdefault("neo4j", SimpleNamespace(GraphDatabase=SimpleNamespace(driver=lambda *_, **__: None)))
    from mempalace_graph.models import MemoryRecord
    from mempalace_graph.neo4j_client import Neo4jClient

    tx = FakeTx()
    record = MemoryRecord(
        id="m1",
        title="Title",
        snippet=None,
        content=None,
        wing="w",
        room="r",
        closet="c",
        drawer="d",
        source_path="/tmp/kg.sqlite3",
        source_record_locator="sqlite:memories:m1",
    )
    Neo4jClient._upsert_memory(tx, record, store_content=False)
    assert "UNWIND" not in tx.query
    assert "FOREACH (person IN $people |" in tx.query
    assert "FOREACH (topic IN $topics |" in tx.query
    assert "FOREACH (project IN $projects |" in tx.query
    assert "FOREACH (tag IN $tags |" in tx.query
    assert tx.params["people"] == []
