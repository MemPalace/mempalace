import os
import shutil
import tempfile
from pathlib import Path

import chromadb
import yaml

from mempalace.miner import mine, scan_project, status
from mempalace.palace import file_already_mined


def write_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def scanned_files(project_root: Path, **kwargs):
    files = scan_project(str(project_root), **kwargs)
    return sorted(path.relative_to(project_root).as_posix() for path in files)


def test_project_mining():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()
        os.makedirs(project_root / "backend")

        write_file(
            project_root / "backend" / "app.py", "def main():\n    print('hello world')\n" * 20
        )
        with open(project_root / "mempalace.yaml", "w") as f:
            yaml.dump(
                {
                    "wing": "test_project",
                    "rooms": [
                        {"name": "backend", "description": "Backend code"},
                        {"name": "general", "description": "General"},
                    ],
                },
                f,
            )

        palace_path = project_root / "palace"
        mine(str(project_root), str(palace_path))

        client = chromadb.PersistentClient(path=str(palace_path))
        col = client.get_collection("mempalace_drawers")
        assert col.count() > 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_scan_project_respects_gitignore():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "ignored.py\ngenerated/\n")
        write_file(project_root / "src" / "app.py", "print('hello')\n" * 20)
        write_file(project_root / "ignored.py", "print('ignore me')\n" * 20)
        write_file(project_root / "generated" / "artifact.py", "print('artifact')\n" * 20)

        assert scanned_files(project_root) == ["src/app.py"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_respects_nested_gitignore():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "*.log\n")
        write_file(project_root / "subrepo" / ".gitignore", "tasks/\n")
        write_file(project_root / "subrepo" / "src" / "main.py", "print('main')\n" * 20)
        write_file(project_root / "subrepo" / "tasks" / "task.py", "print('task')\n" * 20)
        write_file(project_root / "subrepo" / "debug.log", "debug\n" * 20)

        assert scanned_files(project_root) == ["subrepo/src/main.py"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_allows_nested_gitignore_override():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "*.csv\n")
        write_file(project_root / "subrepo" / ".gitignore", "!keep.csv\n")
        write_file(project_root / "drop.csv", "a,b,c\n" * 20)
        write_file(project_root / "subrepo" / "keep.csv", "a,b,c\n" * 20)

        assert scanned_files(project_root) == ["subrepo/keep.csv"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_allows_gitignore_negation_when_parent_dir_is_visible():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "generated/*\n!generated/keep.py\n")
        write_file(project_root / "generated" / "drop.py", "print('drop')\n" * 20)
        write_file(project_root / "generated" / "keep.py", "print('keep')\n" * 20)

        assert scanned_files(project_root) == ["generated/keep.py"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_does_not_reinclude_file_from_ignored_directory():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "generated/\n!generated/keep.py\n")
        write_file(project_root / "generated" / "drop.py", "print('drop')\n" * 20)
        write_file(project_root / "generated" / "keep.py", "print('keep')\n" * 20)

        assert scanned_files(project_root) == []
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_can_disable_gitignore():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "data/\n")
        write_file(project_root / "data" / "stuff.csv", "a,b,c\n" * 20)

        assert scanned_files(project_root, respect_gitignore=False) == ["data/stuff.csv"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_can_include_ignored_directory():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "docs/\n")
        write_file(project_root / "docs" / "guide.md", "# Guide\n" * 20)

        assert scanned_files(project_root, include_ignored=["docs"]) == ["docs/guide.md"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_can_include_specific_ignored_file():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "generated/\n")
        write_file(project_root / "generated" / "drop.py", "print('drop')\n" * 20)
        write_file(project_root / "generated" / "keep.py", "print('keep')\n" * 20)

        assert scanned_files(project_root, include_ignored=["generated/keep.py"]) == [
            "generated/keep.py"
        ]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_can_include_exact_file_without_known_extension():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "README\n")
        write_file(project_root / "README", "hello\n" * 20)

        assert scanned_files(project_root, include_ignored=["README"]) == ["README"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_include_override_beats_skip_dirs():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".pytest_cache" / "cache.py", "print('cache')\n" * 20)

        assert scanned_files(
            project_root,
            respect_gitignore=False,
            include_ignored=[".pytest_cache"],
        ) == [".pytest_cache/cache.py"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_skip_dirs_still_apply_without_override():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".pytest_cache" / "cache.py", "print('cache')\n" * 20)
        write_file(project_root / "main.py", "print('main')\n" * 20)

        assert scanned_files(project_root, respect_gitignore=False) == ["main.py"]
    finally:
        shutil.rmtree(tmpdir)


