import sqlite3
from pathlib import Path

from mempalace_graph.content_resolver import resolve_content


def test_content_resolver_sqlite_locator(tmp_path: Path) -> None:
    db = tmp_path / "kg.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, title TEXT, content TEXT, metadata TEXT)")
    conn.execute("INSERT INTO memories VALUES ('m1', 'Title', 'Full content', '{\"wing\":\"w\"}')")
    conn.commit()
    conn.close()
    resolved = resolve_content(str(db), "sqlite:memories:m1")
    assert resolved.content == "Full content"
    assert resolved.metadata["wing"] == "w"


def test_content_resolver_jsonl_offset(tmp_path: Path) -> None:
    wal = tmp_path / "write_log.jsonl"
    wal.write_text('{"operation":"add","content":"Full"}\n')
    resolved = resolve_content(str(wal), "jsonl:offset:0")
    assert '"content": "Full"' in resolved.content
