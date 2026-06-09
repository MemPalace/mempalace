from mempalace_graph.models import MemoryRecord
from mempalace_graph.normalizer import neo4j_memory_payload, normalize_record


def test_no_duplication_default_payload() -> None:
    record = MemoryRecord(
        id="m",
        title="t",
        snippet=None,
        content="full secret content",
        wing="w",
        room="r",
        closet="c",
        drawer="d",
        source_path="s",
        source_record_locator="l",
    )
    normalized = normalize_record(record, store_content=False)
    payload = neo4j_memory_payload(normalized, store_content=False)
    assert "content" not in payload
    assert "text" not in payload
