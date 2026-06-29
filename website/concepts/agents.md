# Specialist Agents

MemPalace currently supports **agent diaries** through MCP tools. The practical model is simple: give an agent a stable name, and write/read diary entries under that agent's wing.

::: warning Current Scope
This page documents the diary workflow that exists today. MemPalace does **not** currently ship an agent registry, `~/.mempalace/agents/*.json`, or a `mempalace_list_agents` tool.
:::

## What Agents Do

Each agent:

- **Has a focus** — what it pays attention to
- **Keeps a diary** — entries persist across sessions
- **Can read recent history** — useful for patterns, continuity, and follow-up work

## Agent Diary

The diary is a lightweight memory stream for one named agent: observations, findings, decisions, and recurring patterns.

### Writing Entries

```text
MCP tool: mempalace_diary_write
  arguments: {
    "agent_name": "reviewer",
    "entry": "PR#42|auth.bypass.found|missing.middleware.check|pattern:3rd.time.this.quarter|★★★★"
  }
```

### Reading History

```text
MCP tool: mempalace_diary_read
  arguments: { "agent_name": "reviewer", "last_n": 10 }
  → returns last 10 findings, compressed in AAAK
```

### MCP Tools

| Tool | Description |
|------|-------------|
| `mempalace_diary_write` | Write an AAAK diary entry |
| `mempalace_diary_read` | Read recent diary entries |

## How It Works

Each named agent maps to its own wing in the palace:
- `wing_reviewer` — the reviewer's diary, findings, patterns
- `wing_architect` — the architect's decisions, tradeoffs
- `wing_ops` — the ops agent's incidents, deploys

All entries go into a `diary` room within the wing, tagged with topic, timestamp, and agent name.

## Specialization

Separate diary streams let you keep different working contexts apart. A reviewer can keep bug patterns, an architect can keep decisions, and an ops agent can keep incident notes without mixing them into one shared log.

::: tip
If you use multiple specialist prompts or toolchains, keep the agent names stable so each one writes back to the same diary wing over time.
:::
