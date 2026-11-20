"""GitignoreMatcher: ``**`` matching stays polynomial and keeps its semantics.

A ``.gitignore`` comes from the repository being mined, which the user may
only have cloned, so a pattern must never cost more than the product of its
length and the path's. The recursive matcher it replaces branched on every
``**`` and took minutes on a dozen of them.
"""

import time
from pathlib import Path

import pytest

from mempalace.miner import GitignoreMatcher

BASE = Path("/repo") if not str(Path.cwd()).startswith("\\\\") else Path.cwd()


def _matcher(*patterns):
    return GitignoreMatcher.from_patterns(BASE, list(patterns))


def _ignored(matcher, relative, is_dir=False):
    return matcher.matches(BASE / relative, is_dir=is_dir)


@pytest.mark.parametrize(
    "pattern, relative, expected",
    [
        ("**/zzz", "a/b/zzz", True),
        ("**/zzz", "zzz", True),
        ("**/zzz", "a/b/zzy", None),
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/x/y/b", True),
        ("a/**/b", "x/a/b", None),
        ("a/**", "a/x/y", True),
        # Trailing ``**`` spans zero segments, as the matcher always did.
        ("a/**", "a", True),
        ("**/**/zzz", "a/b/zzz", True),
        ("**/**/**/zzz", "zzz", True),
        ("build/", "build/out.o", True),
        ("/root.txt", "root.txt", True),
        ("/root.txt", "sub/root.txt", None),
        ("docs/*.md", "docs/a.md", True),
        ("docs/*.md", "docs/sub/a.md", None),
    ],
)
def test_double_star_semantics(pattern, relative, expected):
    assert _ignored(_matcher(pattern), relative) is expected


def test_ignored_directory_covers_everything_under_it():
    matcher = _matcher("a/b")
    assert _ignored(matcher, "a/b/c/d.txt") is True


def test_negation_still_wins_in_order():
    matcher = _matcher("**/*.log", "!keep/**/*.log")
    assert _ignored(matcher, "x/y/a.log") is True
    assert _ignored(matcher, "keep/x/a.log") is False


def test_many_double_stars_stay_fast():
    stars = "/".join(["**"] * 40) + "/zzz"
    matcher = _matcher(stars)
    deep = "/".join(f"d{i}" for i in range(60)) + "/file.txt"
    started = time.perf_counter()
    assert _ignored(matcher, deep) is None
    assert _ignored(matcher, deep.replace("file.txt", "zzz")) is True
    assert time.perf_counter() - started < 1.0


def test_alternating_double_stars_stay_fast():
    pattern = "/".join(["**", "x"] * 20) + "/zzz"
    matcher = _matcher(pattern)
    deep = "/".join(["x"] * 70) + "/file.txt"
    started = time.perf_counter()
    assert _ignored(matcher, deep) is None
    assert time.perf_counter() - started < 1.0


def test_nested_gitignore_file_with_hostile_pattern(tmp_path):
    (tmp_path / ".gitignore").write_text("/".join(["**"] * 30) + "/zzz\n", encoding="utf-8")
    matcher = GitignoreMatcher.from_dir(tmp_path)
    path = tmp_path.joinpath(*[f"d{i}" for i in range(50)], "zzz")
    started = time.perf_counter()
    assert matcher.matches(path, is_dir=False) is True
    assert time.perf_counter() - started < 1.0
