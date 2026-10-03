"""Black-box regression coverage for release-manifest version checks."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "check_versions.py"
VERSION = "9.12.4"


def _fixture_sources(version=VERSION):
    badge_version = version.replace("-", "--")
    cargo_packages = "\n".join(
        f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
        for name in ("mempalace-cli", "mempalace-core", "mempalace-py")
    )
    return {
        "mempalace/version.py": f'__version__ = "{version}"\n',
        "pyproject.toml": f'[project]\nname = "mempalace"\nversion = "{version}"\n',
        "README.md": (
            "# MemPalace\n"
            f"[version-shield]: https://img.shields.io/badge/version-{badge_version}-4dc9f6?style=flat-square\n"
        ),
        "uv.lock": (
            'version = 1\n[[package]]\nname = "mempalace"\n'
            f'version = "{version}"\nsource = {{ editable = "." }}\n'
        ),
        "Cargo.toml": f'[workspace.package]\nversion = "{version}"\n',
        "Cargo.lock": "version = 4\n" + cargo_packages,
        ".claude-plugin/marketplace.json": json.dumps(
            {"plugins": [{"name": "mempalace", "version": version}]}
        ),
        ".claude-plugin/plugin.json": json.dumps({"name": "mempalace", "version": version}),
        ".codex-plugin/plugin.json": json.dumps({"name": "mempalace", "version": version}),
        ".dsh-plugin/package.json": json.dumps(
            {"name": "@mempalace/dsh-plugin", "version": version}
        ),
        "integrations/openclaw/SKILL.md": f"---\nname: mempalace\nversion: {version}\n---\n# Skill\n",
    }


@pytest.fixture
def fixture_repo(tmp_path):
    for name, content in _fixture_sources().items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def _run(root, *extra):
    return subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(root), *map(str, extra)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _rewrite(root, filename, old, new, count=-1):
    path = root / filename
    content = path.read_text(encoding="utf-8")
    assert old in content
    path.write_text(content.replace(old, new, count), encoding="utf-8")


@pytest.mark.parametrize("filename", ["uv.lock", "Cargo.toml", "Cargo.lock"])
def test_guard_rejects_previously_unchecked_version_drift(fixture_repo, filename):
    _rewrite(fixture_repo, filename, VERSION, "9.12.3", count=1)

    result = _run(fixture_repo)

    assert result.returncode == 1, result.stdout + result.stderr
    diagnostic = result.stdout + result.stderr
    assert filename in diagnostic
    assert VERSION in diagnostic
    assert "9.12.3" in diagnostic


@pytest.mark.parametrize(
    ("filename", "occurrence"),
    [
        ("mempalace/version.py", 0),
        ("pyproject.toml", 0),
        ("README.md", 0),
        ("uv.lock", 0),
        ("Cargo.toml", 0),
        ("Cargo.lock", 0),
        ("Cargo.lock", 1),
        ("Cargo.lock", 2),
        (".claude-plugin/marketplace.json", 0),
        (".claude-plugin/plugin.json", 0),
        (".codex-plugin/plugin.json", 0),
        (".dsh-plugin/package.json", 0),
        ("integrations/openclaw/SKILL.md", 0),
    ],
)
def test_each_version_target_is_checked(fixture_repo, filename, occurrence):
    path = fixture_repo / filename
    parts = path.read_text(encoding="utf-8").split(VERSION)
    assert len(parts) > occurrence + 1
    before = VERSION.join(parts[: occurrence + 1])
    after = VERSION.join(parts[occurrence + 1 :])
    path.write_text(before + "9.12.3" + after, encoding="utf-8")

    result = _run(fixture_repo)

    assert result.returncode == 1, result.stdout + result.stderr
    diagnostic = result.stdout + result.stderr
    assert filename in diagnostic
    assert VERSION in diagnostic
    assert "9.12.3" in diagnostic


@pytest.mark.parametrize("version", [VERSION, "3.10.0", "3.11.0-rc1", "2026.10"])
def test_consistent_versions_pass_without_modifying_sources(fixture_repo, version):
    for name, content in _fixture_sources(version).items():
        (fixture_repo / name).write_text(content, encoding="utf-8")
    before = {name: (fixture_repo / name).read_bytes() for name in _fixture_sources()}

    result = _run(fixture_repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert all((fixture_repo / name).read_bytes() == data for name, data in before.items())


def test_dependency_versions_and_unrelated_badges_are_ignored(fixture_repo):
    additions = {
        "pyproject.toml": '\n[tool.other]\nversion = "0.0.1"\n',
        "Cargo.toml": '\n[dependencies]\nunrelated = "0.0.1"\n',
        "uv.lock": (
            '\n[[package]]\nname = "mempalace"\nversion = "0.0.1"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            '\n[[package]]\nname = "dependency"\nversion = "0.0.2"\n'
            'source = { editable = "." }\n'
        ),
        "Cargo.lock": (
            '\n[[package]]\nname = "mempalace-core"\nversion = "0.0.1"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
            '\n[[package]]\nname = "other"\nversion = "0.0.2"\n'
        ),
        "integrations/openclaw/SKILL.md": "\nversion: 0.0.1\n",
        "README.md": (
            "\n[python-shield]: https://img.shields.io/badge/python-3.9+-blue\n"
            "[other-shield]: https://img.shields.io/badge/version-0.0.1-blue\n"
        ),
    }
    for name, content in additions.items():
        with (fixture_repo / name).open("a", encoding="utf-8") as handle:
            handle.write(content)
    marketplace = fixture_repo / ".claude-plugin/marketplace.json"
    marketplace.write_text(
        json.dumps(
            {
                "version": "0.0.1",
                "plugins": [
                    {"name": "other", "version": "0.0.1"},
                    {"name": "mempalace", "version": VERSION},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _run(fixture_repo)

    assert result.returncode == 0, result.stdout + result.stderr


def test_toml_version_is_selected_from_the_correct_table(fixture_repo):
    for filename, header in [("pyproject.toml", "tool.decoy"), ("Cargo.toml", "package")]:
        path = fixture_repo / filename
        source = path.read_text(encoding="utf-8")
        path.write_text(f'[{header}]\nversion = "0.0.1"\n' + source, encoding="utf-8")

    result = _run(fixture_repo)

    assert result.returncode == 0, result.stdout + result.stderr


def test_percent_encoded_readme_version_matches_the_canonical_version(fixture_repo):
    version = "3.11.0-rc1"
    for filename, source in _fixture_sources(version).items():
        (fixture_repo / filename).write_text(source, encoding="utf-8")
    _rewrite(fixture_repo, "README.md", "3.11.0--rc1", "3.11.0%2Drc1")

    result = _run(fixture_repo)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("filename", list(_fixture_sources()))
def test_missing_source_fails_with_its_path(fixture_repo, filename):
    (fixture_repo / filename).unlink()

    result = _run(fixture_repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert filename in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("mempalace/version.py", "__version__ =\n"),
        ("mempalace/version.py", "__version__ = str(9.12)\n"),
        ("mempalace/version.py", f'__version__ = "{VERSION}"\n__version__ = "{VERSION}"\n'),
        (
            "mempalace/version.py",
            f'__version__ = "{VERSION}"\nif True:\n    __version__ = "9.12.3"\n',
        ),
        ("mempalace/version.py", f'__version__ = "{VERSION}"\n__version__ += ".1"\n'),
        ("pyproject.toml", f'[dependency]\nversion = "{VERSION}"\n'),
        ("pyproject.toml", f'[project]\nversion = "{VERSION}"\nversion = "{VERSION}"\n'),
        ("Cargo.toml", f'[workspace]\nversion = "{VERSION}"\n'),
        ("README.md", f"[unrelated]: https://img.shields.io/badge/version-{VERSION}-blue\n"),
        ("README.md", _fixture_sources()["README.md"] * 2),
        (".claude-plugin/plugin.json", '{"version":'),
        (".claude-plugin/plugin.json", '{"version": null}'),
        (".claude-plugin/plugin.json", '{"version": 9.12}'),
        (".claude-plugin/plugin.json", '{"version": ""}'),
        (".claude-plugin/plugin.json", '{"version": "9.12.4\\npy_version=1.2.3"}'),
        (".claude-plugin/plugin.json", '{"version": "9.12.4", "version": "9.12.4"}'),
        (
            ".claude-plugin/marketplace.json",
            '{"plugins": [{"name": "other", "version": "9.12.4"}]}',
        ),
        (
            ".claude-plugin/marketplace.json",
            json.dumps(
                {
                    "plugins": [
                        {"name": "mempalace", "version": VERSION},
                        {"name": "mempalace", "version": VERSION},
                    ]
                }
            ),
        ),
        (
            "uv.lock",
            '\n[[package]]\nname = "mempalace"\nversion = "9.12.4"\n'
            'source = { registry = "https://pypi.org/simple" }\n',
        ),
        (
            "uv.lock",
            _fixture_sources()["uv.lock"]
            + "\n[[package]]"
            + _fixture_sources()["uv.lock"].split("[[package]]", 1)[1],
        ),
        ("Cargo.lock", 'version = 4\n[[package]]\nname = "mempalace-cli"\nversion = "9.12.4"\n'),
        (
            "Cargo.lock",
            _fixture_sources()["Cargo.lock"]
            + '\n[[package]]\nname = "mempalace-core"\nversion = "9.12.4"\n',
        ),
        ("integrations/openclaw/SKILL.md", f"# Skill\nversion: {VERSION}\n"),
        ("integrations/openclaw/SKILL.md", f"---\nname: mempalace\n---\nversion: {VERSION}\n"),
        ("integrations/openclaw/SKILL.md", f"---\nversion: {VERSION}\nversion: {VERSION}\n---\n"),
    ],
)
def test_malformed_missing_or_ambiguous_targets_fail(fixture_repo, filename, content):
    (fixture_repo / filename).write_text(content, encoding="utf-8")

    result = _run(fixture_repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert filename in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_version_source_is_parsed_without_running_its_code(fixture_repo):
    marker = fixture_repo / "import-would-run.txt"
    (fixture_repo / "mempalace/version.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        f'__version__: str = "{VERSION}"\n',
        encoding="utf-8",
    )

    result = _run(fixture_repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()


def test_successful_github_output_appends_canonical_version(fixture_repo, tmp_path):
    output = tmp_path / "github-output.txt"
    summary = tmp_path / "github-summary.md"
    output.write_text("previous=value\n", encoding="utf-8")
    summary.write_text("Previous summary\n", encoding="utf-8")

    result = _run(fixture_repo, "--github-output", output, "--github-summary", summary)

    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text(encoding="utf-8") == f"previous=value\npy_version={VERSION}\n"
    summary_text = summary.read_text(encoding="utf-8")
    assert summary_text.startswith("Previous summary\n")
    for filename in _fixture_sources():
        assert filename in summary_text
    for name in ("mempalace-cli", "mempalace-core", "mempalace-py"):
        assert name in summary_text


def test_failed_check_does_not_append_github_output(fixture_repo, tmp_path):
    output = tmp_path / "github-output.txt"
    output.write_text("previous=value\n", encoding="utf-8")
    _rewrite(fixture_repo, "uv.lock", VERSION, "9.12.3")

    result = _run(fixture_repo, "--github-output", output)

    assert result.returncode == 1, result.stdout + result.stderr
    assert output.read_text(encoding="utf-8") == "previous=value\n"


def test_invalid_github_output_path_fails_cleanly(fixture_repo):
    result = _run(fixture_repo, "--github-output", fixture_repo)

    assert result.returncode == 1, result.stdout + result.stderr
    assert str(fixture_repo) in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize("flag", ["--github-output", "--github-summary"])
@pytest.mark.parametrize("filename", ["mempalace/version.py", "README.md", "uv.lock"])
def test_report_path_cannot_overwrite_guarded_sources(fixture_repo, flag, filename):
    before = {name: (fixture_repo / name).read_bytes() for name in _fixture_sources()}

    result = _run(fixture_repo, flag, fixture_repo / filename)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert all((fixture_repo / name).read_bytes() == data for name, data in before.items())


@pytest.mark.parametrize("flag", ["--github-output", "--github-summary"])
def test_report_symlink_cannot_overwrite_a_guarded_source(fixture_repo, flag):
    target = fixture_repo / "README.md"
    before = target.read_bytes()
    alias = fixture_repo / "summary-alias.md"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    result = _run(fixture_repo, flag, alias)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert target.read_bytes() == before


def test_default_root_uses_script_location_even_from_another_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def _version_workflow():
    path = REPO_ROOT / ".github" / "workflows" / "version-guard.yml"
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_workflow_runs_on_changes_to_all_guard_inputs_and_guard_implementation():
    workflow = _version_workflow()
    paths = set(workflow["on"]["pull_request"]["paths"])

    assert set(_fixture_sources()) <= paths
    assert {
        "scripts/check_versions.py",
        "tests/test_version_guard.py",
        ".github/workflows/version-guard.yml",
    } <= paths
    assert "v*" in workflow["on"]["push"]["tags"]
    steps = workflow["jobs"]["check-versions"]["steps"]
    assert any("scripts/check_versions.py" in step.get("run", "") for step in steps)


def _bash():
    if sys.platform == "win32":
        git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if git_bash.is_file():
            return str(git_bash)
        pytest.skip("Git Bash is required to validate release tag shell behavior on Windows")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to validate release tag shell behavior")
    return bash


@pytest.mark.parametrize(
    ("tag", "expected_status"),
    [(f"v{VERSION}", 0), ("v9.12.3", 1), ("v9.12.3-rc1", 0)],
)
def test_workflow_preserves_stable_and_prerelease_tag_validation(tmp_path, tag, expected_status):
    workflow = _version_workflow()
    steps = workflow["jobs"]["check-versions"]["steps"]
    tag_step = next(step for step in steps if step.get("name", "").startswith("Verify tag matches"))
    script = tmp_path / "validate-tag.sh"
    with script.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(tag_step["run"])
    summary = tmp_path / "summary.md"
    summary.touch()
    env = dict(os.environ, PY=VERSION, GITHUB_REF_NAME=tag, GITHUB_STEP_SUMMARY=summary.as_posix())

    result = subprocess.run(
        [_bash(), script.as_posix()],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == expected_status, result.stdout + result.stderr
