"""docs-check (R21, R21b), plan-check (R22), the board plan gate reader and the orch CLI."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from commonplace.cli import main as cli_main
from commonplace.orch import docs_check, plan_check
from tests._orch_helpers import config_toml, seed_config
from tests.test_orch_plan_gate import PLAN

REPO = Path(__file__).resolve().parents[1]

MODULES = """# Modules

Preamble text is ignored.

## docs

- **Paths:** `SYSTEM.md`, `MODULES.md`
- **Owns:** The decisions and this map.
- **Promises:** Every entry has one owner.
- **How it fails:** Text drifts.
- **Tests:** tests/test_checks.py

## core

- **Paths:** `commonplace/core.py`, `README.md`
- **Owns:** The core module.
- **Promises:** It works.
- **How it fails:** It breaks loudly.
- **Tests:** tests/test_core.py

## orch

- **Paths:** `commonplace/orch/`, `docs/orch.md`
- **Owns:** Orchestration.
- **Promises:** It orchestrates.
- **How it fails:** It refuses.
- **Tests:** tests/test_orch.py

## rest

- **Paths:** `docs/`, `tests/`
- **Owns:** Docs and tests.
- **Promises:** They exist.
- **How it fails:** They rot.
- **Tests:** The suite.
"""

SYSTEM = """# System

Decisions.

## Waivers

