<!-- Copy this section into your project's AGENTS.md -->

## MemPalace Memory Protocol

This project uses MemPalace for persistent AI memory across sessions. The MCP server is configured globally — all agents have access to 19 memory tools automatically.

### When to Save Memories
- **After completing a significant task**: Save what was done, decisions made, and why
- **After debugging sessions**: Save the root cause, fix, and patterns observed
- **When making architectural decisions**: Save the decision, alternatives considered, and rationale
- **Before ending a long session**: Save key context that the next session will need

### How to Save
Use `mempalace_add_drawer` with:
- `wing`: "{your-project-name}" (this project's wing)
- `room`: Appropriate topic slug (e.g., "auth-migration", "deploy-config", "bug-fixes")
- `content`: Verbatim content — exact words, decisions, code snippets. Never summarize.

### How to Recall
- **Before starting work**: Call `mempalace_search` with the topic you're working on
- **When unsure about past decisions**: Search for the decision topic
- **When context seems missing**: Check `mempalace_kg_query` for entity relationships

### Agent Diary
Each agent can maintain a personal diary via `mempalace_diary_write` / `mempalace_diary_read`. Use this for session-level notes, observations, and learnings.

### Available Tools (19 total)
- Palace read: `mempalace_status`, `mempalace_search`, `mempalace_list_wings`, `mempalace_list_rooms`, `mempalace_get_taxonomy`, `mempalace_check_duplicate`, `mempalace_get_aaak_spec`
- Palace write: `mempalace_add_drawer`, `mempalace_delete_drawer`
- Knowledge Graph: `mempalace_kg_query`, `mempalace_kg_add`, `mempalace_kg_invalidate`, `mempalace_kg_timeline`, `mempalace_kg_stats`
- Navigation: `mempalace_traverse`, `mempalace_find_tunnels`, `mempalace_graph_stats`
- Diary: `mempalace_diary_write`, `mempalace_diary_read`

## Oh-My-OpenAgent Note

No changes to `oh-my-openagent.json` are needed — MCPs from `opencode.json` are inherited automatically.
