"""Detection table for commonplace.guards.bypass: commands that skip or disable git hooks."""

from __future__ import annotations

import pytest

from commonplace.guards.bypass import Finding, match_hook_bypass

POSITIVES: list[tuple[str, str]] = [
    # no-verify: flags, clusters, abbreviations, wrappers and nesting
    ("git commit --no-verify -m x", "no-verify"),
    ("git commit -n", "no-verify"),
    ("git commit -anm 'fix parser'", "no-verify"),
    ("git commit -nm x", "no-verify"),
    ("git commit -S -n -m x", "no-verify"),
    ("git commit -C HEAD~1 -n", "no-verify"),
    ("git commit --no-ver -m x", "no-verify"),
    ("git commit --fixup=abc -n", "no-verify"),
    ("git push --no-verify", "no-verify"),
    ("git push --no-verif origin", "no-verify"),
    ("git merge --no-verify feature", "no-verify"),
    ("git am --no-verify patch.mbox", "no-verify"),
    ("git rebase --no-verify main", "no-verify"),
    ("git -C sub/repo commit -n", "no-verify"),
    ("git commit -m 'wip' && git push --no-verify", "no-verify"),
    ("/usr/bin/git commit -n", "no-verify"),
    ("git.exe commit -n", "no-verify"),
    ("bash -c 'git commit --no-verify'", "no-verify"),
    ('bash -lc "git commit -n"', "no-verify"),
    ("eval 'git push --no-verify'", "no-verify"),
    ("echo $(git commit -n)", "no-verify"),
    ("echo `git commit -n`", "no-verify"),
    ("(cd sub && git commit -n)", "no-verify"),
    ("{ git commit -n; }", "no-verify"),
    ("if x; then git commit -n; fi", "no-verify"),
    ("sudo -u root git push --no-verify", "no-verify"),
    ("nice -n 5 git commit -n -m x", "no-verify"),
    ("git commit -m a#b -n", "no-verify"),
    ('git commit -m "$(cat msg)" -n', "no-verify"),
    ('git commit -m "unterminated -n', "no-verify"),
    # hooks-path: -c, --config-env, git config writes, env overrides
    ("git -c core.hooksPath=/dev/null commit -m x", "hooks-path"),
    ("git -c CORE.HOOKSPATH=/dev/null status", "hooks-path"),
    ("git -ccore.hooksPath=x status", "hooks-path"),
    ("git --config-env=core.hooksPath=HOOKS_DIR commit -m x", "hooks-path"),
    ("git config core.hooksPath .githooks", "hooks-path"),
    ("git config --global core.hooksPath /dev/null", "hooks-path"),
    ("git config --local --replace-all core.hooksPath x", "hooks-path"),
    ("git config --unset core.hooksPath", "hooks-path"),
    ("git config set core.hooksPath x", "hooks-path"),
    ("git config --worktree core.hooksPath x", "hooks-path"),
    ("git config --file .git/config core.hooksPath x", "hooks-path"),
    ("GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit -m x", "hooks-path"),
    ("GIT_CONFIG_PARAMETERS=\"'core.hookspath'='/dev/null'\" git status", "hooks-path"),
    # hook-env: prefixes on git writes, exported switches
    ("HUSKY=0 git commit -m x", "hook-env"),
    ("env HUSKY=0 git push", "hook-env"),
    ("env -u X SKIP=lint git commit -m x", "hook-env"),
    ("SKIP=lint git commit -m x", "hook-env"),
    ("PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit -m x", "hook-env"),
    ("LEFTHOOK=0 git push origin main", "hook-env"),
    ("LEFTHOOK_EXCLUDE=lint git commit -m x", "hook-env"),
    ("HUSKY_SKIP_HOOKS=1 git merge feature", "hook-env"),
    ("SKIP_SIMPLE_GIT_HOOKS=1 git commit -m x", "hook-env"),
    ("timeout 60 env HUSKY=0 git push", "hook-env"),
    ("export HUSKY=0", "hook-env"),
    ("export LEFTHOOK=0; npm test", "hook-env"),
    ("export SKIP_SIMPLE_GIT_HOOKS=1", "hook-env"),
    ("export SKIP=lint && git commit -m x", "hook-env"),
    ("export PRE_COMMIT_ALLOW_NO_CONFIG=1 && git push", "hook-env"),
    # hook-files: delete, move, truncate, chmod, redirect, git rm, uninstall
    ("rm -rf .git/hooks/pre-commit", "hook-files"),
    ("rm .husky/pre-commit", "hook-files"),
    ("find .git/hooks -type f -delete", "hook-files"),
    ("find .husky -name 'pre-*' -exec rm {} +", "hook-files"),
    ("rm -rf .husky", "hook-files"),
    ("rm .git/hooks/*", "hook-files"),
    ("unlink lefthook.yml", "hook-files"),
    ("mv .husky ../husky-old", "hook-files"),
    ("shred -u .pre-commit-config.yaml", "hook-files"),
    ("truncate -s 0 .git/hooks/pre-push", "hook-files"),
    ("chmod -x .git/hooks/pre-commit", "hook-files"),
    ("chmod a-x .husky/pre-push", "hook-files"),
    ("chmod 644 .husky/pre-push", "hook-files"),
    ("chmod 0644 .git/hooks/pre-push", "hook-files"),
    ("> .git/hooks/pre-commit", "hook-files"),
    (": > .husky/pre-commit", "hook-files"),
    ("echo '' > .husky/pre-commit", "hook-files"),
    ("cat > .git/hooks/pre-commit <<'EOF'\n#!/bin/sh\nEOF", "hook-files"),
    ("git rm .husky/pre-commit", "hook-files"),
    ("git rm -r --cached .husky", "hook-files"),
    ("pre-commit uninstall", "hook-files"),
    ("npx lefthook uninstall", "hook-files"),
    ("rm -rf $(git rev-parse --git-dir)/hooks", "hook-files"),
    ("rm $(git rev-parse --git-path hooks)/pre-commit", "hook-files"),
    ("mv .pre-commit-config.yaml old.yaml", "hook-files"),
]

