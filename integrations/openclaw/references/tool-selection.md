# MemPalace tool selection

Use this reference when the compact OpenClaw skill does not contain enough detail
to choose between MemPalace MCP tools.

## Search and browse

Start with search for context, then fetch exact drawers when precision matters.

- `mempalace_search` — semantic search. Keep `query` short; put background in
  `context` if available.
- `mempalace_check_duplicate` — check before filing standalone drawers.
- `mempalace_list_wings`, `mempalace_list_rooms`, `mempalace_list_drawers` —
  browse taxonomy and recent/filtered drawers.
- `mempalace_get_drawer`, `mempalace_get_taxonomy`, `mempalace_status` — exact
  drawer/taxonomy/overview lookup.
- `mempalace_get_aaak_spec` — read the compressed diary/memory dialect.

## Knowledge graph

Use KG tools for durable facts about people, projects, ownership, status,
relationships, and dates.

- `mempalace_kg_query` — current or point-in-time entity facts.
- `mempalace_kg_add` — add an independent fact.
- `mempalace_kg_supersede` — atomically replace one single-valued fact.
- `mempalace_kg_invalidate` — end a fact without replacement.
- `mempalace_kg_timeline`, `mempalace_kg_stats` — inspect graph history/health.

## Write and diary

Store durable context verbatim. Do not summarize so far that original meaning is
lost.

- `mempalace_add_drawer` — file one drawer in a wing/room.
- `mempalace_checkpoint` — semantic-dedup several drawers plus one diary entry.
- `mempalace_update_drawer` — correct or move an existing drawer.
- `mempalace_diary_write`, `mempalace_diary_read` — continuity index across
  sessions; use AAAK when the convention calls for it.

## Graph, ingest, and cleanup

Use graph tools when relationships between topics matter. Ingest/cleanup tools
can alter many memories or host state, so prefer dry runs and explicit user
intent before destructive actions.

- Graph: `mempalace_traverse`, `mempalace_follow_tunnels`,
  `mempalace_find_tunnels`, `mempalace_list_tunnels`,
  `mempalace_create_tunnel`, `mempalace_delete_tunnel`,
  `mempalace_list_hallways`, `mempalace_delete_hallway`,
  `mempalace_graph_stats`.
- `mempalace_mine` — mine a directory, or one conversation file with
  `mode="convos"`. `source` is the directory or conversation file; optional
  controls include `mode`, `wing`, `agent`, `limit`, `dry_run`, and the convos
  `extract` strategy (`exchange` or `general`).
- `mempalace_sync` — prune drawers for ignored, deleted, or moved source files.
  Scope with `project_dir` or `wing`; preview first and set `apply` only after
  confirming the deletion set.
- `mempalace_delete_by_source` — bulk-delete one exact `source_file` metadata
  value. Keep its default dry run for the first call.
- `mempalace_delete_drawer` — irreversibly remove one drawer by ID.
- System/session: `mempalace_reconnect`, `mempalace_hook_settings`,
  `mempalace_memories_filed_away`.
