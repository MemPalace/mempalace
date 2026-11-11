## rules-and-guards

- **Paths:** `commonplace/rules/`, `commonplace/guards/`, `commonplace/privacy.py`, `commonplace/redact.py`
- **Owns:**

  - Canonical Space create in the checked REST write inventory.

  - The goals writes `goal_set` and `card_goal` in the checked MCP write inventory, because goals become agent card context.

  - The project panel write `PUT /api/projects/<slug>/panel` (`project_panel_set`) in the checked REST write inventory.

  - Owner-reviewed rules and their freshness gate, guard building blocks, sensitive-text flags and redaction.

  - Rule evidence (`commonplace/rules/evidence.py`): bounded `rule_events` plus `rule_stats` counters per workspace, written by `rules_feedback` (MCP) and `/api/rules/<id>/feedback` (REST).

  - The pre-push quick check (`guards/prepush.py`): changed-file selection, the shared time budget and the report of what it skipped; and the hook installer (`guards/hooks.py`) whose scripts resolve each worktree's own environment.

  - `commonplace.guards.pretool`, a PreToolUse command that refuses git-hook bypasses (`docs/hook-bypass-guard.md`).

- **Promises:**

  - `commonplace/rules/checks.py` keeps the deadline API (`deadline`, `remaining`, `_deadline`) as thin wrappers over `mempalace/deadline.py` with its own patchable clock; fact readers call the leaf directly and do not import rules.

  - Split completion markers are checked writes on MCP and REST.
  - Canonical create uses header freshness while legacy routes retain body compatibility.

  - Rules take effect only after owner review; writes from a client with stale rules get 428; flags are deterministic.
  - The bypass guard denies hook-skipping shell commands and edits of existing hook files with a reason, passes look-alike text, and never auto-approves.

  - `rules_get` takes `keys=[...]` for selected rules in full (unknown keys named) and, with `effective=true`, `format=index` for the compact index. Both are partial reads: they never record the full set as served and give no `rules_seq`. Defaults are unchanged.

  - The quick check names itself a quick check, reports every skipped or cut-off step, and fails the push only when a check it ran failed. The installed hooks work in a fresh linked worktree or say how to set it up.

  - Evidence never changes a rule or its sequence. Agents write with `memory:write` and read back only their own events plus the rule's counters; people read all events, only owners write. `rules_feedback` is an exempt write: a report, not an instruction.

- **How it fails:**

  - The deploy migration opens an unbound `boards.sqlite3` with a 0.2 s busy timeout on purpose: a locked file fails that workspace fast and is logged, and the next start retries it.

  - Missing and stale served sequences retain the 428 refresh payload.

  - Redaction can miss a sensitive detail; flags are hints, not blocks.

  - Evidence tables are created additively in `migrate()`; events are capped (50 per writer, 200 per rule) while counters keep the full history, so a pruned event is no longer readable but still counted.

  - A test whose name matches no changed module is not selected, so the quick check can pass while a distant test would fail; a cut-off step is reported, not retried; a worktree with no environment blocks the hook with the setup command.

- **Tests:**

  - Route-aware checked REST harness and Space freshness contract tests.

  - `tests/test_rules_index.py` pins index completeness and size, keyed get with unknown keys, partial reads not serving, and unchanged defaults; `tests/spaces_surfaces.json` carries the new `rules_get` and `session_start` arguments.

  - `tests/test_commonplace_rules.py`, `tests/test_commonplace_rules_enforce.py`, `tests/test_commonplace_rules_evidence.py`, `tests/test_commonplace_privacy.py`, the `tests/test_guards_` files.

  - `tests/test_rules_enforcement_points.py` holds one skip-detecting check per enforcement point (injection, session hook, write sequence check, reviewers); `docs/design/RULES.md` maps each point to its tests.

  - `tests/test_guards_prepush.py` covers test selection, the budget (cut-off lint, cut-off tests, skipped steps), docs-check and the per-worktree tool path; `tests/test_guards_hooks.py` installs the hooks into a clone and pushes and commits from fresh linked worktrees.
