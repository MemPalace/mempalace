"""Tests for mempalace.convo_scanner."""

import json
from pathlib import Path

from mempalace.convo_scanner import (
    _decode_slug_fallback,
    _extract_cwd_from_session,
    _resolve_project_name,
    _safe_mtime,
    is_claude_projects_root,
    scan_claude_projects,
)


# ── is_claude_projects_root ─────────────────────────────────────────────


def test_is_claude_projects_root_true(tmp_path):
    project_dir = tmp_path / "-home-user-dev-foo"
    project_dir.mkdir()
    (project_dir / "abc.jsonl").write_text("{}\n")
    assert is_claude_projects_root(tmp_path)


def test_is_claude_projects_root_false_no_dash_prefix(tmp_path):
    project_dir = tmp_path / "normal-folder"
    project_dir.mkdir()
    (project_dir / "abc.jsonl").write_text("{}\n")
    assert not is_claude_projects_root(tmp_path)


def test_is_claude_projects_root_false_no_jsonl(tmp_path):
    project_dir = tmp_path / "-home-user-foo"
    project_dir.mkdir()
    (project_dir / "other.txt").write_text("hello")
    assert not is_claude_projects_root(tmp_path)


def test_is_claude_projects_root_false_empty(tmp_path):
    assert not is_claude_projects_root(tmp_path)


def test_is_claude_projects_root_false_nonexistent(tmp_path):
    assert not is_claude_projects_root(tmp_path / "does-not-exist")


# ── cwd extraction ──────────────────────────────────────────────────────


def test_extract_cwd_from_session(tmp_path):
    f = tmp_path / "session.jsonl"
    lines = [
        json.dumps({"type": "file-history-snapshot", "messageId": "x"}),
        json.dumps({"type": "user", "cwd": "/home/user/dev/myproj", "content": "hi"}),
    ]
    f.write_text("\n".join(lines) + "\n")
    assert _extract_cwd_from_session(f) == "/home/user/dev/myproj"


def test_extract_cwd_from_session_skips_malformed(tmp_path):
    f = tmp_path / "session.jsonl"
    f.write_text(
        "{not valid json\n" + json.dumps({"type": "user", "cwd": "/home/user/dev/good"}) + "\n"
    )
    assert _extract_cwd_from_session(f) == "/home/user/dev/good"


def test_extract_cwd_from_session_none_if_absent(tmp_path):
    f = tmp_path / "session.jsonl"
    f.write_text(json.dumps({"type": "x", "messageId": "y"}) + "\n")
    assert _extract_cwd_from_session(f) is None


def test_extract_cwd_from_session_none_if_file_missing(tmp_path):
    assert _extract_cwd_from_session(tmp_path / "missing.jsonl") is None


# ── slug fallback ───────────────────────────────────────────────────────


def test_decode_slug_fallback_last_segment():
    assert _decode_slug_fallback("-home-user-dev-foo") == "foo"


def test_decode_slug_fallback_double_dash():
    assert _decode_slug_fallback("-home-user--bentokit") == "bentokit"


def test_decode_slug_fallback_empty():
    assert _decode_slug_fallback("") == ""


def test_decode_slug_fallback_only_dashes():
    assert _decode_slug_fallback("---") == "---"


# ── safe metadata helpers ───────────────────────────────────────────────


def test_safe_mtime_returns_zero_on_stat_error(tmp_path, monkeypatch):
    f = tmp_path / "session.jsonl"
    f.write_text("{}\n")
    original_stat = Path.stat

    def fail_stat(self):
        if self == f:
            raise OSError("permission denied")
        return original_stat(self)

    monkeypatch.setattr(Path, "stat", fail_stat)
    assert _safe_mtime(f) == 0.0


# ── _resolve_project_name ───────────────────────────────────────────────


def test_resolve_project_name_uses_cwd(tmp_path):
    pdir = tmp_path / "-home-user-dev-coolproj"
    pdir.mkdir()
    session = pdir / "a.jsonl"
    session.write_text(json.dumps({"type": "user", "cwd": "/home/user/dev/cool-proj-real"}) + "\n")
    assert _resolve_project_name(pdir) == "cool-proj-real"