- W1: generated lock files
"""

FILES = {
    "MODULES.md": MODULES,
    "SYSTEM.md": SYSTEM,
    "README.md": "readme\n",
    "commonplace/core.py": "VALUE = 1\n",
    "commonplace/orch/run.py": "RUN = 1\n",
    "docs/orch.md": "orch docs\n",
    "docs/other.md": "other docs\n",
    "tests/test_core.py": "def test_core():\n    pass\n",
}


def _git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.test",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr}")
    return result.stdout.strip()


def _write(repo, files):
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _commit(repo, files, message="change"):
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    base = _commit(repo, FILES, "base")
    return repo, base


def _block(name, text):
    return MODULES.replace(f"## {name}\n", f"## {name}\n\n{text}\n", 1)


def _run(repo, base, *extra):
    return docs_check.main(["--repo", str(repo), "--base", base, "--head", "HEAD", *extra])


# ---------------------------------------------------------------- R21


def test_r21_a_documented_change_passes(repo, capsys):
    repo, base = repo
    _commit(
        repo,
        {
            "commonplace/core.py": "VALUE = 2\n",
            "MODULES.md": MODULES.replace("It works.", "It works twice."),
            "SYSTEM.md": SYSTEM + "\nMore decisions.\n",
        },
    )
    assert _run(repo, base) == 0, capsys.readouterr().out
    assert docs_check.check(repo, base, "HEAD") == []


def test_r21_nothing_changed_passes(repo):
    repo, base = repo
    assert docs_check.check(repo, base, base) == []


def test_r21_unowned_path_fails(repo, capsys):
    repo, base = repo
    _commit(repo, {"stray.txt": "x\n", "SYSTEM.md": SYSTEM + "\nx\n"})
    assert _run(repo, base) == 1
    out = capsys.readouterr().out
    assert "'stray.txt' is owned by no MODULES.md block" in out
    assert "'stray.txt' is not listed in MODULES.md" in out


def test_r21_new_commonplace_entry_must_be_listed(repo):
    repo, base = repo
    _commit(
        repo,
        {
            "commonplace/extra.py": "X = 1\n",
            "SYSTEM.md": SYSTEM + "\nx\n",
        },
    )
    problems = docs_check.check(repo, base, "HEAD")
    assert "'commonplace/extra.py' is not listed in MODULES.md" in problems
    assert "'commonplace/extra.py' is owned by no MODULES.md block" in problems


def test_r21_touched_module_with_an_unchanged_block_fails(repo):
    repo, base = repo
    _commit(repo, {"commonplace/core.py": "VALUE = 2\n", "SYSTEM.md": SYSTEM + "\nx\n"})
    assert docs_check.check(repo, base, "HEAD") == [
        "module 'core' changed ('commonplace/core.py') but its MODULES.md block did not"
    ]


def test_r21_longest_listed_path_owns_a_deeper_file(repo):
    repo, base = repo
    _commit(repo, {"docs/orch.md": "changed\n", "SYSTEM.md": SYSTEM + "\nx\n"})
    assert docs_check.check(repo, base, "HEAD") == [
        "module 'orch' changed ('docs/orch.md') but its MODULES.md block did not"
    ]
    _commit(repo, {"MODULES.md": MODULES.replace("It orchestrates.", "It orchestrates well.")})
    assert docs_check.check(repo, base, "HEAD") == []


def test_r21_missing_system_change_fails(repo):
    repo, base = repo
    _commit(
        repo,
        {
            "commonplace/core.py": "VALUE = 2\n",
            "MODULES.md": MODULES.replace("It works.", "It works twice."),
        },
    )
    assert docs_check.check(repo, base, "HEAD") == ["SYSTEM.md did not change"]


def test_r21_whitespace_only_doc_edits_do_not_count(repo):
    """A block or SYSTEM.md edit that only changes whitespace is not a documented change."""
    repo, base = repo
    _commit(
        repo,
        {
            "commonplace/core.py": "VALUE = 2\n",
            "MODULES.md": MODULES.replace("It works.", "It  works.\n"),
            "SYSTEM.md": SYSTEM + "\n\n   \n",
        },
    )
    assert docs_check.check(repo, base, "HEAD") == [
        "module 'core' changed ('commonplace/core.py') but its MODULES.md block did not",
        "SYSTEM.md changed only in whitespace",
    ]


@pytest.mark.parametrize(
    "modules, expected",
    [
        (MODULES.replace("- **Owns:** The core module.", "- **Owns:**"), "empty Owns field"),
        (MODULES.replace("- **Tests:** The suite.\n", ""), "has no Tests field"),
        (MODULES.replace("`README.md`", "`README.md`, `docs/`"), "'docs/' is listed twice"),
        (MODULES.replace("`README.md`", "`README.md`, `commonplace/`"), "split into its entries"),
        (MODULES.replace("`docs/orch.md`", "`docs/*.md`"), "not a plain path"),
        (MODULES + "\n## docs\n\n- **Paths:** `x`\n", "block 'docs' appears twice"),
        (MODULES.replace("- **Paths:** `docs/`, `tests/`", "- **Paths:** docs"), "no paths"),
    ],
)
def test_r21_empty_or_broken_block_fails(repo, modules, expected):
    repo, base = repo
    _commit(repo, {"MODULES.md": modules, "SYSTEM.md": SYSTEM + "\nx\n"})
    problems = docs_check.check(repo, base, "HEAD")
    assert any(expected in problem for problem in problems), problems
    assert _run(repo, base) == 1


def test_r21_missing_modules_file_fails(repo):
    repo, base = repo
    _commit(repo, {"MODULES.md": None, "SYSTEM.md": SYSTEM + "\nx\n"})
    assert docs_check.check(repo, base, "HEAD") == ["MODULES.md is missing"]


def test_r21_deleted_path_keeps_its_base_owner(repo):
    repo, base = repo
    modules = MODULES.replace("`commonplace/core.py`, ", "")
    _commit(repo, {"commonplace/core.py": None, "MODULES.md": modules, "SYSTEM.md": SYSTEM + "x\n"})
    assert docs_check.check(repo, base, "HEAD") == []


def test_r21_new_modules_file_counts_every_block_as_changed(repo):
    repo, _base = repo
    first = _commit(repo, {"MODULES.md": None}, "drop map")
    _commit(repo, {"MODULES.md": MODULES, "commonplace/core.py": "V = 3\n", "SYSTEM.md": "s\n"})
    assert docs_check.check(repo, first, "HEAD") == []


@pytest.mark.parametrize("ref", ["no-such-ref", "HEAD~99", ""])
def test_r21_bad_ref_is_an_error(repo, ref, capsys):
    repo, _base = repo
    assert _run(repo, ref) == 2
    assert "bad ref" in capsys.readouterr().err
    with pytest.raises(docs_check.DocsCheckError, match="bad ref"):
        docs_check.resolve(repo, "--all")


def test_r21_not_a_repository_is_an_error(tmp_path, capsys):
    assert docs_check.main(["--repo", str(tmp_path), "--base", "HEAD"]) == 2
    assert "error" in capsys.readouterr().err


def test_r21_unrelated_histories_are_an_error(repo, tmp_path):
    repo, _base = repo
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _commit(other, {"x.txt": "x\n"})
    _git(repo, "fetch", "-q", str(other), "HEAD:refs/heads/unrelated")
    with pytest.raises(docs_check.DocsCheckError, match="no merge base"):
        docs_check.check(repo, "unrelated", "HEAD")


def test_r21_head_tree_has_no_unowned_entry():
    """R21: every root and ``commonplace/`` entry of this repository's HEAD is listed."""
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    problems = docs_check.check(REPO, "HEAD", "HEAD")
    assert problems == [], problems


