"""Tests for mine --force flag (PR #1344)."""
import os
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
    (d / "readme.md").write_text("# Test\n\nContent.\n" * 10)
    (d / "code.py").write_text("x = 1\n" * 30)
    return d


class TestForceClean:
    def test_deletes_all_drawers_for_directory(self, palace, project):
        from mempalace.miner import mine
        from mempalace.cli import _force_clean
        from mempalace.palace import get_collection

        mine(project_dir=str(project), palace_path=palace, agent="test")
        col = get_collection(palace)
        before = col.count()
        assert before > 0

        deleted = _force_clean(palace, str(project))
        assert deleted == before
        assert col.count() == 0

    def test_path_boundary_isolation(self, palace, project, tmp_path):
        """_force_clean('/proj') must not delete drawers from '/proj_other'."""
        from mempalace.miner import mine
        from mempalace.cli import _force_clean
        from mempalace.palace import get_collection

        other = tmp_path / "proj_other"
        other.mkdir()
        (other / "mempalace.yaml").write_text(
            "wing: other\nrooms:\n  - name: general\n    keywords: []\n"
        )
        (other / "data.txt").write_text("other project data\n" * 30)

        mine(project_dir=str(project), palace_path=palace, agent="test")
        mine(project_dir=str(other), palace_path=palace, agent="test")
        col = get_collection(palace)
        total = col.count()

        _force_clean(palace, str(project))
        after = col.count()
        assert 0 < after < total, "Should have deleted only one project's drawers"

    def test_force_remine_updates_hashes(self, palace, project):
        from mempalace.miner import mine, file_content_hash
        from mempalace.cli import _force_clean
        from mempalace.palace import get_collection

        mine(project_dir=str(project), palace_path=palace, agent="test")

        (project / "readme.md").write_text("# Rewritten\n" * 20)
        new_hash = file_content_hash(project / "readme.md")

        _force_clean(palace, str(project))
        mine(project_dir=str(project), palace_path=palace, agent="test")

        col = get_collection(palace)
        result = col.get(include=["metadatas"])
        readme_metas = [m for m in result["metadatas"] if "readme.md" in m.get("source_file", "")]
        assert all(m["content_hash"] == new_hash for m in readme_metas)

    def test_force_clean_empty_palace(self, palace):
        from mempalace.cli import _force_clean

        deleted = _force_clean(palace, "/nonexistent/path")
        assert deleted == 0