def test_resolve_project_name_falls_back_when_no_cwd(tmp_path):
    pdir = tmp_path / "-home-user-dev-foo"
    pdir.mkdir()
    (pdir / "a.jsonl").write_text(json.dumps({"type": "x"}) + "\n")
    assert _resolve_project_name(pdir) == "foo"


def test_resolve_project_name_prefers_newer_session(tmp_path):
    """Newest session's cwd wins — covers the case where user renamed the
    project directory between sessions."""

    pdir = tmp_path / "-home-user-dev-old"
    pdir.mkdir()
    old = pdir / "old.jsonl"
    old.write_text(json.dumps({"type": "user", "cwd": "/home/user/dev/old"}) + "\n")
    # Ensure distinguishable mtimes
    old_mtime = old.stat().st_mtime - 100
    import os

    os.utime(old, (old_mtime, old_mtime))

    new = pdir / "new.jsonl"
    new.write_text(json.dumps({"type": "user", "cwd": "/home/user/dev/new-name"}) + "\n")
    assert _resolve_project_name(pdir) == "new-name"


# ── scan_claude_projects ────────────────────────────────────────────────


def test_scan_claude_projects_empty_dir(tmp_path):
    assert scan_claude_projects(tmp_path) == []


def test_scan_claude_projects_not_a_projects_root(tmp_path):
    """Returns empty list if the dir doesn't look like .claude/projects/."""
    (tmp_path / "some-folder").mkdir()
    (tmp_path / "some-folder" / "readme.md").write_text("hi")
    assert scan_claude_projects(tmp_path) == []


def test_scan_claude_projects_finds_projects(tmp_path):
    p1 = tmp_path / "-home-user-dev-alpha"
    p1.mkdir()
    (p1 / "a.jsonl").write_text(json.dumps({"type": "user", "cwd": "/home/user/dev/alpha"}) + "\n")
    (p1 / "b.jsonl").write_text(json.dumps({"type": "user", "cwd": "/home/user/dev/alpha"}) + "\n")

    p2 = tmp_path / "-home-user-dev-beta"
    p2.mkdir()
    (p2 / "x.jsonl").write_text(json.dumps({"type": "user", "cwd": "/home/user/dev/beta"}) + "\n")

    result = scan_claude_projects(tmp_path)
    names = [p.name for p in result]
    assert "alpha" in names
    assert "beta" in names
    # alpha has 2 sessions, beta has 1 — alpha ranks higher
    alpha = next(p for p in result if p.name == "alpha")
    beta = next(p for p in result if p.name == "beta")
    assert alpha.user_commits == 2
    assert beta.user_commits == 1


def test_scan_claude_projects_ignores_dirs_without_jsonl(tmp_path):
    empty_proj = tmp_path / "-home-user-dev-empty"
    empty_proj.mkdir()
    (empty_proj / "notes.md").write_text("hi")
    assert scan_claude_projects(tmp_path) == []


def test_scan_claude_projects_marks_as_mine(tmp_path):
    p = tmp_path / "-home-user-dev-owned"
    p.mkdir()
    (p / "s.jsonl").write_text(json.dumps({"type": "user", "cwd": "/home/user/dev/owned"}) + "\n")
    result = scan_claude_projects(tmp_path)
    assert len(result) == 1
    assert result[0].is_mine is True


def test_scan_claude_projects_dedup_by_name(tmp_path):
    """Two encoded dirs resolving to the same project name collapse to one."""
    p1 = tmp_path / "-home-user-a-proj"
    p1.mkdir()
    (p1 / "s.jsonl").write_text(json.dumps({"type": "user", "cwd": "/home/user/a/proj"}) + "\n")
    (p1 / "t.jsonl").write_text(json.dumps({"type": "user", "cwd": "/home/user/a/proj"}) + "\n")

    p2 = tmp_path / "-home-user-b-proj"
    p2.mkdir()
    (p2 / "u.jsonl").write_text(json.dumps({"type": "user", "cwd": "/home/user/b/proj"}) + "\n")

    result = scan_claude_projects(tmp_path)
    # Both decode to "proj"; only one remains — the one with more sessions wins
    assert len(result) == 1
    assert result[0].name == "proj"
    assert result[0].user_commits == 2
