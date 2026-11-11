"""PreToolUse guard: bypass attempts are denied, look-alike harmless calls pass."""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from commonplace.guards import pretool

REFUSED_COMMANDS = [
    "git commit --no-verify -m wip",
    "git commit -n -m wip",
    "git commit -anm wip",
    "git push --no-verify origin HEAD",
    "git -C sub push --no-verify",
    "git add . && git commit -m x --no-verify",
    "git -c core.hooksPath=/dev/null commit -m x",
    "git config core.hooksPath /tmp/none",
    "git config --unset core.hooksPath",
    "HUSKY=0 git commit -m x",
    "SKIP=lint git commit -m x",
    "export HUSKY=0",
    "rm .git/hooks/pre-commit",
    "rm -f .husky/pre-push",
    "chmod -x .git/hooks/pre-push",
    "mv .git/hooks/pre-commit /tmp/off",
    "rm .pre-commit-config.yaml",
    "pre-commit uninstall",
    "bash -c 'git commit -n -m x'",
    "sudo git push --no-verify",
]

ALLOWED_COMMANDS = [
    "git commit -m 'document the --no-verify rule'",
    'git commit -m "never use -n or core.hooksPath"',
    "git push -n origin main",
    "git push --dry-run",
    "git status",
    "git config core.hooksPath",
    "git config --get core.hooksPath",
    "git log --grep=--no-verify --oneline",
    "grep -rn 'no-verify' docs",
    "echo 'HUSKY=0 skips hooks'",
    "HUSKY=0 npm ci",
    "SKIP=1 make build",
    "cat .git/hooks/pre-commit",
    "ls .husky",
    "rm .git/hooks/pre-commit.sample",
    "rm -rf node_modules",
    "head -n 5 README.md",
    "pre-commit run --all-files",
    "git commit -m 'fix'",
    "cat <<'EOF'\ngit commit --no-verify\nEOF",
]


def call(tool_name: str, **tool_input) -> dict:
    return {"tool_name": tool_name, "tool_input": tool_input}


@pytest.mark.parametrize("command", REFUSED_COMMANDS)
def test_bash_bypass_is_denied(command):
    decision = pretool.evaluate(call("Bash", command=command))
    assert decision is not None and decision.blocked, command
    out = pretool.render(decision)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"].startswith("blocked:")


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_harmless_lookalike_is_allowed(command):
    assert pretool.evaluate(call("Bash", command=command)) is None, command


def test_codex_argv_command_is_checked():
    argv = ["bash", "-lc", "git commit --no-verify -m x"]
    assert pretool.evaluate(call("local_shell", command=argv)) is not None
    assert pretool.evaluate(call("shell", command=["git", "commit", "-m", "-n"])) is None
    assert pretool.evaluate(call("exec_command", cmd="git push --no-verify")) is not None


def test_non_shell_tool_with_command_text_is_ignored():
    assert pretool.evaluate(call("mcp__x__run", command="git commit --no-verify")) is None
    assert pretool.evaluate(call("Read", file_path=".git/hooks/pre-commit")) is None


def test_edit_of_existing_hook_file_is_denied(tmp_path):
    hook = tmp_path / ".husky" / "pre-commit"
    hook.parent.mkdir()
    hook.write_text("exit 0\n")
    for tool in ("Edit", "MultiEdit", "Write"):
        payload = {"tool_name": tool, "cwd": str(tmp_path), "tool_input": {"file_path": str(hook)}}
        decision = pretool.evaluate(payload)
        assert decision is not None and decision.rule == "hook-files", tool


def test_creating_a_missing_hook_and_ordinary_files_are_allowed(tmp_path):
    new_hook = {"file_path": str(tmp_path / ".git" / "hooks" / "pre-push")}
    assert pretool.evaluate({"tool_name": "Write", "tool_input": new_hook}) is None
    sample = {"file_path": ".git/hooks/pre-push.sample"}
    assert pretool.evaluate(call("Edit", **sample)) is None
    assert pretool.evaluate(call("Edit", file_path="src/app.py")) is None
    # Ordinary edits of a hook-runner config stay possible; deleting it does not.
    assert pretool.evaluate(call("Edit", file_path=".pre-commit-config.yaml")) is None


def test_apply_patch_touching_hooks_is_denied():
    update = "*** Begin Patch\n*** Update File: .husky/pre-commit\n@@\n-a\n+b\n*** End Patch"
    delete = "*** Begin Patch\n*** Delete File: .pre-commit-config.yaml\n*** End Patch"
    move = "*** Update File: x.py\n*** Move to: .git/hooks/pre-commit\n"
    ok = "*** Begin Patch\n*** Update File: src/app.py\n@@\n-a\n+b\n*** End Patch"
    for patch in (update, delete, move):
        assert pretool.evaluate(call("apply_patch", command=patch)) is not None, patch
    assert pretool.evaluate(call("apply_patch", command=ok)) is None


def test_warn_mode_flags_without_denying():
    decision = pretool.evaluate(call("Bash", command="git commit -n"), enforce=False)
    assert decision is not None and not decision.blocked
    out = pretool.render(decision)
    assert out["systemMessage"].startswith("warning:")
    assert "permissionDecision" not in out["hookSpecificOutput"]


@pytest.mark.parametrize("payload", [None, [], "x", {}, {"tool_name": "Bash"}, call("Bash")])
def test_unusable_payloads_pass(payload):
    assert pretool.evaluate(payload) is None


def _run(payload: str, *, enforce: bool = True) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = pretool.run(io.StringIO(payload), out, err, enforce=enforce)
    return code, out.getvalue(), err.getvalue()


def test_run_denies_on_stdout_and_silent_when_clean():
    code, out, err = _run(json.dumps(call("Bash", command="git push --no-verify")))
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "--no-verify" in err
    assert _run(json.dumps(call("Bash", command="git status"))) == (0, "", "")


def test_run_lets_unreadable_input_through_loudly():
    code, out, err = _run("not json")
    assert (code, out) == (0, "")
    assert "not JSON" in err


def test_module_entry_point_round_trip():
    payload = json.dumps(call("Bash", command="HUSKY=0 git commit -m x"))
    result = subprocess.run(
        [sys.executable, "-m", "commonplace.guards.pretool"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    warn = subprocess.run(
        [sys.executable, "-m", "commonplace.guards.pretool", "--warn"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "permissionDecision" not in warn.stdout
    assert "warning:" in warn.stdout
