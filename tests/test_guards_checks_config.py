"""Tests for the per-repo checks.toml parser and command builder."""

from __future__ import annotations

import pytest

from commonplace.guards.checks_config import (
    CONFIG_RELPATH,
    DEFAULT_BUDGET_SECONDS,
    ChecksConfigError,
    CheckSpec,
    load_checks_config,
    parse_checks_config,
)

FULL = """\
budget_seconds = 5
[[check]]
name = "ruff"
run = "ruff check {files}"
globs = ["*.py"]
fix = "ruff check --fix {files}"

[[check]]
name = "tsc"
run = "npx tsc --noEmit"
globs = ["src/*.ts"]
"""


def _error(text: str) -> str:
    with pytest.raises(ChecksConfigError) as info:
        parse_checks_config(text)
    return str(info.value)


def _check(run: str = "lint {files}", **extra: object) -> str:
    lines = ["[[check]]", 'name = "lint"', f'run = "{run}"']
    for key, value in extra.items():
        lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"


def test_defaults_for_empty_file():
    config = parse_checks_config("")
    assert DEFAULT_BUDGET_SECONDS == 10.0
    assert config.budget_seconds == 10.0
    assert config.checks == ()
    assert config.source == CONFIG_RELPATH


def test_full_parse():
    config = parse_checks_config(FULL)
    assert config.budget_seconds == 5.0
    assert isinstance(config.budget_seconds, float)
    ruff, tsc = config.checks
    assert ruff == CheckSpec(
        name="ruff",
        run="ruff check {files}",
        globs=("*.py",),
        fix="ruff check --fix {files}",
    )
    assert tsc == CheckSpec(name="tsc", run="npx tsc --noEmit", globs=("src/*.ts",), fix=None)


def test_default_globs_match_everything():
    (spec,) = parse_checks_config(_check()).checks
    assert spec.globs == ("*",)
    assert spec.files_for(["a/b.py", "README.md"]) == ["a/b.py", "README.md"]


def test_zero_checks_and_float_budget_are_valid():
    config = parse_checks_config("budget_seconds = 2.5\ncheck = []\n")
    assert config.budget_seconds == 2.5
    assert config.checks == ()


def test_custom_source_names_the_file_in_errors():
    with pytest.raises(ChecksConfigError, match="^other/checks.toml: budget: unknown key"):
        parse_checks_config("budget = 5\n", source="other/checks.toml")


def test_bad_toml_names_file_and_line():
    message = _error("[[check]\nname = 'x'\n")
    assert message.startswith(f"{CONFIG_RELPATH}: invalid TOML")
    assert "line 1" in message


def test_unknown_top_level_key_is_named():
    assert _error("budget = 5\n") == f"{CONFIG_RELPATH}: budget: unknown key"


def test_unknown_check_key_is_named():
    message = _error(_check(glob='["*.py"]'))
    assert message == f"{CONFIG_RELPATH}: check[0].glob: unknown key"


@pytest.mark.parametrize(
    "text", ["check = 1\n", "check = [1]\n", '[check]\nname = "x"\nrun = "y"\n']
)
def test_check_must_be_array_of_tables(text):
    assert _error(text) == f"{CONFIG_RELPATH}: check: expected an array of tables ([[check]])"


def test_missing_name_is_required():
    assert _error('[[check]]\nrun = "x"\n') == f"{CONFIG_RELPATH}: check[0].name: required"


def test_missing_run_is_required():
    assert _error('[[check]]\nname = "lint"\n') == f"{CONFIG_RELPATH}: check[0].run: required"


@pytest.mark.parametrize(
    "body",
    [
        'name = ""\nrun = "x"',
        'name = "   "\nrun = "x"',
        'name = 3\nrun = "x"',
        'name = "a"\nrun = ""',
        'name = "a"\nrun = 7',
        'name = "a"\nrun = "x"\nfix = ""',
        'name = "a"\nrun = "x"\nfix = 1',
    ],
)
def test_wrong_string_types_are_rejected(body):
    message = _error(f"[[check]]\n{body}\n")
    assert message.startswith(f"{CONFIG_RELPATH}: check[0].")
    assert "expected a non-empty string" in message


def test_globs_must_be_list_of_strings():
    text = _check() + '\n[[check]]\nname = "b"\nrun = "x"\nglobs = "*.py"\n'
    assert _error(text) == f"{CONFIG_RELPATH}: check[1].globs: expected a list of strings"


