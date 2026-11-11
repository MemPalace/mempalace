"""Pre-push quick check: changed-file selection, the time budget and the worktree case."""

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from commonplace.guards import prepush


def _git(repo, *args):
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _write(path: Path, text: str = "", mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


FAKE_TOOL = """#!/bin/sh
echo "$0 $*" >> "$TOOL_LOG"
[ -n "$TOOL_SLEEP" ] && exec sleep "$TOOL_SLEEP"
[ "${TOOL_EXIT:-0}" != 0 ] && echo "pkg/mod.py:3: E999 fake finding"
exit "${TOOL_EXIT:-0}"
"""


def _fake_ruff(root: Path) -> Path:
    return _write(root / ".venv" / "bin" / "ruff", FAKE_TOOL, 0o755)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo on ``main`` with a ruff stack and a ``feature`` branch changing one module."""
    top = tmp_path / "repo"
    top.mkdir()
    _git(top, "init", "-q", "-b", "main")
    _write(top / "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    _write(top / "pkg" / "__init__.py")
    _write(top / "pkg" / "mod.py", "X = 1\n")
    _write(top / "tests" / "test_mod.py", "def test_ok():\n    assert True\n")
    _git(top, "add", "-A")
    _git(top, "commit", "-q", "-m", "base")
    _git(top, "checkout", "-q", "-b", "feature")
    _write(top / "pkg" / "mod.py", "X = 2\n")
    _git(top, "commit", "-qam", "change")
    monkeypatch.setenv("TOOL_LOG", str(tmp_path / "tool.log"))
    for name in ("TOOL_SLEEP", "TOOL_EXIT"):
        monkeypatch.delenv(name, raising=False)
    return top


# ── changed-file selection ─────────────────────────────────────────────


@pytest.fixture
def tree(tmp_path):
    for name in (
        "test_guards_stacks.py",
        "test_stacks_extra.py",
        "test_import_grant.py",
        "test_import_grant_more.py",
        "test_import_grants_other.py",
        "test_guards.py",
        "conftest.py",
    ):
        _write(tmp_path / "tests" / name)
    return tmp_path


def test_a_changed_test_file_selects_itself(tree):
    got = prepush.select_tests(tree, ["tests/test_guards_stacks.py"])
    assert got.files == ("tests/test_guards_stacks.py",)
    assert got.unmapped == ()


def test_a_package_module_maps_by_package_and_module_name(tree):
    got = prepush.select_tests(tree, ["commonplace/guards/stacks.py"])
    assert got.files == ("tests/test_guards_stacks.py",)


def test_the_bare_module_name_is_only_a_fallback(tree):
    assert prepush.select_tests(tree, ["commonplace/other/stacks.py"]).files == (
        "tests/test_stacks_extra.py",
    )


def test_a_top_level_module_maps_to_its_exact_and_suffixed_tests_only(tree):
    got = prepush.select_tests(tree, ["commonplace/import_grant.py"])
    assert got.files == ("tests/test_import_grant.py", "tests/test_import_grant_more.py")


def test_a_package_init_maps_to_every_test_of_the_package(tree):
    got = prepush.select_tests(tree, ["commonplace/guards/__init__.py"])
    assert got.files == ("tests/test_guards.py", "tests/test_guards_stacks.py")


def test_unmapped_changes_are_returned_not_dropped(tree):
    got = prepush.select_tests(
        tree, ["README.md", "commonplace/nothing_tests_this.py", "tests/conftest.py", "a.js"]
    )
    assert got.files == ()
    assert got.unmapped == (
        "README.md",
        "commonplace/nothing_tests_this.py",
        "tests/conftest.py",
        "a.js",
    )


def test_a_removed_test_file_is_not_selected(tree):
    got = prepush.select_tests(tree, ["tests/test_gone.py"])
    assert got.files == ()
    assert got.unmapped == ("tests/test_gone.py",)


def test_selection_is_sorted_and_deduplicated(tree):
    got = prepush.select_tests(
        tree, ["commonplace/guards/stacks.py", "tests/test_guards_stacks.py", "x/stacks.py"]
    )
    assert got.files == ("tests/test_guards_stacks.py", "tests/test_stacks_extra.py")


# ── budget ─────────────────────────────────────────────────────────────


def test_budget_defaults_and_overrides():
    warnings = []
    assert prepush.parse_budget(None, warnings.append) == 60.0
    assert prepush.parse_budget("", warnings.append) == 60.0
    assert prepush.parse_budget("12.5", warnings.append) == 12.5
    assert warnings == []


@pytest.mark.parametrize("bad", ["abc", "0", "-3", "nan", "inf"])
def test_an_unusable_budget_warns_and_uses_the_default(bad):
    warnings = []
    assert prepush.parse_budget(bad, warnings.append) == 60.0
    assert len(warnings) == 1 and repr(bad) in warnings[0]


def test_budget_counts_down_on_the_clock():
    now = [100.0]
    budget = prepush.Budget(10, clock=lambda: now[0])
    assert budget.remaining() == 10
    now[0] = 104.0
    assert (budget.elapsed(), budget.remaining()) == (4.0, 6.0)
    now[0] = 500.0
    assert budget.remaining() == 0.0


def _run(top, budget=30.0, **kwargs):
    out = io.StringIO()
    report = prepush.run_quick_checks(
        top,
        kwargs.pop("changed", ["pkg/mod.py", "tests/test_mod.py"]),
        base="main",
        heads=[_git(top, "rev-parse", "HEAD")],
        budget_seconds=budget,
        out=out,
        **kwargs,
    )
    return report, out.getvalue()


def test_a_green_run_says_it_is_a_quick_check_not_the_full_suite(repo):
    _fake_ruff(repo)
    report, text = _run(repo)
    names = {o.name: o.status for o in report.outcomes}
    assert names["lint ruff"] == names["format ruff"] == "passed"
    assert names["tests"] == "passed"
    assert report.exit_code == 0 and report.complete
    assert "QUICK CHECK" in text.splitlines()[0]
    assert "not the full test suite and not CI" in text
    assert "QUICK CHECK PASSED" in text


def test_a_failing_check_fails_the_run_and_prints_the_fix(repo, monkeypatch):
    _fake_ruff(repo)
    monkeypatch.setenv("TOOL_EXIT", "1")
    report, text = _run(repo)
    assert report.exit_code == 1
    assert "QUICK CHECK FAILED" in text
    assert "pkg/mod.py:3" in text
    assert "run: ruff check --fix" in text


def test_budget_runs_out_mid_check_and_the_rest_is_reported_skipped(repo, monkeypatch):
    _fake_ruff(repo)
    monkeypatch.setenv("TOOL_SLEEP", "30")
    report, text = _run(repo, budget=1.0)
    status = {o.name: o.status for o in report.outcomes}
    assert status["lint ruff"] == "incomplete"
    assert status["format ruff"] == "skipped"
    assert status["tests"] == "skipped"
    assert report.exit_code == 0, "cut-short work is reported, never a failure"
    assert not report.complete
    assert "QUICK CHECK INCOMPLETE" in text
    assert "QUICK CHECK PASSED" not in text
    assert "not a pass for those" in text
    assert report.total_seconds < 10


def test_budget_runs_out_mid_tests_and_names_what_was_not_confirmed(repo, monkeypatch):
    _fake_ruff(repo)
    for name, body in {
        "test_a.py": "def test_a():\n    assert True\n",
        "test_b.py": "def test_b():\n    assert True\n",
        "test_c.py": "import time\n\ndef test_c():\n    time.sleep(60)\n",
    }.items():
        _write(repo / "tests" / name, body)
    changed = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]
    report, text = _run(repo, budget=6.0, changed=changed)
    tests = next(o for o in report.outcomes if o.name == "tests")
    assert tests.status == "incomplete"
    assert "2 of 3 test files confirmed finished" in tests.detail
    assert "test_c.py" in tests.detail and "test_a.py" not in tests.detail
    assert "python -m pytest" in (tests.rerun or "")
    assert report.exit_code == 0
    assert "QUICK CHECK INCOMPLETE" in text


def test_a_tool_missing_from_the_worktree_is_skipped_loudly(repo, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(repo / "bin" / "python"))
    report, text = _run(repo, env={"PATH": str(repo / "empty")})
    lint = next(o for o in report.outcomes if o.name == "lint ruff")
    assert lint.status == "skipped" and "ruff is not installed" in lint.detail
    assert "[SKIPPED] lint ruff" in text


def test_a_pushed_commit_that_is_not_checked_out_skips_file_steps(repo):
    _fake_ruff(repo)
    report, text = _run(repo, files_current=False)
    status = {o.name: o.status for o in report.outcomes}
    assert status["lint/format"] == status["tests"] == "skipped"
    assert "not the checked-out HEAD" in text
    assert Path(os.environ["TOOL_LOG"]).exists() is False


def test_docs_check_runs_against_the_base_when_the_repo_has_modules_md(repo):
    _fake_ruff(repo)
    _write(repo / "MODULES.md", "# Modules\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "modules")
    report, text = _run(repo)
    docs = next(o for o in report.outcomes if o.name == "docs-check")
    assert docs.status == "failed"
    assert report.exit_code == 1
    assert "python -m commonplace orch docs-check --base main --head HEAD" in text


def test_docs_check_with_an_unknown_base_is_skipped_not_failed(repo):
    _write(repo / "MODULES.md", "# Modules\n")
    out = io.StringIO()
    report = prepush.run_quick_checks(
        repo,
        ["pkg/mod.py"],
        base="origin/nope",
        heads=[_git(repo, "rev-parse", "HEAD")],
        out=out,
        env={"PATH": ""},
    )
    docs = next(o for o in report.outcomes if o.name == "docs-check")
    assert docs.status == "skipped" and "origin/nope" in docs.detail


# ── worktree: each tree's own tools win ────────────────────────────────


def test_tool_path_puts_the_worktrees_own_bins_first(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
    path = prepush.tool_path(tmp_path, "/usr/bin").split(os.pathsep)
    assert path[:2] == [str(tmp_path / ".venv" / "bin"), str(tmp_path / "node_modules" / ".bin")]
    assert path[-1] == "/usr/bin"
    assert str(Path(sys.executable).parent) in path
    bare = prepush.tool_path(tmp_path / "bare", "/usr/bin").split(os.pathsep)
    assert str(tmp_path / "bare" / ".venv" / "bin") not in bare


def test_a_linked_worktree_runs_its_own_ruff_not_the_one_on_path(repo, tmp_path, monkeypatch):
    decoy = _write(
        tmp_path / "decoy" / "ruff", '#!/bin/sh\necho "decoy $*" >> "$TOOL_LOG"\nexit 1\n', 0o755
    )
    monkeypatch.setenv("PATH", f"{decoy.parent}{os.pathsep}{os.environ['PATH']}")
    _fake_ruff(repo)  # the main checkout also has one; the worktree gets its own below
    work = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(work), "-b", "wt-branch", "feature")
    own = _fake_ruff(work)
    monkeypatch.chdir(work)
    code = prepush.main(["--manual", "--base", "main", "--budget", "20"])
    log = Path(os.environ["TOOL_LOG"]).read_text()
    assert code == 0
    assert str(own) in log
    assert str(repo / ".venv") not in log and "decoy" not in log


def test_main_reads_pushed_refs_from_stdin_and_checks_only_those_files(
    repo, tmp_path, monkeypatch, capsys
):
    _fake_ruff(repo)
    head = _git(repo, "rev-parse", "HEAD")
    zero = "0" * 40
    monkeypatch.chdir(repo)
    stdin = io.StringIO(f"refs/heads/feature {head} refs/heads/feature {zero}\n")
    code = prepush.main(["origin", "url", "--budget", "20"], stdin=stdin)
    err = capsys.readouterr().err
    assert code == 0
    log = Path(os.environ["TOOL_LOG"]).read_text()
    assert "pkg/mod.py" in log and "tests/test_mod.py" not in log.split("format")[0]
    assert "1 changed file," in err


def test_main_with_only_a_delete_has_nothing_to_check(repo, monkeypatch, capsys):
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    stdin = io.StringIO(f"(delete) {'0' * 40} refs/heads/old {head}\n")
    assert prepush.main(["origin", "url"], stdin=stdin) == 0
    assert "nothing to check" in capsys.readouterr().err


def test_a_bad_budget_value_does_not_block_the_push(repo, monkeypatch, capsys):
    _fake_ruff(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv(prepush.BUDGET_ENV, "soon")
    assert prepush.main(["--manual", "--base", "main"]) == 0
    assert "ignoring budget 'soon'" in capsys.readouterr().err


def test_the_module_runs_as_a_script_from_the_worktree(repo):
    _fake_ruff(repo)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "commonplace.guards.prepush",
            "--manual",
            "--base",
            "main",
            "--budget",
            "20",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
    )
    assert proc.returncode == 0, proc.stderr
    assert "QUICK CHECK" in proc.stderr


def test_a_tool_beside_the_interpreter_in_use_is_found(repo, monkeypatch):
    """A hook pointed at another environment (COMMONPLACE_PYTHON) still finds that env's ruff."""
    other = repo.parent / "other-env" / "bin"
    _fake_ruff(other.parent.parent / "other-env-root")  # unrelated, must not be used
    _write(other / "ruff", FAKE_TOOL, 0o755)
    monkeypatch.setattr(sys, "executable", str(other / "python"))
    report, _ = _run(repo, env={"PATH": "", "TOOL_LOG": os.environ["TOOL_LOG"]})
    lint = next(o for o in report.outcomes if o.name == "lint ruff")
    assert lint.status == "passed"
