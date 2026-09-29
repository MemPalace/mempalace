"""
convo_scanner.py — Parse Claude Code conversation directories into ProjectInfo.

Claude Code stores sessions under ``~/.claude/projects/<slug>/<id>.jsonl``,
where the ``<slug>`` is the original CWD with ``/`` replaced by ``-``. That
encoding is lossy: we can't tell whether ``foo-bar`` in a slug is the
literal project name ``foo-bar`` or two path segments ``foo/bar``.

Fortunately, every message record in the JSONL carries a ``cwd`` field with
the true path. This scanner reads one record per session to recover the
accurate project name, falling back to slug-decoding only if the JSONL
is malformed or empty.

Output is the same ``ProjectInfo`` shape used by ``project_scanner``, so the
``discover_entities`` orchestrator can mix-and-match sources.

Public:
    is_claude_projects_root(path) -> bool
    scan_claude_projects(path) -> list[ProjectInfo]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from mempalace.project_scanner import ProjectInfo


MAX_HEADER_LINES = 20  # lines to read per session looking for `cwd`


def is_claude_projects_root(path: Path) -> bool:
    """Return True if path looks like `.claude/projects/`.

    Heuristic: at least one child dir whose name starts with ``-`` and which
    contains at least one ``.jsonl`` file.
    """
    if not path.is_dir():
        return False
    try:
        children = list(path.iterdir())
    except OSError:
        return False
    for child in children:
        if not (child.is_dir() and child.name.startswith("-")):
            continue
        try:
            if any(p.suffix == ".jsonl" for p in child.iterdir() if p.is_file()):
                return True
        except OSError:
            continue
    return False


def _extract_cwd_from_session(session_file: Path) -> Optional[str]:
    """Return the ``cwd`` from the first message record that carries one.

    Returns None if the file can't be read, has no JSON, or no record has cwd.
    """
    try:
        with open(session_file, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= MAX_HEADER_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _decode_slug_fallback(slug: str) -> str:
    """Best-effort project name from slug when cwd is unavailable.

    The slug is lossy (`/` and `-` both become `-`). Last non-empty segment
    is the closest guess at the project name, preserving kebab-case is
    impossible without cwd.
    """
    stripped = slug.lstrip("-")
    parts = [p for p in stripped.split("-") if p]
    return parts[-1] if parts else slug


def _safe_mtime(path: Path) -> float:
    """Return file mtime, defaulting old on permission or filesystem errors."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _resolve_project_name(project_dir: Path) -> str:
    """Read one session's cwd to recover the original project name.

    Falls back to slug-decoding if no session has a readable cwd.
    """
    sessions = sorted(
        (p for p in project_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"),
        key=_safe_mtime,
        reverse=True,  # newest first — most likely to be well-formed
    )
    for session in sessions:
        cwd = _extract_cwd_from_session(session)
        if cwd:
            return Path(cwd).name or cwd
    return _decode_slug_fallback(project_dir.name)


def scan_claude_projects(path: str | Path) -> list[ProjectInfo]:
    """Scan a ``.claude/projects/`` directory for Claude Code conversations.

    One ProjectInfo per subdir. ``has_git`` is False (the directory isn't a
    repo itself) but ``total_commits`` is repurposed here as session count so
    the UX surfaces a density signal for ranking.
    """
    root = Path(path).expanduser().resolve()
    if not is_claude_projects_root(root):
        return []

    projects: dict[str, ProjectInfo] = {}
    for sub in sorted(root.iterdir()):
        if not (sub.is_dir() and sub.name.startswith("-")):
            continue
        try:
            sessions = [p for p in sub.iterdir() if p.is_file() and p.suffix == ".jsonl"]
        except OSError:
            continue
        if not sessions:
            continue

        name = _resolve_project_name(sub)
        session_count = len(sessions)

        proj = ProjectInfo(
            name=name,
            repo_root=sub,
            manifest=None,
            has_git=False,
            total_commits=session_count,
            user_commits=session_count,
            is_mine=True,  # Claude Code sessions are authored by the user
        )
        existing = projects.get(name)
        if existing is None or session_count > existing.user_commits:
            projects[name] = proj

    return sorted(
        projects.values(),
        key=lambda p: (-p.user_commits, p.name),
    )
