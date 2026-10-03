"""Real producer writes retain verbatim content and expose filing provenance."""

from copy import deepcopy
import json

import pytest

from _mcp_server_helpers import _patch_mcp_server
from mempalace.searcher import search_memories


def _assert_written_provenance(
    collection, palace_path, documents, *, origin, writer, wing=None, source_file=None
):
    """Check physical storage, then public search, without altering either."""
    before = collection.get(include=["documents", "metadatas"])
    ordered = sorted(
        zip(before["documents"], before["metadatas"]),
        key=lambda row: row[1].get("chunk_index", 0),
    )
    assert [doc for doc, _ in ordered] == documents
    assert all(meta["origin"] == origin for _, meta in ordered)
    snapshot = deepcopy(before)

    found = search_memories(
        "verbatim provenance café",
        palace_path,
        wing=wing,
        source_file=source_file,
        n_results=len(documents) + 1,
    )
    assert "error" not in found, found
    assert found["results"], found
    assert all(hit["origin"] == origin for hit in found["results"])
    assert all(hit["added_by"] == writer for hit in found["results"])
    assert collection.get(include=["documents", "metadatas"]) == snapshot
    return ordered


def test_project_writer_stamps_origin_and_retains_file_text(collection, palace_path, tmp_path):
    from mempalace.miner import process_file

    content = (
        "# verbatim provenance café — projeto 東京\n"
        "def preserve_words():\n"
        "    return 'Authorship is separate from who filed this memory.'"
    )
    source = tmp_path / "app.py"
    source.write_text(content, encoding="utf-8")

    count, _, skipped = process_file(
        source, tmp_path, collection, "project", [], "FileIndexer", False
    )
    assert count == 1
    assert skipped is None
    _assert_written_provenance(
        collection,
        palace_path,
        [content],
        origin="project_file",
        writer="FileIndexer",
        source_file=str(source),
    )
    assert source.read_text(encoding="utf-8") == content


def test_format_writer_preserves_extracted_chunks(collection, palace_path, tmp_path):
    from mempalace.format_miner import _file_chunks_locked

    source = tmp_path / "attachment.pdf"
    source.write_bytes(b"isolated conversion input")
    documents = [
        "Extracted verbatim provenance café — primeira página 東京.",
        "Second extracted page preserves punctuation: [quoted] `literal`.",
    ]
    chunks = [{"chunk_index": i, "content": text} for i, text in enumerate(documents)]
    count, skipped = _file_chunks_locked(
        collection,
        str(source),
        chunks,
        "documents",
        "reports",
        "FormatIndexer",
        source_mtime=source.stat().st_mtime,
        content="\n".join(documents),
    )
    assert count == 2
    assert skipped is False
    _assert_written_provenance(
        collection,
        palace_path,
        documents,
        origin="project_file",
        writer="FormatIndexer",
        source_file=str(source),
    )
    assert source.read_bytes() == b"isolated conversion input"


def test_live_conversation_writer_preserves_exchange(collection, palace_path, tmp_path):
    from mempalace.convo_miner import file_conversation_exchange

    source = tmp_path / "active-session.jsonl"
    source.write_text("isolated session input", encoding="utf-8")
    content = "USER: verbatim provenance café 東京\nASSISTANT: Keep these exact words."
    drawer_id = file_conversation_exchange(
        collection,
        wing="conversations",
        room="today",
        text=content,
        source_file=str(source),
        agent="ExchangeIndexer",
        extra_metadata={"origin": "agent_note", "added_by": "other"},
    )
    assert drawer_id
    _assert_written_provenance(
        collection,
        palace_path,
        [content],
        origin="conversation",
        writer="ExchangeIndexer",
        source_file=str(source),
    )


def test_historical_conversation_writer_preserves_chunks(collection, palace_path, tmp_path):
    from mempalace.convo_miner import _file_chunks_locked

    source = tmp_path / "history.txt"
    documents = [
        "USER: verbatim provenance café 東京\nASSISTANT: Preserve the first exchange.",
        "USER: Another exchange?\nASSISTANT: Preserve the second exchange exactly.",
    ]
    source.write_text("\n\n".join(documents), encoding="utf-8")
    chunks = [{"chunk_index": i, "content": text} for i, text in enumerate(documents)]
    count, _, skipped = _file_chunks_locked(
        collection,
        str(source),
        chunks,
        "conversations",
        "history",
        "HistoryIndexer",
        "exchange",
        authored_at="2025-01-02T03:04:05",
    )
    assert count == 2
    assert skipped is False
    ordered = _assert_written_provenance(
        collection,
        palace_path,
        documents,
        origin="conversation",
        writer="HistoryIndexer",
        source_file=str(source),
    )
    assert all(meta["authored_at"] == "2025-01-02T03:04:05" for _, meta in ordered)


