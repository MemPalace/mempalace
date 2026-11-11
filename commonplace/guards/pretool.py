"""PreToolUse command that refuses git-hook bypasses by agents.

Claude Code and Codex run a configured command before a tool call and pass the call as
JSON on stdin. This command reads it, asks :mod:`commonplace.guards.bypass` about shell
commands, checks file-edit tools for hook files, and answers on stdout:

* clean call: no output, exit 0 (the harness keeps its normal permission flow);
* bypass, enforcing (default): a ``permissionDecision: "deny"`` JSON with the reason, exit 0;
* bypass, ``--warn``: the call is not denied; a ``systemMessage`` and ``additionalContext``
  flag it loudly instead.

Input the command cannot read (bad JSON) is let through with a warning on stderr: a broken
guard must not stop every shell call, and the merge gate stays the enforcement.

Run it as ``python -m commonplace.guards.pretool [--warn]``; it imports nothing heavy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO

from .bypass import Finding, is_hook_path, match_hook_bypass

SHELL_TOOLS = frozenset(
    {"bash", "shell", "local_shell", "exec_command", "run_shell_command", "powershell"}
)
_EDIT_TOOLS = frozenset({"write", "edit", "multiedit", "notebookedit"})
_PATCH_TOOLS = frozenset({"apply_patch"})
_PATH_KEYS = ("file_path", "notebook_path", "path")
_PATCH_LINE = re.compile(r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+?)\s*$", re.M)
_FILE_REASON = "editing or deleting a git hook file disables the hook"
_GUIDANCE = "Fix the failing check, or report the broken hook on your task; do not bypass hooks."


@dataclass(frozen=True)
class Decision:
    """A guard answer for one tool call."""

    rule: str
    reason: str
    blocked: bool

    @property
    def message(self) -> str:
        prefix = "blocked:" if self.blocked else "warning:"
        return f"{prefix} {self.reason}. {_GUIDANCE}"


def _shell_command(tool_input: dict) -> str | None:
    command = tool_input.get("command", tool_input.get("cmd"))
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        return shlex.join(command)
    return command if isinstance(command, str) else None


def _exists(path: str, cwd: str | None) -> bool:
    return os.path.exists(os.path.join(cwd, path) if cwd else path)


def _patch_hits(patch: str, cwd: str | None) -> str | None:
    """Reason when an apply_patch body updates, moves or deletes a hook file."""
    for action, path in _PATCH_LINE.findall(patch):
        if action == "Delete File" and is_hook_path(path):
            return _FILE_REASON
        if is_hook_path(path, include_config=False):
            if action != "Add File" or _exists(path, cwd):
                return _FILE_REASON
    return None


def _edit_hit(name: str, tool_input: dict, cwd: str | None) -> str | None:
    for key in _PATH_KEYS:
        path = tool_input.get(key)
        if isinstance(path, str) and is_hook_path(path, include_config=False):
            # Creating a missing hook is installing one; rewriting an existing one is not.
            if name != "write" or _exists(path, cwd):
                return _FILE_REASON
    return None


def evaluate(payload: object, *, enforce: bool = True) -> Decision | None:
    """Decision for one PreToolUse payload, or None when the call is fine."""
    if not isinstance(payload, dict):
        return None
    name = str(payload.get("tool_name") or "").lower()
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    if name in SHELL_TOOLS:
        command = _shell_command(tool_input)
        finding: Finding | None = match_hook_bypass(command, enforce=enforce) if command else None
        if finding is not None:
            return Decision(finding.rule, finding.reason, enforce)
        return None
    reason = None
    if name in _EDIT_TOOLS:
        reason = _edit_hit(name, tool_input, cwd)
    elif name in _PATCH_TOOLS:
        patch = tool_input.get("command", tool_input.get("input"))
        reason = _patch_hits(patch, cwd) if isinstance(patch, str) else None
    return Decision("hook-files", reason, enforce) if reason else None


def render(decision: Decision) -> dict:
    """The JSON object the harness reads from stdout."""
    if decision.blocked:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.message,
            }
        }
    return {
        "systemMessage": decision.message,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": decision.message,
        },
    }


def run(stdin: IO[str], stdout: IO[str], stderr: IO[str], *, enforce: bool = True) -> int:
    try:
        payload = json.loads(stdin.read())
    except ValueError:
        print("commonplace guard: tool call input is not JSON; allowed unchecked", file=stderr)
        return 0
    decision = evaluate(payload, enforce=enforce)
    if decision is None:
        return 0
    json.dump(render(decision), stdout)
    stdout.write("\n")
    print(f"commonplace guard {decision.message}", file=stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m commonplace.guards.pretool",
        description="PreToolUse guard: refuse git-hook bypasses (reads the tool call from stdin).",
    )
    parser.add_argument(
        "--warn",
        action="store_true",
        help="advisory mode: flag a bypass loudly instead of denying it",
    )
    args = parser.parse_args(argv)
    return run(sys.stdin, sys.stdout, sys.stderr, enforce=not args.warn)


if __name__ == "__main__":
    raise SystemExit(main())