def test_file_already_mined_check_mtime():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = os.path.join(tmpdir, "palace")
        os.makedirs(palace_path)
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")

        test_file = os.path.join(tmpdir, "test.txt")
        with open(test_file, "w") as f:
            f.write("hello world")

        mtime = os.path.getmtime(test_file)

        # Not mined yet
        assert file_already_mined(col, test_file) is False
        assert file_already_mined(col, test_file, check_mtime=True) is False

        # Add it with mtime
        col.add(
            ids=["d1"],
            documents=["hello world"],
            metadatas=[{"source_file": test_file, "source_mtime": str(mtime)}],
        )

        # Already mined (no mtime check)
        assert file_already_mined(col, test_file) is True
        # Already mined (mtime matches)
        assert file_already_mined(col, test_file, check_mtime=True) is True

        # Modify file and force a different mtime (Windows has low mtime resolution)
        with open(test_file, "w") as f:
            f.write("modified content")
        os.utime(test_file, (mtime + 10, mtime + 10))

        # Still mined without mtime check
        assert file_already_mined(col, test_file) is True
        # Needs re-mining with mtime check
        assert file_already_mined(col, test_file, check_mtime=True) is False

        # Record with no mtime stored should return False for check_mtime
        col.add(
            ids=["d2"],
            documents=["other"],
            metadatas=[{"source_file": "/fake/no_mtime.txt"}],
        )
        assert file_already_mined(col, "/fake/no_mtime.txt", check_mtime=True) is False
    finally:
        # Release ChromaDB file handles before cleanup (required on Windows)
        del col, client
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mine_dry_run_with_tiny_file_no_crash():
    """Dry-run must not crash when process_file returns 0 drawers (room was None)."""
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        # One normal file and one that falls below MIN_CHUNK_SIZE
        write_file(project_root / "good.py", "def main():\n    print('hello world')\n" * 20)
        write_file(project_root / "tiny.txt", "x")

        with open(project_root / "mempalace.yaml", "w") as f:
            yaml.dump(
                {
                    "wing": "test_project",
                    "rooms": [{"name": "general", "description": "General"}],
                },
                f,
            )

        palace_path = project_root / "palace"
        # Should not raise TypeError on the summary print
        mine(str(project_root), str(palace_path), dry_run=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_status_missing_palace_does_not_create_empty_collection(tmp_path, capsys):
    palace_path = tmp_path / "missing-palace"

    status(str(palace_path))

    out = capsys.readouterr().out
    assert "No palace found" in out
    assert not palace_path.exists()


def test_mine_uses_remote_collection_when_remote_url_set(tmp_path, monkeypatch):
    """When remote_url is passed, mine uses RemoteCollection."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from mempalace.miner import mine
    from unittest.mock import patch, MagicMock

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}

    (tmp_path / "hello.txt").write_text("this is some content for testing the miner remote path")
    write_file(
        tmp_path / "mempalace.yaml",
        "wing: test_remote\nrooms:\n  - name: general\n    description: General\n",
    )

    with patch("mempalace.miner.get_remote_collection", return_value=mock_col):
        mine(
            project_dir=str(tmp_path),
            palace_path=str(tmp_path / "palace"),
            remote_url="http://x",
            remote_token="tok",
        )

    assert mock_col.upsert.called or mock_col.get.called


def test_mine_raises_when_remote_url_but_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MEMPALACE_TOKEN", raising=False)
    from mempalace.miner import mine
    import pytest

    write_file(
        tmp_path / "mempalace.yaml",
        "wing: test_remote\nrooms:\n  - name: general\n    description: General\n",
    )

    with pytest.raises(RuntimeError, match="remote-url requires a token"):
        mine(
            project_dir=str(tmp_path),
            palace_path=str(tmp_path / "palace"),
            remote_url="http://x",
        )