def test_globs_entries_must_be_non_empty_strings():
    message = _error(_check(globs="[1]"))
    assert message == f"{CONFIG_RELPATH}: check[0].globs: expected a list of strings"
    message = _error(_check(globs='[""]'))
    assert message == f"{CONFIG_RELPATH}: check[0].globs: expected a list of strings"


def test_globs_must_not_be_empty():
    assert (
        _error(_check(globs="[]"))
        == f"{CONFIG_RELPATH}: check[0].globs: expected at least one glob"
    )


@pytest.mark.parametrize("value", ["true", '"5"', "[5]"])
def test_budget_must_be_a_number(value):
    message = _error(f"budget_seconds = {value}\n")
    assert message == f"{CONFIG_RELPATH}: budget_seconds: expected a number of seconds"


@pytest.mark.parametrize("value", ["0", "-1", "0.0", "nan", "inf"])
def test_budget_must_be_positive_and_finite(value):
    message = _error(f"budget_seconds = {value}\n")
    assert message == (f"{CONFIG_RELPATH}: budget_seconds: must be a finite number greater than 0")


def test_duplicate_check_names_are_rejected():
    text = _check() + _check()
    assert _error(text) == f"{CONFIG_RELPATH}: check[1].name: duplicate check name 'lint'"


def test_basename_glob_matches_nested_files():
    spec = CheckSpec("ruff", "ruff check {files}", ("*.py",), None)
    assert spec.files_for(["a/b.py", "c.txt", "top.py", "a/b.py"]) == ["a/b.py", "top.py"]


def test_glob_with_slash_matches_full_path():
    spec = CheckSpec("tsc", "tsc", ("src/*.ts",), None)
    assert spec.files_for(["src/a.ts", "lib/src/a.ts", "a.ts"]) == ["src/a.ts"]


def test_files_for_normalises_and_keeps_input_order():
    spec = CheckSpec("ruff", "ruff check {files}", ("*.py",), None)
    assert spec.files_for(["z.py", "./a.py", "z.py", "m/q.py"]) == ["z.py", "a.py", "m/q.py"]


def test_command_quotes_files_with_spaces_and_metacharacters():
    spec = CheckSpec("ruff", "ruff check {files}", ("*.py",), None)
    command = spec.command_for(["my dir/a b.py", "c.py", "x.txt", "d;rm -rf x.py"])
    assert command == "ruff check 'my dir/a b.py' c.py 'd;rm -rf x.py'"


def test_command_is_none_without_match():
    spec = CheckSpec("ruff", "ruff check {files}", ("*.py",), None)
    assert spec.command_for(["x.txt"]) is None
    assert spec.command_for([]) is None


def test_command_without_placeholder_is_unchanged():
    spec = CheckSpec("make", "make lint", ("*.py",), None)
    assert spec.command_for(["a.py"]) == "make lint"
    assert spec.command_for(["a.txt"]) is None


def test_fix_command():
    spec = CheckSpec("ruff", "ruff check {files}", ("*.py",), "ruff check --fix {files}")
    assert spec.fix_command_for(["a b.py"]) == "ruff check --fix 'a b.py'"
    assert spec.fix_command_for(["a.txt"]) is None


def test_fix_command_is_none_without_fix():
    spec = CheckSpec("ruff", "ruff check {files}", ("*.py",), None)
    assert spec.fix_command_for(["a.py"]) is None


def test_missing_file_loads_as_none(tmp_path):
    assert load_checks_config(tmp_path) is None


def test_load_from_repo_dir(tmp_path):
    config_dir = tmp_path / ".commonplace"
    config_dir.mkdir()
    (config_dir / "checks.toml").write_text("# café checks\n" + FULL, encoding="utf-8")
    config = load_checks_config(str(tmp_path))
    assert config is not None
    assert config.source == CONFIG_RELPATH
    assert [spec.name for spec in config.checks] == ["ruff", "tsc"]


def test_load_reports_errors_with_relative_path(tmp_path):
    config_dir = tmp_path / ".commonplace"
    config_dir.mkdir()
    (config_dir / "checks.toml").write_text("budget = 5\n", encoding="utf-8")
    with pytest.raises(
        ChecksConfigError, match=r"^\.commonplace/checks\.toml: budget: unknown key"
    ):
        load_checks_config(tmp_path)
