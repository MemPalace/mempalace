"""Parallel documentation changes merge without a git conflict (card #427).

Module blocks live one per file in ``docs/modules/``; ``SYSTEM.md`` takes extra sections as
files in ``docs/system/``. The scenarios below branch twice from one base, merge both
branches in either order and compare with the single-file layout, where they conflict.
"""

import os
import subprocess

import pytest

from commonplace.orch import docs_check

BLOCK = """## {name}

- **Paths:** {paths}
- **Owns:**

  - {name} owns its files.

  - {name} second topic.

- **Promises:**

  - {name} promise one.

  - {name} promise two.

  - {name} promise three.

- **How it fails:**

  - {name} fails loudly.

- **Tests:**

  - {name} test one.

  - {name} test two.

  - {name} test three.
"""

FORMAT = "# Modules\n\nFormat only.\n"
SYSTEM = "# System\n\nIntro.\n\n## Decisions\n\n### Old\n\n- Old decision.\n\n## Waivers\n\n- W1: locks\n"


def block(name, paths=None):
    return BLOCK.format(name=name, paths=paths or f"`{name}/`")


def git(repo, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Example", "-c", "user.email=e@example.test"]
        + ["-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stdout}{result.stderr}")
    return result


def commit(repo, files, message="change"):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def fragment_base():
    """The new layout: a format file, one file per module, a system section directory."""
    return {
        "MODULES.md": FORMAT,
        "SYSTEM.md": SYSTEM,
        "docs/system/old-section.md": "### Old section\n\n- Old.\n",
        "docs/modules/system-docs.md": block("system-docs", "`SYSTEM.md`, `MODULES.md`, `docs/`"),
        "docs/modules/core.md": block("core"),
        "docs/modules/web.md": block("web"),
        "core/a.py": "A = 1\n",
        "web/a.py": "A = 1\n",
    }


def single_file_base():
    """Today's layout: every block in ``MODULES.md``, every section in ``SYSTEM.md``."""
    modules = "# Modules\n\n" + "\n".join(
        [
            block("system-docs", "`SYSTEM.md`, `MODULES.md`"),
            block("core"),
            block("web"),
        ]
    )
    return {
        "MODULES.md": modules,
        "SYSTEM.md": SYSTEM,
        "core/a.py": "A = 1\n",
        "web/a.py": "A = 1\n",
    }


def new_module(name):
    return {f"docs/modules/{name}.md": block(name), f"{name}/a.py": "A = 1\n"}


def edit(path_text, old, new):
    assert old in path_text
    return path_text.replace(old, new, 1)


def two_branches(tmp_path, base_files, change_a, change_b):
    """A repo with ``base`` and two branches ``a`` and ``b`` that each commit one change."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "develop")
    commit(repo, base_files, "base")
    for name, change in (("a", change_a), ("b", change_b)):
        git(repo, "checkout", "-q", "-b", name, "develop")
        commit(repo, change(repo), f"branch {name}")
    return repo


def merge_both_orders(repo):
    """``{"a then b": CompletedProcess, "b then a": ...}``; each merge starts from develop."""
    results = {}
    for first, second in (("a", "b"), ("b", "a")):
        git(repo, "checkout", "-q", "-B", f"try-{first}", "develop")
        git(repo, "merge", "-q", "--no-edit", first)
        result = git(repo, "merge", "--no-edit", second, check=False)
        results[f"{first} then {second}"] = result
        if result.returncode != 0:
            git(repo, "merge", "--abort")
    return results


def assert_clean(results):
    for order, result in results.items():
        assert result.returncode == 0, f"{order} conflicted:\n{result.stdout}{result.stderr}"


def assert_conflicting(results):
    for order, result in results.items():
        assert result.returncode != 0, f"{order} merged cleanly"
        assert "CONFLICT" in result.stdout, result.stdout


def read(repo, name):
    return (repo / name).read_text(encoding="utf-8")


# ------------------------------------------------ the new layout merges cleanly


def test_two_new_modules_merge_in_either_order(tmp_path):
    def add(name):
        def change(_repo):
            return {**new_module(name), f"docs/system/{name}.md": f"### {name}\n\n- {name}.\n"}

        return change

    repo = two_branches(tmp_path, fragment_base(), add("alpha"), add("beta"))
    assert_clean(merge_both_orders(repo))
    for tip in ("try-a", "try-b"):
        assert docs_check.check(repo, "develop", tip) == []
        text = docs_check.assembled(repo, tip, "modules")
        assert "## alpha" in text and "## beta" in text


def test_promise_and_test_bullets_in_different_modules_merge(tmp_path):
    def promise(repo):
        text = read(repo, "docs/modules/core.md")
        return {
            "docs/modules/core.md": edit(
                text,
                "  - core promise three.\n",
                "  - core promise three.\n\n  - core new promise.\n",
            )
        }

    def test_bullet(repo):
        text = read(repo, "docs/modules/web.md")
        return {
            "docs/modules/web.md": edit(
                text, "  - web test three.\n", "  - web test three.\n\n  - web new test.\n"
            )
        }

    repo = two_branches(tmp_path, fragment_base(), promise, test_bullet)
    assert_clean(merge_both_orders(repo))


def test_promise_and_test_bullets_in_one_module_file_merge(tmp_path):
    def promise(repo):
        text = read(repo, "docs/modules/core.md")
        return {
            "docs/modules/core.md": edit(
                text,
                "  - core promise three.\n",
                "  - core promise three.\n\n  - core new promise.\n",
            )
        }

    def test_bullet(repo):
        text = read(repo, "docs/modules/core.md")
        return {
            "docs/modules/core.md": edit(
                text, "  - core test three.\n", "  - core test three.\n\n  - core new test.\n"
            )
        }

    repo = two_branches(tmp_path, fragment_base(), promise, test_bullet)
    assert_clean(merge_both_orders(repo))


def test_two_system_sections_merge_in_either_order(tmp_path):
    def decision(name):
        return lambda _repo: {f"docs/system/{name}.md": f"### Decision {name}\n\n- {name}.\n"}

    repo = two_branches(tmp_path, fragment_base(), decision("one"), decision("two"))
    assert_clean(merge_both_orders(repo))
    for tip in ("try-a", "try-b"):
        assert "### Decision one" in docs_check.assembled(repo, tip, "system")
        assert "### Decision two" in docs_check.assembled(repo, tip, "system")


# ---------------------------------------------- the single-file layout conflicts


def test_single_file_layout_conflicts_for_two_new_modules(tmp_path):
    def add(name):
        def change(repo):
            text = read(repo, "MODULES.md")
            return {"MODULES.md": text + "\n" + block(name), f"{name}/a.py": "A = 1\n"}

        return change

    repo = two_branches(tmp_path, single_file_base(), add("alpha"), add("beta"))
    assert_conflicting(merge_both_orders(repo))


def test_single_file_layout_conflicts_for_two_decisions(tmp_path):
    def decision(name):
        def change(repo):
            text = read(repo, "SYSTEM.md")
            return {
                "SYSTEM.md": edit(
                    text, "## Waivers", f"### Decision {name}\n\n- {name}.\n\n## Waivers"
                )
            }

        return change

    repo = two_branches(tmp_path, single_file_base(), decision("one"), decision("two"))
    assert_conflicting(merge_both_orders(repo))


# ------------------------------------------------------- docs-check, the layout


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "develop")
    commit(repo, fragment_base(), "base")
    git(repo, "checkout", "-q", "-b", "work")
    return repo


def check(repo):
    return docs_check.check(repo, "develop", "HEAD")


def test_a_touched_module_needs_its_file_to_change(repo):
    commit(repo, {"core/a.py": "A = 2\n", "docs/system/new.md": "### New\n\n- New.\n"})
    assert check(repo) == ["module 'core' changed ('core/a.py') but its MODULES.md block did not"]
    text = read(repo, "docs/modules/core.md")
    commit(
        repo, {"docs/modules/core.md": edit(text, "core test three.", "core test three, again.")}
    )
    assert check(repo) == []


def test_editing_a_module_file_does_not_touch_the_docs_module(repo):
    text = read(repo, "docs/modules/core.md")
    commit(
        repo,
        {
            "docs/modules/core.md": edit(text, "core fails loudly.", "core fails very loudly."),
            "docs/system/new.md": "### New\n\n- New.\n",
        },
    )
    assert check(repo) == []


def test_a_system_section_counts_as_a_system_change(repo):
    commit(repo, {"core/a.py": "A = 2\n"})
    text = read(repo, "docs/modules/core.md")
    commit(repo, {"docs/modules/core.md": edit(text, "core test one.", "core test one!")})
    assert check(repo) == ["SYSTEM.md did not change"]
    commit(repo, {"docs/system/new.md": "### New\n\n- New.\n"})
    assert check(repo) == []


def test_a_whitespace_only_system_section_edit_is_no_change(repo):
    commit(repo, {"docs/system/old-section.md": "### Old section\n\n\n- Old.\n"})
    assert check(repo) == ["SYSTEM.md changed only in whitespace"]


def test_a_new_module_file_lists_its_new_directory(repo):
    commit(repo, {**new_module("alpha"), "docs/system/new.md": "### New\n\n- New.\n"})
    assert check(repo) == []
    commit(repo, {"gamma/a.py": "A = 1\n"})
    assert "'gamma/' is not listed in MODULES.md" in check(repo)


def test_a_module_in_two_places_is_flagged(repo):
    commit(repo, {"MODULES.md": FORMAT + "\n" + block("core"), "docs/system/new.md": "### New\n"})
    assert "block 'core' appears twice" in check(repo)


@pytest.mark.parametrize(
    ("name", "text", "problem"),
    [
        (
            "docs/modules/core.md",
            block("other", "`core/`"),
            "must hold exactly one '## core' block (found 'other')",
        ),
        ("docs/modules/web.md", "just text\n", "must hold exactly one '## web' block (found none)"),
        (
            "docs/modules/two.md",
            block("two") + "\n" + block("three"),
            "must hold exactly one '## two' block (found 'two', 'three')",
        ),
        ("docs/modules/notes.txt", "x\n", "is not a module file"),
        ("docs/modules/sub/x.md", "x\n", "is not a module file"),
        ("docs/system/notes.txt", "### T\n", "is not a system section file"),
        ("docs/system/none.md", "text first\n", "must start with a '### <title>' heading"),
        ("docs/system/empty.md", "\n", "must start with a '### <title>' heading"),
    ],
)
def test_malformed_fragments_are_flagged(repo, name, text, problem):
    commit(repo, {name: text, "docs/system/ok.md": "### Ok\n\n- Ok.\n"})
    assert any(problem in item for item in check(repo)), check(repo)


def test_print_shows_the_assembled_documents(repo, capsys):
    assert docs_check.main(["--repo", str(repo), "--head", "HEAD", "--print", "modules"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Modules") and out.count("\n## ") == 3
    assert docs_check.main(["--repo", str(repo), "--head", "HEAD", "--print", "system"]) == 0
    out = capsys.readouterr().out
    assert "### Old\n" in out and "### Old section" in out


def test_a_block_left_in_modules_md_is_still_read(repo):
    commit(
        repo,
        {
            "MODULES.md": FORMAT + "\n" + block("legacy"),
            "legacy/a.py": "A = 1\n",
            "docs/system/new.md": "### New\n\n- New.\n",
        },
    )
    assert check(repo) == []


def test_init_starter_block_lists_unlisted_entries(repo, capsys):
    commit(repo, {"gamma/a.py": "A = 1\n"})
    assert docs_check.main(["--repo", str(repo), "--init"]) == 0
    out = capsys.readouterr().out
    assert "## unassigned" in out and "`gamma/`" in out


def test_the_repository_layout_parses_cleanly():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    if not (repo / ".git").exists():
        pytest.skip("not a git checkout")
    modules = docs_check.load_modules(repo, "HEAD")
    assert modules is not None and modules.problems == []
    assert len(modules.blocks) >= 27