def test_r21_init_lists_unlisted_entries(repo, capsys):
    repo, base = repo
    assert _run(repo, base, "--init") == 0
    assert capsys.readouterr().out == ""
    _commit(repo, {"stray.txt": "x\n"})
    assert _run(repo, base, "--init") == 0
    out = capsys.readouterr().out
    assert out.startswith("## unassigned") and "`stray.txt`" in out and "- **Owns:**" in out
    _commit(repo, {"MODULES.md": None})
    assert _run(repo, base, "--init") == 0
    out = capsys.readouterr().out
    assert out.startswith("# Modules") and "`commonplace/core.py`" in out and "`docs/`" in out


# ---------------------------------------------------------------- R21b


def test_r21b_base_listed_waiver_waives_the_obligations(repo):
    repo, base = repo
    _commit(repo, {"commonplace/core.py": "VALUE = 2\n"}, "change\n\nDocs-waiver: W1")
    assert docs_check.check(repo, base, "HEAD") == []


def test_r21b_waiver_not_in_the_base_list_fails(repo):
    repo, base = repo
    _commit(repo, {"commonplace/core.py": "VALUE = 2\n"}, "change\n\nDocs-waiver: W7")
    problems = docs_check.check(repo, base, "HEAD")
    assert "waiver 'W7' is not in the base SYSTEM.md waiver list" in problems


def test_r21b_waiver_added_in_the_same_change_does_not_count(repo):
    repo, base = repo
    _commit(
        repo,
        {"commonplace/core.py": "VALUE = 2\n", "SYSTEM.md": SYSTEM + "- W2: this change\n"},
        "change\n\nDocs-waiver: W2",
    )
    problems = docs_check.check(repo, base, "HEAD")
    assert "waiver 'W2' is not in the base SYSTEM.md waiver list" in problems
    assert any("module 'core' changed" in problem for problem in problems), problems


def test_r21b_waiver_never_hides_an_unowned_path(repo):
    repo, base = repo
    _commit(repo, {"stray.txt": "x\n"}, "change\n\nDocs-waiver: W1")
    problems = docs_check.check(repo, base, "HEAD")
    assert "'stray.txt' is owned by no MODULES.md block" in problems


def test_r21b_waiver_list_parser():
    text = "## Waivers\n\nW1: a\n- W2: b\nW3:\nW04: c\n## Next\nW5: d\n"
    assert docs_check.waiver_list(text) == {"W1", "W2"}
    assert docs_check.waiver_list(None) == set()


# ---------------------------------------------------------------- R22


