"""Tests for content_hash foundation (PR #1343)."""
import hashlib
import os
import tempfile
from pathlib import Path

import chromadb
import pytest


@pytest.fixture
def palace(tmp_path):
    p = str(tmp_path / "palace")
    os.makedirs(p)
    client = chromadb.PersistentClient(path=p)
    client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
    client.get_or_create_collection("mempalace_closets", metadata={"hnsw:space": "cosine"})
    del client
    return p


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "mempalace.yaml").write_text(
        "wing: test\nrooms:\n  - name: general\n    keywords: []\n"
    )
    (d / "readme.md").write_text("# Test\n\nContent for hashing.\n" * 10)
    return d


class TestFileContentHash:
    def test_deterministic(self, project):
        from mempalace.miner import file_content_hash

        h1 = file_content_hash(project / "readme.md")
        h2 = file_content_hash(project / "readme.md")
        assert h1 == h2
        assert len(h1) == 32

    def test_changes_with_content(self, project):
        from mempalace.miner import file_content_hash

        h1 = file_content_hash(project / "readme.md")
        (project / "readme.md").write_text("completely different\n" * 10)
        h2 = file_content_hash(project / "readme.md")
        assert h1 != h2

    def test_strips_whitespace(self, tmp_path):
        from mempalace.miner import file_content_hash

        f = tmp_path / "padded.txt"
        f.write_text("  hello world  \n\n\n")
        h1 = file_content_hash(f)
        f.write_text("hello world")
        h2 = file_content_hash(f)
        assert h1 == h2, "Whitespace-only differences should produce same hash"


class TestMineStoresHash:
    def test_project_mine_stores_content_hash(self, palace, project):
        from mempalace.miner import mine
        from mempalace.palace import get_collection

        mine(project_dir=str(project), palace_path=palace, agent="test")
        col = get_collection(palace)
        result = col.get(include=["metadatas"])
        assert len(result["ids"]) > 0
        for meta in result["metadatas"]:
            assert "content_hash" in meta
            assert len(meta["content_hash"]) == 32

    def test_stored_hash_matches_computed(self, palace, project):
        from mempalace.miner import mine, file_content_hash
        from mempalace.palace import get_collection

        mine(project_dir=str(project), palace_path=palace, agent="test")
        col = get_collection(palace)
        result = col.get(include=["metadatas"])
        for meta in result["metadatas"]:
            sf = meta.get("source_file", "")
            if sf and os.path.exists(sf):
                assert meta["content_hash"] == file_content_hash(Path(sf))


class TestAddDrawerFallbackHash:
    def test_no_file_hash_falls_back_to_content_hash(self, palace):
        from mempalace.miner import add_drawer
        from mempalace.palace import get_collection

        col = get_collection(palace)
        content = "MCP-created drawer with no source file on disk"
        add_drawer(col, "w", "r", content, "virtual://mcp", 0, "test")
        result = col.get(include=["metadatas"])
        expected = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()
        assert result["metadatas"][0]["content_hash"] == expected

    def test_explicit_hash_takes_precedence(self, palace):
        """_build_drawer_metadata accepts content_hash and stores it."""
        from mempalace.miner import _build_drawer_metadata

        meta = _build_drawer_metadata("w", "r", "f.txt", 0, "test", "content", None, "abc123")
        assert meta["content_hash"] == "abc123"
