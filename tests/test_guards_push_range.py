"""Tests for pre-push changed-file detection, run against temporary git repos."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from commonplace.guards.push_range import (
    ZERO_SHA,
    PushRangeError,
    PushUpdate,
    changed_files,
    default_branch,
    parse_pre_push_lines,
)

if shutil.which("git") is None:
    pytest.skip("git is not installed", allow_module_level=True)

SHA_A = "a" * 40
SHA_B = "b" * 40


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    return path


def write(repo: Path, rel: str, text: str = "x\n") -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str = "change") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A work repo whose origin has main with base.py and old.py."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    repo = make_repo(tmp_path / "work")
    git(repo, "remote", "add", "origin", str(origin))
    write(repo, "base.py")
    write(repo, "old.py")
    commit(repo, "base")
    git(repo, "push", "-q", "origin", "main")
    return repo


def feature_then_main_moves(repo: Path) -> str:
    """Branch feature with feat.py from main, then move main on origin. Returns feature sha."""
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo, "feat.py")
    local = commit(repo, "feat")
    git(repo, "checkout", "-q", "main")
    write(repo, "main_only.py")
    commit(repo, "main moves")
    git(repo, "push", "-q", "origin", "main")
    return local


def test_parse_skips_blank_lines_and_accepts_sha256():
    sha256 = "c" * 64
    text = f"\nrefs/heads/a {SHA_A} refs/heads/a {SHA_B}\n\nrefs/tags/v1 {sha256} refs/tags/v1 {ZERO_SHA}\n"
    updates = parse_pre_push_lines(text)
    assert updates == [
        PushUpdate("refs/heads/a", SHA_A, "refs/heads/a", SHA_B),
        PushUpdate("refs/tags/v1", sha256, "refs/tags/v1", ZERO_SHA),
    ]


def test_parse_rejects_wrong_field_count_with_line_number():
    with pytest.raises(PushRangeError, match="pre-push line 2: expected 4 fields, got 3"):
        parse_pre_push_lines(f"\nrefs/heads/a {SHA_A} refs/heads/a\n")


def test_parse_rejects_bad_sha_with_line_number():
    with pytest.raises(PushRangeError, match="pre-push line 1: not a sha"):
        parse_pre_push_lines(f"refs/heads/a {SHA_A}x refs/heads/a {SHA_B}\n")
    with pytest.raises(PushRangeError, match="pre-push line 1: not a sha"):
        parse_pre_push_lines(f"refs/heads/a {SHA_A} refs/heads/a {'g' * 40}\n")
    with pytest.raises(PushRangeError, match="pre-push line 1: not a sha"):
        parse_pre_push_lines(f"refs/heads/a {'a' * 41} refs/heads/a {SHA_B}\n")


def test_existing_branch_lists_only_new_commits(project: Path):
    git(project, "checkout", "-q", "-b", "feature")
    write(project, "a.py")
    old = commit(project, "feat 1")
    git(project, "push", "-q", "origin", "feature")
    write(project, "pkg/b.py")
    commit(project, "feat 2")
    write(project, "pkg/b.py", "y\n")
    new = commit(project, "feat 3")
    update = PushUpdate("refs/heads/feature", new, "refs/heads/feature", old)
    assert changed_files(project, [update]) == ["pkg/b.py"]


def test_new_branch_diffs_against_merge_base_not_main(project: Path):
    local = feature_then_main_moves(project)
    update = PushUpdate("refs/heads/feature", local, "refs/heads/feature", ZERO_SHA)
    assert changed_files(project, [update]) == ["feat.py"]


def test_force_push_with_unknown_remote_sha_falls_back_to_merge_base(project: Path):
    local = feature_then_main_moves(project)
    update = PushUpdate("refs/heads/feature", local, "refs/heads/feature", SHA_B)
    assert changed_files(project, [update]) == ["feat.py"]


def test_delete_lists_nothing(project: Path):
    head = git(project, "rev-parse", "HEAD")
    update = PushUpdate("refs/heads/feature", ZERO_SHA, "refs/heads/feature", head)
    assert changed_files(project, [update]) == []


def test_tag_lists_nothing(project: Path):
    head = git(project, "rev-parse", "HEAD")
    git(project, "tag", "v1", head)
    new_tag = PushUpdate("refs/tags/v1", head, "refs/tags/v1", ZERO_SHA)
    moved_tag = PushUpdate("refs/tags/v1", head, "refs/tags/v1", SHA_B)
    assert changed_files(project, [new_tag, moved_tag]) == []


def test_deleted_files_are_excluded(project: Path):
    git(project, "checkout", "-q", "-b", "feature")
    (project / "old.py").unlink()
    write(project, "new.py")
    local = commit(project, "swap files")
    main = git(project, "rev-parse", "main")
    update = PushUpdate("refs/heads/feature", local, "refs/heads/feature", main)
    assert changed_files(project, [update]) == ["new.py"]


def test_file_added_then_removed_in_range_is_excluded(project: Path):
    git(project, "checkout", "-q", "-b", "feature")
    write(project, "tmp.py")
    commit(project, "add tmp")
    (project / "tmp.py").unlink()
    write(project, "keep.py")
    local = commit(project, "remove tmp")
    update = PushUpdate("refs/heads/feature", local, "refs/heads/feature", ZERO_SHA)
    assert changed_files(project, [update]) == ["keep.py"]


def test_repo_without_remote_lists_all_commit_files(tmp_path: Path):
    repo = make_repo(tmp_path / "solo")
    write(repo, "keep.py")
    commit(repo, "one")
    write(repo, "pkg/more.py")
    head = commit(repo, "two")
    update = PushUpdate("refs/heads/main", head, "refs/heads/main", ZERO_SHA)
    assert changed_files(repo, [update]) == ["keep.py", "pkg/more.py"]


def test_repo_without_remote_excludes_added_then_removed(tmp_path: Path):
    repo = make_repo(tmp_path / "solo")
    write(repo, "keep.py")
    write(repo, "tmp.py")
    commit(repo, "one")
    (repo / "tmp.py").unlink()
    head = commit(repo, "remove tmp")
    update = PushUpdate("refs/heads/main", head, "refs/heads/main", ZERO_SHA)
    assert changed_files(repo, [update]) == ["keep.py"]


def test_linked_worktree_uses_work_tree_relative_paths(project: Path, tmp_path: Path):
    worktree = tmp_path / "wt"
    git(project, "worktree", "add", "-q", "-b", "wt-feature", str(worktree), "main")
    write(worktree, "pkg/w.py")
    local = commit(worktree, "worktree change")
    update = PushUpdate("refs/heads/wt-feature", local, "refs/heads/wt-feature", ZERO_SHA)
    assert changed_files(worktree, [update]) == ["pkg/w.py"]


def test_repo_argument_may_be_a_subdirectory(project: Path):
    git(project, "checkout", "-q", "-b", "feature")
    write(project, "sub/x.py")
    write(project, "root.py")
    local = commit(project, "feat")
    update = PushUpdate("refs/heads/feature", local, "refs/heads/feature", ZERO_SHA)
    assert changed_files(project / "sub", [update]) == ["root.py", "sub/x.py"]


def test_non_ascii_filename_comes_out_raw(project: Path):
    git(project, "checkout", "-q", "-b", "feature")
    write(project, "café/naïve 日本.md")
    local = commit(project, "unicode")
    update = PushUpdate("refs/heads/feature", local, "refs/heads/feature", ZERO_SHA)
    assert changed_files(project, [update]) == ["café/naïve 日本.md"]


def test_unrelated_history_falls_back_to_commits_not_on_remote(project: Path):
    git(project, "checkout", "-q", "--orphan", "orphan")
    git(project, "rm", "-r", "-q", "-f", ".")
    write(project, "o.py")
    local = commit(project, "orphan root")
    update = PushUpdate("refs/heads/orphan", local, "refs/heads/orphan", SHA_B)
    assert changed_files(project, [update]) == ["o.py"]


def test_default_branch_prefers_remote_main(project: Path, tmp_path: Path):
    assert default_branch(project) == "refs/remotes/origin/main"
    empty = make_repo(tmp_path / "empty")
    assert default_branch(empty) is None
