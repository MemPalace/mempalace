"""Tests for commonplace.guards.stacks: detection, planning, budgeted runs, real tools."""

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from commonplace.guards.stacks import (
    PlannedCommand,
    Stack,
    detect_stacks,
    plan_commands,
    resolve_executable,
    run_checks,
)


def _names(stacks):
    return [stack.name for stack in stacks]


def _write(root: Path, name: str, text: str = "") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ── detection ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("marker", ["ruff.toml", ".ruff.toml"])
def test_ruff_detected_from_its_own_config_file(tmp_path, marker):
    _write(tmp_path, marker, "line-length = 100\n")
    stacks = detect_stacks(tmp_path)
    assert stacks == [Stack("ruff", (".py", ".pyi"), marker)]


def test_ruff_detected_from_pyproject_tool_table(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n\n[tool.ruff.lint]\nselect = []\n")
    assert _names(detect_stacks(tmp_path)) == ["ruff"]
    assert detect_stacks(tmp_path)[0].marker == "pyproject.toml"


def test_pyproject_without_ruff_table_is_not_ruff(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n# [tool.ruff] is only a comment\n")
    assert detect_stacks(tmp_path) == []


def test_oxlint_from_config_or_package_dependency(tmp_path):
    _write(tmp_path / "config", ".oxlintrc.json", "{}")
    stacks = detect_stacks(tmp_path / "config")
    assert stacks == [Stack("oxlint", stacks[0].extensions, ".oxlintrc.json")]
    assert ".tsx" in stacks[0].extensions

    _write(tmp_path / "dep", "package.json", json.dumps({"devDependencies": {"oxlint": "^1"}}))
    stacks = detect_stacks(tmp_path / "dep")
    assert _names(stacks) == ["oxlint"]
    assert stacks[0].marker == "package.json"


def test_eslint_from_flat_config_legacy_config_or_package_key(tmp_path):
    _write(tmp_path / "flat", "eslint.config.mjs", "export default [];\n")
    assert detect_stacks(tmp_path / "flat")[0].marker == "eslint.config.mjs"

    _write(tmp_path / "legacy", ".eslintrc.yml", "root: true\n")
    assert detect_stacks(tmp_path / "legacy")[0].marker == ".eslintrc.yml"

    _write(tmp_path / "pkg", "package.json", json.dumps({"eslintConfig": {"root": True}}))
    assert detect_stacks(tmp_path / "pkg")[0].marker == "package.json"


def test_biome_and_prettier_detection_and_extensions(tmp_path):
    _write(tmp_path, "biome.jsonc", "{}")
    _write(tmp_path, ".prettierrc.toml", "semi = false\n")
    stacks = detect_stacks(tmp_path)
    assert _names(stacks) == ["biome", "prettier"]
    biome, prettier = stacks
    assert biome.marker == "biome.jsonc"
    assert ".jsonc" in biome.extensions and ".css" in biome.extensions
    assert prettier.marker == ".prettierrc.toml"
    assert ".md" in prettier.extensions and ".scss" in prettier.extensions


def test_prettier_from_package_key(tmp_path):
    _write(tmp_path, "package.json", json.dumps({"prettier": {"semi": False}}))
    assert detect_stacks(tmp_path)[0].marker == "package.json"


def test_malformed_package_json_is_ignored(tmp_path):
    _write(tmp_path, "package.json", '{"devDependencies": {"oxlint": ')
    _write(tmp_path, "tsconfig.json", "{}")
    assert _names(detect_stacks(tmp_path)) == ["tsc"]


def test_non_object_package_json_is_ignored(tmp_path):
    _write(tmp_path, "package.json", "[1, 2]")
    assert detect_stacks(tmp_path) == []


def test_tsc_cargo_and_gofmt_detection(tmp_path):
    _write(tmp_path, "tsconfig.json", "{}")
    _write(tmp_path, "Cargo.toml", "[package]\n")
    _write(tmp_path, "go.mod", "module x\n")
    stacks = detect_stacks(tmp_path)
    assert _names(stacks) == ["tsc", "cargo-fmt", "gofmt"]
    assert [s.marker for s in stacks] == ["tsconfig.json", "Cargo.toml", "go.mod"]


def test_stack_order_is_stable_as_listed(tmp_path):
    _write(tmp_path, "go.mod", "module x\n")
    _write(tmp_path, "ruff.toml", "")
    _write(tmp_path, "biome.json", "{}")
    _write(tmp_path, "package.json", json.dumps({"devDependencies": {"oxlint": "^1"}}))
    assert _names(detect_stacks(tmp_path)) == ["ruff", "oxlint", "biome", "gofmt"]


def test_empty_repo_has_no_stacks(tmp_path):
    assert detect_stacks(tmp_path) == []


# ── planning ───────────────────────────────────────────────────────────


def test_plan_limits_each_stack_to_its_changed_files(tmp_path):
    _write(tmp_path, "ruff.toml", "")
    _write(tmp_path, "biome.json", "{}")
    stacks = detect_stacks(tmp_path)
    changed = ["a.py", "sub/d.pyi", "web/x.jsx", "README.md", "notes.txt"]
    commands = plan_commands(tmp_path, stacks, changed)
    assert [c.name for c in commands] == ["ruff", "biome"]
    ruff, biome = commands
    assert ruff.argv == ("ruff", "check", "--force-exclude", "a.py", "sub/d.pyi")
    assert ruff.files == ("a.py", "sub/d.pyi")
    assert ruff.fix == "ruff check --fix a.py sub/d.pyi"
    assert biome.argv == ("biome", "check", "--no-errors-on-unmatched", "web/x.jsx")
    assert biome.fix == "biome check --write web/x.jsx"


def test_no_command_when_no_changed_file_matches(tmp_path):
    _write(tmp_path, "ruff.toml", "")
    assert plan_commands(tmp_path, detect_stacks(tmp_path), ["README.md"]) == []


def test_tsc_is_one_project_command_without_fix(tmp_path):
    _write(tmp_path, "tsconfig.json", "{}")
    commands = plan_commands(tmp_path, detect_stacks(tmp_path), ["a.ts", "b/c.tsx", "d.ts"])
    assert commands == [
        PlannedCommand(
            name="tsc",
            argv=("tsc", "--noEmit", "-p", "tsconfig.json"),
            files=("a.ts", "b/c.tsx", "d.ts"),
            fix=None,
        )
    ]


def test_fix_strings_quote_files_with_spaces(tmp_path):
    _write(tmp_path, "go.mod", "module x\n")
    commands = plan_commands(tmp_path, detect_stacks(tmp_path), ["my dir/it's.go"])
    assert commands[0].argv == ("gofmt", "-l", "my dir/it's.go")
    assert commands[0].fail_on_output is True
    assert commands[0].fix == "gofmt -w 'my dir/it'\"'\"'s.go'"


def test_cargo_fmt_and_gofmt_argv(tmp_path):
    _write(tmp_path, "Cargo.toml", "[package]\n")
    _write(tmp_path, "go.mod", "module x\n")
    cargo, gofmt = plan_commands(tmp_path, detect_stacks(tmp_path), ["src/lib.rs", "m.go"])
    assert cargo.argv == ("cargo", "fmt", "--check", "--", "src/lib.rs")
    assert cargo.fix == "cargo fmt -- src/lib.rs"
    assert gofmt.argv == ("gofmt", "-l", "m.go")
    assert gofmt.fix == "gofmt -w m.go"


def test_changed_paths_are_repo_relative(tmp_path):
    _write(tmp_path, "ruff.toml", "")
    changed = [str(tmp_path / "a.py"), "./b.py", "./b.py"]
    commands = plan_commands(tmp_path, detect_stacks(tmp_path), changed)
    assert commands[0].files == ("a.py", "b.py")


def test_resolve_prefers_node_modules_bin_for_js_tools(tmp_path):
    local = tmp_path / "node_modules" / ".bin" / "prettier"
    _write(tmp_path, "node_modules/.bin/prettier", "#!/bin/sh\n")
    assert resolve_executable(tmp_path, "prettier") == str(local)
    assert resolve_executable(tmp_path, "definitely-not-a-tool-xyz") is None


# ── running ────────────────────────────────────────────────────────────


def _cmd(code: str, **kwargs) -> PlannedCommand:
    return PlannedCommand(
        name=kwargs.pop("name", "fake"), argv=(sys.executable, "-c", code), **kwargs
    )


def test_passing_command_prints_header_and_one_line_summary(tmp_path):
    out = io.StringIO()
    commands = [_cmd("pass", files=("a.py",)), _cmd("pass", files=("b.py", "c.py"))]
    report = run_checks(commands, tmp_path, out=out)
    assert report.ok and not report.over_budget
    assert [r.status for r in report.results] == ["passed", "passed"]
    lines = out.getvalue().splitlines()
    assert lines[0] == "commonplace checks: 2 commands on 3 files, budget 10s"
    assert lines[-1].startswith("commonplace checks: done in ") and lines[-1].endswith(
        ", budget 10s"
    )
    assert "over budget" not in out.getvalue()


def test_failure_prints_fix_and_listed_locations_only(tmp_path):
    code = "print('a.py:3:1: E999 msg'); print('other.py:9:1: E1'); raise SystemExit(1)"
    cmd = _cmd(code, name="ruff", files=("a.py",), fix="ruff check --fix a.py")
    out = io.StringIO()
    report = run_checks([cmd], tmp_path, out=out)
    result = report.results[0]
    assert result.status == "failed" and not report.ok
    assert result.locations == ["a.py:3"]
    text = out.getvalue()
    assert "ruff: failed in" in text
    assert "  fix: ruff check --fix a.py" in text
    assert "\n  a.py:3\n" in text
    assert "other.py" not in text


def test_tsc_style_locations_are_parsed(tmp_path):
    code = "print('src/bad.ts(4,7): error TS2322: nope'); raise SystemExit(2)"
    cmd = _cmd(code, name="tsc", files=("src/bad.ts",))
    report = run_checks([cmd], tmp_path, out=io.StringIO())
    assert report.results[0].locations == ["src/bad.ts:4"]


def test_missing_tool_is_printed_and_does_not_fail(tmp_path):
    cmd = PlannedCommand(name="ruff", argv=("definitely-not-installed-xyz", "check"))
    out = io.StringIO()
    report = run_checks([cmd], tmp_path, out=out)
    assert report.results[0].status == "missing"
    assert report.ok
    assert "ruff: not installed, not checked (install it or remove the stack)" in out.getvalue()


def test_hard_timeout_is_a_failure(tmp_path):
    cmd = _cmd("import time; time.sleep(30)", name="slow")
    out = io.StringIO()
    report = run_checks([cmd], tmp_path, out=out, hard_timeout_seconds=0.5)
    assert report.results[0].status == "timeout"
    assert not report.ok
    assert "slow: timed out after 0.5s (hard limit)" in out.getvalue()


def test_fail_on_output_reports_listed_files_even_on_exit_zero(tmp_path):
    listing = _cmd(
        "print('x.go'); print('./y.go')",
        name="gofmt",
        files=("x.go", "y.go"),
        fix="gofmt -w x.go y.go",
        fail_on_output=True,
    )
    clean = _cmd("pass", name="gofmt", files=("x.go",), fail_on_output=True)
    report = run_checks([listing, clean], tmp_path, out=io.StringIO())
    assert report.results[0].status == "failed"
    assert report.results[0].locations == ["x.go", "y.go"]
    assert report.results[1].status == "passed"


def test_shell_command_runs_through_the_shell(tmp_path):
    passing = PlannedCommand(name="cfg-ok", shell=f'"{sys.executable}" -c "pass"')
    failing = PlannedCommand(name="cfg-bad", shell=f'"{sys.executable}" -c "raise SystemExit(2)"')
    report = run_checks([passing, failing], tmp_path, out=io.StringIO())
    assert [r.status for r in report.results] == ["passed", "failed"]


def test_over_budget_prints_timings_slowest_first(tmp_path):
    ticks = iter([0.0, 2.0, 2.0, 9.0])  # first command takes 2s, second 7s
    out = io.StringIO()
    commands = [_cmd("pass", name="first"), _cmd("pass", name="second")]
    report = run_checks(commands, tmp_path, budget_seconds=5, out=out, clock=lambda: next(ticks))
    assert report.over_budget and report.total_seconds == 9.0
    text = out.getvalue()
    assert "over budget: 9.0s of 5s" in text
    assert text.index("  second: 7.0s") < text.index("  first: 2.0s")


def test_under_budget_prints_only_the_total(tmp_path):
    ticks = iter([0.0, 1.5])
    out = io.StringIO()
    report = run_checks([_cmd("pass")], tmp_path, out=out, clock=lambda: next(ticks))
    assert not report.over_budget
    assert out.getvalue().splitlines()[-1] == "commonplace checks: done in 1.5s, budget 10s"


def test_locations_are_capped_at_twenty(tmp_path):
    code = "\n".join(
        ["for i in range(1, 26):", "    print(f'a.py:{i}:1: E1')", "raise SystemExit(1)"]
    )
    out = io.StringIO()
    report = run_checks([_cmd(code, files=("a.py",))], tmp_path, out=out)
    assert len(report.results[0].locations) == 25
    text = out.getvalue()
    assert text.count("  a.py:") == 20
    assert "  ... and 5 more" in text


def test_unparsed_failure_prints_last_twenty_output_lines(tmp_path):
    code = "\n".join(["for i in range(30):", "    print(f'line {i}')", "raise SystemExit(1)"])
    out = io.StringIO()
    report = run_checks([_cmd(code, files=("a.py",))], tmp_path, out=out)
    assert report.results[0].locations == []
    text = out.getvalue()
    assert text.count("  line ") == 20
    assert "  line 10\n" in text and "  line 9\n" not in text


# ── real-tool smoke: one clean file passes, one bad file fails ──────────


def _require_tool(monkeypatch, name: str) -> None:
    """Skip unless the tool is on PATH; the interpreter's bin dir (venv) goes first."""
    bin_dir = str(Path(sys.executable).parent)
    if shutil.which(name, path=bin_dir) is None and shutil.which(name) is None:
        pytest.skip(f"{name} is not installed (not on PATH), real-tool smoke test skipped")
    monkeypatch.setenv("PATH", bin_dir + os.pathsep + os.environ.get("PATH", ""))


def _run_stack(root: Path, stack_name: str, changed: list[str]):
    commands = plan_commands(root, detect_stacks(root), changed)
    selected = [command for command in commands if command.name == stack_name]
    assert len(selected) == 1, [command.name for command in commands]
    return run_checks(selected, root, out=io.StringIO())


def _smoke(root: Path, stack_name: str, clean: tuple[str, str], bad: tuple[str, str]) -> None:
    """The clean file passes alone; with the bad file added the stack fails on it."""
    clean_name, clean_text = clean
    bad_name, bad_text = bad
    _write(root, clean_name, clean_text)
    passed = _run_stack(root, stack_name, [clean_name]).results[0]
    assert passed.status == "passed", passed.output

    _write(root, bad_name, bad_text)
    report = _run_stack(root, stack_name, [clean_name, bad_name])
    failed = report.results[0]
    assert failed.status == "failed", failed.output
    named = any(loc.split(":")[0] == bad_name for loc in failed.locations)
    assert named or bad_name in failed.output, failed.output
    assert not report.ok


def test_smoke_ruff(tmp_path, monkeypatch):
    _require_tool(monkeypatch, "ruff")
    _write(tmp_path, "ruff.toml", "line-length = 100\n")
    _smoke(
        tmp_path, "ruff", ("clean.py", "import os\n\nprint(os.sep)\n"), ("bad.py", "import sys\n")
    )


def test_smoke_gofmt(tmp_path, monkeypatch):
    _require_tool(monkeypatch, "gofmt")
    _write(tmp_path, "go.mod", "module example.com/smoke\n\ngo 1.21\n")
    _smoke(
        tmp_path,
        "gofmt",
        ("clean.go", "package main\n\nfunc main() {}\n"),
        ("bad.go", "package main\nfunc main(){}\n"),
    )


def test_smoke_cargo_fmt(tmp_path, monkeypatch):
    _require_tool(monkeypatch, "cargo")
    probe = subprocess.run(["cargo", "fmt", "--version"], capture_output=True, check=False)
    if probe.returncode != 0:
        pytest.skip("rustfmt is not installed (cargo fmt --version failed), smoke test skipped")
    _write(
        tmp_path, "Cargo.toml", '[package]\nname = "smoke"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    _write(tmp_path, "src/main.rs", "fn main() {}\n")
    _smoke(
        tmp_path,
        "cargo-fmt",
        ("src/clean.rs", "fn clean() {}\n"),
        ("src/bad.rs", "fn bad( ) {}\n"),
    )


def test_smoke_prettier(tmp_path, monkeypatch):
    _require_tool(monkeypatch, "prettier")
    _write(tmp_path, ".prettierrc", "{}\n")
    _smoke(
        tmp_path, "prettier", ("clean.js", "const a = 1;\n"), ("bad.js", "const a   =   {b:1}\n")
    )


def test_smoke_eslint(tmp_path, monkeypatch):
    _require_tool(monkeypatch, "eslint")
    _write(tmp_path, "eslint.config.mjs", 'export default [{ rules: { "no-var": "error" } }];\n')
    _smoke(tmp_path, "eslint", ("clean.js", "const a = 1;\n"), ("bad.js", "var b = 2;\n"))


def test_smoke_biome(tmp_path, monkeypatch):
    _require_tool(monkeypatch, "biome")
    _write(tmp_path, "biome.json", "{}\n")
    _smoke(tmp_path, "biome", ("clean.js", "const a = 1;\n"), ("bad.js", "const a   =   {b:1}\n"))


def test_smoke_oxlint(tmp_path, monkeypatch):
    _require_tool(monkeypatch, "oxlint")
    _write(tmp_path, ".oxlintrc.json", "{}\n")
    _smoke(
        tmp_path,
        "oxlint",
        ("clean.js", "const a = 1;\nconsole.log(a);\n"),
        ("bad.js", "debugger;\n"),
    )


def test_smoke_tsc(tmp_path, monkeypatch):
    _require_tool(monkeypatch, "tsc")
    _write(
        tmp_path,
        "tsconfig.json",
        '{"compilerOptions": {"strict": true, "noEmit": true}, "include": ["*.ts"]}\n',
    )
    _smoke(
        tmp_path,
        "tsc",
        ("clean.ts", "export const a: number = 1;\n"),
        ("bad.ts", 'export const b: number = "x";\n'),
    )