@pytest.mark.parametrize("chunked", [False, True], ids=["single", "chunked"])
def test_manual_note_with_source_is_agent_note(
    monkeypatch, config, kg, collection, palace_path, tmp_path, chunked
):
    from mempalace.mcp_server import tool_add_drawer, tool_get_drawer

    _patch_mcp_server(monkeypatch, config, kg)
    config._file_config["chunk_size"] = 180
    text = "verbatim provenance café 東京\nManual filing: [owner] `original` words.\n"
    content = text * (7 if chunked else 1)
    source = tmp_path / "referenced.py"
    source.write_text("A reference is not proof that this note was mined.", encoding="utf-8")

    written = tool_add_drawer("notes", "decisions", content, str(source), "Observer.Á")
    assert written["success"] is True, written
    assert (written["chunks"] > 1) is chunked
    fetched = tool_get_drawer(written["drawer_id"])
    assert fetched["content"] == content
    documents = [
        content[i : i + config.chunk_size] for i in range(0, len(content), config.chunk_size)
    ]
    _assert_written_provenance(
        collection,
        palace_path,
        documents,
        origin="agent_note",
        writer="Observer.Á",
        source_file=str(source),
    )


@pytest.mark.parametrize("chunked", [False, True], ids=["single", "chunked"])
def test_diary_writer_exposes_agent_fallback_without_rewriting_entry(
    monkeypatch, config, kg, collection, palace_path, chunked
):
    from mempalace.mcp_server import tool_diary_write, tool_get_drawer

    _patch_mcp_server(monkeypatch, config, kg)
    config._file_config["chunk_size"] = 180
    text = "verbatim provenance café 東京\nDiary observation preserves [brackets] and `code`.\n"
    entry = text * (7 if chunked else 1)

    written = tool_diary_write("TestAgent", entry, topic="observations")
    assert written["success"] is True, written
    assert (written["chunks"] > 1) is chunked
    fetched = tool_get_drawer(written["entry_id"])
    assert fetched["content"] == entry
    documents = [entry[i : i + config.chunk_size] for i in range(0, len(entry), config.chunk_size)]
    ordered = _assert_written_provenance(
        collection,
        palace_path,
        documents,
        origin="diary",
        writer="testagent",
        wing="wing_testagent",
    )
    assert all("added_by" not in meta and meta["agent"] == "testagent" for _, meta in ordered)


def test_sweeper_stamps_conversation_without_inventing_writer(collection, palace_path, tmp_path):
    from mempalace.sweeper import sweep

    content = "verbatim provenance café 東京\nThe speaker is not a filing identity."
    record = {
        "type": "user",
        "timestamp": "2026-01-02T03:04:05Z",
        "sessionId": "provenance-session",
        "uuid": "provenance-message",
        "message": {"role": "user", "content": content},
    }
    source = tmp_path / "session.jsonl"
    source.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    written = sweep(str(source), palace_path)
    assert written["drawers_added"] == 1
    ordered = _assert_written_provenance(
        collection,
        palace_path,
        [f"USER: {content}"],
        origin="conversation",
        writer=None,
        source_file=str(source),
    )
    assert "added_by" not in ordered[0][1]


def test_diary_ingest_stamps_diary_without_inventing_writer(collection, palace_path, tmp_path):
    from mempalace.diary_ingest import ingest_diaries

    diary_dir = tmp_path / "diaries"
    diary_dir.mkdir()
    source = diary_dir / "2026-01-02.md"
    content = "## 10:00 — observation\nverbatim provenance café 東京; preserve this original entry."
    source.write_text(content, encoding="utf-8")

    written = ingest_diaries(diary_dir, palace_path)
    assert written["days_updated"] == 1
    ordered = _assert_written_provenance(
        collection,
        palace_path,
        [content],
        origin="diary",
        writer=None,
        source_file=str(source),
    )
    assert "added_by" not in ordered[0][1]
    assert "agent" not in ordered[0][1]
    assert source.read_text(encoding="utf-8") == content
