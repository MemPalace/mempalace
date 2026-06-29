"""Tests for mempalace.instructions_cli — instruction text output."""

from unittest.mock import patch

import pytest

from mempalace.instructions_cli import AVAILABLE, INSTRUCTIONS_DIR, run_instructions


def test_run_instructions_valid_name(capsys):
    """Valid name prints the .md file content."""
    name = "init"
    expected = (INSTRUCTIONS_DIR / f"{name}.md").read_text(encoding="utf-8")
    run_instructions(name)
    captured = capsys.readouterr()
    assert captured.out.strip() == expected.strip()


def test_run_instructions_all_available(capsys):
    """Every name in AVAILABLE should succeed without error."""
    for name in AVAILABLE:
        run_instructions(name)
        out = capsys.readouterr().out
        assert len(out) > 0


def test_run_instructions_invalid_name(capsys):
    """Invalid name should sys.exit(1) and print error to stderr."""
    with pytest.raises(SystemExit) as exc_info:
        run_instructions("nonexistent")
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Unknown instructions: nonexistent" in captured.err
    assert "Available:" in captured.err


def test_run_instructions_missing_md_file(capsys, tmp_path):
    """If the .md file is missing on disk, should sys.exit(1)."""
    with patch("mempalace.instructions_cli.INSTRUCTIONS_DIR", tmp_path):
        with patch("mempalace.instructions_cli.AVAILABLE", ["fakecmd"]):
            with pytest.raises(SystemExit) as exc_info:
                run_instructions("fakecmd")
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "Instructions file not found" in captured.err
