"""Tests for mempalace.project_scanner."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mempalace.project_scanner import (
    PersonInfo,
    ProjectInfo,
    _dedupe_people,
    _is_bot,
    _looks_like_real_name,
    _collect_manifest_names,
    _merge_detected,
    _parse_cargo,
    _parse_gomod,
    _parse_package_json,
    _parse_pyproject,
    _UnionFind,
    discover_entities,
    find_git_repos,
    scan,
    to_detected_dict,
)

# Keep only a small portability-focused allowlist for git subprocesses in tests.
GIT_ENV_ALLOWLIST = ("HOME", "SystemRoot", "ComSpec", "TMPDIR", "TEMP", "TMP")
GIT_EXECUTABLE = shutil.which("git")


def _gitdir_marker(path: Path) -> str:
    return f"gitdir: {path}\n"


# ── manifest parsers ────────────────────────────────────────────────────


def test_parse_package_json(tmp_path):
    f = tmp_path / "package.json"
    f.write_text(json.dumps({"name": "my-package", "version": "1.0.0"}))
    assert _parse_package_json(f) == "my-package"


def test_parse_package_json_missing_name(tmp_path):
    f = tmp_path / "package.json"
    f.write_text(json.dumps({"version": "1.0.0"}))
    assert _parse_package_json(f) is None


def test_parse_package_json_malformed(tmp_path):
    f = tmp_path / "package.json"
    f.write_text("{ not valid json")
    assert _parse_package_json(f) is None


def test_parse_pyproject_pep621(tmp_path):
    f = tmp_path / "pyproject.toml"
    f.write_text('[project]\nname = "my-py-package"\n')
    assert _parse_pyproject(f) == "my-py-package"


def test_parse_pyproject_poetry(tmp_path):
    f = tmp_path / "pyproject.toml"
    f.write_text('[tool.poetry]\nname = "poetry-pkg"\n')
    assert _parse_pyproject(f) == "poetry-pkg"


def test_parse_cargo(tmp_path):
    f = tmp_path / "Cargo.toml"
    f.write_text('[package]\nname = "rust-crate"\nversion = "0.1.0"\n')
    assert _parse_cargo(f) == "rust-crate"


def test_parse_gomod(tmp_path):
    f = tmp_path / "go.mod"
    f.write_text("module github.com/user/my-go-mod\n\ngo 1.21\n")
    assert _parse_gomod(f) == "my-go-mod"


# ── bot filtering ───────────────────────────────────────────────────────


def test_is_bot_catches_github_actions():
    assert _is_bot("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")


def test_is_bot_catches_dependabot():
    assert _is_bot("dependabot[bot]", "dependabot@github.com")


def test_is_bot_catches_pr_bot():
    assert _is_bot("Comfy Org PR Bot", "prbot@example.com")


def test_is_bot_does_not_flag_github_privacy_email():
    # Real humans use ...@users.noreply.github.com when privacy is enabled.
    # Must NOT be filtered.
    assert not _is_bot("Igor Lins e Silva", "123456+igorls@users.noreply.github.com")


def test_is_bot_does_not_flag_robot_person_name():
    # "Robot" as a surname should not trigger the \bbot$ pattern
    # since \b requires a boundary before 'bot'.
    assert not _is_bot("Sarah Robot", "sarah@example.com")


def test_looks_like_real_name_accepts_human():
    assert _looks_like_real_name("Igor Lins e Silva")
    assert _looks_like_real_name("Jane Doe")


def test_looks_like_real_name_rejects_handles():
    assert not _looks_like_real_name("666ghj")
    assert not _looks_like_real_name("comfyanonymous")
    assert not _looks_like_real_name("bensig")
    assert not _looks_like_real_name("")
    assert not _looks_like_real_name("no_spaces_handle")


# ── union-find dedup ────────────────────────────────────────────────────


def test_unionfind_merges_shared_email():
    commits = [
        ("Milla J", "shared@example.com", "repo1"),
        ("MSL", "shared@example.com", "repo1"),
        ("Milla J", "other@example.com", "repo1"),
    ]
    people = _dedupe_people(commits)
    # All three commits collapse into one "Milla J" person (MSL is filtered
    # as display name because it lacks a space but its commits still count).
    assert "Milla J" in people
    assert people["Milla J"].total_commits == 3
    assert "MSL" not in people


def test_unionfind_keeps_distinct_people_separate():
    commits = [
        ("Alice Example", "alice@example.com", "r"),
        ("Bob Sample", "bob@sample.org", "r"),
    ]
    people = _dedupe_people(commits)
    assert "Alice Example" in people
    assert "Bob Sample" in people


def test_unionfind_merges_shared_name():
    """Same display name, two different emails, same person."""
    commits = [
        ("Jane Doe", "jane@work.com", "r"),
        ("Jane Doe", "jane@personal.com", "r"),
    ]
    people = _dedupe_people(commits)
    assert people["Jane Doe"].total_commits == 2
    assert len(people["Jane Doe"].emails) == 2


# ── project_info / person_info ─────────────────────────────────────────


def test_project_info_confidence_is_mine():
    p = ProjectInfo(name="x", repo_root=Path("."), is_mine=True)
    assert p.confidence == 0.99


def test_project_info_confidence_no_git():
    p = ProjectInfo(name="x", repo_root=Path("."), has_git=False, manifest="package.json")
    assert p.confidence > 0.8


def test_person_info_signal_pluralization():
    p = PersonInfo(name="x", total_commits=1, repos={"a"})
    assert "1 commit across 1 repo" == p.to_signal()
    p2 = PersonInfo(name="y", total_commits=5, repos={"a", "b"})
    assert "5 commits across 2 repos" == p2.to_signal()


# ── find_git_repos ──────────────────────────────────────────────────────


def test_find_git_repos_detects_root_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    repos = find_git_repos(tmp_path)
    assert tmp_path in repos


def test_find_git_repos_detects_nested(tmp_path):
    sub = tmp_path / "subproject"
    sub.mkdir()
    (sub / ".git").mkdir()
    repos = find_git_repos(tmp_path)
    assert sub in repos


def test_find_git_repos_skips_nested_inside_repo(tmp_path):
    """If root is a repo, nested repos are still discovered as separate roots."""
    (tmp_path / ".git").mkdir()
    deep = tmp_path / "a" / "b" / "nested-repo"
    deep.mkdir(parents=True)
    (deep / ".git").mkdir()
    repos = find_git_repos(tmp_path)
    assert tmp_path in repos
    assert deep in repos


def test_find_git_repos_detects_git_file_markers(tmp_path):
    (tmp_path / ".git").write_text(_gitdir_marker(tmp_path.parent / "root.git"))
    sub = tmp_path / "subproject"
    sub.mkdir()
    (sub / ".git").write_text(_gitdir_marker(tmp_path.parent / "sub.git"))
    repos = find_git_repos(tmp_path)
    assert tmp_path in repos
    assert sub in repos


def test_find_git_repos_empty_dir(tmp_path):
    assert find_git_repos(tmp_path) == []


# ── scan ────────────────────────────────────────────────────────────────


def _require_git() -> None:
    if GIT_EXECUTABLE is None:
        pytest.skip("git executable not available")


def _git_test_env(name: str, email: str) -> dict[str, str]:
    env = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    for key in GIT_ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _git(*args: str) -> list[str]:
    _require_git()
    assert GIT_EXECUTABLE is not None
    return [GIT_EXECUTABLE, *args]


def _git_commit(
    path: Path, filename: str, content: str, message: str, name: str, email: str
) -> None:
    _require_git()
    env = _git_test_env(name, email)
    (path / filename).write_text(content)
    subprocess.run(_git("add", filename), cwd=path, check=True, env=env)
    subprocess.run(_git("commit", "-q", "-m", message), cwd=path, check=True, env=env)


def _init_git_repo(path: Path, name: str = "Jane Doe", email: str = "jane@example.com"):
    """Helper: init a git repo with one commit."""
    _require_git()
    subprocess.run(_git("init", "-q"), cwd=path, check=True)
    subprocess.run(_git("config", "user.name", name), cwd=path, check=True)
    subprocess.run(_git("config", "user.email", email), cwd=path, check=True)
    subprocess.run(_git("config", "commit.gpgsign", "false"), cwd=path, check=True)
    _git_commit(path, "README.md", "hello", "initial", name, email)


def test_scan_project_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "my-app"}))
    _init_git_repo(tmp_path)
    projects, people = scan(tmp_path)
    assert len(projects) == 1
    assert projects[0].name == "my-app"
    assert projects[0].is_mine is True


def test_scan_project_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "pyproj"\n')
    _init_git_repo(tmp_path)
    projects, _ = scan(tmp_path)
    assert any(p.name == "pyproj" for p in projects)


def test_scan_prefers_root_manifest_with_explicit_priority(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "package-name"}))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "pyproject-name"\n')
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "package.json").write_text(json.dumps({"name": "nested-name"}))
    _init_git_repo(tmp_path)
    projects, _ = scan(tmp_path)
    assert projects[0].name == "pyproject-name"


def test_scan_fallback_to_dir_name_when_no_manifest(tmp_path):
    repo = tmp_path / "my-repo-name"
    repo.mkdir()
    _init_git_repo(repo)
    projects, _ = scan(tmp_path)
    assert any(p.name == "my-repo-name" for p in projects)


def test_scan_manifest_only_no_git(tmp_path):
    """A dir with a manifest but no git still produces a project."""
    (tmp_path / "package.json").write_text(json.dumps({"name": "manifest-only"}))
    projects, people = scan(tmp_path)
    assert len(projects) == 1
    assert projects[0].name == "manifest-only"
    assert projects[0].has_git is False
    assert people == []


def test_collect_manifest_names_stops_at_git_file_boundary(tmp_path):
    (tmp_path / ".git").write_text(_gitdir_marker(tmp_path.parent / "root.git"))
    (tmp_path / "package.json").write_text(json.dumps({"name": "root-name"}))
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".git").write_text(_gitdir_marker(tmp_path.parent / "nested.git"))
    (nested / "package.json").write_text(json.dumps({"name": "nested-name"}))
    manifests = _collect_manifest_names(tmp_path)
    assert [name for _file, name, _dir in manifests] == ["root-name"]


def test_scan_excludes_bot_commits_from_totals(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "my-app"}))
    _init_git_repo(tmp_path, name="Jane Doe", email="jane@example.com")
    _git_commit(
        tmp_path,
        "bot.txt",
        "generated",
        "bot update",
        "github-actions[bot]",
        "41898282+github-actions[bot]@users.noreply.github.com",
    )
    projects, people = scan(tmp_path)
    assert projects[0].total_commits == 1
    assert projects[0].user_commits == 1
    assert [person.name for person in people] == ["Jane Doe"]


def test_scan_empty_dir(tmp_path):
    projects, people = scan(tmp_path)
    assert projects == []
    assert people == []


def test_scan_returns_empty_for_nonexistent(tmp_path):
    missing = tmp_path / "does-not-exist"
    projects, people = scan(missing)
    assert projects == []
    assert people == []


# ── to_detected_dict ────────────────────────────────────────────────────


def test_to_detected_dict_shape():
    projects = [ProjectInfo(name="p", repo_root=Path("."), is_mine=True, manifest="package.json")]
    people = [PersonInfo(name="Jane Doe", total_commits=5, repos={"r"})]
    d = to_detected_dict(projects, people)
    # ``topics`` is the LLM-refine bucket for cross-wing tunnel signal —
    # always present even when empty so callers can rely on the shape.
    assert set(d.keys()) == {"people", "projects", "topics", "uncertain"}
    assert d["projects"][0]["name"] == "p"
    assert d["projects"][0]["type"] == "project"
    assert d["people"][0]["name"] == "Jane Doe"
    assert d["people"][0]["type"] == "person"
    assert d["topics"] == []
    assert d["uncertain"] == []


# ── merge ───────────────────────────────────────────────────────────────


def test_merge_primary_wins_case_insensitive():
    primary = {
        "people": [],
        "projects": [
            {
                "name": "mempalace",
                "type": "project",
                "confidence": 0.99,
                "frequency": 10,
                "signals": ["pyproject.toml"],
            }
        ],
        "uncertain": [],
    }
    secondary = {
        "people": [],
        "projects": [],
        "uncertain": [
            {
                "name": "MemPalace",
                "type": "uncertain",
                "confidence": 0.4,
                "frequency": 6,
                "signals": ["regex"],
            }
        ],
    }
    merged = _merge_detected(primary, secondary)
    # `MemPalace` (uncertain) is deduped against `mempalace` (project) case-insensitively
    assert len(merged["projects"]) == 1
    assert len(merged["uncertain"]) == 0


def test_merge_drops_secondary_uncertain_when_requested():
    primary = {"people": [], "projects": [], "uncertain": []}
    secondary = {
        "people": [],
        "projects": [],
        "uncertain": [
            {"name": "Foo", "type": "uncertain", "confidence": 0.4, "frequency": 3, "signals": []}
        ],
    }
    merged = _merge_detected(primary, secondary, drop_secondary_uncertain=True)
    assert merged["uncertain"] == []


def test_merge_keeps_distinct_names():
    primary = {
        "people": [
            {
                "name": "Alice Smith",
                "type": "person",
                "confidence": 0.9,
                "frequency": 10,
                "signals": [],
            }
        ],
        "projects": [],
        "uncertain": [],
    }
    secondary = {
        "people": [
            {
                "name": "Bob Jones",
                "type": "person",
                "confidence": 0.7,
                "frequency": 3,
                "signals": [],
            }
        ],
        "projects": [],
        "uncertain": [],
    }
    merged = _merge_detected(primary, secondary)
    assert len(merged["people"]) == 2


# ── discover_entities ──────────────────────────────────────────────────


def test_discover_entities_falls_back_to_prose_when_no_git(tmp_path):
    """If no manifests or git, regex detector on prose is the only source."""
    notes = tmp_path / "notes.md"
    notes.write_text(
        "Riley said hello. Riley asked about it. Riley laughed. "
        "Hey Riley, thanks for the help. Riley pushed the change. "
        "Riley decided to go."
    )
    d = discover_entities(str(tmp_path))
    # Prose-only fallback kicks in — Riley appears with person signals
    all_names = [e["name"] for cat in d.values() for e in cat]
    assert "Riley" in all_names


def test_discover_entities_prefers_real_signal_over_prose(tmp_path):
    """When manifest exists, its name wins even if prose has noisy candidates."""
    (tmp_path / "package.json").write_text(json.dumps({"name": "realproj"}))
    _init_git_repo(tmp_path)
    (tmp_path / "doc.md").write_text(
        "Something. Another. Whatever. Context. Context. Context. Context. "
        "realproj. realproj. realproj. realproj."
    )
    d = discover_entities(str(tmp_path))
    proj_names = [e["name"] for e in d["projects"]]
    assert "realproj" in proj_names


def test_discover_entities_keeps_uncertain_for_llm_when_real_signal(tmp_path):
    """With --llm, regex-uncertain prose candidates should reach refinement."""
    (tmp_path / "package.json").write_text(json.dumps({"name": "realproj"}))
    _init_git_repo(tmp_path)
    (tmp_path / "doc.md").write_text("Noise appeared. Noise repeated. Noise again.")

    class FakeProvider:
        def __init__(self):
            self.prompts = []

        def classify(self, _system, user, json_mode=True):
            self.prompts.append(user)
            return SimpleNamespace(
                text='{"classifications": [{"name": "Noise", "label": "COMMON_WORD"}]}'
            )

    provider = FakeProvider()
    d = discover_entities(str(tmp_path), llm_provider=provider, show_progress=False)

    assert len(provider.prompts) == 1
    assert "Noise" in provider.prompts[0]
    assert "Noise" not in [e["name"] for cat in d.values() for e in cat]


def test_discover_entities_keeps_llm_only_project_uncertain_when_real_signal(tmp_path):
    """Repo roots should not auto-promote LLM-only tools/topics into projects."""
    (tmp_path / "package.json").write_text(json.dumps({"name": "realproj"}))
    _init_git_repo(tmp_path)
    (tmp_path / "doc.md").write_text("Terraform shipped. Terraform changed. Terraform runs.")

    class FakeProvider:
        def classify(self, _system, _user, json_mode=True):
            return SimpleNamespace(
                text='{"classifications": [{"name": "Terraform", "label": "PROJECT"}]}'
            )

    d = discover_entities(str(tmp_path), llm_provider=FakeProvider(), show_progress=False)

    assert "realproj" in [e["name"] for e in d["projects"]]
    assert "Terraform" not in [e["name"] for e in d["projects"]]
    assert "Terraform" in [e["name"] for e in d["uncertain"]]


def test_discover_entities_collapses_case_variants_between_manifest_and_convo(tmp_path):
    """A project named `myproj` in a manifest and `MyProj` as a Claude Code
    cwd must collapse into one entry. Matches the case-insensitive dedup
    used by `_merge_detected` and `miner.add_to_known_entities`."""
    root = tmp_path / "projects_root"
    root.mkdir()

    # Entry 1: a git+manifest project named lowercase `myproj`
    repo = root / "-home-u-src-myproj"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"name": "myproj"}))
    _init_git_repo(repo)

    # Entry 2: same root ALSO looks like a Claude Code `.claude/projects/` dir;
    # the convo_scanner inside will resolve `cwd` to `/home/u/src/MyProj`
    # (CamelCase variant of the same project).
    session = repo / "abc.jsonl"
    session.write_text(json.dumps({"type": "user", "cwd": "/home/u/src/MyProj"}) + "\n")

    d = discover_entities(str(root))

    project_names = [e["name"] for e in d["projects"]]
    # One entry, not two. First-seen casing ("myproj" from the manifest scan)
    # is the winner since it was seeded first.
    assert len(project_names) == 1
    assert project_names[0].lower() == "myproj"


# ── _UnionFind basics ──────────────────────────────────────────────────


def test_unionfind_find_creates_singleton():
    uf = _UnionFind()
    assert uf.find("x") == "x"


def test_unionfind_union_merges():
    uf = _UnionFind()
    uf.union("a", "b")
    assert uf.find("a") == uf.find("b")


def test_unionfind_transitive():
    uf = _UnionFind()
    uf.union("a", "b")
    uf.union("b", "c")
    assert uf.find("a") == uf.find("c")
