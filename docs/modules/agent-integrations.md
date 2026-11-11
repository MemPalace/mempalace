## agent-integrations

- **Paths:** `.agents/`, `.antigravity-plugin/`, `.claude-plugin/`, `.codex-plugin/`, `.cursor-plugin/`, `.dsh-plugin/`, `.mcp.json`, `mcp.json`, `commands/`, `hooks/`, `integrations/`, `skills/`, `rules/`, `examples/`
- **Owns:**

  - Plugin manifests, slash commands, hook scripts, editor rules, packaged skills and integration examples for coding agents.

- **Promises:**

  - Hooks fail open and stay within the hook time budget; manifests reference files that exist; no integration enables an external provider by default.

- **How it fails:**

  - A manifest that points at a moved file breaks installation for that agent only; a slow hook delays the user's session.

- **Tests:**

  - `tests/test_claude_plugin_hook_config.py`, `tests/test_claude_plugin_hook_wrappers.py`, `tests/test_antigravity_plugin_manifest.py`, `tests/test_claude_marketplace_skills.py`, `tests/test_hooks_cli.py`, `tests/test_untangle_skill.py`.

  - `skills/untangle-codebase/` is a generic, repo-independent debt-cleanup skill (SKILL.md plus a read-only `scripts/measure.py`); `.claude-plugin/skills/untangle-codebase/` is its byte-identical bundle copy, and `tests/test_untangle_skill.py` keeps it well-formed (front matter, required sections, no local paths).

  - `.dsh-plugin/test/autosave.test.mjs` holds a hook run open until the test releases it (`until` in `.dsh-plugin/test/host.mjs`) to check that unloading joins a running hook and that queued runs collapse, and checks what must not have happened after one event-loop turn (`loopTurn`) or after `unload()`, not after a timer. Positive checks wait for the hook spawn, warning or transcript content. The `unload()` negatives only see runs that go through `schedule()`, because `unload()` drains exactly those.

  - `.dsh-plugin/test/recall.test.mjs` checks cancellation and unload by whether the wait ends at all (`settles` with a 30 s hang guard), with the palace read held open and every plugin timer longer than the guard. Only the two first-assembly budget tests measure time; `tests/test_dsh_plugin.py` skips them in the functional run and runs them as `test_dsh_plugin_node_suite_budgets` in the `timing` lane (`pytest -o addopts= -m timing tests/test_dsh_plugin.py`).
