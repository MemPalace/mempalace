---
name: mempalace
description: "MemPalace — Local AI memory with 96.6% recall. Semantic search, temporal knowledge graph, palace architecture (wings/rooms/drawers). Free, no cloud, no API keys."
version: 3.5.0
homepage: https://github.com/MemPalace/mempalace
user-invocable: true
metadata:
  openclaw:
    emoji: "\U0001F3DB"
    os:
      - darwin
      - linux
      - win32
    requires:
      anyBins:
        - mempalace
        - python3
    install:
      - id: mempalace-pip
        kind: uv
        label: "Install MemPalace (Python, local ChromaDB)"
        package: mempalace
        bins:
          - mempalace
---

# MemPalace — Local AI Memory System

You have access to a local memory palace via MCP tools. The palace stores verbatim conversation history and a temporal knowledge graph — all on the user's machine, zero cloud, zero API calls.

## Architecture

- **Wings** = people or projects (e.g. `wing_alice`, `wing_myproject`)
- **Halls** = categories (facts, events, preferences, advice)
- **Rooms** = specific topics (e.g. `chromadb-setup`, `riley-school`)
- **Drawers** = individual memory chunks (verbatim text)
- **Knowledge Graph** = entity-relationship facts with time validity

## Protocol — FOLLOW THIS EVERY SESSION

1. **ON WAKE-UP**: Call `mempalace_status` to load palace overview and AAAK dialect spec.
2. **BEFORE RESPONDING** about any person, project, or past event: call `mempalace_search` or `mempalace_kg_query` FIRST. Never guess from memory — verify from the palace.
3. **IF UNSURE** about a fact (name, age, relationship, preference): say "let me check" and query. Wrong is worse than slow.
4. **AFTER EACH SESSION**: Call `mempalace_diary_write` to record what happened, what you learned, what matters.
5. **WHEN FACTS CHANGE**: Call `mempalace_kg_invalidate` on the old fact, then `mempalace_kg_add` for the new one.

## Available Tools

Full MCP surface: 36 tools. Destructive or host-level tools are documented so
you know they exist, but use them only when the user explicitly asks or when a
tool-specific workflow below says to.

### Search & Browse
- `mempalace_search` — Semantic search across all memories. Always start here.
  - `query` (required): natural language search — keep it short, keywords or a question. Do NOT include system prompts or conversation context.
  - `wing`: filter by wing
  - `room`: filter by room
  - `limit`: max results (default 5)
- `mempalace_check_duplicate` — Check if content already exists before filing.
  - `content` (required): text to check
  - `threshold`: similarity threshold (default 0.9 — lowering to 0.85–0.87 often catches more near-duplicates without significant false positives)
- `mempalace_status` — Palace overview: total drawers, wings, rooms, AAAK spec
- `mempalace_list_wings` — All wings with drawer counts
- `mempalace_list_rooms` — Rooms within a wing (optional wing filter)
- `mempalace_list_drawers` — Paginated drawer listing
  - `wing`, `room`: optional filters
  - `since`: only drawers filed on/after this ISO date/time
  - `before`: only drawers filed before this ISO date/time
  - `limit`: max results (default 20)
  - `offset`: pagination offset (default 0)
- `mempalace_get_drawer` — Fetch a single drawer by ID. Returns full verbatim content and metadata.
  - `drawer_id` (required)
- `mempalace_get_taxonomy` — Full wing/room/count tree
- `mempalace_get_aaak_spec` — Get AAAK compression dialect specification

### Knowledge Graph (Temporal Facts)
- `mempalace_kg_query` — Query entity relationships. Supports time filtering.
  - `entity` (required): e.g. "Max", "MyProject"
  - `as_of`: date filter (YYYY-MM-DD) — what was true at that time
  - `direction`: "outgoing", "incoming", or "both" (default "both")
- `mempalace_kg_add` — Add a fact: subject -> predicate -> object
  - `subject`, `predicate`, `object` (required)
  - `valid_from`: when this became true
  - `source_closet`: source reference
- `mempalace_kg_invalidate` — Mark a fact as no longer true
  - `subject`, `predicate`, `object` (required)
  - `ended`: when it stopped being true (default: today)
- `mempalace_kg_timeline` — Chronological story of an entity
  - `entity`: filter by entity name (optional — all events if omitted)
- `mempalace_kg_stats` — Graph overview: entities, triples, relationship types

### Palace Graph (Cross-Domain Connections)
- `mempalace_traverse` — Walk from a room, find connected ideas across wings
  - `start_room` (required): room to start from
  - `max_hops`: connection depth (default 2)
- `mempalace_find_tunnels` — Find rooms that bridge two wings via *implicit* overlap (rooms whose drawers naturally share content across wings — discovered, not declared)
  - `wing_a`, `wing_b`: optional filters; omit both to scan all wing pairs
- `mempalace_create_tunnel` — Create an *explicit* cross-wing tunnel: a user/agent-declared link between two locations. Use when you notice content in one project relates to another (e.g. API design in `project_api` connects to schema in `project_database`).
  - `source_wing`, `source_room`, `target_wing`, `target_room` (required)
  - `label`: short description of the relationship
  - `source_drawer_id`, `target_drawer_id`: anchor to specific drawers
