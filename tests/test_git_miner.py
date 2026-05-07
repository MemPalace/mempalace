import os
import tempfile
import shutil
import subprocess
import chromadb
from mempalace.git_miner import (
    _parse_git_log,
    format_commit_content,
    detect_git_room,
    mine_git_log,
    _DELIM,
    _FIELD_DELIM,
)


def _make_git_repo(tmpdir):
    """Create a temporary git repo with a few commits."""
    subprocess.run(["git", "init", tmpdir], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", tmpdir, "config", "user.email", "test@test.com"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", tmpdir, "config", "user.name", "Test User"],
        capture_output=True,
        check=True,
    )
    # Commit 1
    filepath = os.path.join(tmpdir, "app.py")
    with open(filepath, "w") as f:
        f.write("print('hello')\n")
    subprocess.run(["git", "-C", tmpdir, "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", tmpdir, "commit", "-m", "feat: initial app setup"],
        capture_output=True,
        check=True,
    )
    # Commit 2
    with open(filepath, "w") as f:
        f.write("print('hello world')\n")
    subprocess.run(["git", "-C", tmpdir, "add", "."], capture_output=True, check=True)
    subprocess.run(
        [
            "git",
            "-C",
            tmpdir,
            "commit",
            "-m",
            "fix: resolve crash on startup\n\nThe bug was caused by a missing import.",
        ],
        capture_output=True,
        check=True,
    )
    return tmpdir


def test_parse_git_log():
    raw = (
        f"{_DELIM}abc1234{_FIELD_DELIM}Alice{_FIELD_DELIM}2025-06-01T12:00:00+00:00"
        f"{_FIELD_DELIM}feat: add login{_FIELD_DELIM}Added OAuth flow\n"
        f"src/auth.py\nsrc/login.py\n"
        f"{_DELIM}def5678{_FIELD_DELIM}Bob{_FIELD_DELIM}2025-05-30T09:00:00+00:00"
        f"{_FIELD_DELIM}fix: null pointer{_FIELD_DELIM}\n"
        f"src/main.py\n"
    )
    commits = _parse_git_log(raw)
    assert len(commits) == 2
    assert commits[0]["hash"] == "abc1234"
    assert commits[0]["author"] == "Alice"
    assert commits[0]["subject"] == "feat: add login"
    assert commits[0]["body"] == "Added OAuth flow"
    assert commits[0]["files"] == ["src/auth.py", "src/login.py"]
    assert commits[1]["hash"] == "def5678"
    assert commits[1]["files"] == ["src/main.py"]


def test_format_commit_content():
    commit = {
        "hash": "abc1234",
        "author": "Alice",
        "date": "2025-06-01",
        "subject": "feat: add login page",
        "body": "Implements OAuth2 flow.",
        "files": ["src/auth.py", "src/login.py"],
    }
    content = format_commit_content(commit)
    assert "feat: add login page" in content
    assert "Implements OAuth2 flow." in content
    assert "src/auth.py" in content
    assert "src/login.py" in content


def test_format_commit_content_many_files():
    commit = {
        "hash": "abc1234",
        "author": "Alice",
        "date": "2025-06-01",
        "subject": "big refactor",
        "body": "",
        "files": [f"file_{i}.py" for i in range(30)],
    }
    content = format_commit_content(commit)
    assert "(+10 more)" in content
    assert "file_0.py" in content
    assert "file_19.py" in content
    assert "file_20.py" not in content


def test_detect_git_room_keywords():
    commit = {
        "subject": "fix: resolve crash on login",
        "body": "The bug was a null pointer issue",
        "files": ["src/auth.py"],
    }
    room = detect_git_room(commit)
    assert room == "problems"


def test_detect_git_room_with_config():
    commit = {
        "subject": "update styles",
        "body": "",
        "files": ["frontend/components/Header.tsx"],
    }
    rooms_config = [
        {"name": "frontend", "description": "UI code", "keywords": ["react", "components"]},
        {"name": "backend", "description": "API", "keywords": ["api", "server"]},
    ]
    room = detect_git_room(commit, rooms_config)
    assert room == "frontend"


def test_detect_git_room_fallback():
    commit = {
        "subject": "update readme",
        "body": "",
        "files": ["README.md"],
    }
    room = detect_git_room(commit)
    assert room == "general"


def test_mine_git_log_dry_run():
    tmpdir = tempfile.mkdtemp()
    try:
        _make_git_repo(tmpdir)
        palace_path = os.path.join(tmpdir, "palace")
        mine_git_log(tmpdir, palace_path, dry_run=True)
        # Palace should not exist since dry run
        assert not os.path.exists(palace_path)
    finally:
        shutil.rmtree(tmpdir)


def test_mine_git_log_stores_commits():
    tmpdir = tempfile.mkdtemp()
    try:
        _make_git_repo(tmpdir)
        palace_path = os.path.join(tmpdir, "palace")
        mine_git_log(tmpdir, palace_path)

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        assert col.count() == 2

        results = col.get(include=["metadatas", "documents"])
        for meta in results["metadatas"]:
            assert meta["ingest_mode"] == "git-log"
            assert "commit_hash" in meta
            assert "commit_author" in meta
            assert "commit_date" in meta
            assert meta["source_file"].startswith("git://")
    finally:
        shutil.rmtree(tmpdir)


def test_mine_git_log_dedup():
    tmpdir = tempfile.mkdtemp()
    try:
        _make_git_repo(tmpdir)
        palace_path = os.path.join(tmpdir, "palace")
        # Mine twice
        mine_git_log(tmpdir, palace_path)
        mine_git_log(tmpdir, palace_path)

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        # Should still be 2, not 4
        assert col.count() == 2
    finally:
        shutil.rmtree(tmpdir)