@pytest.mark.parametrize(
    "line",
    [
        "- lease lasts 600 seconds according to the measured database value M",
        "- lease M: lasts 600 seconds according to the measured database value",
        "- 600 seconds according to the current database lease value GUESS",
    ],
)
def test_r22_g3_label_must_lead_the_line(line):
    text = PLAN.replace("test_example.py", "tests/test_orch_checks.py").replace(
        "- M: lease 600 s (`db.py:51`); T: 0 duplicate jobs after a lost ack.", line
    )
    found = plan_check.violation(text)
    assert found is not None and found.rule == "G3", found


def test_r22_g7_missing_test_file_fails():
    text = "\n".join(
        "R-test files: tests/test_missing_plan_regression.py: R1"
        if line.startswith("R-test files:")
        else line
        for line in PLAN.splitlines()
    )
    found = plan_check.violation(text)
    assert found is not None and found.rule == "G7", found


@pytest.mark.parametrize("label", ["M", "T", "G", "MEASURED", "TARGET", "GUESS"])
@pytest.mark.parametrize("prefix", ["", "  ", "- ", "  + ", "* ", "1. ", "2) "])
@pytest.mark.parametrize("separator", [" ", ": "])
def test_r22_g3_leading_labels_pass(label, prefix, separator):
    text = PLAN.replace(
        "- M: lease 600 s (`db.py:51`); T: 0 duplicate jobs after a lost ack.",
        f"{prefix}{label}{separator}lease lasts 600 seconds according to the database value.",
    )
    assert plan_check.violation(text) is None


