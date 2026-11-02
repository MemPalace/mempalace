"""Files changed by the commits a ``git push`` sends, read from pre-push input."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass

ZERO_SHA = "0" * 40
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_TAG_PREFIX = "refs/tags/"
_LOCAL_DEFAULTS = ("refs/heads/main", "refs/heads/master")


def _is_zero(sha: str) -> bool:
    return set(sha) == {"0"}


@dataclass(frozen=True)
class PushUpdate:
    """One ref update from git's pre-push stdin."""

    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str

    @property
    def is_delete(self) -> bool:
        return _is_zero(self.local_sha)

    @property
    def is_new_branch(self) -> bool:
        return _is_zero(self.remote_sha) and not self.is_delete

    @property
    def is_tag(self) -> bool:
        return self.remote_ref.startswith(_TAG_PREFIX)


class PushRangeError(ValueError):
    """Pre-push input or a git command cannot be used to find the pushed files."""


def parse_pre_push_lines(text: str) -> list[PushUpdate]:
    """Parse git's pre-push stdin: ``<local ref> <local sha> <remote ref> <remote sha>`` lines."""
    updates: list[PushUpdate] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 4:
            raise PushRangeError(f"pre-push line {number}: expected 4 fields, got {len(fields)}")
        local_ref, local_sha, remote_ref, remote_sha = fields
        for sha in (local_sha, remote_sha):
            if _SHA_RE.fullmatch(sha) is None:
                raise PushRangeError(f"pre-push line {number}: not a sha: {sha!r}")
        updates.append(PushUpdate(local_ref, local_sha, remote_ref, remote_sha))
    return updates


def _git(
    repo: str | os.PathLike[str], *args: str, ok: tuple[int, ...] = (0,)
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", os.fspath(repo), "-c", "core.quotePath=false", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=False,
    )
    if proc.returncode not in ok:
        detail = proc.stderr.strip() or f"exit status {proc.returncode}"
        raise PushRangeError(f"git {args[0]} failed: {detail}")
    return proc


def _names(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [name for name in proc.stdout.split("\0") if name]


def _toplevel(repo: str | os.PathLike[str]) -> str:
    return _git(repo, "rev-parse", "--show-toplevel").stdout.strip()


def _ref_exists(repo: str | os.PathLike[str], ref: str) -> bool:
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", ok=(0, 1))
    return proc.returncode == 0


def default_branch(repo: str | os.PathLike[str], remote: str = "origin") -> str | None:
    """Return the full ref of the default branch, or None when none can be found."""
    head = _git(repo, "symbolic-ref", "-q", f"refs/remotes/{remote}/HEAD", ok=(0, 1))
    candidates = [head.stdout.strip()] if head.returncode == 0 else []
    candidates += [
        f"refs/remotes/{remote}/main",
        f"refs/remotes/{remote}/master",
        *_LOCAL_DEFAULTS,
    ]
    for ref in candidates:
        if _ref_exists(repo, ref):
            return ref
    return None


def _has_commit(repo: str | os.PathLike[str], sha: str) -> bool:
    proc = _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", ok=(0, 1, 128))
    return proc.returncode == 0


def _merge_base(repo: str | os.PathLike[str], one: str, other: str) -> str | None:
    proc = _git(repo, "merge-base", one, other, ok=(0, 1))
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _diff_paths(repo: str | os.PathLike[str], base: str, head: str) -> list[str]:
    return _names(_git(repo, "diff", "-z", "--name-only", "--diff-filter=ACMR", base, head))


def _log_paths(repo: str | os.PathLike[str], head: str, remote: str) -> list[str]:
    return _names(
        _git(
            repo,
            "log",
            "-z",
            "--format=",
            "--name-only",
            "--diff-filter=ACMR",
            head,
            "--not",
            f"--remotes={remote}",
        )
    )


def _tree_paths(repo: str | os.PathLike[str], sha: str) -> set[str]:
    proc = _git(repo, "ls-tree", "-r", "-z", "--full-tree", "--name-only", sha)
    return set(_names(proc))


def _update_paths(
    repo: str | os.PathLike[str], update: PushUpdate, remote: str, default_ref: str | None
) -> list[str]:
    if not _is_zero(update.remote_sha) and _has_commit(repo, update.remote_sha):
        base = _merge_base(repo, update.remote_sha, update.local_sha)
        if base is not None:
            return _diff_paths(repo, base, update.local_sha)
    ref = default_ref or default_branch(repo, remote)
    if ref is not None:
        base = _merge_base(repo, update.local_sha, ref)
        # A base equal to the pushed commit means it is already on the default
        # branch; the log fallback then lists only commits no remote has.
        if base is not None and base != update.local_sha:
            return _diff_paths(repo, base, update.local_sha)
    return _log_paths(repo, update.local_sha, remote)


def changed_files(
    repo: str | os.PathLike[str],
    updates: Iterable[PushUpdate],
    remote: str = "origin",
    default_ref: str | None = None,
) -> list[str]:
    """Return sorted, unique work-tree-relative paths the pushed commits add or change.

    Deleted and tag updates contribute nothing. Paths that are not in the tree
    of each pushed commit are dropped. Never modifies the repository.
    """
    top = _toplevel(repo)
    found: set[str] = set()
    for update in updates:
        if update.is_delete or update.is_tag:
            continue
        paths = _update_paths(top, update, remote, default_ref)
        if paths:
            tree = _tree_paths(top, update.local_sha)
            found.update(path for path in paths if path in tree)
    return sorted(found)
