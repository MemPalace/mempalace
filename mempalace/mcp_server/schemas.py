# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


# ==================== MCP PROTOCOL ====================

TOOLS = {
    "mempalace_status": {
        "description": "Palace overview — total drawers, wing and room counts",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_status,
    },
    "mempalace_list_wings": {
        "description": "List all wings with drawer counts",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_list_wings,
    },
    "mempalace_list_rooms": {
        "description": "List rooms within a wing (or all rooms if no wing given)",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Wing to list rooms for (optional)"},
            },
        },
        "handler": tool_list_rooms,
    },
    "mempalace_get_taxonomy": {
        "description": "Full taxonomy: wing → room → drawer count",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_get_taxonomy,
    },
    "mempalace_get_aaak_spec": {
        "description": "Get the AAAK dialect specification — the compressed memory format MemPalace uses. Call this if you need to read or write AAAK-compressed memories.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_get_aaak_spec,
    },
    "mempalace_kg_query": {
        "description": "Query the knowledge graph for an entity's relationships. Returns typed facts with temporal validity. E.g. 'Max' → child_of Alice, loves chess, does swimming. Filter by date with as_of to see what was true at a point in time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Entity to query (e.g. 'Max', 'MyProject', 'Alice')",
                },
                "as_of": {
                    "type": "string",
                    "description": "Date/datetime filter — only facts valid at this time (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, optional)",
                },
                "direction": {
                    "type": "string",
                    "description": "outgoing (entity→?), incoming (?→entity), or both (default: both)",
                },
            },
            "required": ["entity"],
        },
        "handler": tool_kg_query,
    },
    "mempalace_kg_add": {
        "description": "Add a fact to the knowledge graph. Subject → predicate → object with optional time window. E.g. ('Max', 'started_school', 'Year 7', valid_from='2026-09-01'). Pass valid_to to backfill an already-ended historical fact in a single call.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "The entity doing/being something"},
                "predicate": {
                    "type": "string",
                    "description": "The relationship type (e.g. 'loves', 'works_on', 'daughter_of')",
                },
                "object": {"type": "string", "description": "The entity being connected to"},
                "valid_from": {
                    "type": "string",
                    "description": "When this became true (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, optional)",
                },
                "valid_to": {
                    "type": "string",
                    "description": "When this stopped being true (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, optional). Use for backfilling already-ended historical facts.",
                },
                "source_closet": {
                    "type": "string",
                    "description": "Closet ID where this fact appears (optional)",
                },
                "source_file": {
                    "type": "string",
                    "description": "Source file path the fact was extracted from (optional)",
                },
                "source_drawer_id": {
                    "type": "string",
                    "description": "Drawer ID the fact was extracted from (optional, RFC 002 provenance)",
                },
            },
            "required": ["subject", "predicate", "object"],
        },
        "handler": tool_kg_add,
    },
    "mempalace_kg_invalidate": {
        "description": "Mark a fact as no longer true. E.g. ankle injury resolved, job ended, moved house.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Entity"},
                "predicate": {"type": "string", "description": "Relationship"},
                "object": {"type": "string", "description": "Connected entity"},
                "ended": {
                    "type": "string",
                    "description": "When it stopped being true (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, default: today)",
                },
            },
            "required": ["subject", "predicate", "object"],
        },
        "handler": tool_kg_invalidate,
    },
    "mempalace_kg_supersede": {
        "description": "Atomically replace a fact with its successor at a shared boundary. Use when a single-valued fact changes (model, employer, address) instead of separate kg_invalidate + kg_add — a point-in-time query at the boundary then returns only the new value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "The entity whose fact is changing"},
                "predicate": {
                    "type": "string",
                    "description": "The relationship type (e.g. 'uses_model', 'works_at')",
                },
                "old_object": {"type": "string", "description": "The value being replaced"},
                "new_object": {"type": "string", "description": "The new value"},
                "at": {
                    "type": "string",
                    "description": "Boundary instant (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, optional; defaults to now UTC)",
                },
            },
            "required": ["subject", "predicate", "old_object", "new_object"],
        },
        "handler": tool_kg_supersede,
    },
    "mempalace_kg_timeline": {
        "description": "Chronological timeline of facts. Shows the story of an entity (or everything) in order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Entity to get timeline for (optional — omit for full timeline)",
                },
            },
        },
        "handler": tool_kg_timeline,
    },
    "mempalace_kg_stats": {
        "description": "Knowledge graph overview: entities, triples, current vs expired facts, relationship types.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_kg_stats,
    },
    "mempalace_traverse": {
        "description": "Walk the palace graph from a room. Shows connected ideas across wings — the tunnels. Like following a thread through the palace: start at 'chromadb-setup' in wing_code, discover it connects to wing_myproject (planning) and wing_user (feelings about it).",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_room": {
                    "type": "string",
                    "description": "Room to start from (e.g. 'chromadb-setup', 'riley-school')",
                },
                "max_hops": {
                    "type": "integer",
                    "description": "How many connections to follow (default: 2)",
                },
            },
            "required": ["start_room"],
        },
        "handler": tool_traverse_graph,
    },
    "mempalace_find_tunnels": {
        "description": "Find rooms that bridge two wings — the hallways connecting different domains. E.g. what topics connect wing_code to wing_team?",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing_a": {"type": "string", "description": "First wing (optional)"},
                "wing_b": {"type": "string", "description": "Second wing (optional)"},
            },
        },
        "handler": tool_find_tunnels,
    },
    "mempalace_graph_stats": {
        "description": "Palace graph overview: total rooms, tunnel connections, edges between wings.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_graph_stats,
    },
    "mempalace_mesh_peers": {
        "description": "Mesh estate snapshot (RFC 004): this replica's identity, version vector and node profile; each configured peer's reachability, last sync outcome, remote version vector and advertised profile; origins known only transitively; origin_profiles keyed by replica_id; and estate_source saying whether the peer status was observed in this process or published by the palace's hub (with published_at and whether that hub is still alive). Exactly the GET /sync/peers payload — tokens are never included.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_mesh_peers,
    },
    "mempalace_create_tunnel": {
        "description": "Create a cross-wing tunnel linking two palace locations. Use when content in one project relates to another — e.g., an API design in project_api connects to a database schema in project_database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_wing": {"type": "string", "description": "Wing of the source"},
                "source_room": {"type": "string", "description": "Room in the source wing"},
                "target_wing": {"type": "string", "description": "Wing of the target"},
                "target_room": {"type": "string", "description": "Room in the target wing"},
                "label": {"type": "string", "description": "Description of the connection"},
                "source_drawer_id": {
                    "type": "string",
                    "description": "Optional specific drawer ID",
                },
                "target_drawer_id": {
                    "type": "string",
                    "description": "Optional specific drawer ID",
                },
            },
            "required": ["source_wing", "source_room", "target_wing", "target_room"],
        },
        "handler": tool_create_tunnel,
    },
    "mempalace_list_tunnels": {
        "description": "List all explicit cross-wing tunnels. Optionally filter by wing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {
                    "type": "string",
                    "description": "Filter tunnels by wing (shows tunnels where wing is source or target)",
                },
            },
        },
        "handler": tool_list_tunnels,
    },
    "mempalace_delete_tunnel": {
        "description": "Delete an explicit tunnel by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tunnel_id": {"type": "string", "description": "Tunnel ID to delete"},
            },
            "required": ["tunnel_id"],
        },
        "handler": tool_delete_tunnel,
    },
    "mempalace_list_hallways": {
        "description": "List within-wing hallway records (entity-to-entity co-occurrence links built at mine time). Optionally filter by wing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {
                    "type": "string",
                    "description": "Filter hallways by wing",
                },
            },
        },
        "handler": tool_list_hallways,
    },
    "mempalace_delete_hallway": {
        "description": "Delete a hallway record by its ID. Returns {deleted: bool}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hallway_id": {"type": "string", "description": "Hallway ID to delete"},
            },
            "required": ["hallway_id"],
        },
        "handler": tool_delete_hallway,
    },
    "mempalace_follow_tunnels": {
        "description": "Follow tunnels from a room to see what it connects to in other wings. Returns connected rooms with drawer previews.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Wing to start from"},
                "room": {"type": "string", "description": "Room to follow tunnels from"},
            },
            "required": ["wing", "room"],
        },
        "handler": tool_follow_tunnels,
    },
    "mempalace_search": {
        "description": "Semantic search. Returns verbatim drawer content with similarity scores. IMPORTANT: 'query' must contain ONLY search keywords. Use 'context' for background. Results with cosine distance > max_distance are filtered out.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Short search query ONLY — keywords or a question. Max 250 chars.",
                    "maxLength": 250,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 5)",
                    "minimum": 1,
                    "maximum": 100,
                },
                "wing": {"type": "string", "description": "Filter by wing (optional)"},
                "room": {"type": "string", "description": "Filter by room (optional)"},
                "source_file": {
                    "type": "string",
                    "description": (
                        "Filter to one exact source_file (optional). Matches the full "
                        "stored path exactly (leading/trailing whitespace trimmed); no "
                        "glob or basename matching. Pass the value from a result's "
                        "'source_path' field; the displayed 'source_file' is only a basename."
                    ),
                },
                "since": {
                    "type": "string",
                    "description": (
                        "Only drawers filed on/after this ISO date or datetime "
                        "(inclusive), e.g. '2026-04-01' or '2026-04-01T09:30:00'. "
                        "Compares the drawer's created_at (filed_at) wall-clock; "
                        "drawers without a filed_at are excluded while set."
                    ),
                },
                "before": {
                    "type": "string",
                    "description": (
                        "Only drawers filed strictly before this ISO date or "
                        "datetime (exclusive). Same comparison rules as 'since'."
                    ),
                },
                "max_distance": {
                    "type": "number",
                    "description": "Max cosine distance threshold (0=identical, 2=opposite). Results further than this are dropped. Lower = stricter. Default 1.5. Set to 0 to disable.",
                },
                "candidate_strategy": {
                    "type": "string",
                    "enum": ["vector", "union"],
                    "description": "Candidate source strategy. 'vector' preserves default semantic search; 'union' also merges backend BM25 lexical candidates before reranking.",
                },
                "cli_compatible": {
                    "type": "boolean",
                    "description": "Preserve standalone CLI candidate selection, ranking, and output. Used by the CLI Hub forwarder.",
                },
                "context": {
                    "type": "string",
                    "description": "Background context for the search (optional). NOT used for embedding — only for future re-ranking.",
                },
            },
            "required": ["query"],
        },
        "handler": tool_search,
    },
    "mempalace_check_duplicate": {
        "description": "Check if content already exists in the palace before filing",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Content to check"},
                "threshold": {
                    "type": "number",
                    "description": "Similarity threshold 0-1 (default 0.9)",
                },
            },
            "required": ["content"],
        },
        "handler": tool_check_duplicate,
    },
    "mempalace_add_drawer": {
        "description": "File verbatim content into the palace. Checks for duplicates first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Wing (project name)"},
                "room": {
                    "type": "string",
                    "description": "Room (aspect: backend, decisions, meetings...)",
                },
                "content": {
                    "type": "string",
                    "description": "Verbatim content to store — exact words, never summarized",
                },
                "source_file": {"type": "string", "description": "Where this came from (optional)"},
                "added_by": {"type": "string", "description": "Who is filing this (default: mcp)"},
            },
            "required": ["wing", "room", "content"],
        },
        "handler": tool_add_drawer,
    },
    "mempalace_checkpoint": {
        "description": "Save a whole session in one call: semantic-dedups each item, files non-duplicates as drawers, then writes one diary entry. Use this instead of many separate check_duplicate/add_drawer/diary_write calls — it renders as a single tool-call card in the host UI.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Verbatim items to file. Each is {wing, room, content} — content is the exact words, never summarized.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "wing": {"type": "string", "description": "Wing (project name)"},
                            "room": {
                                "type": "string",
                                "description": "Room (short topic: decisions, backend...)",
                            },
                            "content": {
                                "type": "string",
                                "description": "Verbatim content to store",
                            },
                        },
                        "required": ["wing", "room", "content"],
                    },
                },
                "diary": {
                    "type": "object",
                    "description": "Optional diary entry written after filing: {agent_name, entry, topic?, wing?}. entry is AAAK-format.",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "description": "Agent name (e.g. cursor-ide)",
                        },
                        "entry": {"type": "string", "description": "Diary entry in AAAK format"},
                        "topic": {"type": "string", "description": "Topic tag (optional)"},
                        "wing": {"type": "string", "description": "Target wing (optional)"},
                    },
                },
                "dedup_threshold": {
                    "type": "number",
                    "description": "Similarity threshold 0-1 for the per-item dedup check (default 0.9)",
                },
                "added_by": {
                    "type": "string",
                    "description": "Who is filing these drawers. An explicit value takes precedence; otherwise the diary agent_name, else 'checkpoint'.",
                },
            },
            "required": ["items"],
        },
        "handler": tool_checkpoint,
    },
    "mempalace_delete_drawer": {
        "description": "Delete a drawer by ID. Irreversible.",
        "input_schema": {
            "type": "object",
            "properties": {
                "drawer_id": {"type": "string", "description": "ID of the drawer to delete"},
            },
            "required": ["drawer_id"],
        },
        "handler": tool_delete_drawer,
    },
    "mempalace_mine": {
        "description": (
            "Mine a directory into the palace — the MCP equivalent of `mempalace mine`. "
            "mode='convos' also accepts a single conversation file. "
            "mode='projects' (default) ingests code/docs; mode='convos' ingests chat "
            "transcripts; mode='extract' ingests office documents (PDF/DOCX/RTF, requires "
            "the mempalace[extract] extra). Runs synchronously and returns the miner's "
            "summary as `output`. The palace write lock is automatic; a concurrent mine "
            "returns a structured already-running error. Orphan cleanup is separate — use "
            "mempalace_sync."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Directory to mine, or one conversation file with mode='convos'.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["projects", "convos", "extract"],
                    "description": (
                        "Ingest mode: projects (code/docs, default), convos (chat "
                        "transcripts), extract (office docs)."
                    ),
                },
                "wing": {
                    "type": "string",
                    "description": "Target wing (default: source directory name).",
                },
                "agent": {
                    "type": "string",
                    "description": "Recorded on every drawer (default: mempalace).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max files to process (0 = all). Default: 0.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Report what would be filed without writing. Default: false.",
                },
                "extract": {
                    "type": "string",
                    "enum": ["exchange", "general"],
                    "description": (
                        "Convos extraction strategy: exchange (default) or general. "
                        "Ignored by other modes."
                    ),
                },
            },
            "required": ["source"],
        },
        "handler": tool_mine,
    },
    "mempalace_delete_by_source": {
        "description": "Bulk-delete every drawer mined from one source_file (exact match). Use to clean up benchmark/test data accidentally mined into a user wing (#1722). Returns a dry-run match count and sample by default; pass dry_run=false to commit. Irreversible.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_file": {
                    "type": "string",
                    "description": "Exact source_file metadata value to remove (e.g. the full path that was mined)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview the match count without deleting; default true. Pass false to actually delete.",
                },
            },
            "required": ["source_file"],
        },
        "handler": tool_delete_by_source,
    },
    "mempalace_sync": {
        "description": "Prune drawers whose source files are gitignored, deleted, or moved. Returns dry-run report by default; pass apply=true to commit deletions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_dir": {
                    "type": "string",
                    "description": "Project root to scope the sync (optional; auto-detected from drawer metadata if omitted)",
                },
                "wing": {"type": "string", "description": "Limit to one wing (optional)"},
                "apply": {
                    "type": "boolean",
                    "description": "Actually delete drawers; default is dry-run preview",
                },
            },
        },
        "handler": tool_sync,
    },
    "mempalace_get_drawer": {
        "description": "Fetch a single drawer by ID — returns full content and metadata.",
        "input_schema": {
            "type": "object",
            "properties": {
                "drawer_id": {"type": "string", "description": "ID of the drawer to fetch"},
            },
            "required": ["drawer_id"],
        },
        "handler": tool_get_drawer,
    },
    "mempalace_list_drawers": {
        "description": "List drawers with pagination. Optional wing/room filter and since/before date filter on filed_at (since inclusive, before exclusive; drawers without a parseable filed_at are excluded when a date bound is set). Returns IDs, wings, rooms, content previews, and total matching count for pagination.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Filter by wing (optional)"},
                "room": {"type": "string", "description": "Filter by room (optional)"},
                "since": {
                    "type": "string",
                    "description": "Only drawers filed on or after this ISO date/time, inclusive (e.g. '2026-04-01'). Optional.",
                },
                "before": {
                    "type": "string",
                    "description": "Only drawers filed before this ISO date/time, exclusive (e.g. '2026-05-01'). Optional.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results per page (default 20, max 100)",
                    "minimum": 1,
                    "maximum": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Offset for pagination (default 0)",
                    "minimum": 0,
                },
            },
        },
        "handler": tool_list_drawers,
    },
    "mempalace_update_drawer": {
        "description": "Update an existing drawer's content and/or metadata (wing, room). Fetches existing drawer first; returns error if not found.",
        "input_schema": {
            "type": "object",
            "properties": {
                "drawer_id": {"type": "string", "description": "ID of the drawer to update"},
                "content": {
                    "type": "string",
                    "description": "New content (optional — omit to keep existing)",
                },
                "wing": {
                    "type": "string",
                    "description": "New wing (optional — omit to keep existing)",
                },
                "room": {
                    "type": "string",
                    "description": "New room (optional — omit to keep existing)",
                },
            },
            "required": ["drawer_id"],
        },
        "handler": tool_update_drawer,
    },
    "mempalace_diary_write": {
        "description": "Write to your personal agent diary in AAAK format. Your observations, thoughts, what you worked on, what matters. Each agent has their own diary with full history. Write in AAAK for compression — e.g. 'SESSION:2026-04-04|built.palace.graph+diary.tools|ALC.req:agent.diaries.in.aaak|★★★'. Use entity codes from the AAAK spec.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "Your name — each agent gets their own diary wing",
                },
                "entry": {
                    "type": "string",
                    "description": "Your diary entry in AAAK format — compressed, entity-coded, emotion-marked",
                },
                "topic": {
                    "type": "string",
                    "description": "Topic tag (optional, default: general)",
                },
                "wing": {
                    "type": "string",
                    "description": "Target wing for this diary entry (optional). If omitted, uses wing_{agent_name}. Use this to write diary entries to a project wing instead of an agent-specific wing.",
                },
                "content": {
                    "type": "string",
                    "description": "Alias for 'entry' — accepted because add_drawer uses 'content'. Provide either 'entry' or 'content'; 'entry' wins if both are given.",
                },
            },
            # 'entry' (or its alias 'content') is enforced at dispatch, not via a
            # top-level anyOf: Anthropic rejects schemas with a top-level
            # anyOf/oneOf/allOf and drops the whole tools array (400).
            "required": ["agent_name"],
        },
        "handler": tool_diary_write,
    },
    "mempalace_diary_read": {
        "description": "Read your recent diary entries (in AAAK). See what past versions of yourself recorded — your journal across sessions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "Your name — each agent gets their own diary wing",
                },
                "last_n": {
                    "type": "integer",
                    "description": "Number of recent entries to read (default: 10)",
                },
                "wing": {
                    "type": "string",
                    "description": "Wing to read diary entries from (optional). If omitted, reads from wing_{agent_name}.",
                },
            },
            "required": ["agent_name"],
        },
        "handler": tool_diary_read,
    },
    "mempalace_hook_settings": {
        "description": (
            "Get or set hook behavior. silent_save: True = save directly "
            "(no MCP clutter), False = legacy blocking. desktop_toast: "
            "True = show desktop notification. Call with no args to view."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "silent_save": {
                    "type": "boolean",
                    "description": "True = silent direct save, False = blocking MCP calls",
                },
                "desktop_toast": {
                    "type": "boolean",
                    "description": "True = show desktop toast via notify-send",
                },
            },
        },
        "handler": tool_hook_settings,
    },
    "mempalace_memories_filed_away": {
        "description": "Check if a recent palace checkpoint was saved. Returns message count and timestamp.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_memories_filed_away,
    },
    "mempalace_reconnect": {
        "description": (
            "Force reconnect to the palace database. Use after external scripts or CLI commands"
            " modified the palace directly, which can leave the in-memory HNSW index stale."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": tool_reconnect,
    },
    "mempalace_event_append": {
        "description": (
            "Append an immutable agent-coordination event to the logstream (RFC 003). Use for"
            " delegating work (task.request), replying (task.reply), and announcing artifacts"
            " (patch.ready). Events are append-only; corrections are new events."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": "Event type, e.g. 'task.request', 'task.reply', 'patch.ready'",
                },
                "stream": {
                    "type": "string",
                    "description": "Logical stream, e.g. 'project/mempalace' or 'shared_agent_brain'",
                },
                "room": {
                    "type": "string",
                    "description": "Sub-channel, e.g. 'delegation', 'patches', 'reviews', 'status'",
                },
                "topic": {
                    "type": "string",
                    "description": "Topic to group related work/sub-team, e.g. 'auth-v2', 'ui-redesign' (optional)",
                },
                "from_agent": {"type": "string", "description": "Writer agent identity"},
                "to_agent": {
                    "type": "string",
                    "description": "Target agent, or '*' for broadcast (optional)",
                },
                "correlation_id": {
                    "type": "string",
                    "description": "Task/conversation id tying request and reply events (optional)",
                },
                "branch": {"type": "string", "description": "Git branch, when relevant (optional)"},
                "base_commit": {
                    "type": "string",
                    "description": "Git commit the work started from (optional)",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "One of: open, claimed, ready, applied, blocked, failed, superseded"
                        " (optional)"
                    ),
                },
                "body": {
                    "type": "string",
                    "description": "Verbatim human-readable content (optional, max 256 KiB)",
                },
                "metadata": {
                    "type": "object",
                    "description": "Extra structured fields, stored verbatim (optional)",
                },
                "artifact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ids of already-stored artifacts to reference (optional)",
                },
            },
            "required": ["type", "stream", "room", "from_agent"],
        },
        "handler": tool_event_append,
    },
    "mempalace_task_create": {
        "description": (
            "Create a complete immutable task.request for another agent and return its exact"
            " stored event plus one short ready-to-paste handoff line. Use this instead of"
            " assembling raw task fields, especially when connected to a remote shared-brain"
            " hub. The caller must preview the exact task with the user before this append."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project routing name"},
                "from_agent": {"type": "string", "description": "Requesting agent identity"},
                "to_agent": {"type": "string", "description": "Worker agent identity"},
                "goal": {"type": "string", "description": "Exact verbatim task goal"},
                "branch": {"type": "string", "description": "Git branch for the work"},
                "base_commit": {
                    "type": "string",
                    "description": (
                        "Immutable hexadecimal commit id the worker must start from;"
                        " branches and tags are rejected"
                    ),
                },
                "done": {
                    "type": "string",
                    "description": "Exact verbatim definition of done",
                },
            },
            "required": [
                "project",
                "from_agent",
                "to_agent",
                "goal",
                "branch",
                "base_commit",
                "done",
            ],
        },
        "handler": tool_task_create,
    },
    "mempalace_event_list": {
        "description": (
            "List agent-coordination events with structured filters. Use order='desc' to return"
            " newest events first (e.g. for sweeping recent inbox or inspecting project tail in"
            " a single call); default order is 'asc' (oldest first, append order). Use"
            " since_event_id as the resume cursor: it means strictly AFTER that event in append"
            " order (rowid > anchor), so it cannot skip anything. For reverse/historical paging,"
            " use before_event_id (rowid < anchor). Do NOT resume with since_created_at — a"
            " peer's event syncs in whenever it arrives, so it can already be older than a"
            " timestamp cursor and be missed permanently; since_created_at is a time window"
            " ('what happened today'), not a cursor. Pass preview=true when sweeping a busy"
            " stream. to_agent=<you> also matches '*' broadcasts. To wait for future events, use"
            " mempalace_event_wait."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stream": {"type": "string", "description": "Filter by stream (optional)"},
                "room": {"type": "string", "description": "Filter by room (optional)"},
                "topic": {"type": "string", "description": "Filter by topic (optional)"},
                "type": {"type": "string", "description": "Filter by event type (optional)"},
                "to_agent": {
                    "type": "string",
                    "description": "Filter by target agent; also matches '*' broadcasts (optional)",
                },
                "from_agent": {"type": "string", "description": "Filter by writer (optional)"},
                "correlation_id": {
                    "type": "string",
                    "description": "Filter by correlation id (optional)",
                },
                "status": {"type": "string", "description": "Filter by status (optional)"},
                "since_event_id": {
                    "type": "string",
                    "description": "Return only events strictly after this event id in append order (optional)",
                },
                "before_event_id": {
                    "type": "string",
                    "description": "Return only events strictly before this event id in append order (optional)",
                },
                "since_created_at": {
                    "type": "string",
                    "description": (
                        "Time window filter, inclusive: events created at or after this time"
                        " (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, optional). NOT a resume cursor"
                        " — use since_event_id for that; a timestamp cursor silently drops"
                        " peer events that sync in late. Dedup by id when using this."
                    ),
                },
                "limit": {"type": "integer", "description": "Max events to return (default 50)"},
                "order": {
                    "type": "string",
                    "description": "'asc' (oldest first, default) or 'desc' (newest first, optional)",
                },
                "preview": {
                    "type": "boolean",
                    "description": (
                        "Truncate each event body to a short excerpt (marks body_truncated +"
                        " body_length) so scanning many events stays cheap. since_event_id is"
                        " strictly AFTER that id, so do not pass the truncated event's own id"
                        " to re-fetch it — repeat the original filters with preview=false"
                        " (default false)"
                    ),
                },
            },
        },
        "handler": tool_event_list,
    },
    "mempalace_event_wait": {
        "description": (
            "Block until a matching coordination event exists or the timeout expires (default"
            " 60s, max 5 minutes). Returns {timed_out: true, events: []} on timeout — a normal"
            " result, not an error. This is the right tool for actively waiting on a"
            " correlation_id you delegated or claimed. It already backs off internally, so do"
            " not wrap it in a tight retry loop: on timeout just call it again with"
            " since_event_id updated to the last event you processed. For long-lived consumers"
            " (daemons, dashboards) prefer the push stream at GET /logstream/stream, which"
            " takes the live-tail filter subset and the same since_event_id resume."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stream": {"type": "string", "description": "Filter by stream (optional)"},
                "room": {"type": "string", "description": "Filter by room (optional)"},
                "topic": {"type": "string", "description": "Filter by topic (optional)"},
                "type": {"type": "string", "description": "Filter by event type (optional)"},
                "to_agent": {
                    "type": "string",
                    "description": "Filter by target agent; also matches '*' broadcasts (optional)",
                },
                "from_agent": {"type": "string", "description": "Filter by writer (optional)"},
                "correlation_id": {
                    "type": "string",
                    "description": "Filter by correlation id (optional)",
                },
                "status": {"type": "string", "description": "Filter by status (optional)"},
                "since_event_id": {
                    "type": "string",
                    "description": "Only match events strictly after this event id (optional)",
                },
                "since_created_at": {
                    "type": "string",
                    "description": (
                        "Time window filter, inclusive (optional). NOT a resume cursor — use"
                        " since_event_id, which cannot skip a late-syncing peer event."
                    ),
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "How long to wait in milliseconds (default 60000, max 300000)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max events to return when matches exist (default 50)",
                },
            },
        },
        "handler": tool_event_wait,
    },
    "mempalace_event_ack": {
        "description": (
            "Acknowledge a coordination event: appends a new event.ack routed back to the"
            " original writer with the correlation id copied. Never mutates the target event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Id of the event to acknowledge"},
                "from_agent": {"type": "string", "description": "Acknowledging agent identity"},
                "status": {
                    "type": "string",
                    "description": (
                        "One of: open, claimed, ready, applied, blocked, failed, superseded"
                        " (optional)"
                    ),
                },
                "body": {"type": "string", "description": "Verbatim ack notes (optional)"},
                "topic": {
                    "type": "string",
                    "description": "Topic override (defaults to target event's topic, optional)",
                },
            },
            "required": ["event_id", "from_agent"],
        },
        "handler": tool_event_ack,
    },
    "mempalace_artifact_put": {
        "description": (
            "Store exact artifact content (unified diff patch, file, log, json, note) for agent"
            " handoffs. Returns id, sha256, and size_bytes. UTF-8 text only, max 4 MiB."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "One of: patch, file, log, json, note",
                },
                "content": {"type": "string", "description": "Exact artifact content"},
                "created_by": {"type": "string", "description": "Writer agent identity"},
                "metadata": {
                    "type": "object",
                    "description": "Extra structured fields, e.g. branch/base_commit (optional)",
                },
            },
            "required": ["kind", "content", "created_by"],
        },
        "handler": tool_artifact_put,
    },
    "mempalace_artifact_get": {
        "description": (
            "Fetch a coordination artifact by id — exact content plus sha256 for verification."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "Artifact id to fetch"},
            },
            "required": ["artifact_id"],
        },
        "handler": tool_artifact_get,
    },
    "mempalace_patch_submit": {
        "description": (
            "Convenience: store a patch artifact and append its patch.ready event in one call."
            " Use when handing completed work to another agent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Unified diff content"},
                "from_agent": {"type": "string", "description": "Submitting agent identity"},
                "stream": {
                    "type": "string",
                    "description": "Logical stream, e.g. 'project/mempalace'",
                },
                "room": {"type": "string", "description": "Sub-channel (default 'patches')"},
                "topic": {"type": "string", "description": "Topic name (optional)"},
                "to_agent": {"type": "string", "description": "Target agent or '*' (optional)"},
                "correlation_id": {
                    "type": "string",
                    "description": "Task id tying this patch to its request (optional)",
                },
                "branch": {"type": "string", "description": "Git branch (optional)"},
                "base_commit": {
                    "type": "string",
                    "description": "Git commit the patch applies to (optional)",
                },
                "body": {"type": "string", "description": "Verbatim notes (optional)"},
                "metadata": {"type": "object", "description": "Extra structured fields (optional)"},
            },
            "required": ["content", "from_agent", "stream"],
        },
        "handler": tool_patch_submit,
    },
}


SUPPORTED_PROTOCOL_VERSIONS = [
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
]