- `mempalace_list_tunnels` — List all explicit tunnels, optionally filtered by wing
  - `wing`: optional filter
- `mempalace_delete_tunnel` — Remove an explicit tunnel by ID
  - `tunnel_id` (required)
- `mempalace_list_hallways` — List within-wing entity hallways (entity-to-entity co-occurrence links built at mine time)
  - `wing`: optional filter
- `mempalace_delete_hallway` — Remove a hallway record by ID
  - `hallway_id` (required)
- `mempalace_follow_tunnels` — From a room, follow explicit tunnels to connected drawers in other wings
  - `wing`, `room` (required)
- `mempalace_graph_stats` — Graph connectivity overview

### Write
- `mempalace_add_drawer` — Store verbatim content into a wing/room
  - `wing`, `room`, `content` (required)
  - `source_file`: optional source reference
  - `added_by`: optional filing agent label
  - Checks for duplicates automatically
- `mempalace_checkpoint` — Save a whole session in one call: dedup each item, file non-duplicates, then write one diary entry
  - `items` (required): array of `{wing, room, content}`; content must be verbatim
  - `diary`: optional `{agent_name, entry, topic?, wing?}`; entry should use AAAK format
  - `dedup_threshold`: similarity threshold (default 0.9)
- `mempalace_update_drawer` — Update an existing drawer's content and/or move it to a different wing/room
  - `drawer_id` (required)
  - `content`, `wing`, `room`: at least one must be provided (no-op otherwise)
- `mempalace_delete_drawer` — Remove a drawer by ID
  - `drawer_id` (required)

### Ingest & Cleanup
- `mempalace_mine` — Mine a directory into the palace. Host-level ingest; call only when the user asks to import files.
  - `source` (required): directory to mine
  - `mode`: `projects` (default), `convos`, or `extract`
  - `wing`: target wing (default: source directory name)
  - `agent`: recorded on every drawer (default `mempalace`)
  - `limit`: max files to process (0 = all)
  - `dry_run`: preview without writing
  - `extract`: convos extraction strategy (`exchange` default, or `general`)
- `mempalace_sync` — Prune drawers whose source files are gitignored, deleted, or moved. Use dry-run first.
  - `project_dir`: optional project root scope
  - `wing`: optional wing scope
  - `apply`: actually delete; default is dry-run preview
- `mempalace_delete_by_source` — Bulk-delete drawers with one exact `source_file`. Destructive; use dry-run first.
  - `source_file` (required): exact metadata value to remove
  - `dry_run`: preview match count and sample (default true)

### Diary & Session
- `mempalace_diary_write` — Write a session diary entry
  - `agent_name` (required): your name/identifier
  - `entry` (required): what happened, what you learned, what matters
  - `topic`: category tag (default "general")
- `mempalace_diary_read` — Read recent diary entries
  - `agent_name` (required)
  - `last_n`: number of entries (default 10)
- `mempalace_memories_filed_away` — Acknowledge the latest silent auto-save checkpoint.
  - Returns: how many messages were tucked into drawers since the last ack
  - When to call: at the START of a session, to confirm prior-conversation persistence

### System
- `mempalace_hook_settings` — Get or set auto-save hook behavior. Host-level setting; do not change silently.
  - `silent_save`: true saves directly without MCP-level clutter
  - `desktop_toast`: true shows a desktop notification when saves complete
- `mempalace_reconnect` — Force reconnect to the palace database after external writes or stale index state

## Setup

Install MemPalace and populate the palace (uv recommended):

```bash
uv tool install mempalace   # or: pip install mempalace
mempalace init ~/my-convos
mempalace mine ~/my-convos
```

### OpenClaw MCP config

Add to your OpenClaw MCP configuration:

```json
{
  "mcpServers": {
    "mempalace": {
      "command": "python3",
      "args": ["-m", "mempalace.mcp_server"]
    }
  }
}
```

Or via CLI:

```bash
openclaw mcp set mempalace '{"command":"python3","args":["-m","mempalace.mcp_server"]}'
```

### Other MCP hosts

```bash
# Claude Code
claude mcp add mempalace -- python -m mempalace.mcp_server

# Cursor — add to .cursor/mcp.json
# Codex — add to .codex/mcp.json
```

## Tips

- Search is semantic (meaning-based), not keyword. "What did we discuss about database performance?" works better than "database".
- The knowledge graph stores typed relationships with time windows. Use it for facts about people and projects — it knows WHEN things were true.
- Diary entries accumulate across sessions. Write one at the end of each conversation to build continuity.
- Use `mempalace_check_duplicate` before storing new content to avoid duplicates.
- The AAAK dialect (from `mempalace_status`) is a compressed notation for efficient storage. Read it naturally — expand codes mentally, treat *markers* as emotional context.

## License

[MemPalace](https://github.com/MemPalace/mempalace) is MIT licensed. Created by Milla Jovovich, Ben Sigman, Igor Lins e Silva, and contributors.
