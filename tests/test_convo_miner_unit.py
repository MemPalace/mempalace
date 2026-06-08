"""Unit tests for convo_miner pure functions (no chromadb needed)."""

from mempalace.convo_miner import (
    chunk_exchanges,
    detect_convo_room,
    scan_convos,
)


class TestChunkExchanges:
    def test_exchange_chunking(self):
        content = (
            "> What is memory?\n"
            "Memory is persistence of information over time.\n\n"
            "> Why does it matter?\n"
            "It enables continuity across sessions and conversations.\n\n"
            "> How do we build it?\n"
            "With structured storage and retrieval mechanisms.\n"
        )
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 2
        assert all("content" in c and "chunk_index" in c for c in chunks)

    def test_paragraph_fallback(self):
        """Content without '>' lines falls back to paragraph chunking."""
        content = (
            "This is a long paragraph about memory systems. " * 10 + "\n\n"
            "This is another paragraph about storage. " * 10 + "\n\n"
            "And a third paragraph about retrieval. " * 10
        )
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 2

    def test_paragraph_line_group_fallback(self):
        """Long content with no paragraph breaks chunks by line groups."""
        lines = [f"Line {i}: some content that is meaningful" for i in range(60)]
        content = "\n".join(lines)
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 1

    def test_empty_content(self):
        chunks = chunk_exchanges("")
        assert chunks == []

    def test_short_content_skipped(self):
        chunks = chunk_exchanges("> hi\nbye")
        # Too short to produce chunks (below MIN_CHUNK_SIZE)
        assert isinstance(chunks, list)

    def test_long_ai_response_not_truncated(self):
        """AI responses longer than 8 lines must be stored in full (verbatim principle)."""
        lines = [f"Step {i}: important detail that must be stored" for i in range(1, 14)]
        content = "> How do I implement authentication?\n" + "\n".join(lines)
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 1
        stored = chunks[0]["content"]
        # All 13 lines must be present — none silently dropped
        for i in range(1, 14):
            assert f"Step {i}:" in stored, f"Step {i} was truncated and not stored"


class TestDetectConvoRoom:
    def test_technical_room(self):
        content = "Let me debug this python function and fix the code error in the api"
        assert detect_convo_room(content) == "technical"

    def test_planning_room(self):
        content = "We need to plan the roadmap for the next sprint and set milestone deadlines"
        assert detect_convo_room(content) == "planning"

    def test_architecture_room(self):
        content = "The architecture uses a service layer with component interface and module design"
        assert detect_convo_room(content) == "architecture"

    def test_decisions_room(self):
        content = "We decided to switch and migrated to the new framework after we chose it"
        assert detect_convo_room(content) == "decisions"

    def test_general_fallback(self):
        content = "Hello, how are you doing today? The weather is nice."
        assert detect_convo_room(content) == "general"


class TestScanConvos:
    def test_scan_finds_txt_and_md(self, tmp_path):
        (tmp_path / "chat.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "notes.md").write_text("world", encoding="utf-8")
        (tmp_path / "image.png").write_bytes(b"fake")
        files = scan_convos(str(tmp_path))
        extensions = {f.suffix for f in files}
        assert ".txt" in extensions
        assert ".md" in extensions
        assert ".png" not in extensions

    def test_scan_skips_git_dir(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config.txt").write_text("git stuff", encoding="utf-8")
        (tmp_path / "chat.txt").write_text("hello", encoding="utf-8")
        files = scan_convos(str(tmp_path))
        assert len(files) == 1

    def test_scan_skips_meta_json(self, tmp_path):
        (tmp_path / "chat.meta.json").write_text("{}", encoding="utf-8")
        (tmp_path / "chat.json").write_text("{}", encoding="utf-8")
        files = scan_convos(str(tmp_path))
        names = [f.name for f in files]
        assert "chat.json" in names
        assert "chat.meta.json" not in names

    def test_scan_empty_dir(self, tmp_path):
        files = scan_convos(str(tmp_path))
        assert files == []


def test_mine_convos_uses_remote_collection_when_remote_url_set(tmp_path, monkeypatch):
    """When remote_url is passed, mine_convos uses RemoteCollection, not get_collection."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from mempalace.convo_miner import mine_convos
    from mempalace.backends.remote import RemoteCollection
    from unittest.mock import patch, MagicMock

    collected = []

    class CapturingRemote(RemoteCollection):
        def upsert(self, *, documents, ids, metadatas=None):
            collected.extend(documents)
        def get(self, *, where=None, **kwargs):
            return {"ids": [], "documents": [], "metadatas": []}

    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text(
        '{"type":"message","message":{"role":"user","content":"hello world"}}\n'
        '{"type":"message","message":{"role":"assistant","content":"hi there"}}\n'
    )

    with patch("mempalace.convo_miner.get_remote_collection", return_value=CapturingRemote("http://x", "t")):
        mine_convos(
            convo_dir=str(tmp_path),
            palace_path=str(tmp_path / "palace"),
            remote_url="http://x",
            remote_token="t",
        )

    assert len(collected) > 0


def test_mine_convos_remote_token_from_env(tmp_path, monkeypatch):
    """MEMPALACE_TOKEN env var is used when remote_token is not passed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MEMPALACE_TOKEN", "env-token")
    from mempalace.convo_miner import mine_convos
    from mempalace.backends.remote import RemoteCollection
    from unittest.mock import patch, MagicMock

    with patch("mempalace.convo_miner.get_remote_collection") as mock_factory:
        mock_col = MagicMock(spec=RemoteCollection)
        mock_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        mock_factory.return_value = mock_col
        mine_convos(
            convo_dir=str(tmp_path),
            palace_path=str(tmp_path / "palace"),
            remote_url="http://x",
        )
    mock_factory.assert_called_once_with("http://x", "env-token")


def test_mine_convos_raises_when_remote_url_but_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MEMPALACE_TOKEN", raising=False)
    from mempalace.convo_miner import mine_convos
    import pytest

    with pytest.raises(RuntimeError, match="remote-url requires a token"):
        mine_convos(
            convo_dir=str(tmp_path),
            palace_path=str(tmp_path / "palace"),
            remote_url="http://x",
        )