NEGATIVES: list[str] = [
    "find .git/hooks -type f",
    "find . -name '*.pyc' -delete",
    # commit messages, echo, grep and log mention the flags as data
    'git commit -m "docs: never use --no-verify"',
    'echo "git commit --no-verify"',
    "grep -- --no-verify file.txt",
    "git log --grep=--no-verify",
    "printf 'HUSKY=0'",
    "git commit -m 'fix: hook' && git push -n origin main",
    "git commit -- -n",
    "git commit -mn",
    "git commit -m '-n'",
    "git commit --message=--no-verify",
    "git commit --message --no-verify",
    "git commit -m \"$(printf '%s' --no-verify)\"",
    "echo '$(git commit -n)'",
    "echo ok # git commit --no-verify",
    "echo 'git commit -n'",
    # git commands that are not hook bypasses
    "git push -n origin main",
    "git push --dry-run origin main",
    "git push origin main",
    "git push --force-with-lease origin feat",
    "git merge --no-ff feature",
    "git merge --abort",
    "git commit --amend --no-edit",
    "git rebase --continue",
    "git rebase -i HEAD~3",
    "git log -n 5",
    "git show HEAD --no-verify",
    "git diff --cached -- .husky/pre-commit",
    "git -C repo status",
    "git -c user.name=x commit -m y",
    "git -c core.autocrlf=true status",
    # core.hooksPath reads
    "git config --get core.hooksPath",
    "git config core.hooksPath",
    "git config --list",
    "git config get core.hooksPath",
    "git config --get-all core.hooksPath",
    "git config user.name 'Agent'",
    "git config --global user.email agent@example.com",
    # env prefixes and exports that do not disable hooks
    "HUSKY=0 npm ci",
    "env HUSKY=0 npm ci",
    "HUSKY=1 git commit -m x",
    "LEFTHOOK=1 git push",
    "SKIP=lint npm test",
    "echo HUSKY=0",
    "export HUSKY=1",
    "export PATH=$PATH:/opt/tools/bin",
    "export SKIP=lint && npm test",
    "export SKIP= && git status",
    "unset HUSKY",
    # hook files: reads, edits, sample hooks, executable bits
    "cat .husky/pre-commit",
    "ls .git/hooks",
    "less .git/hooks/pre-commit",
    "vim .husky/pre-commit",
    "sed -i 's/npm test/npm run lint/' .husky/pre-commit",
    "cp .husky/pre-commit /tmp/pre-commit.bak",
    "rm .git/hooks/pre-commit.sample",
    "rm .git/hooks.sample",
    "chmod +x .git/hooks/pre-commit",
    "chmod 755 .husky/pre-push",
    "rm -rf node_modules dist",
    "rm -rf build/ && ls .git/hooks",
    "printf '%s\\n' 'HUSKY=0' > /tmp/notes.txt",
    "npm test && git status",
    # heredoc bodies are data
    "cat <<EOF\ngit commit --no-verify\nEOF",
    "git commit -F - <<EOF\n--no-verify\nEOF",
]


