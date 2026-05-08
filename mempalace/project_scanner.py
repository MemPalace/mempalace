"""
project_scanner.py — Detect projects and people from real signal.

For a codebase with build manifests or git history, this beats regex-based
entity detection by a wide margin: the project's own name is already written
down in package.json / pyproject.toml / Cargo.toml / go.mod, and the people
who worked on it are in `git log`.

This module is used as the primary signal in `mempalace init`. The regex
detector in entity_detector.py stays as a fallback for prose-only folders
(notes, research, writing).

Public:
    scan(root) -> (projects, people)
    to_detected_dict(projects, people) -> {people: [...], projects: [...], uncertain: []}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # Python 3.9/3.10 backport
    except ImportError:
        tomllib = None  # type: ignore


SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    "coverage",
    ".terraform",
    "vendor",
    "target",
    ".mempalace",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

MAX_DEPTH = 6
MAX_COMMITS_PER_REPO = 1000
GIT_TIMEOUT = 10


# ==================== DATACLASSES ====================


@dataclass
class ProjectInfo:
    name: str
    repo_root: Path
    manifest: Optional[str] = None
    has_git: bool = False
    total_commits: int = 0
    user_commits: int = 0
    is_mine: bool = False

    @property
    def confidence(self) -> float:
        if self.is_mine:
            return 0.99
        if self.has_git and self.total_commits > 0:
            return 0.7
        return 0.85  # manifest-only, no git

    def to_signal(self) -> str:
        parts: list[str] = []
        if self.manifest:
            parts.append(self.manifest)
        if self.has_git:
            if self.is_mine and self.user_commits:
                parts.append(f"{self.user_commits} of your commits")
            elif self.user_commits:
                parts.append(f"{self.user_commits}/{self.total_commits} yours")
            else:
                parts.append(f"{self.total_commits} commits (none by you)")
        return ", ".join(parts) or "repo"


@dataclass
class PersonInfo:
    name: str
    total_commits: int = 0
    emails: set[str] = field(default_factory=set)
    repos: set[str] = field(default_factory=set)

    @property
    def confidence(self) -> float:
        if self.total_commits >= 100 or len(self.repos) >= 3:
            return 0.99
        if self.total_commits >= 20:
            return 0.85
        return 0.65

    def to_signal(self) -> str:
        r = len(self.repos)
        return f"{self.total_commits} commit{'s' if self.total_commits != 1 else ''} across {r} repo{'s' if r != 1 else ''}"


# ==================== MANIFEST PARSING ====================


def _parse_package_json(path: Path) -> Optional[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None
    name = data.get("name")
    return name if isinstance(name, str) and name else None


def _parse_toml(path: Path) -> dict:
    if tomllib is None:
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _parse_pyproject(path: Path) -> Optional[str]:
    data = _parse_toml(path)
    name = data.get("project", {}).get("name")
    if isinstance(name, str) and name:
        return name
    name = data.get("tool", {}).get("poetry", {}).get("name")
    return name if isinstance(name, str) and name else None


def _parse_cargo(path: Path) -> Optional[str]:
    data = _parse_toml(path)
    name = data.get("package", {}).get("name")
    return name if isinstance(name, str) and name else None


def _parse_gomod(path: Path) -> Optional[str]:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("module "):
                mod = line.split(None, 1)[1].strip()
                return mod.split("/")[-1] or None
    except OSError:
        return None
    return None


MANIFEST_PRIORITY = {
    "pyproject.toml": 0,
    "package.json": 1,
    "Cargo.toml": 2,
    "go.mod": 3,
}
# Sentinel so unknown manifests always sort after the known manifest types above.
UNKNOWN_MANIFEST_PRIORITY = max(MANIFEST_PRIORITY.values()) + 1
MANIFEST_PARSERS = {
    "package.json": _parse_package_json,
    "pyproject.toml": _parse_pyproject,
    "Cargo.toml": _parse_cargo,
    "go.mod": _parse_gomod,
}


# ==================== GIT HELPERS ====================


def _run_git(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_user_identity(repo: Path) -> tuple[str, str]:
    """Return (name, email) for this repo, falling back to global config."""
    name = _run_git(repo, "config", "user.name", timeout=2).strip()
    email = _run_git(repo, "config", "user.email", timeout=2).strip()
    return name, email


def _global_git_identity() -> tuple[str, str]:
    try:
        n = subprocess.run(
            ["git", "config", "--global", "user.name"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.strip()
        e = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.strip()
        return n, e
    except (OSError, subprocess.SubprocessError):
        return "", ""


def _git_authors(repo: Path) -> list[tuple[str, str]]:
    out = _run_git(
        repo,
        "log",
        f"--max-count={MAX_COMMITS_PER_REPO}",
        "--format=%aN|%aE",
    )
    result = []
    for line in out.splitlines():
        if "|" in line:
            name, email = line.split("|", 1)
            result.append((name.strip(), email.strip()))
    return result


# ==================== BOT / NAME FILTERING ====================


_BOT_NAME_PATTERNS = [
    r"\[bot\]",
    r"^dependabot",
    r"^renovate",
    r"^github-actions",
    r"^actions-user",
    r"-bot$",
    r"\bbot$",  # catches "PR Bot", "Release Bot", etc. Not "robot" (no \b)
    r"^bot-",
    r"^snyk",
    r"^greenkeeper",
    r"^semantic-release",
    r"^allcontributors",
    r"-autoroll$",
    r"^auto-format",
    r"^pre-commit-ci",
]
_BOT_EMAIL_PATTERNS = [
    # `@users.noreply.github.com` is GitHub's privacy-protected human email —
    # do NOT filter it. Real bots identify themselves via the display name
    # (usually containing "[bot]"), which is caught by _BOT_NAME_PATTERNS.
    r"bot@",
    r"-bot@",
    r"\[bot\]@",
]

_BOT_RE_NAMES = [re.compile(p) for p in _BOT_NAME_PATTERNS]
_BOT_RE_EMAILS = [re.compile(p) for p in _BOT_EMAIL_PATTERNS]


def _is_bot(name: str, email: str) -> bool:
    ln, le = name.lower(), email.lower()
    return any(rx.search(ln) for rx in _BOT_RE_NAMES) or any(rx.search(le) for rx in _BOT_RE_EMAILS)


def _looks_like_real_name(name: str) -> bool:
    """Heuristic: a human's name has a space and at least two title-cased parts.

    Filters out handles (lowercase, digits, one-token usernames).
    """
    if not name or " " not in name:
        return False
    parts = name.split()
    if len(parts) < 2:
        return False
    # First and last parts must start with an uppercase letter
    return parts[0][:1].isupper() and parts[-1][:1].isupper()


# ==================== DIRECTORY WALK ====================


def _walk(root: Path, max_depth: int = MAX_DEPTH):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        rel = Path(dirpath).relative_to(root)
        depth = 0 if rel == Path(".") else len(rel.parts)
        if depth > max_depth:
            dirs.clear()
            continue
        yield Path(dirpath), dirs, files


def _has_git_marker(path: Path) -> bool:
    git_path = path / ".git"
    return git_path.is_dir() or git_path.is_file()


def _manifest_sort_key(entry: tuple[str, str, Path], repo_root: Path) -> tuple[int, int, str]:
    """Sort manifests by shallowest path first, then known manifest priority,
    then lexicographic path for deterministic tie-breaking.
    """
    manifest_file, _project_name, manifest_dir = entry
    try:
        rel = manifest_dir.relative_to(repo_root)
        depth = len(rel.parts)
        rel_str = rel.as_posix()
    except ValueError:
        depth = MAX_DEPTH + 1
        rel_str = manifest_dir.as_posix()
    return (depth, MANIFEST_PRIORITY.get(manifest_file, UNKNOWN_MANIFEST_PRIORITY), rel_str)


def find_git_repos(root: Path, max_depth: int = MAX_DEPTH) -> list[Path]:
    """Return git repo roots under `root` (including root itself if it's a repo)."""
    root = root.resolve()
    repos: list[Path] = []
    if _has_git_marker(root):
        # Root is a repo — still walk for nested repos (submodules, etc.)
        repos.append(root)
    for dirpath, dirs, _ in _walk(root, max_depth):
        if dirpath == root:
            continue
        if _has_git_marker(dirpath):
            repos.append(dirpath)
            dirs.clear()  # don't descend into this repo's contents from here
    return repos


def _collect_manifest_names(repo_root: Path) -> list[tuple[str, str, Path]]:
    """Return (manifest_filename, project_name, dirpath) within a repo.

    Does not descend into nested git repos.
    """
    found: list[tuple[str, str, Path]] = []
    for dirpath, dirs, files in _walk(repo_root):
        if dirpath != repo_root and _has_git_marker(dirpath):
            dirs.clear()
            continue
        for fname in files:
            parser = MANIFEST_PARSERS.get(fname)
            if not parser:
                continue
            name = parser(dirpath / fname)
            if name:
                found.append((fname, name, dirpath))
    return sorted(found, key=lambda entry: _manifest_sort_key(entry, repo_root))


# ==================== MAIN SCAN ====================


class _UnionFind:
    """Minimal union-find for (name, email) identity resolution."""

    def __init__(self) -> None:
        self.parent: dict = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
            return x
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _dedupe_people(
    all_commits: list[tuple[str, str, str]],
) -> dict[str, PersonInfo]:
    """Group commits by identity. Two commits are the same person if they
    share a name OR an email. Display name = most frequent non-bot variant.

    ``all_commits`` is a list of (name, email, repo_str) triples from every repo.
    """
    uf = _UnionFind()
    for name, email, _repo in all_commits:
        uf.union(("name", name), ("email", email) if email else ("name", name))

    # Aggregate by component root
    component_commits: dict = {}
    for name, email, repo in all_commits:
        key = uf.find(("name", name))
        entry = component_commits.setdefault(
            key, {"name_counts": {}, "emails": set(), "repos": set(), "total": 0}
        )
        entry["name_counts"][name] = entry["name_counts"].get(name, 0) + 1
        if email:
            entry["emails"].add(email)
        entry["repos"].add(repo)
        entry["total"] += 1

    # Pick display name per component: the most-frequent variant that looks
    # like a real name; fall back to most-frequent overall.
    people: dict[str, PersonInfo] = {}
    for _key, entry in component_commits.items():
        candidates = sorted(entry["name_counts"].items(), key=lambda x: -x[1])
        display = next(
            (n for n, _ in candidates if _looks_like_real_name(n)),
            candidates[0][0],
        )
        if not _looks_like_real_name(display):
            continue  # Skip handles and single-token names
        # If we already have this display (rare — distinct components with the
        # same chosen display), merge into the existing entry.
        existing = people.get(display)
        if existing:
            existing.total_commits += entry["total"]
            existing.emails.update(entry["emails"])
            existing.repos.update(entry["repos"])
        else:
            people[display] = PersonInfo(
                name=display,
                total_commits=entry["total"],
                emails=set(entry["emails"]),
                repos=set(entry["repos"]),
            )
    return people


def scan(root: str | os.PathLike) -> tuple[list[ProjectInfo], list[PersonInfo]]:
    """Scan `root` for projects and people. Returns (projects, people) sorted."""
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        return [], []

    repos = find_git_repos(root_path)

    # Identify current user from first repo's git config, fall back to global
    me_name, me_email = "", ""
    if repos:
        me_name, me_email = _git_user_identity(repos[0])
    if not me_name and not me_email:
        me_name, me_email = _global_git_identity()

    projects: dict[str, ProjectInfo] = {}
    all_commits: list[tuple[str, str, str]] = []

    for repo in repos:
        manifests = _collect_manifest_names(repo)
        if manifests:
            manifest_file, proj_name, _ = manifests[0]
        else:
            manifest_file, proj_name = None, repo.name

        authors = _git_authors(repo)
        non_bot_authors = [(name, email) for name, email in authors if not _is_bot(name, email)]
        total_commits = len(non_bot_authors)
        user_commits = 0
        author_counts: dict[str, int] = {}
        for name, email in non_bot_authors:
            author_counts[name] = author_counts.get(name, 0) + 1
            all_commits.append((name, email, str(repo)))
            if (me_name and name == me_name) or (me_email and email == me_email):
                user_commits += 1

        is_mine = False
        if user_commits > 0:
            sorted_authors = sorted(author_counts.items(), key=lambda x: -x[1])
            top5 = {n for n, _ in sorted_authors[:5]}
            if me_name and me_name in top5:
                is_mine = True
            elif total_commits and user_commits / total_commits >= 0.10:
                is_mine = True
            elif user_commits >= 20:
                is_mine = True

        proj = ProjectInfo(
            name=proj_name,
            repo_root=repo,
            manifest=manifest_file,
            has_git=True,
            total_commits=total_commits,
            user_commits=user_commits,
            is_mine=is_mine,
        )
        existing = projects.get(proj_name)
        if existing is None or proj.user_commits > existing.user_commits:
            projects[proj_name] = proj

    people = _dedupe_people(all_commits)

    # Handle case: root has manifests but no git repo anywhere
    if not repos:
        manifests = _collect_manifest_names(root_path)
        for manifest_file, proj_name, _dirpath in manifests:
            if proj_name in projects:
                continue
            projects[proj_name] = ProjectInfo(
                name=proj_name,
                repo_root=root_path,
                manifest=manifest_file,
                has_git=False,
            )

    project_list = sorted(
        projects.values(),
        key=lambda p: (not p.is_mine, -p.user_commits, -p.total_commits, p.name),
    )
    people_list = sorted(people.values(), key=lambda p: -p.total_commits)

    return project_list, people_list


# ==================== ADAPTER ====================


def to_detected_dict(
    projects: list[ProjectInfo],
    people: list[PersonInfo],
    project_cap: int = 15,
    people_cap: int = 15,
) -> dict:
    """Convert scan results into the dict shape produced by entity_detector.detect_entities."""
    proj_entries = [
        {
            "name": p.name,
            "type": "project",
            "confidence": round(p.confidence, 2),
            "frequency": p.user_commits or p.total_commits,
            "signals": [p.to_signal()],
        }
        for p in projects[:project_cap]
    ]
    people_entries = [
        {
            "name": p.name,
            "type": "person",
            "confidence": round(p.confidence, 2),
            "frequency": p.total_commits,
            "signals": [p.to_signal()],
        }
        for p in people[:people_cap]
    ]
    return {
        "people": people_entries,
        "projects": proj_entries,
        "topics": [],
        "uncertain": [],
    }


# ==================== MERGE WITH REGEX DETECTOR ====================


def _merge_detected(primary: dict, secondary: dict, drop_secondary_uncertain: bool = False) -> dict:
    """Merge two detected dicts. Primary entries win on name conflict.

    Dedup is case-insensitive so "mempalace" (manifest name) absorbs "MemPalace"
    (docs/prose reference) instead of surfacing both.

    If ``drop_secondary_uncertain`` is True, the secondary's uncertain bucket is
    dropped entirely — useful when the primary signal is strong (real repo
    found) and we'd rather not ask the user to adjudicate prose-regex noise.
    """
    seen = {e["name"].lower() for cat in primary.values() for e in cat}
    merged = {k: list(v) for k, v in primary.items()}
    for cat_key in ("people", "projects", "topics", "uncertain"):
        if cat_key == "uncertain" and drop_secondary_uncertain:
            continue
        for e in secondary.get(cat_key, []):
            if e["name"].lower() in seen:
                continue
            merged.setdefault(cat_key, []).append(e)
            seen.add(e["name"].lower())
    return merged


def discover_entities(
    project_dir: str | os.PathLike,
    languages: tuple = ("en",),
    prose_file_cap: int = 10,
    project_cap: int = 15,
    people_cap: int = 15,
    llm_provider: object = None,
    show_progress: bool = True,
    corpus_origin: dict | None = None,
) -> dict:
    """Top-level entity discovery: real signals first, prose detection second.

    Returns the same dict shape as ``entity_detector.detect_entities`` so it
    plugs into ``confirm_entities`` unchanged.

    Order of signal preference:
      1. Package manifests (package.json, pyproject.toml, Cargo.toml, go.mod)
         → canonical project names
      2. Git commit authors → real people with real commit counts
      3. Claude Code conversation dirs (~/.claude/projects/) → per-session
         project names (pulled from each session's ``cwd`` metadata)
      4. Regex entity detection on prose files → supplementary names only
         mentioned in docs/notes (not code)
      5. Optional LLM refinement pass — reclassifies ambiguous candidates
         using the caller-supplied provider
      6. Optional corpus-origin persona filter — when the corpus is
         identified as AI-dialogue, candidates whose name matches an
         agent_persona_name are moved to an ``agent_personas`` bucket
         instead of being reported as people.

    Passing ``llm_provider`` enables phase-2 refinement. The caller is
    responsible for constructing the provider (``llm_client.get_provider``)
    and confirming availability. Refinement is blocking-interactive:
    progress prints to stderr; Ctrl-C returns partial results.

    Passing ``corpus_origin`` enables corpus-origin persona reclassification.
    The expected shape is the dict written by ``mempalace init`` to
    ``<palace>/.mempalace/origin.json`` (see ``corpus_origin.py``).
    """
    projects, people = scan(project_dir)

    # If the target is a Claude Code conversations root, extract per-project
    # entries from there too. Same ProjectInfo shape, so dedup logic works.
    from mempalace.convo_scanner import is_claude_projects_root, scan_claude_projects

    root_path = Path(project_dir).expanduser().resolve()
    if is_claude_projects_root(root_path):
        convo_projects = scan_claude_projects(root_path)
        # Dedup by name against the git-manifest list, preferring entries
        # with more user_commits as signal strength. Keyed case-insensitively
        # so a `pyproject.toml` name like `mempalace` and a Claude Code
        # `cwd` variant like `MemPalace` collapse into one entry — matches
        # the case-insensitive dedup used in `_merge_detected` and
        # `miner.add_to_known_entities`.
        by_name: dict[str, ProjectInfo] = {p.name.lower(): p for p in projects}
        for cp in convo_projects:
            key = cp.name.lower()
            existing = by_name.get(key)
            if existing is None or cp.user_commits > existing.user_commits:
                by_name[key] = cp
        projects = sorted(
            by_name.values(),
            key=lambda p: (not p.is_mine, -p.user_commits, -p.total_commits, p.name),
        )

    real_signal = to_detected_dict(projects, people, project_cap=project_cap, people_cap=people_cap)

    # Secondary pass: prose-only extraction catches names mentioned in docs
    # that never made a commit (e.g. a stakeholder or family member in notes).
    from mempalace.entity_detector import detect_entities, scan_for_detection

    prose_files = scan_for_detection(str(project_dir), max_files=prose_file_cap)
    prose_detected = (
        detect_entities(prose_files, languages=languages)
        if prose_files
        else {"people": [], "projects": [], "topics": [], "uncertain": []}
    )

    # Without LLM refinement, suppress regex "uncertain" noise when real
    # manifest/git signal exists. With LLM refinement enabled, keep those
    # candidates so the model can promote real entities or drop common words.
    has_real_signal = bool(projects) or bool(people)
    merged = _merge_detected(
        real_signal,
        prose_detected,
        drop_secondary_uncertain=has_real_signal and llm_provider is None,
    )

    # Optional LLM refinement pass (when an llm_provider was supplied).
    if llm_provider is not None:
        from mempalace.llm_refine import collect_corpus_text, refine_entities

        corpus = collect_corpus_text(str(project_dir))
        result = refine_entities(
            merged,
            corpus,
            llm_provider,
            show_progress=show_progress,
            allow_project_promotions=not has_real_signal,
            corpus_origin=corpus_origin,
        )
        if show_progress:
            status_bits = []
            if result.cancelled:
                status_bits.append("cancelled")
            if result.reclassified:
                status_bits.append(f"reclassified {result.reclassified}")
            if result.dropped:
                status_bits.append(f"dropped {result.dropped}")
            if result.errors:
                status_bits.append(f"{len(result.errors)} batch error(s)")
            if status_bits:
                import sys as _sys

                print(f"  LLM refine: {', '.join(status_bits)}", file=_sys.stderr)
        merged = result.merged

    # Corpus-origin persona reclassification — applied last so it sweeps
    # candidates contributed by every upstream source (manifests, git authors,
    # prose, LLM refinement). Idempotent: no corpus_origin → exact v3.3.3 shape.
    if corpus_origin is not None:
        from mempalace.entity_detector import _apply_corpus_origin

        merged = _apply_corpus_origin(merged, corpus_origin)

    return merged


# ==================== CLI ====================


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "."
    projs, ppl = scan(target)
    print(f"=== PROJECTS ({len(projs)}) ===")
    for p in projs[:30]:
        mark = "★" if p.is_mine else " "
        print(f"  {mark} {p.name:35} conf={p.confidence:.2f}  {p.to_signal()}")
    print()
    print(f"=== PEOPLE ({len(ppl)}) ===")
    for p in ppl[:30]:
        print(f"    {p.name:30} conf={p.confidence:.2f}  {p.to_signal()}")
