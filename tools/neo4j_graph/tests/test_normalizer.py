from mempalace_graph.models import MemoryRecord
from mempalace_graph.normalizer import neo4j_memory_payload, normalize_record, stable_id


def base_record() -> MemoryRecord:
    return MemoryRecord(
        id="",
        title=None,
        snippet=None,
        content="First line of content. More content.",
        wing="",
        room="",
        closet="",
        drawer="",
        people=["Alice", "alice"],
        topics=["Graph", "graph"],
        projects=[],
        tags=[],
        source_path="/tmp/kg.sqlite3",
        source_record_locator="sqlite:memories:rowid:1",
    )


def test_stable_id_generation() -> None:
    assert stable_id("a", "b", "c") == stable_id("a", "b", "c")


def test_normalizer_dedup_snippet_and_no_content_payload() -> None:
    record = normalize_record(base_record(), store_content=False, store_snippet=True, snippet_chars=10)
    assert record.id
    assert record.people == ["Alice"]
    assert record.snippet == "First line"
    assert record.content is None
    payload = neo4j_memory_payload(record)
    assert "content" not in payload
