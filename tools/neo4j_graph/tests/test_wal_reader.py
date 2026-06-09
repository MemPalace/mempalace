from pathlib import Path

from mempalace_graph.mempalace_wal_reader import read_wal_events


def test_wal_parsing_and_invalid_line(tmp_path: Path) -> None:
    wal = tmp_path / "write_log.jsonl"
    wal.write_text('{"timestamp":"2026-01-01","operation":"add_drawer","params":{"drawer_id":"d1"}}\n{bad\n')
    events, offset, errors = read_wal_events(wal)
    assert len(events) == 1
    assert events[0].record_id == "d1"
    assert offset == wal.stat().st_size
    assert errors
