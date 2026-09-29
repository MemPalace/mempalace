"""Regression tests for issue #185 — gitignore protection on `mempalace init`.

Issue #185 reports that `mempalace init <dir>` writes `mempalace.yaml` and
`entities.json` into the project root, where they could be committed by
accident. The fix adds `_ensure_mempalace_files_gitignored()` which appends
the two filenames to `.gitignore` when `<dir>` is a git repository.
"""

from pathlib import Path
from unittest.mock import mock_open, patch

from mempalace.cli import _ensure_mempalace_files_gitignored


def _git_init(path: Path) -> None:
    """Mark a directory as a git repo without invoking git itself."""
    (path / ".git").mkdir()


def test_no_op_when_not_a_git_repo(tmp_path):
    assert _ensure_mempalace_files_gitignored(tmp_path) is False
    assert not (tmp_path / ".gitignore").exists()


def test_creates_gitignore_with_both_entries(tmp_path):
    _git_init(tmp_path)
    assert _ensure_mempalace_files_gitignored(tmp_path) is True
    contents = (tmp_path / ".gitignore").read_text()
    assert "mempalace.yaml" in contents
    assert "entities.json" in contents
    assert "issue #185" in contents


def test_appends_only_missing_entries(tmp_path):
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("node_modules/\nmempalace.yaml\n")
    assert _ensure_mempalace_files_gitignored(tmp_path) is True
    contents = (tmp_path / ".gitignore").read_text()
    # mempalace.yaml must not be duplicated
    assert contents.count("mempalace.yaml") == 1
    # entities.json was missing → must now be present
    assert "entities.json" in contents
    # original entries preserved
    assert "node_modules/" in contents


def test_idempotent_when_both_already_present(tmp_path):
    _git_init(tmp_path)
    initial = "mempalace.yaml\nentities.json\n"
    (tmp_path / ".gitignore").write_text(initial)
    assert _ensure_mempalace_files_gitignored(tmp_path) is False
    assert (tmp_path / ".gitignore").read_text() == initial


def test_handles_gitignore_without_trailing_newline(tmp_path):
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("dist")  # no trailing newline
    assert _ensure_mempalace_files_gitignored(tmp_path) is True
    contents = (tmp_path / ".gitignore").read_text()
    # Original entry preserved on its own line, not glued to the new block
    assert "dist\n" in contents
    assert "mempalace.yaml" in contents
    assert "entities.json" in contents


def test_gitignore_io_pins_utf8_and_defensive_decode(tmp_path):
    """Regression for #1648: never fall back to the Windows locale codec."""
    _git_init(tmp_path)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("# café\n", encoding="utf-8")
    append_handle = mock_open()

    with (
        patch.object(Path, "read_text", return_value="# café\n") as read_text,
        patch("builtins.open", append_handle),
    ):
        assert _ensure_mempalace_files_gitignored(tmp_path) is True

    read_text.assert_called_once_with(encoding="utf-8", errors="replace")
    append_handle.assert_called_once_with(gitignore, "a", encoding="utf-8")
