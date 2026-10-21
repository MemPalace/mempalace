# Cursor Rules — MemPalace recall

Optional [Cursor rules](https://cursor.com/docs/rules) that make the
agent search MemPalace before answering questions about past work,
people, projects, or prior decisions.

These are for users who install MemPalace **without** the Cursor plugin
(or who want recall behaviour in a specific project). If you installed
the [Cursor plugin](../../../.cursor-plugin/README.md), it already ships
the `alwaysApply: false` rule at the plugin root — you do not need to
copy anything.

## Which file to use

| File | `alwaysApply` | Fires when | Use when |
|------|---------------|------------|----------|
| [`mempalace-recall.mdc`](mempalace-recall.mdc) | `false` | Cursor's matcher decides the turn is recall-relevant (from the rule `description`) | **Recommended.** Recall without paying for the rule on unrelated work. |
| [`mempalace-recall-always.mdc`](mempalace-recall-always.mdc) | `true` | Every conversation in scope, every turn | You want recall guaranteed in context and accept the cost. |

The always-on variant is heavier: it sits in context on every turn and
makes the agent more eager to call `mempalace_search`, which adds MCP
latency and works against MemPalace's "memory should feel instant"
budget. Prefer the `false` variant unless you specifically want recall
forced into every conversation. Pick **one** of the two — do not install
both.

## Install

User scope (every workspace) — copy into `~/.cursor/rules/`:

```bash
mkdir -p ~/.cursor/rules
cp examples/cursor/rules/mempalace-recall.mdc ~/.cursor/rules/
```

Project scope (this repo only) — copy into `.cursor/rules/`:

```bash
mkdir -p .cursor/rules
cp examples/cursor/rules/mempalace-recall.mdc .cursor/rules/
```

For the aggressive variant, copy `mempalace-recall-always.mdc` instead
(only one of the two). Then reload Cursor:
<kbd>Cmd</kbd>-<kbd>Shift</kbd>-<kbd>P</kbd> → **Developer: Reload Window**.

## How recall is delivered

Recall ships in three orthogonal layers — install any combination:

| Layer | What it does | Where |
|-------|--------------|-------|
| `sessionStart` hook | Injects wing-scoped recall context once per new chat | [`hooks/cursor/`](../../../hooks/cursor/) |
| `mempalace-recall` skill | Full search-before-answer protocol, model-invoked or attached | [`skills/mempalace-recall/`](../../../skills/mempalace-recall/) |
| Recall rule (these files) | Nudges search-before-answer on recall-relevant turns | here, or the plugin root `rules/` |

All three reference the same canonical protocol in
[`integrations/shared/recall-protocol.md`](../../../integrations/shared/recall-protocol.md).