def test_r22_g7_uses_current_repo_root_at_check_time(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    nested = repo / "nested"
    nested.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: unused\n", encoding="utf-8")
    test_file = repo / "tests/test_orch_acceptance.py"
    test_file.parent.mkdir()
    test_file.write_text("", encoding="utf-8")
    monkeypatch.chdir(nested)
    assert plan_check.violation(PLAN) is None
    test_file.unlink()
    assert plan_check.violation(PLAN).rule == "G7"
    test_file.mkdir()
    assert plan_check.violation(PLAN).rule == "G7"


def test_r22_g7_rejects_paths_outside_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    outside = tmp_path / "test_outside.py"
    outside.write_text("", encoding="utf-8")
    monkeypatch.chdir(repo)
    for name in (str(outside), "../test_outside.py"):
        text = PLAN.replace("tests/test_orch_acceptance.py", name)
        assert plan_check.violation(text).rule == "G7"


@pytest.mark.skipif(os.name == "nt", reason="symlinks require an additional Windows privilege")
def test_r22_g7_rejects_escaping_and_looping_symlinks(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    outside = tmp_path / "test_outside.py"
    outside.write_text("", encoding="utf-8")
    link = repo / "linked.py"
    monkeypatch.chdir(repo)
    text = PLAN.replace("tests/test_orch_acceptance.py", "linked.py")
    link.symlink_to(outside)
    assert plan_check.violation(text).rule == "G7"
    link.unlink()
    link.symlink_to(link)
    assert plan_check.violation(text).rule == "G7"


def _replace(old, new, count=1):
    def apply(text):
        if old not in text:
            raise RuntimeError(f"fixture has no {old!r}")
        return text.replace(old, new, count)

    return apply


FENCED_ONLY = _replace("## 7. Risks\n", "```\n## 7. Risks\n```\n")
BROKEN = [
    ("G0", FENCED_ONLY),
    ("G1", _replace("## 5. Docs\n", "")),
    ("G1", _replace("## 2. Versions\n## 3. Authority\n", "## 3. Authority\n## 2. Versions\n")),
    ("G2", _replace("**c. Data.**", "c. Data.")),
    (
        "G2",
        _replace(
            "**d. Risk.** No secrets, no money, no new authority; the owner approves.",
            "**d. Risk.** None.",
        ),
    ),
    ("G2", lambda t: t.replace("**b.", "**@@.").replace("**c.", "**b.").replace("**@@.", "**c.")),
    ("G3", _replace("**b. Smallest", "- 15 jobs per hour at most\n**b. Smallest")),
    ("G4", _replace("| Bounded retry |", "| Retry |")),
    (
        "G4",
        _replace(
            "| Hub | 15 s | ≤ 3 | step_key | retry message | R1 |",
            "| Hub | 15 s | ≤ 3 | step_key | | R1 |",
        ),
    ),
    ("G4", _replace("| retry message | R1 |", "| retry message | R1; R2 |")),
    ("G4", _replace("| Hub | 15 s |", "| Hub | 15 s | extra |")),
    (
        "G4",
        _replace(
            "|---|---|---|---|---|---|\n| Hub | 15 s | ≤ 3 | step_key | retry message | R1 |\n", ""
        ),
    ),
    (
        "G4",
        _replace(
            "| Dependency | Timeout |", "| Other | table |\n|---|---|\n\n| Dependency | Timeout |"
        ),
    ),
    ("G5", _replace("| ID | Fault injected | Assertion |", "| Id | Fault | Assertion |")),
    ("G5", _replace("| R1 | ack lost after commit | 1 job |", "| R1 | a | b |\n| R1 | c | d |")),
    ("G5", _replace("| R1 | ack lost after commit | 1 job |", "| X1 | ack lost | 1 job |")),
    (
        "G5",
        _replace(
            "| ID | Fault injected | Assertion |\n|---|---|---|\n| R1 | ack lost after commit | 1 job |\n",
            "No table here, only words that are long enough for the rule.\n",
        ),
    ),
    ("G6", _replace("| retry message | R1 |", "| retry message | R1, R2 |")),
    ("G7", _replace("R-test files: tests/test_orch_acceptance.py: R1\n", "")),
    (
        "G7",
        _replace(
            "tests/test_orch_acceptance.py: R1",
            "tests/test_orch_acceptance.py: R1; test_other.py: R1",
        ),
    ),
    ("G7", _replace("tests/test_orch_acceptance.py: R1", "tests/test_orch_acceptance.py: R1 nope")),
    ("G7", _replace("tests/test_orch_acceptance.py: R1", "R1")),
    (
        "G7",
        _replace(
            "| R1 | ack lost after commit | 1 job |",
            "| R1 | ack lost after commit | 1 job |\n| R2 | b | c |",
        ),
    ),
]


@pytest.mark.parametrize(
    "rule, break_it", BROKEN, ids=[f"{r}-{i}" for i, (r, _) in enumerate(BROKEN)]
)
def test_r22_each_rule_broken_once_fails(rule, break_it, tmp_path, capsys):
    text = break_it(PLAN)
    found = plan_check.violation(text)
    assert found is not None and found.rule == ("G1" if rule == "G0" else rule), found
    path = tmp_path / "plan.md"
    path.write_text(text, encoding="utf-8")
    assert cli_main(["orch", "plan-check", str(path)]) == 1
    assert capsys.readouterr().out.startswith(f"plan-check: {found.rule}:")


SURROUNDED = (
    "# A plan\n\nIntro with 12 numbers and `## 3.` in code.\n\n## Changes\n\n| a | b |\n|---|---|\n\n"
    + PLAN.replace(
        "**b. Smallest",
        "```\n- 99 unlabelled numbers inside a fence\n## 9. Fenced heading\n```\n**b. Smallest",
    )
    + "\n```toml\n## 0. Fenced\n```\n"
)


@pytest.mark.parametrize("text", [PLAN, SURROUNDED], ids=["fixture", "surrounded"])
def test_r22_the_fixture_passes(text, tmp_path, capsys):
    """R22 (G0 included): the fixture, also inside prose with fenced noise, passes."""
    assert plan_check.violation(text) is None
    path = tmp_path / "plan.md"
    path.write_text(text, encoding="utf-8")
    assert plan_check.main([str(path)]) == 0
    assert capsys.readouterr().out == "plan-check: OK\n"


def test_r22_check_raises_and_reads_errors(tmp_path, capsys, monkeypatch):
    with pytest.raises(plan_check.PlanCheckError, match="G1"):
        plan_check.check("no headings")
    assert plan_check.violation(None).rule == "G1"
    assert plan_check.main([str(tmp_path / "missing.md")]) == 2
    big = tmp_path / "big.md"
    big.write_text("x" * (plan_check.MAX_PLAN_BYTES + 1), encoding="utf-8")
    assert plan_check.main([str(big)]) == 2
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(PLAN))
    assert plan_check.main(["-"]) == 0
    assert "OK" in capsys.readouterr().out


def test_r22_require_plan_raises_a_tool_error():
    from commonplace.extensions import ToolError

    plan_check.require_plan(PLAN)
    with pytest.raises(ToolError, match=r"^no: plan fails plan-check \(G2: "):
        plan_check.require_plan("## 0.\n## 1.\n## 2.\n## 3.\n## 4.\n## 5.\n## 6.\n## 7.\n")
    with pytest.raises(ToolError, match="G1"):
        plan_check.require_plan(None)


# ---------------------------------------------------------------- plan gate reader


def test_board_gate_reads_only_an_accepted_live_config(tmp_path):
    assert plan_check.board_gate(tmp_path, "shared") is None
    assert not (tmp_path / "runner.sqlite3").exists()
    (tmp_path / "runner.sqlite3").write_bytes(b"")
    assert plan_check.board_gate(tmp_path, "shared") is None
    body = config_toml(
        board="shared", machine="mach_0123456789abcdef", planner_connections=["planner-a"]
    )
    seed_config(tmp_path, "shared", body)
    gate = plan_check.board_gate(tmp_path, "shared")
    assert gate.board == "shared" and gate.is_planner("conn:planner-a")
    assert not gate.is_planner("conn:other") and not gate.is_planner(None)
    assert plan_check.board_gate(tmp_path, "elsewhere") is None
    seed_config(tmp_path, "shared", "not = [valid", version=2)
    gate = plan_check.board_gate(tmp_path, "shared")
    assert gate is not None and gate.planners == frozenset()


# ---------------------------------------------------------------- orch CLI


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONPLACE_DATA", raising=False)
    root = tmp_path / "data"
    assert cli_main(["--data", str(root), "init", "--admin-email", "owner@example.test"]) == 0
    return root


def _cli(data, *args, capsys):
    capsys.readouterr()
    code = cli_main(["--data", str(data), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_orch_cli_submit_accept_render_diff_and_retire(data, tmp_path, capsys):
    code, _out, _err = _cli(
        data, "token", "add", "planner-a", "--workspace", "personal", capsys=capsys
    )
    assert code == 0
    first = tmp_path / "v1.toml"
    first.write_text(
        config_toml(
            board="personal", machine="mach_0123456789abcdef", planner_connections=["planner-a"]
        ),
        encoding="utf-8",
    )
    ws = ("--workspace", "personal")
    code, out, err = _cli(
        data, "orch", "submit", str(first), *ws, "--source-ref", "main", capsys=capsys
    )
    assert code == 0, err
    v1 = json.loads(out)
    assert v1["ref"] == "v1" and v1["status"] == "proposed" and v1["source_ref"] == "main"
    assert "body" not in v1
    board = ("--board", "personal")
    code, out, err = _cli(
        data, "orch", "accept", *ws, *board, "v1", "--sha256", "0" * 64, capsys=capsys
    )
    assert code == 2 and "sha256_mismatch" in err
    code, out, err = _cli(
        data, "orch", "accept", *ws, *board, "1", "--sha256", v1["sha256"], capsys=capsys
    )
    assert code == 0, err
    accepted = json.loads(out)
    assert accepted["accepted"] == "v1" and accepted["cursor"] == "armed"
    assert "roles" in accepted["diff"] and accepted["previous"] is None

    second = tmp_path / "v2.toml"
    second.write_text(
        config_toml(
            board="personal",
            machine="mach_0123456789abcdef",
            planner_connections=["planner-a"],
            max_agents=2,
        ),
        encoding="utf-8",
    )
    code, out, err = _cli(data, "orch", "submit", str(second), *ws, *board, capsys=capsys)
    assert code == 0, err
    v2 = json.loads(out)
    code, out, err = _cli(data, "orch", "diff", *ws, *board, "v1", "v2", capsys=capsys)
    assert code == 0 and "limits.max_agents" in out, (out, err)
    code, out, err = _cli(data, "orch", "reject", *ws, *board, "v2", capsys=capsys)
    assert code == 0 and json.loads(out)["status"] == "rejected"
    code, out, err = _cli(data, "orch", "list", *ws, *board, capsys=capsys)
    assert code == 0 and [row["ref"] for row in json.loads(out)] == ["v2", "v1"]
    code, out, err = _cli(data, "orch", "render", *ws, *board, capsys=capsys)
    assert code == 0 and out.startswith("# Orchestrator config `personal`"), (out, err)
    code, out, err = _cli(data, "orch", "render", *ws, *board, "v2", capsys=capsys)
    assert code == 0 and "max_agents: 2" in out, out
    code, out, err = _cli(data, "orch", "render", *ws, *board, "--channel", "exp:x", capsys=capsys)
    assert code == 2 and "not_found" in err
    code, out, err = _cli(data, "orch", "exp", "retire", *ws, *board, "nothing", capsys=capsys)
    assert code == 2 and "unknown_experiment" in err
    code, out, err = _cli(
        data, "orch", "submit", str(second), *ws, "--board", "Bad Board", capsys=capsys
    )
    assert code == 2 and "invalid_board" in err
    broken = tmp_path / "broken.toml"
    broken.write_text("schema = [", encoding="utf-8")
    code, out, err = _cli(data, "orch", "submit", str(broken), *ws, capsys=capsys)
    assert code == 2 and "toml_invalid" in err
    assert v2["ref"] == "v2"


def test_orch_cli_requires_a_workspace_owner(data, tmp_path, capsys):
    from commonplace.control_db import ControlDB

    db = ControlDB(str(data / "control.sqlite3"))
    try:
        member = db.create_user("member@example.test")
    finally:
        db.close()
    path = tmp_path / "c.toml"
    path.write_text(
        config_toml(board="personal", machine="mach_0123456789abcdef"), encoding="utf-8"
    )
    code, out, err = _cli(
        data, "orch", "submit", str(path), "--workspace", "personal", "--by", member, capsys=capsys
    )
    assert code == 2 and "owner" in err
    code, out, err = _cli(
        data, "orch", "list", "--workspace", "nowhere", "--board", "x", capsys=capsys
    )
    assert code == 2


def test_orch_cli_restore_status_and_ack(data, capsys):
    from commonplace.backup import RESTORE_MARKER
    from commonplace.control_db import ControlDB
    from commonplace.workspace_store import workspace_path

    db = ControlDB(str(data / "control.sqlite3"))
    try:
        workspace = db.workspace("personal")["id"]
    finally:
        db.close()
    directory = Path(workspace_path(data, workspace))
    directory.mkdir(parents=True, exist_ok=True)
    marker = {"version": 1, "backup_created_at": "2026-01-02T03:04:05Z", "id": "a" * 32}
    (directory / RESTORE_MARKER).write_text(json.dumps(marker), encoding="utf-8")
    ws = ("--workspace", "personal")
    code, out, err = _cli(data, "orch", "restore-status", *ws, capsys=capsys)
    assert code == 0, err
    state = json.loads(out)
    assert state["restore_pending"] is True
    assert state["ack_text"] == "effects after 2026-01-02T03:04:05Z may be unknown"
    code, out, err = _cli(data, "orch", "restore-ack", *ws, "--text", "ok", capsys=capsys)
    assert code == 2 and "restore_text" in err
    code, out, err = _cli(
        data, "orch", "restore-ack", *ws, "--text", state["ack_text"], capsys=capsys
    )
    assert code == 0 and json.loads(out) == {"acknowledged": True, "arming": []}, err
    code, out, err = _cli(data, "orch", "restore-status", *ws, capsys=capsys)
    assert json.loads(out)["restore_pending"] is False


def _cursor(data, board):
    import sqlite3

    from commonplace.control_db import ControlDB
    from commonplace.workspace_store import workspace_path

    db = ControlDB(str(data / "control.sqlite3"))
    try:
        workspace = db.workspace("personal")["id"]
    finally:
        db.close()
    directory = Path(workspace_path(data, workspace))
    conn = sqlite3.connect(str(directory / "runner.sqlite3"))
    try:
        row = conn.execute("SELECT cursor, gen FROM orch_cursor WHERE board=?", (board,)).fetchone()
    finally:
        conn.close()
    return directory, row


def test_orch_cli_accept_and_restore_ack_arm_the_trigger_cursor(data, tmp_path, capsys):
    """The CLI arms the cursor right after a configuring accept and after a restore
    acknowledgement, like the MCP and REST surfaces (no NULL-cursor window)."""
    from commonplace.backup import RESTORE_MARKER

    path = tmp_path / "c.toml"
    path.write_text(config_toml(board="personal", machine="mach_0123456789abcdef"), "utf-8")
    ws, board = ("--workspace", "personal"), ("--board", "personal")
    code, out, err = _cli(data, "orch", "submit", str(path), *ws, capsys=capsys)
    assert code == 0, err
    digest = json.loads(out)["sha256"]
    code, out, err = _cli(
        data, "orch", "accept", *ws, *board, "v1", "--sha256", digest, capsys=capsys
    )
    assert code == 0, err
    assert json.loads(out)["cursor"] == "armed"
    directory, row = _cursor(data, "personal")
    assert row == (0, 1)
    marker = {"version": 1, "backup_created_at": "2026-01-02T03:04:05Z", "id": "b" * 32}
    (directory / RESTORE_MARKER).write_text(json.dumps(marker), encoding="utf-8")
    text = "effects after 2026-01-02T03:04:05Z may be unknown"
    code, out, err = _cli(data, "orch", "restore-ack", *ws, "--text", text, capsys=capsys)
    assert code == 0, err
    assert json.loads(out)["arming"] == [{"board": "personal", "gen": 2, "armed": True}]
    assert _cursor(data, "personal")[1] == (0, 2)


def test_repo_checks_run_without_a_data_directory(tmp_path, monkeypatch, capsys):
    """plan-check and docs-check never open or lock a data directory."""
    missing = tmp_path / "no-data"
    path = tmp_path / "plan.md"
    path.write_text(PLAN, encoding="utf-8")
    assert cli_main(["--data", str(missing), "orch", "plan-check", str(path)]) == 0
    assert cli_main(["--data", str(missing), "docs-check", "--repo", str(tmp_path)]) == 2
    assert cli_main(["--data", str(missing), "orch", "docs-check", "--repo", str(tmp_path)]) == 2
    assert not missing.exists()


@pytest.mark.parametrize("value", ["v0", "x", "v1.5", ""])
def test_orch_cli_rejects_bad_versions(data, value, capsys):
    with pytest.raises(SystemExit):
        cli_main(
            [
                "--data",
                str(data),
                "orch",
                "diff",
                "--workspace",
                "personal",
                "--board",
                "b",
                value,
                "v1",
            ]
        )