@pytest.mark.parametrize(("command", "rule"), POSITIVES)
def test_positive_commands_are_flagged(command: str, rule: str) -> None:
    finding = match_hook_bypass(command)
    assert isinstance(finding, Finding), command
    assert finding.rule == rule, command


@pytest.mark.parametrize("command", NEGATIVES)
def test_negative_commands_are_not_flagged(command: str) -> None:
    assert match_hook_bypass(command) is None, command


def test_table_sizes() -> None:
    assert len(POSITIVES) >= 35
    assert len(NEGATIVES) >= 35


def test_advisory_mode_warns_without_blocking() -> None:
    finding = match_hook_bypass("git commit -n", enforce=False)
    assert finding is not None
    assert finding.blocked is False
    assert finding.message.startswith("warning:")


def test_enforce_message_blocks_and_points_at_broken_hook() -> None:
    finding = match_hook_bypass("git commit -n")
    assert finding is not None
    assert finding.blocked is True
    assert finding.message.startswith("blocked:")
    assert "report the broken hook" in finding.message
    assert "do not bypass hooks" in finding.message


def test_segment_points_at_the_offending_part_of_a_chain() -> None:
    finding = match_hook_bypass("npm test && git commit -m x --no-verify; ls -la")
    assert finding is not None
    assert finding.rule == "no-verify"
    assert finding.segment == "git commit -m x --no-verify"


def test_first_finding_in_command_order_wins() -> None:
    finding = match_hook_bypass("git push --no-verify && HUSKY=0 git commit -m x")
    assert finding is not None
    assert finding.segment == "git push --no-verify"


def test_substitution_segment_is_restored_as_text() -> None:
    finding = match_hook_bypass('git commit -m "$(cat msg)" -n')
    assert finding is not None
    assert finding.segment == 'git commit -m "$(cat msg)" -n'


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "oops',
        "git commit -n -m 'unterminated",
        "echo `unbalanced",
        "echo $(git commit -n",
        "cat <<EOF\nnever closed",
        "\\",
        '"',
        "'",
        "(((",
        'export HUSKY=0 "',
    ],
)
def test_unbalanced_or_odd_input_does_not_raise(command: str) -> None:
    result = match_hook_bypass(command)
    assert result is None or isinstance(result, Finding)


def test_unbalanced_quote_still_detects_bypass() -> None:
    finding = match_hook_bypass('git commit -n -m "oops')
    assert finding is not None
    assert finding.rule == "no-verify"
