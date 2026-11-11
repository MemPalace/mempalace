# Git-hook bypass guard

Agents should fix a failing hook, not skip it. `commonplace.guards.pretool` is a
PreToolUse command for Claude Code and Codex: the harness runs it before every tool
call, passes the call as JSON on stdin, and the command answers allow (silence) or deny
with a reason. It is defense in depth; the merge gate stays the enforcement.

## What it refuses

| Rule | Examples |
| --- | --- |
| `no-verify` | `git commit --no-verify`, `git commit -n` (also in clusters such as `-anm`), `git push --no-verify`; `git push -n` is a dry run and passes |
| `hooks-path` | `git -c core.hooksPath=...`, `git config core.hooksPath ...`, `--unset core.hooksPath`; reading the value passes |
| `hook-env` | `HUSKY=0 git commit`, `SKIP=... git commit`, `export HUSKY=0`; `HUSKY=0 npm ci` passes |
| `hook-files` | `rm`, `mv`, `truncate`, `chmod -x` or `pre-commit uninstall` on `.git/hooks`, `.husky`, lefthook and pre-commit config files; file-edit tools and `apply_patch` changing or deleting an existing hook file |

Quoted text is not a bypass: `git commit -m "never use --no-verify"`, `grep no-verify docs`
and heredoc bodies pass. Chains, `git -C`, `sudo`/`env` wrappers, `bash -c` payloads and
Codex argv-style commands are parsed. Editing a hook-runner config such as
`.pre-commit-config.yaml` with a file tool stays possible (adding a hook is normal work);
deleting it does not. `*.sample` hooks are never flagged.

The command text is only parsed, never run. Known gaps are listed in
`commonplace/guards/bypass.py` (aliases that add `-n`, `sed -i` on a hook file,
`git rebase --exec` payloads).

## Install

Add the guard to the harness's tool-call hooks. The same JSON shape works for Claude Code
(`~/.claude/settings.json` or `.claude/settings.json`) and Codex (`~/.codex/hooks.json` or
`.codex/hooks.json`; Codex asks you to review and trust the hook in `/hooks`):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|MultiEdit|Write|apply_patch|shell|local_shell|exec_command",
        "hooks": [
          {
            "type": "command",
            "command": "python3 -m commonplace.guards.pretool",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Use the Python that has `commonplace` installed (for example
`uv run --project /path/to/checkout python -m commonplace.guards.pretool`). Merge the
entry into an existing `PreToolUse` list instead of replacing it. Nothing here is
installed for you.

## Answers

- Clean call: no output, exit 0. The harness keeps its normal permission flow; the guard
  never auto-approves anything.
- Bypass: JSON on stdout with `hookSpecificOutput.permissionDecision: "deny"` and a reason
  that tells the agent to fix the failing check or report the broken hook on its task. The
  reason is also written to stderr. Exit code 0.
- `--warn` (advisory): the call is not denied; `systemMessage` and `additionalContext`
  carry a `warning:` line so the bypass is flagged loudly. Use it while rolling out.
- Input that is not JSON is allowed with a stderr warning, so a broken guard cannot block
  every shell call.

Try it by hand:

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"git commit --no-verify -m x"}}' \
  | python3 -m commonplace.guards.pretool
```

Tests: `tests/test_guards_pretool.py` (refused and look-alike tables, file tools, warn
mode, process round trip) and `tests/test_guards_bypass.py` (the shell matcher).
