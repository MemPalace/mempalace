import os
import tempfile
import shutil
from pathlib import Path
from collections import defaultdict
from unittest.mock import MagicMock, patch

import pytest


# ============ Fixtures ============

@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with a few commits."""
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()

    # Initialize repo
    os.system(f"cd {repo_path} && git init --quiet 2>/dev/null")
    os.system(f"cd {repo_path} && git config user.email 'test@test.com'")
    os.system(f"cd {repo_path} && git config user.name 'Test User'")

    # Create a file and commit
    (repo_path / "main.py").write_text("print('hello')")
    os.system(f"cd {repo_path} && git add . && git commit --quiet -m 'Initial commit' 2>/dev/null")

    # Create another branch with changes
    os.system(f"cd {repo_path} && git checkout -b feature --quiet 2>/dev/null")
    (repo_path / "utils.py").write_text("def helper(): pass")
    os.system(f"cd {repo_path} && git add . && git commit --quiet -m 'Add utils' 2>/dev/null")

    # Go back to master/main
    os.system(f"cd {repo_path} && git checkout master --quiet 2>/dev/null || git checkout main --quiet 2>/dev/null")

    return repo_path


# ============ Test DetectRoomFromFiles ============

class TestDetectRoomFromFiles:
    def test_python_files(self):
        from mempalace.git_miner import detect_room_from_files

        files = ["main.py", "utils.py", "tests/test_main.py"]
        assert detect_room_from_files(files) == "code"

    def test_markdown_files(self):
        from mempalace.git_miner import detect_room_from_files

        files = ["README.md", "docs/api.md"]
        assert detect_room_from_files(files) == "docs"

    def test_config_files(self):
        from mempalace.git_miner import detect_room_from_files

        files = ["config.yaml", "settings.yml"]
        assert detect_room_from_files(files) == "config"

    def test_mixed_files_python_dominates(self):
        from mempalace.git_miner import detect_room_from_files

        files = ["main.py", "utils.py", "README.md", "config.yaml"]
        assert detect_room_from_files(files) == "code"

    def test_mixed_files_markdown_dominates(self):
        from mempalace.git_miner import detect_room_from_files

        files = ["README.md", "docs/api.md", "docs/setup.md", "main.py"]
        assert detect_room_from_files(files) == "docs"

    def test_empty_list(self):
        from mempalace.git_miner import detect_room_from_files

        assert detect_room_from_files([]) == "general"

    def test_nested_paths_python(self):
        from mempalace.git_miner import detect_room_from_files

        files = ["src/main.py", "src/utils/helpers.py"]
        # Should detect .py extension and return "code"
        assert detect_room_from_files(files) == "code"

    def test_fallback_when_single_file(self):
        from mempalace.git_miner import detect_room_from_files

        files = ["some_random_file.xyz"]
        assert detect_room_from_files(files) == "general"

    def test_database_files(self):
        from mempalace.git_miner import detect_room_from_files

        files = ["migrations/001_init.sql"]
        assert detect_room_from_files(files) == "database"

    def test_styles_files(self):
        from mempalace.git_miner import detect_room_from_files

        files = ["styles/main.css", "styles/theme.scss"]
        assert detect_room_from_files(files) == "styles"


# ============ Test Git Functions With Repo ============

class TestGitMinerWithRepo:
    def test_get_branches(self, git_repo):
        from mempalace.git_miner import get_branches

        branches = get_branches(git_repo)
        assert len(branches) >= 2
        assert any(b in branches for b in ["master", "main"])
        assert "feature" in branches

    def test_get_all_commits(self, git_repo):
        from mempalace.git_miner import get_all_commits

        commits = get_all_commits(git_repo)
        assert len(commits) >= 1
        assert all("hash" in c for c in commits)
        assert all("subject" in c for c in commits)

    def test_get_all_commits_specific_branch(self, git_repo):
        from mempalace.git_miner import get_all_commits

        commits = get_all_commits(git_repo, "feature")
        assert len(commits) >= 2  # Has both commits

    def test_get_commit_files_changed(self, git_repo):
        from mempalace.git_miner import get_commit_files_changed, get_all_commits

        commits = get_all_commits(git_repo, "feature")
        # Find the commit that added utils.py
        for commit in commits:
            if "Add utils" in commit["subject"]:
                files = get_commit_files_changed(git_repo, commit["hash"])
                assert "utils.py" in files
                break

    def test_run_git(self, git_repo):
        from mempalace.git_miner import run_git

        result = run_git(git_repo, "rev-parse", "--git-dir")
        assert result.endswith(".git") or result == ".git"


# ============ Test AddGitDrawer ============

class TestAddGitDrawer:
    def test_add_git_drawer(self):
        from mempalace.git_miner import add_git_drawer

        collection = MagicMock()

        add_git_drawer(
            collection=collection,
            wing="test_wing",
            room="code",
            content="test content",
            source="test_source",
            chunk_index=0,
            agent="test_agent",
        )

        collection.upsert.assert_called_once()
        call_args = collection.upsert.call_args
        assert len(call_args[1]["documents"]) == 1
        assert "test content" in call_args[1]["documents"][0]


# ============ Test ProcessCommit ============

class TestProcessCommit:
    def test_process_commit_dry_run(self, git_repo):
        from mempalace.git_miner import process_commit, get_all_commits

        commits = get_all_commits(git_repo)
        commit = commits[0]

        drawers, room = process_commit(
            repo_path=git_repo,
            commit=commit,
            collection=None,
            closets_col=None,
            wing="test",
            agent="test",
            dry_run=True,
        )

        assert drawers >= 1
        assert room is not None


class TestChunkDiff:
    def test_single_chunk_for_small_diff(self):
        from mempalace.git_miner import chunk_diff

        small_diff = "@@ -1,3 +1,3 @@\n-old line\n+new line\n"
        chunks = chunk_diff(small_diff)
        assert len(chunks) == 1
        assert chunks[0][0] == 0  # chunk_index

    def test_multiple_chunks_for_large_diff(self):
        from mempalace.git_miner import chunk_diff, CHUNK_SIZE

        # Create a diff larger than CHUNK_SIZE
        large_diff = "@@ -1,100 +1,100 @@\n" + "line content\n" * 200
        chunks = chunk_diff(large_diff)
        assert len(chunks) > 1
        # Check that chunk indices are sequential
        indices = [c[0] for c in chunks]
        assert indices == list(range(len(chunks)))

    def test_empty_diff_returns_single_empty_chunk(self):
        from mempalace.git_miner import chunk_diff

        chunks = chunk_diff("")
        assert len(chunks) == 1
        assert chunks[0] == (0, "")

    def test_chunk_boundaries(self):
        from mempalace.git_miner import chunk_diff

        # Create diff with clear section headers
        diff = "@@ -1,3 +1,3 @@ function_a\nold\n@@ -10,3 +10,3 @@ function_b\nold\n"
        chunks = chunk_diff(diff)
        # Should try to break at @@ boundaries
        assert len(chunks) >= 1
