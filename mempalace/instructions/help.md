# MemPalace

AI memory system. Store everything, find anything. Local, free, no API key.

---

## Slash Commands

| Command              | Description                    |
|----------------------|--------------------------------|
| /mempalace:init      | Install and set up MemPalace   |
| /mempalace:search    | Search your memories           |
| /mempalace:mine      | Mine projects and conversations|
| /mempalace:status    | Palace overview and stats      |
| /mempalace:help      | This help message              |

---

## MCP Tools (19)

### Palace (read)
- mempalace_status -- Palace status and stats
- mempalace_list_wings -- List all wings
- mempalace_list_rooms -- List rooms in a wing
- mempalace_get_taxonomy -- Get the full taxonomy tree
- mempalace_search -- Search memories by query
- mempalace_check_duplicate -- Check if a memory already exists
- mempalace_get_aaak_spec -- Get the AAAK specification

### Palace (write)
- mempalace_add_drawer -- Add a new memory (drawer)
- mempalace_delete_drawer -- Delete a memory (drawer)

### Knowledge Graph
- mempalace_kg_query -- Query the knowledge graph
- mempalace_kg_add -- Add a knowledge graph entry
- mempalace_kg_invalidate -- Invalidate a knowledge graph entry
- mempalace_kg_timeline -- View knowledge graph timeline
- mempalace_kg_stats -- Knowledge graph statistics

### Navigation
- mempalace_traverse -- Traverse the palace structure
- mempalace_find_tunnels -- Find cross-wing connections
- mempalace_graph_stats -- Graph connectivity statistics

### Agent Diary
- mempalace_diary_write -- Write a diary entry
- mempalace_diary_read -- Read diary entries

---

## CLI Commands

    mempalace init <dir>                  Initialize a new palace
    mempalace mine <dir>                  Mine a project (default mode)
    mempalace mine <dir> --mode convos    Mine conversation exports
    mempalace search "query"              Search your memories
    mempalace split <dir>                 Split large transcript files
    mempalace wake-up                     Load palace into context
    mempalace compress                    Compress palace storage
    mempalace status                      Show palace status
    mempalace repair                      Rebuild vector index
    mempalace mcp                         Show MCP setup command
    mempalace hook run                    Run hook logic (for harness integration)
    mempalace instructions <name>         Output skill instructions

---

## Auto-Save Hooks

- Stop hook -- Automatically saves memories every 15 messages. Counts human
  messages in the session transcript (skipping command-messages). When the
  threshold is reached, blocks the AI with a save instruction. Uses
  ~/.mempalace/hook_state/ to track save points per session. If
  stop_hook_active is true, passes through to prevent infinite loops.

- PreCompact hook -- Emergency save before context compaction. Always blocks
  with a comprehensive save instruction because compaction means the AI is
  about to lose detailed context.

Hooks read JSON from stdin and output JSON to stdout. They can be invoked via:

    echo '{"session_id":"abc","stop_hook_active":false,"transcript_path":"..."}' | mempalace hook run --hook stop --harness claude-code

---

## Architecture

    Wings (projects/people)
      +-- Rooms (topics)
            +-- Closets (summaries)
                  +-- Drawers (verbatim memories)

    Halls connect rooms within a wing.
    Tunnels connect rooms across wings.

The palace is stored locally using ChromaDB for vector search and SQLite for
metadata. No cloud services or API keys required.

---

## Getting Started

1. /mempalace:init -- Set up your palace
2. /mempalace:mine -- Mine a project or conversation
3. /mempalace:search -- Find what you stored
