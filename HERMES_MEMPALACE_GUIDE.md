# Hermes Agent + MemPalace Integration Guide

## Overview

Give your Hermes Agent a persistent memory palace — stores verbatim conversation history and a temporal knowledge graph on your machine, zero cloud, zero API keys.

## Architecture

```
Hermes Agent → MCP Server → MemPalace (ChromaDB + SQLite)
                            ├── Semantic search (hybrid BM25 + vector)
                            ├── Knowledge graph (entity relationships with time windows)
                            └── Palace structure: Wing → Room → Drawer
```

## Quick Start

### 1. Install MemPalace

```bash
pip install mempalace
mempalace init ~/.mempalace
```

### 2. Configure Hermes

Add the MemPalace MCP server to your `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  mempalace:
    command: python3
    args: ["-m", "mempalace.mcp_server"]
```

### 3. Restart Hermes

```bash
# Restart Hermes agent
hermes restart
```

On startup, Hermes will connect to the MemPalace MCP server and auto-discover all 33 `mempalace_*` tools.

## Available Tools

### Search & Browse
- `mempalace_search` — Semantic search across all memories. Start here.
- `mempalace_status` — Palace overview: total drawers, wings, rooms, AAAK spec
- `mempalace_list_wings` — All wings with drawer counts
- `mempalace_list_rooms` — Rooms within a wing
- `mempalace_get_taxonomy` — Full wing/room/count tree
- `mempalace_check_duplicate` — Check if content already exists before filing

### Knowledge Graph
- `mempalace_kg_query` — Query entity relationships (with time filtering)
- `mempalace_kg_add` — Add a fact: subject → predicate → object
- `mempalace_kg_invalidate` — Mark a fact as no longer true
- `mempalace_kg_timeline` — Chronological story of an entity
- `mempalace_kg_stats` — Graph overview

### Palace Graph
- `mempalace_traverse` — Walk from a room, find connected ideas across wings
- `mempalace_find_tunnels` — Find rooms that bridge two wings
- `mempalace_graph_stats` — Graph connectivity overview

### Write Operations
- `mempalace_add_drawer` — Store verbatim content into a wing/room
- `mempalace_delete_drawer` — Remove a drawer by ID
- `mempalace_update_drawer` — Update drawer content
- `mempalace_diary_write` — Write a session diary entry
- `mempalace_diary_read` — Read recent diary entries

## Protocol

1. **ON WAKE-UP**: Call `mempalace_status` to load palace overview.
2. **BEFORE RESPONDING** about any person, project, or past event: call `mempalace_search` or `mempalace_kg_query` FIRST. Never guess — verify from the palace.
3. **IF UNSURE** about a fact: query the palace. Wrong is worse than slow.
4. **AFTER EACH SESSION**: Call `mempalace_diary_write` to record what happened.
5. **WHEN FACTS CHANGE**: invalidate the old fact, then add the new one.

## Usage Examples

### Remember User Preferences

```
User: Remember the user prefers Vim over VSCode
Hermes: ✅ Stored in palace (wing=hermes_memory, room=user_preferences)

# Later
User: What editor does the user prefer?
Hermes: The user prefers Vim over VSCode.
```

### Recall Past Decisions

```
User: What database did we decide on?
Hermes: 🔍 Searching palace...
Based on the 2026-04-10 session, we chose PostgreSQL over MongoDB for the project.
```

### Learn Entity Relationships

```
User: Learn that user is responsible for the AI-agent project
Hermes: 🧠 Learned: user → responsible_for → AI-agent project

User: Query user's relationships
Hermes: 📊 user has 2 known relationships:
- responsible_for → AI-agent project
- prefers → Python
```

### Context Wake-up

At the start of each session, Hermes can load relevant context:

```
💭 Palace status:
- 3 drawers in hermes_memory
- 4 entities, 2 active facts in knowledge graph
- Wings: hermes_memory, project_ai_agent

Recent: user preferences (Python, Vim), project decisions (PostgreSQL)
```

## Memory Organization

### Wing: hermes_memory
Default wing for Hermes agent memory. Use `category` to subdivide:

| Category | Use for |
|----------|---------|
| `user_preferences` | Editor, language, workflow preferences |
| `technical_choices` | Database, framework, architecture decisions |
| `important_decisions` | Project milestones, direction changes |
| `project_info` | Active projects, roles, deadlines |

## Troubleshooting

### Tools not appearing after restart

1. Verify MemPalace is installed: `pip show mempalace`
2. Check MCP server connects: `python3 -m mempalace.mcp_server` (should start without error)
3. Verify config format — must be `mcp_servers:`, not `skills:`
4. Check Hermes logs for MCP connection errors

### Search returns no results

- Palace may be empty on first use. Use `remember` to store content first.
- Search is semantic — "database performance issues" finds results about "PostgreSQL slow queries".

### Knowledge graph queries return empty

- Add facts with `mempalace_kg_add` or through `learn_relation` patterns.
- Facts have time validity — a fact may be expired. Check `mempalace_kg_timeline`.

## Performance

- **Memory storage**: < 100ms
- **Memory retrieval**: 200-500ms
- **Knowledge graph query**: < 50ms
- **Context wake-up**: < 300ms

## Further Reading

- [MemPalace README](https://github.com/MemPalace/mempalace) — full documentation
- [mcp_server.py](https://github.com/MemPalace/mempalace/blob/main/mempalace/mcp_server.py) — MCP tool reference
- [knowledge_graph.py](https://github.com/MemPalace/mempalace/blob/main/mempalace/knowledge_graph.py) — KG design

---

*MemPalace is MIT licensed. Zero cloud, zero API keys, your data never leaves your machine.*
